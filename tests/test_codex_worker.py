"""The Codex worker, phase BC1b (BROWSER_AND_CODEX.md §3.3 to §3.7, §4.6; DESIGN §44): T13, T15, T16, T24 and T14.

A factory job on the fixture app whose builder runs on the codex-worker driver through the real bin/sf-codex-launch
(tests/codex_rig.py). The unit tests play tests/fake_codex.py in Codex's place; the live tests run the real pinned
Codex 0.163.0 (SF_CODEX_BIN) against tests/mock_responses.py and skip without it. Every Codex run here takes a
custody request lease from tests/mock_custody.py, as a server run would (§3.3 (c)). A test passes only on what code
recorded: the receipt, git, the launches the fake logged, the stub's requests and the process table.
"""

from __future__ import annotations

import grp
import json
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Callable
from unittest import mock

from swfactory import chats as real_chats   # the real module: codex_rig.Rig swaps swfactory.chats for a stand-in
from swfactory import custody, gitops
from swfactory import items as tracker
from swfactory.codex import work
from swfactory.config import Config
from swfactory.harness.sandbox import bwrap_available
from swfactory.routing import Reachability
from swfactory.runners import RunContext, make_runner
from tests import codex_rig as R
from tests.helpers import commit_file, make_repo
from tests.mock_responses import wait_for
from tests.test_orchestrator import failing, review_pass, reviews

LIVE = R.live_reason()
NO_BWRAP = None if bwrap_available() else "bubblewrap is not usable here (verify always runs under it)"

# A run that leaves app.py broken (only a comment added) and writes __pycache__/app.*.pyc compiled from the fixed
# source, its header matching app.py's mtime and size: Python in the worktree would load the fix from it
CRAFTED_PYC = r"""python3 - <<'PY'
import importlib.util, marshal, os, pathlib
p = pathlib.Path('app.py')
src = p.read_text()
good = src.replace('return "Hello, there"', 'return f"Hello, {name}"')
p.write_text(src + "\n# tidy\n")
st = os.stat(p)
pyc = pathlib.Path(importlib.util.cache_from_source('app.py'))
pyc.parent.mkdir(exist_ok=True)
pyc.write_bytes(importlib.util.MAGIC_NUMBER + (0).to_bytes(4, 'little')
                + (int(st.st_mtime) & 0xFFFFFFFF).to_bytes(4, 'little') + (st.st_size & 0xFFFFFFFF).to_bytes(4, 'little')
                + marshal.dumps(compile(good, str(p.resolve()), 'exec')))
PY
printf 'STATUS: READY\n\n# Build\nDone.\n\n## Items\n- I-1 done: `python3 -m unittest -q` passed\n' > {out_dir}/build.md"""

# A run that rewrites settings.ini (git ignores it) to content of the same size and puts its mtime back: through a new
# file, which its writer may set the times of (a Codex user can do the same to a file the harness made)
SAME_SIZE_OLD_MTIME = r"""python3 - <<'PY'
import os, pathlib
p = pathlib.Path('settings.ini')
st = os.stat(p)
os.unlink(p)
p.write_text('greeting=good\n')
os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
PY
"""

# codex.group set to the harness's own group: the run's share of the job folder and the worktree is real setfacl
# (on this test's own files only), which needs no root
OWN_GROUP = grp.getgrgid(os.getgid()).gr_name
NO_SETFACL = None if shutil.which("setfacl") else "setfacl is not installed here"


def stage_of(factory, job_id: str) -> str:
    return factory.job(R.REPO, job_id)["stage"]


def in_flight(factory, job_id: str) -> bool:
    return bool((factory.job(R.REPO, job_id).get("codex_run") or {}).get("run"))


def codex_pids(binary: str, root: Path | str | None = None) -> list[int]:
    """Processes whose executable is binary, or (the stand-in, a script) whose interpreter runs it as its first
    argument (the process table, not the harness's word); with root, only those whose CODEX_HOME is under it (this
    rig's), so another suite running the same binary at the same time is left alone."""
    found = []
    target = os.path.realpath(binary)
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            try:
                if os.path.realpath(f"/proc/{entry}/exe") != target:
                    argv = Path(f"/proc/{entry}/cmdline").read_bytes().split(b"\0")
                    if len(argv) < 2 or os.path.realpath(os.fsdecode(argv[1])) != target:
                        continue
                if root is not None:
                    env = Path(f"/proc/{entry}/environ").read_bytes().split(b"\0")
                    home = next((e[len(b"CODEX_HOME="):] for e in env if e.startswith(b"CODEX_HOME=")), b"")
                    if not os.fsdecode(home).startswith(str(root) + os.sep):
                        continue
                found.append(int(entry))
            except OSError:
                continue
    return found


def harness_process(case: unittest.TestCase, rig: R.Rig) -> tuple[subprocess.Popen, Callable[[], Path | None]]:
    """The harness in a process of its own (the rig's config, the scripted stand-ins for every agent but the Codex
    builder) with one job submitted; (the process, a function giving that job's job.json once it exists). The test
    kills it as a crash would."""
    rig.config.save()
    script = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
        from swfactory.config import Config
        from swfactory.events import EventBus
        from swfactory.orchestrator import Factory
        from swfactory.runners import make_runner
        from swfactory.codex import work
        from tests import codex_rig as R
        from tests.test_orchestrator import Recorder
        recorder = Recorder()
        recorder.actions["spec-writer"] = R.spec
        config = Config.load({str(rig.config.path)!r})
        def runners(cfg, driver):
            return make_runner(cfg, driver) if work.is_worker(cfg, driver) else recorder.runner_factory(cfg, driver)
        factory = Factory(config, EventBus(), runner_factory=runners)
        factory.start_jobs()
        factory.submit({R.REPO!r}, "Fix the greeting")
        time.sleep(600)
    """)
    proc = subprocess.Popen([sys.executable, "-c", script], cwd=str(rig.tmp), stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
                            env={**os.environ, "HOME": os.environ["HOME"]})
    case.addCleanup(lambda: proc.poll() is None and os.killpg(proc.pid, signal.SIGKILL))
    jobs = Path(rig.config.data_dir) / "jobs" / R.REPO

    def job_file() -> Path | None:
        found = sorted(jobs.glob("*/job.json")) if jobs.is_dir() else []
        return found[0] if found else None

    return proc, job_file


def job_mid_run(path: Path | None) -> bool:
    """Whether the job.json at path records a Codex run in flight."""
    if path is None:
        return False
    try:
        return bool(json.loads(path.read_text()).get("codex_run"))
    except (OSError, ValueError):
        return False


def scratch(case: unittest.TestCase) -> Path:
    """A temporary folder of the test's own, removed afterwards."""
    import shutil
    import tempfile

    folder = Path(tempfile.mkdtemp(prefix="sf-codex-test-"))
    case.addCleanup(shutil.rmtree, folder, ignore_errors=True)
    return folder


def chat_manager(rig: R.Rig) -> "real_chats.ChatManager":
    """The real ChatManager on the rig's config (the rig's factory has a stand-in), with reachability answered here:
    nothing is probed."""
    reach = Reachability(probe=lambda provider, timeout: (True, "up"), loaded=lambda provider, timeout: (None, ""))
    return real_chats.ChatManager(rig.config, rig.bus, SimpleNamespace(reach=reach), runner_factory=rig.runner_factory)


def turn_done(manager: "real_chats.ChatManager", chat_id: str) -> dict:
    assert wait_for(lambda: manager.chat(chat_id)["status"] != "running", 60), "the turn ended"
    return manager.chat(chat_id)


