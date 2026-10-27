"""The browser adapter, live (BROWSER_AND_CODEX.md §4.6, "live"): bin/sf-browser, its bubblewrap namespace, the real
pinned Playwright MCP, Chrome and the fixture site. Skipped unless the harness's own runtime is installed
(SF_BROWSER_RUNTIME, or SF_BROWSER_NODE, SF_BROWSER_PLAYWRIGHT_MCP and SF_BROWSER_CHROME; tests/browser_rig.py) and
bubblewrap works, as on a fresh checkout. The unit tests run the same scenarios against the recordings."""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import tests.test_browser_shim as U
from swfactory.browser import namespace as NS
from swfactory.browser import runs
from swfactory.browser import session_locks as SL
from swfactory.browser.fixture import FIXTURE_ORIGIN, log_entries
from swfactory.harness.sandbox import MINIMAL_ETC
from tests import browser_rig as B

REASON = B.live_reason()


def descendants(pid: int) -> set[int]:
    """Every live process below pid (one /proc scan per level)."""
    children: dict[int, list[int]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            stat = Path(f"/proc/{entry}/stat").read_text()
        except OSError:
            continue
        fields = stat[stat.rfind(")") + 2:].split()
        if fields[0] in ("Z", "X"):
            continue
        children.setdefault(int(fields[1]), []).append(int(entry))
    found: set[int] = set()
    todo = [pid]
    while todo:
        for child in children.get(todo.pop(), []):
            if child not in found:
                found.add(child)
                todo.append(child)
    return found


def alive(pids: set[int]) -> set[int]:
    out = set()
    for pid in pids:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            continue
        if stat[stat.rfind(")") + 2:].split()[0] not in ("Z", "X"):
            out.add(pid)
    return out


def cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")[:200]
    except OSError:
        return "?"


def wait_gone(pids: set[int], seconds: float) -> set[int]:
    deadline = time.monotonic() + seconds
    while alive(pids) and time.monotonic() < deadline:
        time.sleep(0.1)
    return alive(pids)


class LiveCase(unittest.TestCase):
    def setUp(self) -> None:
        self.data = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.data, ignore_errors=True)

    def run_scenario(self, name: str):
        result, driver, folder = B.run_live(name, self.data, record=False)
        return result, driver, folder

    def fixture_log(self, folder: Path, kind: str) -> list[dict]:
        return log_entries(folder / SL.FIXTURE_LOG, kind)


@unittest.skipUnless(REASON is None, REASON or "")
class LiveSliceTests(LiveCase, U.SliceTests):
    """T1 to T6, T8 and T11's first half, live; the fixture's own log counts the submissions."""

    def test_open_read_fill_press_verify_screenshot(self) -> None:
        _, folder = self.check_open_read_fill_press_verify_screenshot()
        self.assertEqual(len(self.fixture_log(folder, "submission")), 2)   # the submit and the again: true one
        shot = folder / "artifacts" / "greeting.png"
        self.assertGreater(shot.stat().st_size, 1000)   # a real screenshot of the page

    def test_stale_refs_after_a_re_render(self) -> None:
        _, folder = self.check_stale_refs_after_a_re_render()
        self.assertEqual(len(self.fixture_log(folder, "submission")), 1)   # the old ref moved nothing

    def test_enter_in_an_unnamed_form_is_a_commit(self) -> None:
        _, folder = self.check_enter_in_an_unnamed_form_is_a_commit()
        self.assertEqual(len(self.fixture_log(folder, "submission")), 1)


@unittest.skipUnless(REASON is None, REASON or "")
class LiveEffectTests(LiveCase, U.EffectTests):
    def test_links_that_post_are_commits_and_hold_the_origin(self) -> None:
        _, folder = self.check_links_that_post_are_commits_and_hold_the_origin()
        self.assertEqual([e["which"] for e in self.fixture_log(folder, "effect")], ["link", "div"])   # Save held

    def test_a_slow_commit_is_unknown_held_and_never_repeated(self) -> None:
        _, folder = self.check_a_slow_commit_is_unknown_held_and_never_repeated()
        self.assertEqual(len(self.fixture_log(folder, "order")), 1)