@unittest.skipUnless(NO_BWRAP is None, NO_BWRAP or "")
class FixTheFixtureAppTests(unittest.TestCase):
    """T13 with the stand-in Codex: what the harness does around a codex-worker builder."""

    def test_t13_the_harness_commits_verifies_and_writes_the_receipt(self) -> None:
        rig = R.Rig(self, scenario=[[{"run": R.FIX}, {"text": "Fixed the greeting."}]])
        job = rig.run_job()
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        [launch] = rig.launches()
        argv = launch["argv"]
        # Codex ran through the launcher with exactly the preset's arguments: its own sandbox, no -s, no bypass
        self.assertEqual(argv[:5], ["exec", "--json", "--strict-config", "--ignore-rules", "--skip-git-repo-check"])
        self.assertEqual(argv[-1], "-")
        for refused in ("-s", "--sandbox", "-c", "--dangerously-bypass-approvals-and-sandbox", "--full-auto"):
            self.assertNotIn(refused, argv)
        self.assertEqual(sorted(launch["env"]), ["CODEX_HOME", "HOME", "LANG", "PATH", "STRATA_API_KEY", "TMPDIR"])
        self.assertTrue(launch["env"]["PATH"].startswith("/usr/bin"))
        self.assertEqual(launch["env"]["STRATA_API_KEY"], R.KEY)
        self.assertIn("No network in this run", launch["stdin"])
        out_dir = argv[argv.index("--add-dir") + 1]
        self.assertNotIn(str(job["job_dir"]), out_dir, "Codex's writable roots never include the job folder")
        self.assertEqual(launch["codex_home_files"], [".sf-home.json", "config.toml"])
        # the harness made the commit, after the write-policy check and the secret scan; Codex made none
        [receipt] = rig.receipts(job)
        sha = receipt["commit"]["sha"]
        self.assertEqual(receipt["commit"]["by"], "harness")
        self.assertTrue(sha.startswith(receipt["commit"]["response"]["ids"][0]))
        self.assertEqual(receipt["commit"]["response"]["status"], 0)
        author = rig.git("log", "-1", "--format=%an <%ae>|%s", sha)
        self.assertEqual(author.strip(), f"Software Factory <factory@localhost>|factory: {job['id']} build round 1 "
                                         f"(builder on codex-worker)")
        self.assertEqual([c["kind"] for c in receipt["checks"]], ["write_policy", "secret_scan", "verify"])
        verify = receipt["checks"][2]
        self.assertEqual((verify["exit"], verify["check"], verify["commit"]), (0, "ok", sha))
        self.assertEqual(verify["argv"], ["bash", "-c", "python3 -m unittest -q"])
        # the receipt: outside the job folder, the run's ids, Codex's version, profile and provider, diff and paths
        path = Path(rig.config.data_dir) / job["runs"][1]["receipt"]
        self.assertEqual(path.parent, Path(rig.config.data_dir) / "receipts" / R.REPO / job["id"])
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(receipt["run_outcome"], "completed")
        self.assertEqual(receipt["request"]["run"], job["runs"][1]["run"])
        self.assertEqual(receipt["codex"]["version"], "0.163.0")
        self.assertEqual(receipt["codex"]["permissions_profile"], "factory_worker")
        self.assertEqual(receipt["codex"]["approval_policy"], "never")
        self.assertEqual(receipt["codex"]["provider"]["base_url"], rig.stub.url)
        self.assertTrue(receipt["codex"]["home"]["verified"])
        self.assertEqual(receipt["codex"]["home"]["template_sha256"], work.L.TEMPLATE_PIN)
        self.assertTrue(receipt["codex"]["rollout_sha256"])
        self.assertEqual(receipt["artifacts"]["paths"], ["app.py"])
        self.assertTrue(receipt["artifacts"]["diff_sha256"])
        self.assertEqual(job["runs"][1]["outcome"], "completed")
        self.assertEqual(job["runs"][1]["sha256"], work.hashlib.sha256(path.read_bytes()).hexdigest())
        # the criterion carries code-written check evidence with its check set explicitly
        [criterion] = [i for i in rig.items(job) if i["kind"] == "criterion"]
        checks = [e for e in criterion["evidence"] if e["kind"] == "check"]
        self.assertEqual([(e["ref"], e["check"], e["by"]) for e in checks], [(f"verify@{sha[:12]}", "ok", "factory")])
        # a custody request lease covered the Codex run (and was given back)
        ops = [(m, p) for m, p, _, body in rig.register.requests if p == "/leases"]
        self.assertGreaterEqual(len(ops), 2, "one for the Responses check, one for the run")
        self.assertIn("release", rig.register.ops())
        # agent.start says it ran Codex, agent.end gives the outcome
        start = [e.data for e in rig.events(job, "agent.start") if e.agent == "builder"][0]
        self.assertEqual(start["codex"], {"version": "0.163.0", "surface": "exec",
                                          "permissions_profile": "factory_worker", "approval_policy": "never"})
        end = [e.data for e in rig.events(job, "agent.end") if e.agent == "builder"][0]
        self.assertEqual(end["outcome"], "completed")
        # at job end the home is deleted (its rollouts' hash is in the receipt) and the run folders with it
        self.assertFalse((work.work_root(rig.config) / "homes" / f"{R.REPO}--{job['id']}").exists())
        self.assertFalse((work.work_root(rig.config) / "runs" / f"{R.REPO}--{job['id']}").exists())
        self.assertEqual(work.Receipts(rig.config.data_dir).verify_chain(R.REPO, job["id"]), [])

    def test_a_failing_verify_gets_one_corrective_resume_then_needs_you(self) -> None:
        broken = R.FIX.replace("Hello, {name}", "Hi, {name}")
        rig = R.Rig(self, scenario=[[{"run": broken}], [{"run": "true"}]])
        job = rig.run_job()
        self.assertEqual(job["stage"], "needs-human", job.get("reason"))
        self.assertIn("verify failed", job["reason"])
        launches = rig.launches()
        self.assertEqual(len(launches), 2, "one corrective resume, then a person")
        self.assertIn("resume", launches[1]["argv"])
        self.assertIn("verify command on your latest commit and it failed", launches[1]["stdin"])
        # a resume gets no system text, so it is told its own out folder (its --add-dir), not round 1's
        argv = launches[1]["argv"]
        self.assertIn(f"This run's out folder is {argv[argv.index('--add-dir') + 1]}:", launches[1]["stdin"])
        receipts = rig.receipts(job)
        self.assertEqual(receipts[0]["checks"][-1]["check"], "failed")
        self.assertEqual(receipts[1]["previous_sha256"],
                         work.hashlib.sha256((Path(rig.config.data_dir) / job["runs"][1]["receipt"]).read_bytes())
                         .hexdigest())
        [criterion] = [i for i in rig.items(job) if i["kind"] == "criterion"]
        self.assertEqual([e["check"] for e in criterion["evidence"] if e["kind"] == "check"], ["failed"])
        self.assertNotEqual(criterion["status"], "verified", "a failing verify never reads as passing")

    def test_a_resume_writes_build_md_to_the_out_folder_its_prompt_names(self) -> None:
        code, readme = R.FIX.split("printf", 1)
        valid = "printf" + readme.replace("{out_dir}", "{named_out_dir}")
        rig = R.Rig(self, scenario=[[{"run": code + "printf 'not a verdict\\n' > {out_dir}/build.md"}],
                                    [{"run": valid}]])
        job = rig.run_job()
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        first, second = rig.launches()
        self.assertIn("build.md must start with STATUS", second["stdin"])
        self.assertEqual(second["argv"][second["argv"].index("--add-dir") + 1],
                         second["stdin"].split("This run's out folder is ", 1)[1].split(":", 1)[0])
        self.assertTrue((Path(job["job_dir"]) / "build.md").read_text().startswith("STATUS: READY"))

    def test_ignored_files_a_run_leaves_fail_verify_until_they_are_gone(self) -> None:
        """verify runs in the worktree, so it speaks for the commit only when nothing the commit lacks is there: a
        file git ignores that the run wrote (the commit leaves it out) fails the check without running it."""
        rig = R.Rig(self, scenario=[[{"run": R.FIX + " && echo a,b > sample.csv && python3 -c 'import app'"}],
                                    [{"run": "rm -f sample.csv"}]])
        commit_file(rig.repo, ".gitignore", "*.csv\n__pycache__/\n", "ignore data files")
        job = rig.run_job()
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        first, second = rig.receipts(job)
        self.assertEqual(first["commit"]["paths"], ["app.py"], "the ignored file is not in the commit")
        self.assertEqual(first["artifacts"]["ignored_left"], ["sample.csv"], "the bytecode cache is no finding")
        verify = first["checks"][-1]
        self.assertEqual((verify["kind"], verify["check"], verify["exit"]), ("verify", "failed", None))
        self.assertIn("the run left files git ignores, which the commit does not hold: sample.csv", verify["reason"])
        corrective = rig.launches()[1]
        self.assertIn("resume", corrective["argv"])
        self.assertIn("sample.csv", corrective["stdin"])
        # the second run removed it and committed nothing: verify ran again on the same commit, and passed
        self.assertIsNone(second["commit"])
        self.assertEqual((job["verify"]["commit"], job["verify"]["check"]), (first["commit"]["sha"], "ok"))
        self.assertIsNone(job.get("codex_ignored"))
        [criterion] = [i for i in rig.items(job) if i["kind"] == "criterion"]
        self.assertEqual([e["check"] for e in criterion["evidence"] if e["kind"] == "check"], ["failed", "ok"])

    def test_ignored_files_an_unclean_run_left_fail_verify_after_the_retry(self) -> None:
        """A run that did not complete (here killed: unknown) is counted like a completed one: what it left that git
        ignores fails the retry's verify until it is gone."""
        rig = R.Rig(self, scenario=[[{"run": R.FIX + " && echo a,b > sample.csv"}, {"die": True}],
                                    [{"run": R.FIX}], [{"run": "rm -f sample.csv"}]])
        commit_file(rig.repo, ".gitignore", "*.csv\n", "ignore data files")
        job = rig.run_job()
        self.assertEqual((job["stage"], job["failed_step"]), ("failed", "build"), job.get("reason"))
        self.assertEqual(job["codex_ignored"], ["sample.csv"])
        [unknown] = rig.receipts(job)
        self.assertEqual((unknown["run_outcome"], unknown["artifacts"]["ignored_left"]), ("unknown", ["sample.csv"]))
        rig.factory.retry(R.REPO, job["id"])
        job = rig.factory.wait(R.REPO, job["id"], timeout=240)
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        _, retried, cleaned = rig.receipts(job)
        self.assertEqual(retried["commit"]["paths"], ["app.py"], "the ignored file is not in the commit")
        verify = retried["checks"][-1]
        self.assertEqual((verify["kind"], verify["check"], verify.get("not_run")), ("verify", "failed", True))
        self.assertIn("which the commit does not hold: sample.csv", verify["reason"])
        self.assertIn("sample.csv", rig.launches()[2]["stdin"], "the corrective resume names it")
        self.assertIsNone(cleaned["commit"])
        self.assertEqual((job["verify"]["commit"], job["verify"]["check"]), (retried["commit"]["sha"], "ok"))
        self.assertIsNone(job.get("codex_ignored"))

    def test_ignored_files_a_refused_commit_left_still_count(self) -> None:
        rig = R.Rig(self, scenario=[[{"run": R.FIX + f" && echo a,b > sample.csv && echo '# {R.KEY}' >> app.py"}]])
        commit_file(rig.repo, ".gitignore", "*.csv\n", "ignore data files")
        job = rig.run_job()
        self.assertEqual((job["stage"], job["failed_step"]), ("failed", "build"), job.get("reason"))
        self.assertIn("the harness commit was refused", job["reason"])
        [receipt] = rig.receipts(job)
        self.assertEqual((receipt["commit"], receipt["artifacts"]["ignored_left"]), (None, ["sample.csv"]))
        self.assertEqual(job["codex_ignored"], ["sample.csv"])

    def test_a_run_whose_ignored_files_cannot_be_listed_is_counted_by_time(self) -> None:
        """Without the snapshot from before the run (git could not list the files then), what the run made is told
        by time: an ignored file whose mtime or ctime is at or after its start. While git cannot list them at all,
        verify fails closed and the time is kept for the next look. A file from before the run never counts, nor
        one whose ctime only the harness's own share of the worktree with the group changed (setfacl, before the
        run's start)."""
        rig = R.Rig(self, scenario=[[{"run": R.FIX + " && echo a,b > sample.csv"}], [{"run": "rm -f sample.csv"}]],
                    codex={"group": OWN_GROUP})
        commit_file(rig.repo, ".gitignore", "*.csv\n", "ignore data files")

        def spec_and_data(ctx, call) -> None:
            R.spec(ctx, call)
            (Path(ctx.cwd) / "old.csv").write_text("from before the Codex run\n")
            time.sleep(1.5)   # past the slack: older than the Codex run's start

        rig.recorder.actions["spec-writer"] = spec_and_data
        real, calls = gitops.ignored_files, []

        def flaky(worktree):
            calls.append(worktree)
            if len(calls) <= 2:   # the snapshot before the first run, and the listing after it
                raise gitops.GitError(["git", "ls-files"], "fatal: cannot list", 128)
            return real(worktree)

        with mock.patch.object(gitops, "ignored_files", side_effect=flaky):
            job = rig.run_job()
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        first, second = rig.receipts(job)
        verify = first["checks"][-1]
        self.assertEqual((verify["check"], verify.get("not_run")), ("failed", True))
        self.assertIn("could not be listed", verify["reason"])
        corrective = rig.launches()[1]
        self.assertIn("which the commit does not hold: sample.csv;", corrective["stdin"])
        self.assertNotIn("old.csv", corrective["stdin"])
        self.assertIsNone(second["commit"])
        self.assertEqual((job["verify"]["commit"], job["verify"]["check"]), (first["commit"]["sha"], "ok"))
        self.assertEqual((job.get("codex_ignored"), job.get("codex_ignored_since")), (None, None))

    def test_more_ignored_files_than_the_job_keeps_are_still_counted(self) -> None:
        rig = R.Rig(self, scenario=[[{"run": R.FIX + " && echo 1 > a.csv && echo 2 > b.csv"}],
                                    [{"run": "rm -f a.csv"}]])
        commit_file(rig.repo, ".gitignore", "*.csv\n", "ignore data files")
        with mock.patch("swfactory.orchestrator.IGNORED_KEEP", 1):
            job = rig.run_job()
        self.assertEqual(job["stage"], "needs-human", job.get("reason"))
        self.assertIn("which the commit does not hold: b.csv;", job["reason"])
        first, second = rig.receipts(job)
        self.assertEqual(first["artifacts"]["ignored_left"], ["a.csv", "b.csv"])
        self.assertEqual(second["artifacts"]["ignored_left"], ["b.csv"])
        self.assertEqual(job["codex_ignored"], ["b.csv"])

    def test_an_ignored_file_rewritten_with_its_mtime_put_back_still_counts(self) -> None:
        """What a run changed among the ignored files from before it is told by size, mtime and ctime, which no
        process can set back: a rewrite to the same size that puts the mtime back still counts. The harness shares
        the worktree with the group (setfacl, which changes the ctime of every file it gives the group's entry)
        before it lists them, so an ignored file the run never touched does not count."""
        rig = R.Rig(self, scenario=[[{"run": SAME_SIZE_OLD_MTIME + R.FIX}], [{"run": "rm -f settings.ini"}]],
                    codex={"group": OWN_GROUP})
        commit_file(rig.repo, ".gitignore", "*.ini\n", "ignore local settings")

        def spec_and_setup(ctx, call) -> None:
            R.spec(ctx, call)
            for name in ("settings.ini", "untouched.ini"):   # as the repository's setup would leave them
                (Path(ctx.cwd) / name).write_text("greeting=bad!\n")

        rig.recorder.actions["spec-writer"] = spec_and_setup
        job = rig.run_job()
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        first, second = rig.receipts(job)
        self.assertEqual(first["commit"]["paths"], ["app.py"])
        self.assertEqual(first["artifacts"].get("ignored_left"), ["settings.ini"], "untouched.ini is no finding")
        verify = first["checks"][-1]
        self.assertEqual((verify["check"], verify.get("not_run")), ("failed", True))
        self.assertIn("which the commit does not hold: settings.ini", verify["reason"])
        self.assertIsNone(second["commit"])
        self.assertEqual((job["verify"]["commit"], job["verify"]["check"]), (first["commit"]["sha"], "ok"))
        self.assertIsNone(job.get("codex_ignored"))

    def test_a_crafted_bytecode_cache_is_removed_and_never_read(self) -> None:
        """What a run leaves under __pycache__ (or .pytest_cache) is no finding: the harness removes it, so verify
        never reads it. Here it would have made the broken source pass."""
        probe = scratch(self) / "seen.txt"
        rig = R.Rig(self, scenario=[[{"run": CRAFTED_PYC}],
                                    [{"run": f"ls -a __pycache__ > {probe} 2>&1; " + R.FIX}]])
        commit_file(rig.repo, ".gitignore", "__pycache__/\n", "ignore caches")
        job = rig.run_job()
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        first, second = rig.receipts(job)
        self.assertEqual(first["commit"]["paths"], ["app.py"])
        verify = first["checks"][-1]
        self.assertEqual((verify["check"], verify["exit"]), ("failed", 1), "verify ran the broken source")
        self.assertNotIn("not_run", verify)
        self.assertNotIn("ignored_left", first["artifacts"], "a cache is removed, not a finding")
        self.assertNotIn(".pyc", probe.read_text(), "nothing the run left in __pycache__ was still there")
        self.assertEqual(second["checks"][-1]["check"], "ok")

    def test_verify_reads_no_bytecode_from_the_worktree(self) -> None:
        """A .pyc is valid by its source's mtime, which a commit does not hold: committed with it (__pycache__ not
        ignored here), it would pass in the worktree and fail in any fresh checkout. verify compiles from source."""
        rig = R.Rig(self, scenario=[[{"run": CRAFTED_PYC}]])
        job = rig.run_job()
        self.assertEqual(job["stage"], "needs-human", job.get("reason"))
        first = rig.receipts(job)[0]
        self.assertIn("app.py", first["commit"]["paths"])
        self.assertTrue([path for path in first["commit"]["paths"] if path.startswith("__pycache__/app.")],
                        first["commit"]["paths"])
        verify = first["checks"][-1]
        self.assertEqual((verify["check"], verify["exit"]), ("failed", 1), "verify ran the broken source")

    def test_a_delivered_job_reworked_starts_codex_afresh(self) -> None:
        """The job's CODEX_HOME (and the threads' rollouts in it) is deleted when the job is delivered; a delivered job
        that comes back (its merge conflicts, then rework) gets a new session with the full system text."""
        rig = R.Rig(self, scenario=[[{"run": R.FIX}], [{"text": "Looked at the conflict."}]])
        factory = rig.start()
        job = factory.submit(R.REPO, "Fix the greeting", delivery="branch")
        job = factory.wait(R.REPO, job["id"], timeout=240)
        self.assertEqual(job["stage"], "delivered", job.get("reason"))
        self.assertFalse((work.work_root(rig.config) / "homes" / f"{R.REPO}--{job['id']}").exists())
        self.assertIsNone(job["agents"]["builder"]["session"], "no thread outlives the home it lived in")
        app = (rig.repo / "app.py").read_text().replace('return "Hello, there"', 'return "Hi there"')
        commit_file(rig.repo, "app.py", app, "a conflicting greeting")
        factory.approve(R.REPO, job["id"])
        job = factory.wait(R.REPO, job["id"], timeout=60)
        self.assertEqual(job["stage"], "needs-human", job.get("reason"))
        self.assertIn("conflict", job["reason"])
        factory.rework(R.REPO, job["id"], "Resolve the conflict with main.")
        job = factory.wait(R.REPO, job["id"], timeout=240)
        second = rig.launches()[1]
        self.assertNotIn("resume", second["argv"])
        self.assertIn("No network in this run", second["stdin"], "a fresh session gets the system text")
        self.assertIn("Hand-over", second["stdin"])
        self.assertIn("The earlier Codex session is gone with the job's Codex home", second["stdin"])
        self.assertIn("rework-1.md", second["stdin"])
        receipts = rig.receipts(job)
        self.assertEqual(receipts[1]["run_outcome"], "completed")
        self.assertNotEqual(receipts[1]["codex"]["thread_id"], receipts[0]["codex"]["thread_id"], "a new thread")

    def test_a_run_that_changes_nothing_makes_no_commit(self) -> None:
        rig = R.Rig(self, scenario=[[{"run": "printf 'STATUS: READY\\n' > {out_dir}/build.md"}]])
        job = rig.run_job()
        self.assertEqual(job["stage"], "needs-human")
        self.assertIn("no commits", job["reason"])
        [receipt] = rig.receipts(job)
        self.assertEqual((receipt["commit"], receipt["result"]), (None, "nothing to commit"))


@unittest.skipUnless(NO_BWRAP is None, NO_BWRAP or "")
class StopAndCrashTests(unittest.TestCase):
    """T15 and T16 with the stand-in Codex: typed outcomes, the reconcile, no commit, nothing replayed."""

    SLOW = [[{"run": "echo 'half a fix' >> app.py && echo new > notes.txt"}, {"sleep": 60}]]

    def assert_reconciled(self, rig: R.Rig, job: dict, outcome: str) -> dict:
        [receipt] = rig.receipts(job)
        self.assertEqual(receipt["run_outcome"], outcome)
        self.assertNotEqual(receipt["run_outcome"], "completed")
        self.assertIsNone(receipt["commit"])
        reconciled = receipt["reconciled"]
        self.assertEqual(reconciled["paths"], ["app.py", "notes.txt"])
        self.assertTrue(reconciled["diff_sha256"])
        self.assertEqual(rig.git("rev-list", "--count", f"main..{job['branch']}").strip(), "0", "no commit was made")
        self.assertEqual(job["reconcile"]["paths"], ["app.py", "notes.txt"])
        self.assertIn("left changes in app.py, notes.txt", job["reconcile"]["summary"])
        return receipt

    def test_t15_stop_sends_sigint_first_and_the_run_is_cancelled(self) -> None:
        rig = R.Rig(self, scenario=self.SLOW)
        factory = rig.start()
        job = factory.submit(R.REPO, "Fix the greeting")
        self.assertTrue(wait_for(lambda: (rig.fake_dir / "launches.jsonl").exists() and in_flight(factory, job["id"]),
                                 30))
        time.sleep(0.5)
        started = time.monotonic()
        factory.cancel(R.REPO, job["id"])
        job = factory.wait(R.REPO, job["id"], timeout=60)
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertEqual((rig.fake_dir / "signals").read_text().split(), ["SIGINT"], "SIGINT first, and enough")
        self.assertEqual(job["stage"], "cancelled")
        receipt = self.assert_reconciled(rig, job, "cancelled")
        self.assertEqual(receipt["codex"]["exit"], 1, "exec's own answer to SIGINT")
        self.assertEqual(len(rig.launches()), 1, "nothing re-sent the prompt")

    def test_t15_the_wall_clock_is_timed_out_and_retry_shows_the_summary(self) -> None:
        rig = R.Rig(self, scenario=self.SLOW, sections={"limits": {"max_wall_seconds": 2}})
        job = rig.run_job()
        self.assertEqual((job["stage"], job["failed_step"]), ("failed", "build"))
        self.assertIn("outcome timed_out", job["reason"])
        self.assert_reconciled(rig, job, "timed_out")
        self.assertEqual((rig.fake_dir / "signals").read_text().split(), ["SIGINT"])
        retried = rig.factory.retry(R.REPO, job["id"])
        self.assertEqual(retried["detail"], "retrying build: the previous run left changes in app.py, notes.txt; "
                                            "the retry starts from them")

    def test_t15_a_server_stop_leaves_the_job_failed_and_retryable(self) -> None:
        rig = R.Rig(self, scenario=self.SLOW)
        factory = rig.start()
        job = factory.submit(R.REPO, "Fix the greeting")
        self.assertTrue(wait_for(lambda: in_flight(factory, job["id"]) and rig.launches(), 30))
        time.sleep(0.5)
        factory.stop(timeout=20)
        job = factory.job(R.REPO, job["id"])
        self.assertEqual(job["stage"], "failed")
        self.assert_reconciled(rig, job, "cancelled")

    def test_t16_kill_9_of_codex_is_unknown(self) -> None:
        rig = R.Rig(self, scenario=[[{"run": "echo 'half a fix' >> app.py && echo new > notes.txt"}, {"die": True}]])
        job = rig.run_job()
        self.assertEqual(job["stage"], "failed")
        self.assertIn("outcome unknown", job["reason"])
        self.assertIn("killed by signal 9", job["reason"])
        receipt = self.assert_reconciled(rig, job, "unknown")
        self.assertEqual(receipt["result"], "run unknown: reconciled, nothing committed, never replayed")
        self.assertEqual(len(rig.launches()), 1)

    def test_t16_exit_0_without_turn_completed_is_unknown_and_never_continued(self) -> None:
        rig = R.Rig(self, scenario=[[{"run": "echo 'half a fix' >> app.py && echo new > notes.txt"}, {"exit0": True}],
                                    [{"run": R.FIX}]])
        job = rig.run_job()
        self.assertEqual((job["stage"], job["failed_step"]), ("failed", "build"))
        self.assertIn("outcome unknown", job["reason"])
        self.assertIn("exited 0 without a turn.completed event", job["reason"])
        receipt = self.assert_reconciled(rig, job, "unknown")
        self.assertEqual(receipt["result"], "run unknown: reconciled, nothing committed, never replayed")
        self.assertEqual(len(rig.launches()), 1, "no corrective resume continued the unknown run")
        self.assertEqual(job["runs"][-1]["ok"], False)

    def test_exit_0_without_a_completed_turn_is_never_ok(self) -> None:
        """The driver itself (§40): exec's JSONL ending without turn.completed, or with turn.failed, after exit 0 is
        unknown and not ok, for the plain codex preset as for codex-worker."""
        from tests.test_cli_driver import FAKE_COMMAND, driver

        tmp = scratch(self)
        for label, events in (("no terminal event", ['{"type":"thread.started","thread_id":"t-1"}',
                                                     '{"type":"turn.started"}']),
                              ("turn.failed", ['{"type":"thread.started","thread_id":"t-2"}',
                                               '{"type":"turn.failed","error":{"message":"boom"}}'])):
            with self.subTest(label):
                replay = tmp / f"{label.replace(' ', '-')}.jsonl"
                replay.write_text("\n".join(events) + "\n")
                runner = make_runner(SimpleNamespace(driver=lambda name: driver(
                    "codex", command=FAKE_COMMAND, env={"FAKE_CLI_REPLAY": str(replay)}), raw={}), "codex")
                ctx = RunContext(job={"id": "001-x"}, agent=SimpleNamespace(name="builder", body=""), cwd=tmp,
                                 job_dir=tmp, write_paths=[tmp], read_roots=[tmp], log_path=tmp / "01-builder.jsonl",
                                 emit=lambda *a: None, cancel=threading.Event(),
                                 model=SimpleNamespace(key="m", model="m"),
                                 limits=SimpleNamespace(max_wall_seconds=30, max_turns=5))
                result = runner.run(ctx, "go")
                self.assertEqual((result.ok, result.outcome, result.details["exit"]), (False, "unknown", 0))
                self.assertIn("without a turn.completed event", result.error)

    def test_t16_a_retry_after_an_unknown_round_2_run_starts_a_new_session(self) -> None:
        """§3.6: a retry is a fresh run on the worktree as it is, never the old prompt in the old thread."""
        rig = R.Rig(self, scenario=[[{"run": R.FIX}], [{"run": "echo '# half a change' >> app.py"}, {"die": True}],
                                    [{"text": "Picked it up."}]])
        rig.recorder.actions["reviewer"] = reviews("CHANGES", "PASS")
        job = rig.run_job()
        self.assertEqual((job["stage"], job["round"], job["failed_step"]), ("failed", 2, "build"))
        first, second = rig.launches()
        self.assertIn("resume", second["argv"], "round 2 resumed round 1's thread, as designed")
        rig.factory.retry(R.REPO, job["id"])
        rig.factory.wait(R.REPO, job["id"], timeout=240)
        third = rig.launches()[2]
        self.assertNotIn("resume", third["argv"], "the unknown run's thread is never resumed")
        self.assertNotEqual(third["stdin"], second["stdin"])
        self.assertIn("No network in this run", third["stdin"], "a new session gets the system text")
        self.assertIn("Hand-over", third["stdin"])
        self.assertIn(f"The previous builder run ({job['runs'][-1]['run']}) ended unknown", third["stdin"])
        self.assertIn("Reconciled: the previous run left changes in app.py", third["stdin"])

    def test_a_retry_after_a_restart_mid_run_starts_a_new_session(self) -> None:
        rig = R.Rig(self, scenario=[[{"run": R.FIX}], [{"text": "Picked it up."}]])
        rig.recorder.actions["approver"] = _escalate
        job = rig.run_job()
        self.assertEqual(job["stage"], "needs-human", job.get("reason"))
        rig.factory.stop()
        path = Path(job["job_dir"]) / "job.json"   # what a server that died in round 2's Codex run leaves
        data = json.loads(path.read_text())
        data.update(stage="build", step="build", round=2, build_request={"prompt": "Round 2: address it.",
                                                                         "resume": True},
                    codex_run={"run": "09-builder", "agent": "builder", "round": 2, "started": work.utc_now()})
        path.write_text(json.dumps(data, indent=2))
        factory = rig.start()
        job = factory.wait(R.REPO, job["id"], timeout=30)
        self.assertEqual((job["stage"], job["reason"]), ("failed", "interrupted (server restarted)"))
        factory.retry(R.REPO, job["id"])
        factory.wait(R.REPO, job["id"], timeout=240)
        second = rig.launches()[1]
        self.assertNotIn("resume", second["argv"])
        self.assertIn("Hand-over", second["stdin"])
        self.assertIn("The previous builder run (09-builder) ended unknown", second["stdin"])
        self.assertIn("Round 2: address it.", second["stdin"])

    def test_t16_a_harness_restart_mid_run_is_unknown_and_reconciled(self) -> None:
        # a Codex that outlived the harness would write late.txt after its sleep: the reconcile would then be stale
        rig = R.Rig(self, scenario=[[{"run": self.SLOW[0][0]["run"] + " && echo a,b > sample.csv"}, {"sleep": 6},
                                     {"run": "echo late > late.txt"}]])
        commit_file(rig.repo, ".gitignore", "*.csv\n", "ignore data files")
        proc, job_file = harness_process(self, rig)
        self.assertTrue(wait_for(lambda: job_mid_run(job_file()) and len(rig.launches()) == 1, 60),
                        "the Codex run started in the harness process")
        self.assertTrue(wait_for(lambda: codex_pids(rig.binary, rig.tmp), 10), "the process table shows the stand-in")
        time.sleep(0.5)
        os.kill(proc.pid, signal.SIGKILL)   # the harness dies; Codex dies with it (the launcher's parent-death signal)
        proc.wait(10)
        self.assertTrue(wait_for(lambda: not codex_pids(rig.binary, rig.tmp), 5), "Codex died with the harness")
        time.sleep(7)   # past the stand-in's sleep: a Codex still running would have written late.txt by now
        job_id = job_file().parent.name
        worktree = Path(rig.config.data_dir) / "worktrees" / R.REPO / job_id
        self.assertFalse((worktree / "late.txt").exists(), "nothing wrote to the worktree after the harness died")
        factory = rig.start()   # the next start: restart recovery
        job = factory.wait(R.REPO, job_id, timeout=30)
        self.assertEqual(job["stage"], "failed")
        self.assertEqual(job["reason"], "interrupted (server restarted)")
        receipt = self.assert_reconciled(rig, job, "unknown")
        self.assertEqual(receipt["error"], "interrupted (server restarted)")
        self.assertIsNone(job.get("codex_run"))
        # its snapshot went with the old process: what it left that git ignores is counted by time, for verify
        self.assertEqual((receipt["artifacts"]["ignored_left"], job["codex_ignored"]), (["sample.csv"], ["sample.csv"]))
        time.sleep(1.0)
        self.assertEqual(len(rig.launches()), 1, "nothing re-sent the prompt")

    @unittest.skipUnless(NO_SETFACL is None, NO_SETFACL or "")
    def test_a_restart_mid_run_never_counts_what_the_worktrees_share_touched(self) -> None:
        """With a worktree group the harness gives the group its entry on every file of the worktree (setfacl, which
        changes the ctime of each file whose ACL it changes) before the run's start: a restart's count by time sees
        what the run left, never the toolchain the repository's setup made."""
        setup = ("mkdir -p node_modules/pkg && for i in 1 2 3; do echo 'module.exports = 1' > node_modules/pkg/f$i.js; "
                 "done && sleep 1.5")   # older than the Codex run's start, past the slack
        rig = R.Rig(self, scenario=[[{"run": "echo a,b > sample.csv"}, {"sleep": 60}]], codex={"group": OWN_GROUP},
                    repo_extra={"setup": setup})
        commit_file(rig.repo, ".gitignore", "*.csv\nnode_modules/\n", "ignore data files and the toolchain")
        proc, job_file = harness_process(self, rig)
        worktree = lambda: Path(rig.config.data_dir) / "worktrees" / R.REPO / job_file().parent.name  # noqa: E731
        self.assertTrue(wait_for(lambda: job_mid_run(job_file()) and (worktree() / "sample.csv").exists(), 60),
                        "the Codex run started and wrote its file")
        os.kill(proc.pid, signal.SIGKILL)   # the harness dies mid-run; Codex dies with it
        proc.wait(10)
        job_id = job_file().parent.name
        factory = rig.start()   # the next start: restart recovery, its count by time
        job = factory.wait(R.REPO, job_id, timeout=30)
        self.assertEqual((job["stage"], job["reason"]), ("failed", "interrupted (server restarted)"))
        [receipt] = rig.receipts(job)
        self.assertEqual((receipt["artifacts"].get("ignored_left"), job.get("codex_ignored")),
                         (["sample.csv"], ["sample.csv"]))
        self.assertEqual(sorted(p.name for p in (worktree() / "node_modules" / "pkg").iterdir()),
                         ["f1.js", "f2.js", "f3.js"], "the toolchain is still there")