@unittest.skipUnless(REASON is None, REASON or "")
class LiveTabAndUrlTests(LiveCase, U.TabAndUrlTests):
    pass


@unittest.skipUnless(REASON is None, REASON or "")
class LiveInjectionTests(LiveCase, U.InjectionTests):
    pass


@unittest.skipUnless(REASON is None, REASON or "")
class LiveLoginTests(LiveCase, U.LoginTests):
    def test_an_expired_login_is_login_required_without_the_page(self) -> None:
        _, folder = self.check_an_expired_login_is_login_required_without_the_page()
        self.assertEqual(len(self.fixture_log(folder, "login_redirect")), 1)


@unittest.skipUnless(REASON is None, REASON or "")
class LifecycleTests(LiveCase):
    """T7 live, T10 live and T20: Stop, competing runs, revoke, kill -9 and the process table."""

    def launch(self, grant: Path) -> B.McpClient:
        client = B.start_launcher(grant, self.data)
        self.addCleanup(client.close)
        return client

    def test_stop_during_a_wait_frees_everything(self) -> None:
        grant = B.make_grant(self.data)
        client = self.launch(grant)
        driver = B.Driver(client)
        driver("navigate", url=f"{FIXTURE_ORIGIN}/index.html")
        tree = descendants(client._proc.pid) | {client._proc.pid}
        driver.cancel = threading.Event()
        threading.Timer(1.0, driver.cancel.set).start()
        started = time.monotonic()
        step = driver("wait_for", seconds=15)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(step.status, "cancelled")
        folder = SL.session_of_grant(self.data, grant.stem)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not any(e.get("kind") == "cancelled"
                                                      for e in SL.ActionLog.read(folder / SL.ACTION_LOG)):
            time.sleep(0.1)
        self.assertTrue(any(e.get("kind") == "cancelled" for e in SL.ActionLog.read(folder / SL.ACTION_LOG)))
        client.close()
        self.assertEqual(wait_gone(tree, 5), set())
        lock = SL.take_session_lock(self.data, SL.read_grant(B.make_grant(self.data)))   # the session lock is free
        lock.close()

    def test_competing_runs_and_revoke(self) -> None:
        first_grant = B.make_grant(self.data, kind="chat", ref="chat:first",
                                   limits={"commit_seconds": 20}, settings={"shop_delay": 10})
        first = self.launch(first_grant)
        one = B.Driver(first)
        opened = one("navigate", url=f"{FIXTURE_ORIGIN}/shop.html").obs
        second = self.launch(B.make_grant(self.data, kind="job", ref="job:7"))
        busy = B.Driver(second)("observe")
        self.assertEqual(busy.payload, {"error": "session_lock_busy", "locked_by_kind": "chat"})
        self.assertFalse(one("observe").error)   # the first run's next call still works
        again = self.launch(first_grant)            # the same grant started twice
        self.assertEqual(B.Driver(again)("observe").code, "session_lock_busy")
        tree = descendants(first._proc.pid) | {first._proc.pid}
        runs.register(first_grant.stem, first)
        order: list = []
        clicker = threading.Thread(target=lambda: order.append(one("click", observation=one("observe").obs[
            "observation"], ref=one.ref(opened, "button", "Place order"))))
        clicker.start()
        folder = SL.session_of_grant(self.data, first_grant.stem)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not log_entries(folder / SL.FIXTURE_LOG, "order"):
            time.sleep(0.05)
        revoked = runs.revoke(self.data, first_grant.stem)
        self.assertTrue(revoked["closed"])
        clicker.join(15)
        self.assertEqual(wait_gone(tree, 8), set())
        [click] = [a for a in SL.ActionLog.actions(folder / SL.ACTION_LOG) if a["tool"] == "click"]
        self.assertEqual(click["outcome"], "unknown")   # the commit in flight is never sent again
        self.assertEqual(len(log_entries(folder / SL.FIXTURE_LOG, "order")), 1)

    def test_kill_9_of_the_harness_ends_the_namespace(self) -> None:
        grant = B.make_grant(self.data)
        code = ("import sys, os, time, json; sys.path.insert(0, sys.argv[1]); from tests import browser_rig as B;"
                "from pathlib import Path; c = B.start_launcher(Path(sys.argv[2]), Path(sys.argv[3]));"
                "d = B.Driver(c); d('navigate', url='http://127.0.0.1:18937/index.html');"
                "print(c._proc.pid, flush=True); time.sleep(120)")
        env = {key: value for key, value in os.environ.items()}
        harness = subprocess.Popen([sys.executable, "-c", code, str(B.ROOT), str(grant), str(self.data)],
                                   stdout=subprocess.PIPE, text=True, env=env)
        self.addCleanup(harness.kill)
        launcher = int(harness.stdout.readline().strip())
        tree = descendants(launcher) | {launcher}
        self.assertGreater(len(tree), 3)   # bwrap, the shim, Playwright MCP, Chrome, the fixture
        os.kill(harness.pid, signal.SIGKILL)
        harness.wait(5)
        harness.stdout.close()
        self.assertEqual(wait_gone(tree, 5), set())
        before = SL.read_number(SL.browser_root(self.data) / "locks" / "fixture.number")
        nxt = SL.take_session_lock(self.data, SL.read_grant(B.make_grant(self.data)))
        self.assertGreater(nxt.lock_number, before)
        nxt.close()

    def test_sigterm_sigkill_and_eof_leave_nothing(self) -> None:
        for how in ("eof", "sigterm", "sigkill"):
            grant = B.make_grant(self.data)
            client = self.launch(grant)
            driver = B.Driver(client)
            obs = driver("navigate", url=f"{FIXTURE_ORIGIN}/index.html").obs
            tree = descendants(client._proc.pid) | {client._proc.pid}
            # the shim's own Python process (bwrap's command line names the shim too)
            shim = next(pid for pid in tree if b"swfactory.browser.shim" in Path(f"/proc/{pid}/cmdline").read_bytes()
                        and not Path(f"/proc/{pid}/cmdline").read_bytes().startswith(b"bwrap"))
            if how == "eof":
                client._outbox.put(None)   # the writer closes the shim's stdin
            else:
                os.kill(shim, signal.SIGTERM if how == "sigterm" else signal.SIGKILL)
            left = wait_gone(tree, 8)
            self.assertEqual(left, set(), f"{how}: {[cmdline(pid) for pid in left]}")
            client.close()
            folder = SL.session_of_grant(self.data, grant.stem)
            self.assertTrue(obs["observation"])
            SL.take_session_lock(self.data, SL.read_grant(B.make_grant(self.data))).close()
            self.assertEqual(SL.ActionLog.dangling(folder / SL.ACTION_LOG), [])