class OnlyTheFactorysBuilderTests(unittest.TestCase):
    """K20: codex-worker runs a factory job's builder, which the orchestrator clears (the routing check, the codex model
    blocker, the run's custody request lease) and follows (the receipt, the reconcile, the harness commit); a chat or
    any other caller of the driver gets no Codex run."""

    def test_a_chat_whose_agent_routes_to_codex_worker_is_refused(self) -> None:
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                rig = R.Rig(self, check=enabled, codex={"enabled": enabled})
                manager = chat_manager(rig)
                with self.assertRaises(ValueError) as caught:
                    manager.new_chat(R.REPO, agent="builder")
                self.assertIn("codex-worker", str(caught.exception))
                self.assertIn("builder only", str(caught.exception))
                self.assertEqual(manager.chats(), [])
                self.assertEqual(rig.launches(), [])

    def test_a_chat_json_edited_to_codex_worker_runs_no_codex(self) -> None:
        rig = R.Rig(self)
        manager = chat_manager(rig)
        chat = manager.new_chat(R.REPO, agent="coder")
        path = Path(rig.config.data_dir) / "chats" / chat["id"] / "chat.json"
        data = json.loads(path.read_text())
        data.update(driver="codex-worker", model="local")
        path.write_text(json.dumps(data))
        manager.chat_send(chat["id"], "Fix the greeting.")
        done = turn_done(manager, chat["id"])
        self.assertEqual(done["status"], "error")
        self.assertIn("codex-worker", done["error"])
        self.assertEqual(rig.launches(), [], "nothing launched Codex")

    def test_the_driver_refuses_a_run_the_orchestrator_did_not_clear(self) -> None:
        rig = R.Rig(self)
        tmp = scratch(self)
        job = {"repo": R.REPO, "id": "001-fix-greeting", "job_dir": str(tmp), "worktree": str(rig.repo)}
        ctx = RunContext(job=job, agent=SimpleNamespace(name="builder", body=""), cwd=rig.repo, job_dir=tmp,
                         write_paths=[rig.repo], read_roots=[rig.repo], log_path=tmp / "logs" / "02-builder.jsonl",
                         emit=lambda *a: None, cancel=threading.Event(), model=rig.config.model("local"),
                         limits=rig.config.limits())
        result = make_runner(rig.config, "codex-worker").run(ctx, "Fix the greeting.")
        self.assertEqual((result.ok, result.outcome), (False, "failed"))
        self.assertEqual(result.error, "codex-worker runs a factory job's builder only")
        self.assertEqual(rig.launches(), [])
        self.assertFalse((work.work_root(rig.config) / "runs").exists(), "nothing was made for it")


class ResponsesCheckTests(unittest.TestCase):
    """T24: the Responses check and the codex model blocker (K20). Outside a custody request lease only GETs; a
    route no lease would cover never gets a POST and never starts."""

    def submit_refused(self, rig: R.Rig) -> str:
        factory = rig.start()
        with self.assertRaises(ValueError) as caught:
            factory.submit(R.REPO, "Fix the greeting")
        self.assertEqual([launch for launch in rig.launches() if launch["argv"][:1] == ["exec"]], [])
        return str(caught.exception)

    def test_t24_no_recorded_check_is_a_blocker_and_gets_only_gets(self) -> None:
        from swfactory import doctor

        rig = R.Rig(self, check=False)
        model = rig.config.model("local")
        self.assertIn("has not passed its check", work.model_blocker(rig.config, model.provider, model.model))
        found = {c["name"]: c for c in doctor._codex_checks(rig.config)}
        self.assertFalse(found["codex model builder"]["ok"])
        self.assertIn("blocker", found["codex model builder"]["detail"])
        self.assertIn("cannot start", self.submit_refused(rig))
        self.assertEqual(rig.stub.posts(), [], "outside a lease the model server gets no POST")

    def test_t24_under_a_lease_one_responses_request_clears_the_blocker(self) -> None:
        rig = R.Rig(self, check=False)
        model = rig.config.model("local")
        record = work.responses_check(rig.config, model)
        self.assertTrue(record["ok"], record)
        [(method, path, headers, body)] = rig.stub.posts()
        self.assertEqual((method, path), ("POST", "/v1/responses"))
        self.assertEqual(headers["authorization"], f"Bearer {R.KEY}")
        self.assertEqual((body["model"], body["stream"]), (R.STUB_MODEL, False))
        self.assertEqual(record["status"], 200)
        self.assertEqual(len(record["body_sha256"]), 64)
        acquire = [entry for entry in rig.register.requests if entry[1] == "/leases"]
        self.assertEqual(len(acquire), 1, "the check took a custody request lease first")
        self.assertIn("release", rig.register.ops())
        self.assertIsNone(work.model_blocker(rig.config, model.provider, model.model))

    def test_t24_a_404_keeps_the_blocker(self) -> None:
        rig = R.Rig(self, check=False)
        rig.stub.no_responses = True
        record = work.responses_check(rig.config, rig.config.model("local"))
        self.assertEqual((record["ok"], record["status"]), (False, 404))
        self.assertIn("failed", work.model_blocker(rig.config, rig.config.model("local").provider, R.STUB_MODEL))

    def test_t24_a_denied_lease_sends_nothing(self) -> None:
        rig = R.Rig(self, check=False)
        rig.register.maintenance = True
        record = work.responses_check(rig.config, rig.config.model("local"))
        self.assertEqual((record["ok"], record["sent"]), (False, False))
        self.assertIn("denied", record["detail"])
        self.assertEqual(rig.stub.posts(), [])

    def test_t24_a_route_no_lease_would_cover_never_starts(self) -> None:
        for label, provider in (("no custody", {"custody": None}), ("neither lazy_load nor allow_model_swap",
                                                                    {"lazy_load": False})):
            with self.subTest(label):
                rig = R.Rig(self, check=False, sections={"providers": {"strata": provider}})
                model = rig.config.model("local")
                record = work.responses_check(rig.config, model)
                self.assertFalse(record["sent"])
                blocker = work.model_blocker(rig.config, model.provider, model.model)
                self.assertIn("no custody request lease would cover", blocker)
                self.assertIn("cannot start", self.submit_refused(rig))
                self.assertEqual(rig.stub.posts(), [])

    def test_t24_an_endpoint_that_is_not_loaded_is_a_blocker(self) -> None:
        rig = R.Rig(self)
        rig.register.load("strata", "another-model")
        model = rig.config.model("local")
        self.assertIn("is not loaded", work.model_blocker(rig.config, model.provider, model.model))
        self.assertIn("is not loaded", self.submit_refused(rig))

    def test_off_by_default(self) -> None:
        config = Config("/nonexistent/config.json")
        self.assertFalse(work.settings(config)["enabled"])
        rig = R.Rig(self, codex={"enabled": False})
        self.assertIn("codex.enabled is false", self.submit_refused(rig))


class ContainmentGateTests(unittest.TestCase):
    """With a Codex user the host holds Codex in (this version's launcher, the uid egress rule, which is gone after a
    reboot unless its unit loads it): work.containment_blocker refuses the job at submit and every run before it
    starts, and a run whose share-worktree failed is never committed."""

    NOT_LOADED = "the uid egress rule is not loaded: a connect from the Codex user to 127.0.0.1:9 was refused"

    def test_a_job_does_not_start_while_the_rule_is_not_loaded(self) -> None:
        rig = R.Rig(self)
        factory = rig.start()
        with mock.patch.object(work, "containment_blocker", return_value=self.NOT_LOADED) as gate:
            with self.assertRaises(ValueError) as caught:
                factory.submit(R.REPO, "Fix the greeting")
        self.assertIn(self.NOT_LOADED, str(caught.exception))
        self.assertEqual(gate.call_args.args[1].base_url, rig.stub.url, "the route's own provider")

    def test_a_run_does_not_start_once_the_rule_is_gone(self) -> None:
        """The rule was there at submit and is gone before the run (matrix powers off every night)."""
        rig = R.Rig(self, scenario=[[{"run": R.FIX}, {"text": "Fixed."}]])
        answers = iter([None])
        with mock.patch.object(work, "containment_blocker", side_effect=lambda *a: next(answers, self.NOT_LOADED)):
            job = rig.run_job()
        self.assertEqual((job["stage"], job["failed_step"]), ("failed", "build"), job.get("reason"))
        self.assertIn(f"builder cannot start on codex-worker: {self.NOT_LOADED}", job["reason"])
        self.assertEqual(rig.launches(), [], "Codex never started")

    def test_a_run_whose_share_failed_is_never_committed(self) -> None:
        """An older launcher refuses share-worktree; a folder Codex left closed to the harness would be missing from
        the commit, and git would say nothing."""
        rig = R.Rig(self, scenario=[[{"run": R.FIX}, {"text": "Fixed."}]])
        refused = {"error": "sf-codex-launch: refused: unknown verb 'share-worktree'", "exit": 2}
        with mock.patch.object(work, "share_worktree", return_value=refused):
            job = rig.run_job()
        self.assertEqual((job["stage"], job["failed_step"]), ("failed", "build"), job.get("reason"))
        self.assertIn("what Codex wrote could not be made the group's", job["reason"])
        [receipt] = rig.receipts(job)
        self.assertIsNone(receipt["commit"])
        self.assertIn({"kind": "share_worktree", "ok": False, "reason": refused["error"]}, receipt["checks"])
        self.assertNotIn("secret_scan", [c["kind"] for c in receipt["checks"]], "no commit was tried")
        self.assertNotEqual(rig.git("log", "-1", "--format=%an", job["branch"]).strip(), "Software Factory",
                            "the branch holds no harness commit")


class RoutingCheckTests(unittest.TestCase):
    """§3.3's routing check (O4, O10): codex-worker runs the builder only, on this machine; on a repository not marked
    personal_data: false no other role of the job is hosted. Fails closed."""

    def test_a_hosted_reviewer_on_a_repository_with_personal_data_refuses_to_start(self) -> None:
        hosted = {"agents": {"reviewer-code": {"driver": "builtin", "model": "opus"}}}
        rig = R.Rig(self, sections=hosted)
        factory = rig.start()
        with self.assertRaises(ValueError) as caught:
            factory.submit(R.REPO, "Fix the greeting")
        self.assertIn("reviewer-code would read what Codex wrote", str(caught.exception))
        self.assertIn("api.deepseek.com", str(caught.exception))
        free = R.Rig(self, sections=hosted, repo_extra={"personal_data": False})
        routing, _ = free.start()._routing(free.config.repo(R.REPO), ["code"], None)
        self.assertIsNone(work.routing_problem(free.config, R.REPO, routing))

    def test_a_cli_reviewer_counts_as_hosted_and_codex_runs_the_builder_only(self) -> None:
        rig = R.Rig(self, sections={"agents": {"reviewer-code": {"driver": "claude", "model": "local"}}})
        factory = rig.start()
        with self.assertRaises(ValueError) as caught:
            factory.submit(R.REPO, "Fix the greeting")
        self.assertIn("the claude driver reaches an endpoint of its own", str(caught.exception))
        other = R.Rig(self, sections={"agents": {"approver": {"driver": "codex-worker", "model": "local"}}})
        with self.assertRaises(ValueError) as caught:
            other.start().submit(R.REPO, "Fix the greeting")
        self.assertIn("codex-worker runs the builder only", str(caught.exception))

    def failed_at_review(self, **kwargs) -> tuple[R.Rig, dict]:
        """A job whose Codex builder committed and whose local reviewer failed (its model server is down)."""
        rig = R.Rig(self, scenario=[[{"run": R.FIX}]], verify=None, **kwargs)
        rig.recorder.actions["reviewer"] = failing("local model server is down")
        job = rig.run_job()
        self.assertEqual((job["stage"], job["failed_step"]), ("failed", "review"), job.get("reason"))
        return rig, job

    def test_a_job_that_ran_codex_cannot_be_rerouted_off_this_machine(self) -> None:
        rig, job = self.failed_at_review()
        for routing, who in (({"reviewer-code": {"driver": "claude", "model": "local"}}, "reviewer-code"),
                             ({"builder": {"driver": "builtin", "model": "local"},
                               "reviewer-code": {"driver": "builtin", "model": "opus"}}, "reviewer-code"),
                             ({"builder": {"driver": "builtin", "model": "opus"}}, "builder")):
            with self.subTest(routing=routing):
                with self.assertRaises(ValueError) as caught:
                    rig.factory.set_routing(R.REPO, job["id"], routing)
                self.assertIn(f"{who} would read what Codex wrote", str(caught.exception))
                self.assertIn("§3.3", str(caught.exception))
                self.assertEqual(rig.factory.job(R.REPO, job["id"])["routing"], job["routing"], "unchanged")
        free, done = self.failed_at_review(repo_extra={"personal_data": False})
        changed = free.factory.set_routing(R.REPO, done["id"], {"reviewer-code": {"driver": "claude", "model": "local"}})
        self.assertEqual(changed["routing"]["reviewer-code"]["driver"], "claude")

    def test_a_job_that_never_ran_codex_is_rerouted_freely(self) -> None:
        rig = R.Rig(self, verify=None, sections={"agents": {"builder": {"driver": "builtin", "model": "local"}}})
        rig.recorder.actions["reviewer"] = failing("local model server is down")
        job = rig.run_job()
        self.assertEqual(job["stage"], "failed", job.get("reason"))
        changed = rig.factory.set_routing(R.REPO, job["id"], {"reviewer-code": {"driver": "claude", "model": "local"}})
        self.assertEqual(changed["routing"]["reviewer-code"]["driver"], "claude")

    def test_a_route_changed_another_way_is_refused_before_the_run(self) -> None:
        rig, job = self.failed_at_review()
        factory, key = rig.factory, (R.REPO, job["id"])
        hosted, _ = factory._routing(rig.config.repo(R.REPO), job["reviewers"],
                                     {"reviewer-code": {"driver": "claude", "model": "local"}}, keep=job["routing"])
        live = factory._entry(key)
        with factory._job_lock(key):   # as a saved config reload or an older job.json might leave it
            live["routing"] = hosted
            factory._save(live)
        rig.recorder.actions["reviewer"] = review_pass
        factory.retry(R.REPO, job["id"])
        job = factory.wait(R.REPO, job["id"], timeout=60)
        self.assertEqual(job["stage"], "failed")
        self.assertIn("reviewer-code cannot start: reviewer-code would read what Codex wrote", job["reason"])
        self.assertEqual([c for c in rig.recorder.calls if c.agent == "reviewer-code" and c.driver == "claude"], [])

    def test_the_builder_never_runs_on_a_hosted_model(self) -> None:
        rig = R.Rig(self, check=False, sections={"agents": {"builder": {"driver": "codex-worker", "model": "opus"}}},
                    repo_extra={"personal_data": False})
        with self.assertRaises(ValueError) as caught:
            rig.start().submit(R.REPO, "Fix the greeting")
        self.assertIn("models on this machine only", str(caught.exception))