@unittest.skipUnless(REASON is None, REASON or "")
class NamespaceTests(LiveCase):
    """§2.9: nothing in or out, and the exact list of binds (Chrome starts with exactly these in every live test)."""

    def test_the_binds_and_no_network(self) -> None:
        grant = B.make_grant(self.data)
        lock = SL.take_session_lock(self.data, SL.read_grant(grant))
        self.addCleanup(lock.close)
        session = {"session_lock": lock.session_lock, "lock_number": lock.lock_number, "folder": str(lock.folder),
                   "number_file": str(lock.number_file)}
        listener = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]
        probe = (
            "import os, socket, json\n"
            "out = {}\n"
            "for name, addr in (('host_loopback', ('127.0.0.1', %d)), ('tailnet', ('100.100.100.100', 80)),"
            " ('public', ('1.1.1.1', 80))):\n"
            "    s = socket.socket(); s.settimeout(2)\n"
            "    try:\n        s.connect(addr); out[name] = 'connected'\n"
            "    except OSError as exc:\n        out[name] = 'failed'\n"
            "out['root'] = sorted(os.listdir('/'))\n"
            "out['home'] = os.listdir(os.environ['HOME'])\n"
            "out['env'] = sorted(os.environ)\n"
            "out['data_dir'] = sorted(os.listdir(%r)) if os.path.isdir(%r) else None\n"
            "out['etc'] = sorted(os.listdir('/etc'))\n"
            "print(json.dumps(out))\n") % (port, str(self.data), str(self.data))
        argv = NS.namespace_argv(grant.resolve(), SL.read_grant(grant), session,
                                 inner=[os.path.realpath(sys.executable), "-I", "-c", probe])
        done = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        seen = json.loads(done.stdout)
        self.assertEqual((seen["host_loopback"], seen["tailnet"], seen["public"]), ("failed", "failed", "failed"))
        self.assertEqual(seen["home"], [])
        # exactly the explicit environment (bwrap's --chdir adds PWD): no proxy variable in any spelling
        self.assertEqual(seen["env"], ["HOME", "LANG", "PATH", "PWD", "TMPDIR"])
        self.assertEqual(seen["data_dir"], ["browser"])   # only the grant, the lock number and this session folder
        self.assertEqual(seen["etc"], sorted(os.path.basename(p) for p in MINIMAL_ETC if os.path.exists(p)))
        targets = [argv[i + 2] for i, arg in enumerate(argv) if arg in ("--ro-bind", "--bind")]
        writable = [argv[i + 2] for i, arg in enumerate(argv) if arg == "--bind"]
        self.assertEqual(writable, [str(lock.folder)])
        runtime = B.runtime()
        expected_ro = {str(grant.resolve()), str(lock.number_file), str(B.ROOT / "swfactory"),
                       os.path.realpath(runtime["node"]), str(Path(os.path.realpath(runtime["chrome"])).parent),
                       os.path.realpath(str(runs.FIXTURE_SITE))}
        self.assertTrue(expected_ro <= set(targets), expected_ro - set(targets))
        for forbidden in (str(self.data), os.path.expanduser("~"), "/root", "/home", "/etc", "/var", "/run", "/opt"):
            self.assertNotIn(forbidden, targets)