class CustodyDuringTheRunTests(unittest.TestCase):
    """K20 (d): no codex-worker run sends the model server a request outside a custody request lease: one that is lost
    before the spawn starts nothing, one lost mid-run stops Codex (SIGINT first; failed, reconciled, never replayed)."""

    def test_k20_a_lease_lost_mid_run_stops_codex(self) -> None:
        rig = R.Rig(self, scenario=[[{"run": "echo 'half a fix' >> app.py && echo new > notes.txt"}, {"sleep": 60}]],
                    verify=None)
        factory = rig.start()
        job = factory.submit(R.REPO, "Fix the greeting")
        self.assertTrue(wait_for(lambda: rig.launches() and in_flight(factory, job["id"]), 30))
        client = custody.client_for(custody.settings_of(rig.config.model("local").provider))
        [lease] = list(client.leases.values())
        rig.register.restart("e2")   # the operator's register starts over: the run's lease is of its earlier instance
        self.assertFalse(client.renew(lease))
        self.assertEqual(lease.state, "lost")
        started = time.monotonic()
        job = factory.wait(R.REPO, job["id"], timeout=60)
        self.assertLess(time.monotonic() - started, 10.0)
        self.assertEqual((rig.fake_dir / "signals").read_text().split(), ["SIGINT"])
        self.assertEqual(job["stage"], "failed")
        self.assertIn("GPU custody: the lease was lost", job["reason"])
        [receipt] = rig.receipts(job)
        self.assertEqual((receipt["run_outcome"], receipt["commit"]), ("failed", None))
        self.assertEqual(receipt["reconciled"]["paths"], ["app.py", "notes.txt"])
        self.assertEqual(len(rig.launches()), 1, "nothing re-sent the prompt")

    def test_k20_a_lease_lost_before_the_spawn_starts_nothing(self) -> None:
        rig = R.Rig(self, scenario=[[{"run": R.FIX}]], verify=None)
        real = work.model_blocker

        def blocker_then_lost(config, provider, model_id, **kwargs):   # the register restarts right after the check
            found = real(config, provider, model_id, **kwargs)
            lease = custody.lease_of(provider)
            if lease is not None:
                rig.register.restart("e2")
                lease.client.renew(lease)
            return found

        with mock.patch.object(work, "model_blocker", side_effect=blocker_then_lost):
            job = rig.run_job()
        self.assertEqual(job["stage"], "failed")
        self.assertIn("builder cannot start on codex-worker: the lease was lost", job["reason"])
        self.assertEqual(rig.launches(), [])


class VerifyCommandTests(unittest.TestCase):
    """verify, the harness's own re-run: Stop ends it at once, and it starts with the launcher's environment only."""

    @staticmethod
    def sleepers(mark: bytes) -> list[int]:
        found = []
        for entry in os.listdir("/proc"):
            if entry.isdigit():
                try:
                    argv = Path(f"/proc/{entry}/cmdline").read_bytes().split(b"\0")
                except OSError:
                    continue
                if argv[:2] == [b"sleep", mark]:
                    found.append(int(entry))
        return found

    @unittest.skipUnless(NO_BWRAP is None, NO_BWRAP or "")
    def test_stop_ends_a_running_verify_at_once(self) -> None:
        mark = f"{61 + os.getpid() % 1000 / 1000:.3f}"   # this test's own sleep, told apart from any other
        rig = R.Rig(self, scenario=[[{"run": R.FIX}]], verify=f"sleep {mark}")
        factory = rig.start()
        job = factory.submit(R.REPO, "Fix the greeting")
        self.assertTrue(wait_for(lambda: self.sleepers(mark.encode()), 60), "verify runs")
        started = time.monotonic()
        factory.cancel(R.REPO, job["id"])
        job = factory.wait(R.REPO, job["id"], timeout=30)
        self.assertLess(time.monotonic() - started, 10.0)
        self.assertEqual(job["stage"], "cancelled")
        self.assertTrue(wait_for(lambda: not self.sleepers(mark.encode()), 5), "verify's processes are gone")
        self.assertIsNone(job.get("verify"), "a stopped verify is never the commit's result")
        [receipt] = rig.receipts(job)
        self.assertEqual((receipt["checks"][-1]["check"], receipt["checks"][-1]["reason"]),
                         ("failed", "verify was stopped (Stop)"))

    def test_a_stop_already_given_ends_the_command_at_once(self) -> None:
        stop = threading.Event()
        stop.set()
        started = time.monotonic()
        output, code, ended = work._run_capped(["sleep", "30"], 600, {"PATH": "/usr/bin:/bin"}, stop)
        self.assertLess(time.monotonic() - started, 3.0)
        self.assertEqual(ended, "cancelled")
        self.assertNotEqual(code, 0)

    @unittest.skipUnless(NO_BWRAP is None, NO_BWRAP or "")
    def test_verify_starts_with_the_launchers_environment_only(self) -> None:
        rig = R.Rig(self, verify="true")
        started: list[tuple[list, dict | None]] = []
        real = subprocess.Popen

        def popen(argv, *args, **kwargs):
            started.append((list(argv), kwargs.get("env")))
            return real(argv, *args, **kwargs)

        job = {"repo": R.REPO, "id": "001-fix-greeting", "worktree": str(rig.repo), "job_dir": str(scratch(self))}
        with mock.patch.dict(os.environ, {"SF_PLANTED_TOKEN": "planted-secret-0123456789"}), \
                mock.patch.object(work.subprocess, "Popen", side_effect=popen):
            record = work.verify(rig.config, job, "02-builder", None)
        self.assertEqual(record["check"], "ok", record)
        [env] = [env for argv, env in started if argv[:1] == ["bwrap"]]
        self.assertEqual(sorted(env), ["LANG", "PATH"], "never the harness's own environment")


class CommitScanTests(unittest.TestCase):
    """§3.3: the harness commit scans what it stages for every secret value, a file git takes for binary too."""

    KEY = "planted-secret-0123456789abcdef"

    def test_a_secret_in_a_file_git_takes_for_binary_is_refused(self) -> None:
        cases = (("a NUL byte", {}, "fixture.bin", b"\0" + KEY_BYTES),
                 ("-diff in .gitattributes", {".gitattributes": "*.lock -diff\n"}, "deps.lock",
                  b"token = " + KEY_BYTES + b"\n"),
                 ("binary in .gitattributes", {".gitattributes": "*.svg binary\n"}, "logo.svg",
                  b"<svg>" + KEY_BYTES + b"</svg>\n"))
        for label, files, name, data in cases:
            with self.subTest(label):
                repo = make_repo(scratch(self), "r", {"README.md": "# r\n", **files})
                head = gitops.rev_parse(repo, "HEAD")
                (repo / name).write_bytes(data)
                with self.assertRaises(gitops.CommitRefused):
                    gitops.commit_all(repo, "m", secrets=[self.KEY])
                self.assertEqual(gitops.rev_parse(repo, "HEAD"), head, "nothing was committed")
                staged = subprocess.run(["git", "-C", str(repo), "diff", "--cached", "--name-only"],
                                        capture_output=True, text=True, check=True).stdout
                self.assertEqual(staged, "", "the index was put back")
                (repo / name).write_bytes(data.replace(KEY_BYTES, b"nothing secret here"))
                self.assertEqual(gitops.commit_all(repo, "m", secrets=[self.KEY]).paths, [name])


KEY_BYTES = CommitScanTests.KEY.encode()


class EvidenceTests(unittest.TestCase):
    """§3.7: check and receipt are code-only kinds with an explicit check; code closes an item with them only when
    the check is ok."""

    def ledger(self) -> tracker.Ledger:
        scope = tracker.job_scope("r", "001-x")
        ledger = tracker.Ledger(tracker.empty_doc(scope), scope)
        ledger.add(tracker.FACTORY, "Prove it", kind="criterion", owner="builder", verifier=tracker.CODE,
                   status="open", blocking=True)
        return ledger

    def test_check_and_receipt_need_an_explicit_check(self) -> None:
        ledger = self.ledger()
        for kind in ("check", "receipt"):
            with self.subTest(kind):
                with self.assertRaisesRegex(tracker.ItemError, "explicit check"):
                    ledger.add_evidence(tracker.FACTORY, "I-1", [{"kind": kind, "ref": "verify@abc"}])
                with self.assertRaisesRegex(tracker.ItemError, "written only by code|recorded by the factory"):
                    ledger.change("builder", "I-1", "done", evidence=[{"kind": "command", "ref": "make test"},
                                                                      {"kind": kind, "ref": "x", "check": "ok"}])

    def test_code_closes_only_with_an_ok_check(self) -> None:
        ledger = self.ledger()
        with self.assertRaisesRegex(tracker.ItemError, "only with its receipt"):
            ledger.change(tracker.CODE, "I-1", "verified", evidence=[{"kind": "check", "ref": "v", "check": "failed"}])
        ledger.change(tracker.CODE, "I-1", "verified", evidence=[{"kind": "check", "ref": "v", "check": "ok"}])
        self.assertEqual(ledger.get("I-1")["status"], "verified")

    def test_evidence_beside_an_item_keeps_its_status(self) -> None:
        ledger = self.ledger()
        item = ledger.add_evidence(tracker.FACTORY, "I-1", [{"kind": "receipt", "ref": "browser/x", "check": "ok"}])
        self.assertEqual(item["status"], "open")
        self.assertIsNone(ledger.add_evidence(tracker.FACTORY, "I-1",
                                              [{"kind": "receipt", "ref": "browser/x", "check": "ok"}]))
        with self.assertRaises(tracker.ItemError):
            ledger.add_evidence("builder", "I-1", [{"kind": "receipt", "ref": "y", "check": "ok"}])


@unittest.skipUnless(LIVE is None, LIVE or "")
class LiveCodexTests(unittest.TestCase):
    """T13, T15 and T16 with the real pinned Codex 0.163.0 against the Responses stub."""

    def rig(self, script: list, **kwargs) -> R.Rig:
        return R.Rig(self, binary=R.real_codex(), script=script, **kwargs)

    def test_t13_codex_fixes_the_fixture_app_under_its_own_sandbox(self) -> None:
        rig = self.rig([{"cmd": R.FIX}, {"text": "Fixed the greeting."}])
        job = rig.run_job()
        self.assertEqual(job["stage"], "merged", job.get("reason"))
        [receipt] = rig.receipts(job)
        self.assertEqual(receipt["run_outcome"], "completed")
        self.assertEqual(receipt["codex"]["version"], "0.163.0")
        self.assertTrue(receipt["codex"]["home"]["verified"])
        self.assertEqual(receipt["commit"]["by"], "harness")
        self.assertEqual(receipt["checks"][-1]["check"], "ok")
        self.assertEqual(receipt["checks"][-1]["commit"], receipt["commit"]["sha"])
        self.assertEqual(rig.git("log", "-1", "--format=%an", receipt["commit"]["sha"]).strip(), "Software Factory")
        # what reached the model: the harness note, and the command's output (the fix ran under Codex's sandbox)
        posts = rig.stub.posts()
        self.assertTrue(posts and all(path == "/v1/responses" for _, path, _, _ in posts))
        self.assertIn("No network in this run", json.dumps(posts[1][3]["input"]))   # posts[0]: the Responses check
        self.assertIn("Process exited with code 0", rig.stub.tool_outputs()[0])
        [criterion] = [i for i in rig.items(job) if i["kind"] == "criterion"]
        self.assertEqual([e["check"] for e in criterion["evidence"] if e["kind"] == "check"], ["ok"])

    def test_t15_stop_mid_turn_is_cancelled_by_sigint(self) -> None:
        rig = self.rig([{"cmd": "echo 'half a fix' >> app.py"}, {"hang": 60}])
        factory = rig.start()
        job = factory.submit(R.REPO, "Fix the greeting")
        self.assertTrue(wait_for(lambda: rig.stub.number() >= 2, 60), "the second model request is in flight")
        started = time.monotonic()
        factory.cancel(R.REPO, job["id"])
        job = factory.wait(R.REPO, job["id"], timeout=60)
        self.assertLess(time.monotonic() - started, 5.0)
        [receipt] = rig.receipts(job)
        self.assertEqual(receipt["run_outcome"], "cancelled")
        self.assertEqual(receipt["codex"]["exit"], 1, "exec answers SIGINT with exit 1 (SIGTERM would be -15)")
        self.assertEqual(receipt["reconciled"]["paths"], ["app.py"])
        self.assertIsNone(receipt["commit"])
        self.assertFalse(codex_pids(rig.binary, rig.tmp))

    def test_t15_the_wall_clock_is_timed_out(self) -> None:
        rig = self.rig([{"hang": 60}], sections={"limits": {"max_wall_seconds": 4}})
        job = rig.run_job()
        self.assertEqual(job["stage"], "failed")
        [receipt] = rig.receipts(job)
        self.assertEqual((receipt["run_outcome"], receipt["codex"]["exit"]), ("timed_out", 1))

    def test_t16_a_harness_restart_mid_run_is_unknown(self) -> None:
        """The harness is killed mid-turn: Codex dies with it (the launcher's parent-death signal), and the next start
        records the run unknown, reconciled, never sent again."""
        rig = self.rig([{"cmd": "echo 'half a fix' >> app.py"}, {"hang": 60}])
        proc, job_file = harness_process(self, rig)
        self.assertTrue(wait_for(lambda: rig.stub.number() >= 2 and job_mid_run(job_file())
                                 and codex_pids(rig.binary, rig.tmp), 90), "the second model request is in flight")
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(10)
        self.assertTrue(wait_for(lambda: not codex_pids(rig.binary, rig.tmp), 5), "Codex died with the harness")
        job_id = job_file().parent.name
        job = rig.start().wait(R.REPO, job_id, timeout=60)
        self.assertEqual((job["stage"], job["reason"]), ("failed", "interrupted (server restarted)"))
        [receipt] = rig.receipts(job)
        self.assertEqual(receipt["run_outcome"], "unknown")
        self.assertEqual(receipt["reconciled"]["paths"], ["app.py"])
        self.assertEqual(rig.stub.number(), 2, "nothing re-sent the prompt")

    def test_t16_kill_9_of_codex_is_unknown(self) -> None:
        rig = self.rig([{"cmd": "echo 'half a fix' >> app.py"}, {"hang": 60}])
        factory = rig.start()
        job = factory.submit(R.REPO, "Fix the greeting")
        self.assertTrue(wait_for(lambda: rig.stub.number() >= 2 and codex_pids(rig.binary, rig.tmp), 60))
        for pid in codex_pids(rig.binary, rig.tmp):
            os.kill(pid, signal.SIGKILL)
        job = factory.wait(R.REPO, job["id"], timeout=60)
        self.assertEqual(job["stage"], "failed")
        [receipt] = rig.receipts(job)
        self.assertEqual(receipt["run_outcome"], "unknown")
        self.assertEqual(receipt["reconciled"]["paths"], ["app.py"])
        self.assertEqual(rig.stub.number(), 2, "nothing re-sent the prompt")


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------- T14: "Verify in the browser"

def _escalate(ctx, call) -> None:
    (ctx.job_dir / "decision.md").write_text("ESCALATE\n\n## Why\nA person should look at the page.\n")