@unittest.skipUnless(REASON is None, REASON or "")
class ContractLiveTests(LiveCase):
    """The recordings cannot drift from the real Playwright MCP: each scenario runs live and its calls and
    answers must equal the recorded ones (SF_BROWSER_RECORD=1 rewrites them instead)."""

    def test_recorded_and_live_responses_agree(self) -> None:
        differences = []
        for name in B.SCENARIOS:
            data = Path(tempfile.mkdtemp(dir=self.data))
            _, _, folder = B.run_live(name, data, record=True)
            live = B.normalize_calls(folder / "playwright-calls.jsonl", folder)
            if os.environ.get("SF_BROWSER_RECORD") == "1":
                B.write_recording(name, live, {"scenario": name,
                                               "recorded_with": os.environ.get("SF_BROWSER_RECORDED_WITH", "")})
                continue
            recorded = B.recording_of(name)
            if B.comparable(live) != B.comparable(recorded):
                for index, (a, b) in enumerate(zip(B.comparable(live), B.comparable(recorded))):
                    if a != b:
                        differences.append(f"{name} call {index}: live {str(a)[:300]} != recorded {str(b)[:300]}")
                        break
                else:
                    differences.append(f"{name}: {len(live['calls'])} live calls, {len(recorded['calls'])} recorded")
            if [t["name"] for t in live["tools"]] != [t["name"] for t in recorded["tools"]]:
                differences.append(f"{name}: the tool list differs")
        self.assertEqual(differences, [])


if __name__ == "__main__":
    unittest.main()