class _Chats:
    """Stands in for ChatManager where the factory opens the verify chat: records what it was asked."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def new_chat(self, repo, **kwargs) -> dict:
        self.calls.append(("new_chat", repo, kwargs))
        return {"id": "c-0000000001", "browser": []}

    def set_verify(self, chat_id: str, record: dict) -> dict:
        self.calls.append(("set_verify", chat_id, record))
        return {"id": chat_id, "verify": record}


@unittest.skipUnless(NO_BWRAP is None, NO_BWRAP or "")
class VerifyInBrowserTests(unittest.TestCase):
    """T14's harness side: the button is offered only while the worktree exists, the chat.json record and the grant
    built from it, and the browser receipt the core attaches to the job's criteria (never a merge gate)."""

    PREVIEW = {"command": ["python3", "app.py"], "port": 18938}

    def job_needing_you(self) -> tuple[R.Rig, dict]:
        rig = R.Rig(self, scenario=[[{"run": R.FIX}]], repo_extra={"preview": dict(self.PREVIEW)})
        rig.recorder.actions["approver"] = _escalate
        job = rig.run_job()
        self.assertEqual(job["stage"], "needs-human", job.get("reason"))
        return rig, job

    def test_the_button_writes_the_verify_record_and_the_grant_names_the_preview(self) -> None:
        from swfactory.browser import runs as browser_runs

        rig, job = self.job_needing_you()
        offer = rig.factory.job_receipts(R.REPO, job["id"])["preview"]
        self.assertEqual(offer, {"offered": True, "persona": "kate-browser", "port": 18938})
        chats = _Chats()
        rig.factory._chats = chats
        rig.factory.verify_in_browser(R.REPO, job["id"])
        self.assertEqual(chats.calls[0], ("new_chat", None, {"agent": "kate-browser"}))
        _, chat_id, record = chats.calls[1]
        self.assertEqual(record, {"repo": R.REPO, "job": job["id"], "branch": job["branch"],
                                  "preview": {"command": ["python3", "app.py"], "port": 18938, "startup_seconds": 20}})
        preview, origin = browser_runs.preview_of(rig.config, {**record, "preview": {"command": ["rm", "-rf", "/"]}})
        self.assertEqual(origin, "http://127.0.0.1:18938")
        self.assertEqual(preview["command"], ["python3", "app.py"], "the command comes from config, not the record")
        self.assertEqual(preview["dir"], job["worktree"])
        rig.factory._chats = None

    def test_not_offered_without_a_preview_or_a_worktree(self) -> None:
        rig = R.Rig(self, scenario=[[{"run": R.FIX}]])
        rig.recorder.actions["approver"] = _escalate
        job = rig.run_job()
        self.assertEqual(rig.factory.job_receipts(R.REPO, job["id"])["preview"]["offered"], False)
        with self.assertRaisesRegex(Exception, "not offered: the repository has no preview setting"):
            rig.factory.verify_in_browser(R.REPO, job["id"])
        merged = R.Rig(self, scenario=[[{"run": R.FIX}]], repo_extra={"preview": dict(self.PREVIEW)}).run_job()
        self.assertEqual(merged["stage"], "merged")

    def test_the_core_attaches_the_browser_receipt_to_the_criteria(self) -> None:
        rig, job = self.job_needing_you()
        matched = {"receipt": "browser/sessions/SL-1/receipt.json", "grant": "G-20261026-0001",
                   "rows": [{"kind": "action", "expect": {"text": "Hello, Kate", "matched": True}}]}
        decoy = {"receipt": "browser/sessions/SL-2/receipt.json", "grant": "G-20261026-0002",
                 "rows": [{"kind": "action", "expect": {"text": "Hello, Kate", "matched": False}}]}
        self.assertEqual(rig.factory.attach_browser_receipt(R.REPO, job["id"], matched), ["I-1"])
        self.assertEqual(rig.factory.attach_browser_receipt(R.REPO, job["id"], decoy), ["I-1"])
        [criterion] = [i for i in rig.items(rig.factory.job(R.REPO, job["id"])) if i["kind"] == "criterion"]
        receipts = [(e["ref"], e["check"]) for e in criterion["evidence"] if e["kind"] == "receipt"]
        self.assertEqual(receipts, [("browser/sessions/SL-1/receipt.json", "ok"),
                                    ("browser/sessions/SL-2/receipt.json", "failed")])
        self.assertEqual(rig.factory.job(R.REPO, job["id"])["stage"], "needs-human", "never a merge gate in BC1")

    def test_the_preview_runs_from_the_worktree_in_its_sibling_namespace(self) -> None:
        """§2.9: the job's preview in a sibling bwrap of its own, reached over a Unix socket, the worktree
        read-only; the fixed app answers the form with the submitted name."""
        import socket as socket_mod
        import tempfile as tempfile_mod

        from swfactory.browser import namespace as NS

        rig, job = self.job_needing_you()
        folder = Path(tempfile_mod.mkdtemp(prefix="sf-preview-"))
        self.addCleanup(__import__("shutil").rmtree, folder, ignore_errors=True)
        preview, _ = __import__("swfactory.browser.runs", fromlist=["preview_of"]).preview_of(
            rig.config, {"repo": R.REPO, "job": job["id"]})
        started = NS.start_preview({"preview": preview}, folder)
        if started is None:
            self.skipTest("no sibling namespace starts here (the doctor's browser preview sibling line); Verify in "
                          "the browser then shows the verify result without a live preview")
        proc, path = started
        self.addCleanup(lambda: (proc.kill(), proc.wait(10)))
        with socket_mod.socket(socket_mod.AF_UNIX, socket_mod.SOCK_STREAM) as client:
            client.settimeout(10)
            client.connect(path)
            body = b"name=Kate"
            client.sendall(b"POST /greet HTTP/1.0\r\nHost: 127.0.0.1\r\nContent-Type: application/x-www-form-urlencoded"
                           b"\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
            answer = b""
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    break
                answer += chunk
        self.assertTrue(answer.endswith(b"\r\n\r\nHello, Kate"), answer[-200:])
        self.assertFalse((Path(job["worktree"]) / "__pycache__").exists(), "the preview wrote nothing")


class VerifyChatRouteTests(unittest.TestCase):
    """§3.3 for "Verify in the browser": the page is the preview of what Codex wrote, so on a repository not marked
    personal_data: false the chat's model stays on this machine, where it was routed (set_verify) and where each
    turn was placed (an overflow can be hosted)."""

    RECORD = {"repo": R.REPO, "job": "001-fix-greeting", "branch": "factory/001-fix-greeting",
              "preview": {"command": ["python3", "app.py"], "port": 18938}}

    def browsing_chat(self, manager: "real_chats.ChatManager", model: str) -> dict:
        """A chat as a browsing agent's would be (chat.json's browser list), on model."""
        chat = manager.new_chat(None, model=model, agent="coder")
        path = Path(manager.root) / chat["id"] / "chat.json"
        path.write_text(json.dumps({**json.loads(path.read_text()), "browser": []}))
        return chat

    def test_set_verify_refuses_a_route_that_leaves_this_machine(self) -> None:
        rig = R.Rig(self, verify=None, sections={"models": {"spill": {"provider": "strata", "model": R.STUB_MODEL,
                                                                      "overflow": "opus"}}})
        manager = chat_manager(rig)
        for model, why in (("opus", "it runs on api.deepseek.com"), ("spill", "it runs on api.deepseek.com")):
            with self.subTest(model=model):
                chat = self.browsing_chat(manager, model)
                with self.assertRaises(ValueError) as caught:
                    manager.set_verify(chat["id"], self.RECORD)
                self.assertIn("Verify in the browser reads the preview of fixture-app", str(caught.exception))
                self.assertIn(why, str(caught.exception))
                self.assertNotIn("verify", manager.chat(chat["id"]))
        chat = self.browsing_chat(manager, "local")
        self.assertEqual(manager.set_verify(chat["id"], self.RECORD)["verify"]["repo"], R.REPO)
        free = R.Rig(self, verify=None, repo_extra={"personal_data": False})
        other = chat_manager(free)
        chat = self.browsing_chat(other, "opus")
        self.assertEqual(other.set_verify(chat["id"], self.RECORD)["verify"]["repo"], R.REPO)

    def test_a_turn_placed_off_this_machine_is_refused_before_it_runs(self) -> None:
        rig = R.Rig(self, verify=None)
        manager = chat_manager(rig)
        for model, refused in (("opus", True), ("local", False)):
            with self.subTest(model=model):
                chat = manager.new_chat(None, model=model, agent="coder")
                path = Path(manager.root) / chat["id"] / "chat.json"   # the record a set_verify of before the fix left
                path.write_text(json.dumps({**json.loads(path.read_text()), "verify": dict(self.RECORD)}))
                calls = len(rig.recorder.of("coder"))
                manager.chat_send(chat["id"], "Verify the greeting.")
                done = turn_done(manager, chat["id"])
                if refused:
                    self.assertEqual(done["status"], "error")
                    self.assertIn("runs only on a model on this machine", done["error"])
                    self.assertEqual(len(rig.recorder.of("coder")), calls, "the model never saw the turn")
                else:
                    self.assertEqual((done["status"], done["error"]), ("idle", None))
                    self.assertEqual(len(rig.recorder.of("coder")), calls + 1)

    def test_verify_in_browser_leaves_no_chat_when_it_is_refused(self) -> None:
        class Refusing(_Chats):
            def set_verify(self, chat_id: str, record: dict) -> dict:
                raise ValueError("Verify in the browser reads the preview of fixture-app, and it runs on elsewhere")

            def chat_delete(self, chat_id: str) -> None:
                self.calls.append(("chat_delete", chat_id))

        rig = R.Rig(self, scenario=[[{"run": R.FIX}]], verify=None, repo_extra={"preview": dict(self.RECORD["preview"])})
        rig.recorder.actions["approver"] = _escalate
        job = rig.run_job()
        rig.factory._chats = chats = Refusing()
        with self.assertRaisesRegex(ValueError, "runs on elsewhere"):
            rig.factory.verify_in_browser(R.REPO, job["id"])
        self.assertEqual(chats.calls[-1], ("chat_delete", "c-0000000001"))
        rig.factory._chats = None


def _browser_live_reason() -> str | None:
    if NO_BWRAP:
        return NO_BWRAP
    from tests import browser_rig

    return browser_rig.live_reason()


BROWSER_LIVE = _browser_live_reason()


@unittest.skipUnless(BROWSER_LIVE is None, BROWSER_LIVE or "")
class VerifyInBrowserLiveTests(unittest.TestCase):
    """T14 live: Kate's scripted browsing turn through the mock Open WebUI, on a chat whose chat.json carries the
    job's verify record, with the real Playwright MCP and Chrome; the preview in its sibling namespace."""

    def run_turn(self, fixed: bool) -> tuple[dict, dict, list]:
        import re as re_mod

        from tests import browser_rig
        from tests.test_browser_harness import BrowsingChatCase

        case = self
        state: dict = {}

        class Turn(BrowsingChatCase):
            def runTest(self) -> None:   # pragma: no cover - used as a fixture only
                pass

        turn = Turn()
        turn.setUp()
        self.addCleanup(turn.doCleanups)
        worktree = turn.tmp / "data" / "worktrees" / R.REPO / "001-fix-greeting"
        worktree.mkdir(parents=True)
        for path in R.FIXTURE_APP.iterdir():
            if path.is_file():
                (worktree / path.name).write_text(path.read_text())
        if fixed:
            app = worktree / "app.py"
            app.write_text(app.read_text().replace('return "Hello, there"', 'return f"Hello, {name}"'))
        paths = browser_rig.runtime()
        config = turn.browser_config(repos=[{"name": R.REPO, "path": str(R.FIXTURE_APP),
                                             "preview": {"command": ["python3", "app.py"], "port": 18938}}],
                                     browser={"enabled": True, "runtime": dict(paths)})
        attached: list = []

        class Factory:
            def system_item(self, fields, dedupe=None):
                return {"ref": "system#S-1"}

            def attach_browser_receipt(self, repo, job_id, record):
                attached.append((repo, job_id, record))
                return ["I-1"]

        def reply(body: dict) -> dict:
            results = [m for m in body["messages"] if m.get("role") == "tool"]
            page = ""
            latest = None
            for message in results:
                try:
                    payload = json.loads(message["content"])
                except (ValueError, TypeError):
                    continue
                obs = payload.get("observation") if isinstance(payload.get("observation"), dict) else payload
                if isinstance(obs, dict) and obs.get("page"):
                    page, latest = str(obs["page"]), obs.get("observation")

            def ref(role: str, name: str | None = None) -> str:
                pattern = re_mod.escape(role) + (r' "' + re_mod.escape(name) + r'"' if name else "") + \
                    r"[^\n]*?\[ref=((?:f\d+)?e\d+)\]"
                found = re_mod.search(pattern, page)
                case.assertIsNotNone(found, page)
                return found.group(1)
            step = len(results)
            if step == 0:
                return {"tool_calls": [{"name": "mcp__browser__navigate", "arguments": {"url": "http://127.0.0.1:18938/"}}]}
            if step == 1:
                return {"tool_calls": [{"name": "mcp__browser__fill", "arguments": {
                    "observation": latest, "fields": [{"ref": ref("textbox", "Name"), "value": "Kate"}]}}]}
            if step == 2:
                return {"tool_calls": [{"name": "mcp__browser__click", "arguments": {
                    "observation": latest, "ref": ref("button", "Submit"),
                    "expect": {"text": "Hello, Kate", "ref": ref("status")}}}]}
            state["last"] = results[-1]["content"]
            return {"text": "I checked the greeting."}

        turn.owui.reply = reply
        manager = turn.manager(config, Factory())
        chat = manager.new_chat(None, persona="kate-browser")
        manager.set_verify(chat["id"], {"repo": R.REPO, "job": "001-fix-greeting", "branch": "factory/001",
                                        "preview": {"command": ["python3", "app.py"], "port": 18938}})
        manager.chat_send(chat["id"], "Verify the greeting as Kate.")
        from tests.test_personas import wait_for as wait_value

        done = wait_value(lambda: (lambda c: c if c["status"] != "running" else None)(manager.chat(chat["id"])),
                          timeout=120)
        self.assertEqual((done["status"], done["error"]), ("idle", None), done.get("error"))
        return done, state, attached

    def test_t14_kate_sees_hello_kate_on_the_jobs_preview(self) -> None:
        from swfactory.browser import session_locks as SL

        done, _, attached = self.run_turn(fixed=True)
        [record] = done["browser"]
        receipt = json.loads((Path(done["browser"][0]["receipt"]).is_absolute() and Path(record["receipt"])
                              or (Path(self._data(done)) / record["receipt"])).read_text())
        click = [a for a in receipt["actions"] if a["tool"] == "click"][0]
        self.assertEqual(click["expect"]["matched"], True)
        self.assertEqual(receipt["worker"]["playwright_mcp"], "0.0.83")
        grant = SL.read_grant(SL.grant_path(self._data(done), record["grant"]))
        self.assertEqual(grant["origins"]["read"], ["http://127.0.0.1:18938"])
        self.assertEqual(grant["preview"]["dir"].split("/")[-1], "001-fix-greeting")
        self.assertNotIn("fixture", grant["paths"])
        [(repo, job_id, sent)] = attached
        self.assertEqual((repo, job_id), (R.REPO, "001-fix-greeting"))
        self.assertTrue(any((row.get("expect") or {}).get("matched") for row in sent["rows"] if isinstance(row, dict)))

    def test_t14_the_decoy_outside_the_result_element_does_not_match(self) -> None:
        done, _, attached = self.run_turn(fixed=False)
        [(_, _, sent)] = attached
        expects = [row["expect"] for row in sent["rows"] if isinstance(row, dict) and row.get("expect")]
        self.assertEqual([e["matched"] for e in expects], [False])

    @staticmethod
    def _data(done: dict) -> Path:
        return Path(done["cwd"]).parents[2]


class RouteTests(unittest.TestCase):
    """The server's routes for the Codex worker (master token only: never an agent token's)."""

    def test_the_routes_and_their_handlers(self) -> None:
        from swfactory import server

        found = {}
        for method, pattern, handler in server._ROUTES:
            for path in ("/api/jobs/my-app/001-x/receipts", "/api/jobs/my-app/001-x/receipts/02-builder",
                         "/api/jobs/my-app/001-x/verify-in-browser", "/api/codex"):
                if pattern.fullmatch(path):
                    found[(method, path)] = handler.__name__
        self.assertEqual(found, {("GET", "/api/jobs/my-app/001-x/receipts"): "api_job_receipts",
                                 ("GET", "/api/jobs/my-app/001-x/receipts/02-builder"): "api_job_receipt",
                                 ("POST", "/api/jobs/my-app/001-x/verify-in-browser"): "api_verify_in_browser",
                                 ("GET", "/api/codex"): "api_codex"})
        self.assertFalse({"api_job_receipts", "api_job_receipt", "api_verify_in_browser", "api_codex"}
                         & set(getattr(server, "AGENT_TOKEN_ROUTES", set())))
        self.assertTrue(server._PARAM_RULES["jobrun"][0].fullmatch("02-builder"))
        self.assertFalse(server._PARAM_RULES["jobrun"][0].fullmatch("../x"))
