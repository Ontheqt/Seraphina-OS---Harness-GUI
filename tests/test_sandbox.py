"""Tests for swfactory.harness.sandbox: env scrubbing, timeouts, cancel, background cleanup, bwrap."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from swfactory.harness import sandbox as sandbox_mod
from swfactory.harness.sandbox import Sandbox, _OutputReader, bwrap_available, is_secret_name, scrub_env


def pid_alive(pid: int) -> bool:
    """True if pid exists and is not a zombie."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat[stat.rfind(")") + 2:].split()[0] not in ("Z", "X")


def proc_state(pid: int) -> str | None:
    """The /proc state letter of pid (Z for a zombie), or None if there is no such process."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    return stat[stat.rfind(")") + 2:].split()[0]


def wait_group_idle(pgid: int, seconds: float = 3.0) -> bool:
    """Wait until nothing but zombies is left in the process group."""
    deadline = time.monotonic() + seconds
    while sandbox_mod._live_groups([pgid]):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def wait_only_leader_left(pgid: int, seconds: float = 5.0) -> bool:
    """Wait until no process but the leader, zombies included, is in the group (orphans get reaped)."""
    deadline = time.monotonic() + seconds
    while True:
        others = []
        for entry in os.listdir("/proc"):
            if entry.isdigit() and int(entry) != pgid:
                try:
                    stat = Path(f"/proc/{entry}/stat").read_text()
                except OSError:
                    continue
                if stat[stat.rfind(")") + 2:].split()[2] == str(pgid):
                    others.append(entry)
        if not others:
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)


def host_pids_running(argv: list[str]) -> list[int]:
    """Host pids of the live processes whose command line is exactly argv."""
    wanted = "\0".join(argv).encode() + b"\0"
    found = []
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            try:
                cmdline = Path(f"/proc/{entry}/cmdline").read_bytes()
            except OSError:
                continue
            if cmdline == wanted and pid_alive(int(entry)):
                found.append(int(entry))
    return found


def wait_dead(pid: int, seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not pid_alive(pid):
            return True
        time.sleep(0.05)
    return not pid_alive(pid)


class SandboxTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sf-sandbox-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cwd = self.tmp / "work"
        self.cwd.mkdir()

    def sandbox(self, mode: str = "none", **kwargs) -> Sandbox:
        kwargs.setdefault("writable", [self.cwd])
        kwargs.setdefault("network", True)
        kwargs.setdefault("env", {})
        box = Sandbox(mode, self.cwd, **kwargs)
        self.addCleanup(box.cleanup)
        return box

    def first_pid(self, output: str) -> int:
        return int(output.split()[0])


class BasicRunTest(SandboxTestCase):
    def test_exit_code_and_combined_output(self) -> None:
        code, out, timed_out = self.sandbox().run("echo out; echo err >&2; exit 3", 10)
        self.assertEqual(code, 3)
        self.assertEqual(out, "out\nerr\n")
        self.assertFalse(timed_out)

    def test_success_and_cwd(self) -> None:
        code, out, timed_out = self.sandbox().run("pwd", 10)
        self.assertEqual((code, timed_out), (0, False))
        self.assertEqual(out.strip(), str(self.cwd))

    def test_stdin_is_closed(self) -> None:
        start = time.monotonic()
        code, out, _ = self.sandbox().run('read -r line; echo "[${line}] $?"', 10)
        self.assertLess(time.monotonic() - start, 5)
        self.assertEqual(out.strip(), "[] 1")
        self.assertEqual(code, 0)

    def test_runs_in_new_session(self) -> None:
        code, out, _ = self.sandbox().run("ps -o sid= -p $$; echo $$", 10)
        sid, pid = out.split()
        self.assertEqual(sid, pid, "bash should lead its own session (start_new_session=True)")

    def test_unknown_mode_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Sandbox("docker", self.cwd, [], True, {})

    def test_missing_cwd_raises(self) -> None:
        box = Sandbox("none", self.tmp / "missing", [], True, {})
        with self.assertRaises(OSError):
            box.run("true", 5)

    def test_invalid_utf8_output_is_replaced(self) -> None:
        code, out, _ = self.sandbox().run(r"printf 'a\377b'", 10)
        self.assertEqual(code, 0)
        self.assertEqual(out, "a�b")


class EnvironmentTest(SandboxTestCase):
    SECRET_ENV = {
        "FOO_API_KEY": "k1", "GH_TOKEN": "k2", "MY_SECRET": "k3", "ANTHROPIC_BASE_URL": "k4",
        "OPENAI_ORG": "k5", "DEEPSEEK_X": "k6", "FACTORY_CONFIG": "k7", "github_token": "k8",
        "STRATA_KEY_VALUE": "k9",
    }

    def test_secrets_scrubbed_and_extras_added(self) -> None:
        with mock.patch.dict(os.environ, {**self.SECRET_ENV, "SAFE_VAR": "visible"}):
            box = self.sandbox(env={"EXTRA_SETTING": "yes", "PORT": "5130"}, secret_names=["STRATA_KEY_VALUE"])
            code, out, _ = box.run("env", 10)
        self.assertEqual(code, 0)
        env = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        for name in self.SECRET_ENV:
            self.assertNotIn(name, env)
        self.assertEqual(env["SAFE_VAR"], "visible")
        self.assertEqual(env["EXTRA_SETTING"], "yes")
        self.assertEqual(env["PORT"], "5130")
        for name, value in {"CI": "1", "GIT_TERMINAL_PROMPT": "0", "PAGER": "cat", "GIT_PAGER": "cat"}.items():
            self.assertEqual(env[name], value)

    def test_scrub_env_helper(self) -> None:
        source = {"PATH": "/bin", "X_API_KEY": "s", "HF_TOKEN": "s", "APP_SECRET": "s", "NAMED": "s",
                  "OPENAI_BASE": "s", "HOME": "/root"}
        self.assertEqual(scrub_env(source, {"NAMED"}), {"PATH": "/bin", "HOME": "/root"})
        self.assertTrue(is_secret_name("nanogpt_api_key"))
        self.assertFalse(is_secret_name("TOKENIZER_PATH"))

    def test_common_credential_names_are_scrubbed(self) -> None:
        """Not only *_API_KEY/*_TOKEN/*_SECRET: cloud, payment and database credentials too."""
        credentials = ["AWS_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID", "AWS_SESSION_TOKEN", "STRIPE_SECRET_KEY",
                       "DATABASE_PASSWORD", "PGPASSWORD", "MYSQL_PWD", "GOOGLE_APPLICATION_CREDENTIALS",
                       "AZURE_CLIENT_SECRET", "AZURE_STORAGE_CONNECTION_STRING", "SECRET_KEY_BASE",
                       "ENCRYPTION_KEY", "SSH_PRIVATE_KEY", "BASIC_AUTH", "REDIS_PASSWD", "GPG_PASSPHRASE",
                       "SENDGRID_APIKEY", "npm_config__authToken", "CREDENTIALS_DIRECTORY", "db_password"]
        plain = ["PATH", "HOME", "PWD", "OLDPWD", "TOKENIZER_PATH", "MAX_THINKING_TOKENS", "GIT_AUTHOR_NAME",
                 "SSH_AUTH_SOCK", "GIT_CONFIG_KEY_0", "KEYBOARD_LAYOUT", "AUTHOR"]
        for name in credentials:
            self.assertTrue(is_secret_name(name), name)
        for name in plain:
            self.assertFalse(is_secret_name(name), name)
        source = {name: "value-0123456789" for name in credentials + plain}
        self.assertEqual(sorted(scrub_env(source)), sorted(plain))
        with mock.patch.dict(os.environ, {name: "value-0123456789" for name in credentials}):
            code, out, _ = self.sandbox().run("env", 10)
        self.assertEqual(code, 0)
        seen = {line.split("=", 1)[0] for line in out.splitlines() if "=" in line}
        self.assertFalse(seen & set(credentials), seen & set(credentials))

    def test_environment_does_not_mutate_os_environ(self) -> None:
        with mock.patch.dict(os.environ, {"ZZ_API_KEY": "k"}):
            self.sandbox().environment()
            self.assertEqual(os.environ["ZZ_API_KEY"], "k")


class TimeoutAndCancelTest(SandboxTestCase):
    def test_timeout_kills_process_group(self) -> None:
        start = time.monotonic()
        code, out, timed_out = self.sandbox().run("sleep 30 & echo $!; sleep 30", 0.5)
        self.assertLess(time.monotonic() - start, 5)
        self.assertIsNone(code)
        self.assertTrue(timed_out)
        self.assertTrue(wait_dead(self.first_pid(out)), "background child of a timed-out command survived")

    def test_timeout_escalates_to_sigkill(self) -> None:
        with mock.patch.object(sandbox_mod, "KILL_GRACE_SECONDS", 0.3):
            start = time.monotonic()
            code, out, timed_out = self.sandbox().run("trap '' TERM; echo $$; sleep 30", 0.3)
        self.assertLess(time.monotonic() - start, 4)
        self.assertEqual((code, timed_out), (None, True))
        self.assertTrue(wait_dead(self.first_pid(out)))

    def test_cancel_event_stops_command(self) -> None:
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        start = time.monotonic()
        code, out, timed_out = self.sandbox().run("echo started; sleep 30", 30, cancel)
        self.assertLess(time.monotonic() - start, 5)
        self.assertIsNone(code)
        self.assertFalse(timed_out)
        self.assertEqual(out, "started\n")

    def test_no_timeout_when_zero_or_none(self) -> None:
        box = self.sandbox()
        self.assertEqual(box.run("echo ok", None)[:2], (0, "ok\n"))
        self.assertEqual(box.run("echo ok", 0)[:2], (0, "ok\n"))


class BackgroundCleanupTest(SandboxTestCase):
    def test_background_sleep_returns_quickly_and_cleanup_kills_it(self) -> None:
        box = self.sandbox()
        start = time.monotonic()
        code, out, timed_out = box.run("sleep 30 & echo $!", 20)
        self.assertLess(time.monotonic() - start, 3, "a background child holding the pipe blocked the call")
        self.assertEqual((code, timed_out), (0, False))
        pid = self.first_pid(out)
        self.assertTrue(pid_alive(pid))
        box.cleanup()
        self.assertTrue(wait_dead(pid))

    def test_background_http_server_is_killed_by_cleanup(self) -> None:
        box = self.sandbox()
        start = time.monotonic()
        code, out, _ = box.run("python3 -m http.server 0 --bind 127.0.0.1 & echo $!", 20)
        self.assertLess(time.monotonic() - start, 3)
        self.assertEqual(code, 0)
        pid = self.first_pid(out)
        self.assertTrue(pid_alive(pid))
        box.cleanup()
        self.assertTrue(wait_dead(pid))

    def test_nohup_background_is_killed_by_cleanup(self) -> None:
        box = self.sandbox()
        code, out, _ = box.run("nohup sleep 30 >/dev/null 2>&1 & echo $!", 20)
        self.assertEqual(code, 0)
        pid = self.first_pid(out)
        self.assertTrue(pid_alive(pid))
        box.cleanup()
        self.assertTrue(wait_dead(pid))

    def test_cleanup_kills_term_ignoring_background_process(self) -> None:
        box = self.sandbox()
        code, out, _ = box.run("(trap '' TERM; sleep 30) >/dev/null 2>&1 & echo $!", 20)
        pid = self.first_pid(out)
        with mock.patch.object(sandbox_mod, "KILL_GRACE_SECONDS", 0.3):
            box.cleanup()
        self.assertTrue(wait_dead(pid))

    def test_cleanup_is_idempotent_and_sandbox_reusable(self) -> None:
        box = self.sandbox()
        box.cleanup()
        box.run("true", 5)
        box.cleanup()
        box.cleanup()
        self.assertEqual(box.run("echo again", 5)[:2], (0, "again\n"))

    def test_finished_groups_are_forgotten(self) -> None:
        box = self.sandbox()
        box.run("echo done", 5)
        self.assertEqual(box._groups, {})
        self.assertEqual(box.run("sleep 30 & sleep 30", 0.3)[2], True)
        self.assertEqual(box._groups, {})


class PgidReuseTest(SandboxTestCase):
    """A remembered group id must never come to name an unrelated process group."""

    def test_leader_is_held_unreaped_until_its_group_is_empty(self) -> None:
        box = self.sandbox()
        code, out, _ = box.run("sleep 0.5 >/dev/null 2>&1 & echo $!", 10)
        self.assertEqual(code, 0)
        (pgid,) = box._groups
        leader = box._groups[pgid].proc
        self.assertEqual(proc_state(pgid), "Z", "bash's pid (the group id) must stay taken while the group lives")
        box.run("true", 5)
        self.assertIn(pgid, box._groups, "a group with a live member was forgotten")
        self.assertTrue(wait_dead(self.first_pid(out)))
        self.assertTrue(wait_group_idle(pgid))
        box.run("true", 5)
        self.assertEqual(box._groups, {})
        self.assertEqual(leader.returncode, 0)

    def test_cleanup_does_not_signal_groups_that_already_ended(self) -> None:
        box = self.sandbox()
        box.run("sleep 0.2 >/dev/null 2>&1 &", 10)
        (pgid,) = box._groups
        self.assertTrue(wait_group_idle(pgid))
        with mock.patch.object(sandbox_mod.os, "killpg", wraps=os.killpg) as killpg:
            box.cleanup()
        self.assertEqual(killpg.call_args_list, [])
        self.assertEqual(box._groups, {})

    @unittest.skipUnless(os.access("/proc/sys/kernel/ns_last_pid", os.W_OK), "needs root to steer pid allocation")
    def test_unrelated_process_never_receives_a_remembered_group_id(self) -> None:
        box = self.sandbox()
        box.run("sleep 0.2 >/dev/null 2>&1 &", 10)
        (pgid,) = box._groups
        if not wait_only_leader_left(pgid):
            self.skipTest("this container's init does not reap orphaned zombies promptly")
        Path("/proc/sys/kernel/ns_last_pid").write_text(str(pgid - 1))   # the next new pid would be pgid
        victim = subprocess.Popen(["sleep", "30"], start_new_session=True)
        self.addCleanup(victim.wait)
        self.addCleanup(victim.kill)
        self.assertNotEqual(victim.pid, pgid)
        box.cleanup()
        time.sleep(0.1)
        self.assertIsNone(victim.poll(), "cleanup() signalled a process group the sandbox did not start")

    def test_without_proc_every_existing_group_counts_as_alive(self) -> None:
        box = self.sandbox()
        code, out, _ = box.run("sleep 0.2 >/dev/null 2>&1 &", 10)
        (pgid,) = box._groups
        self.assertTrue(wait_group_idle(pgid))
        self.assertEqual(sandbox_mod._live_groups([pgid]), set())
        with mock.patch.object(sandbox_mod.os, "listdir", side_effect=PermissionError("no /proc")):
            self.assertEqual(sandbox_mod._live_groups([pgid]), {pgid}, "the zombie leader still holds the group")
            box.run("true", 5)
            self.assertIn(pgid, box._groups, "a group was released although liveness was unknown")
        box.run("true", 5)
        self.assertEqual(box._groups, {})

    def test_exit_status_without_reaping(self) -> None:
        proc = subprocess.Popen(["bash", "-c", "exit 7"])
        self.addCleanup(proc.wait)
        self.assertEqual(sandbox_mod._exit_status(proc, block=True), 7)
        self.assertEqual(proc_state(proc.pid), "Z")
        self.assertIsNone(proc.returncode)
        killed = subprocess.Popen(["sleep", "30"])
        self.addCleanup(killed.wait)
        self.assertIsNone(sandbox_mod._exit_status(killed))
        killed.kill()
        self.assertEqual(sandbox_mod._exit_status(killed, block=True), -9)


class OutputCapTest(SandboxTestCase):
    def test_reader_keeps_head_and_tail(self) -> None:
        read_fd, write_fd = os.pipe()
        reader = _OutputReader(read_fd, cap_bytes=20)
        reader.start()
        os.write(write_fd, b"0123456789" + b"x" * 100 + b"abcdefghij")
        os.close(write_fd)
        self.assertTrue(reader.eof.wait(5))
        text = reader.stop_capture()
        self.assertTrue(text.startswith("0123456789"))
        self.assertTrue(text.endswith("abcdefghij"))
        self.assertIn("[100 bytes of output dropped]", text)

    def test_huge_output_is_capped_at_ten_megabytes(self) -> None:
        code, out, _ = self.sandbox().run("head -c 12000000 /dev/zero | tr '\\0' a; echo END", 60)
        self.assertEqual(code, 0)
        self.assertLess(len(out), sandbox_mod.OUTPUT_CAP_BYTES + 200)
        self.assertIn("bytes of output dropped", out)
        self.assertTrue(out.endswith("aEND\n"))


class BwrapArgvTest(SandboxTestCase):
    def test_argv_without_network(self) -> None:
        git_dir = self.tmp / "repo.git"
        git_dir.mkdir()
        box = Sandbox("bwrap", self.cwd, [self.cwd, git_dir, self.tmp / "not-yet"], False, {})
        argv = box.argv("make test")
        self.assertEqual(argv[:11], ["bwrap", "--unshare-pid", "--ro-bind", "/", "/", "--dev", "/dev",
                                     "--proc", "/proc", "--tmpfs", "/tmp"])
        self.assertNotIn("--die-with-parent", argv)   # bwrap's PID 1 would kill background children
        self.assertIn(["--bind", str(self.cwd), str(self.cwd)], [argv[i:i + 3] for i in range(len(argv))])
        self.assertIn(["--bind", str(git_dir), str(git_dir)], [argv[i:i + 3] for i in range(len(argv))])
        self.assertNotIn(str(self.tmp / "not-yet"), argv)
        self.assertIn("--unshare-net", argv)
        self.assertEqual(argv[-5:], ["--chdir", str(self.cwd), "bash", "-c", "make test"])

    def test_argv_with_network_and_plain_mode(self) -> None:
        self.assertNotIn("--unshare-net", Sandbox("bwrap", self.cwd, [], True, {}).argv("ls"))
        self.assertEqual(Sandbox("none", self.cwd, [], False, {}).argv("ls"), ["bash", "-c", "ls"])

    def test_wrap_runs_any_argv_the_way_a_command_runs(self) -> None:
        server_dir = self.tmp / "server"
        server_dir.mkdir()
        box = Sandbox("bwrap", self.cwd, [self.cwd], False, {})
        wrapped = box.wrap(["rea-mcp", "--flag"], server_dir)
        self.assertEqual(wrapped[:-4], box.argv("x")[:-5], "the same bwrap options and binds as Bash")
        self.assertEqual(wrapped[-4:], ["--chdir", str(server_dir), "rea-mcp", "--flag"])
        self.assertEqual(box.wrap(["srv"])[-3:], ["--chdir", str(self.cwd), "srv"], "cwd defaults to the sandbox's")
        self.assertEqual(Sandbox("none", self.cwd, [], False, {}).wrap(["srv", "a b"], server_dir), ["srv", "a b"])

    def test_run_refuses_when_bwrap_unusable(self) -> None:
        box = Sandbox("bwrap", self.cwd, [self.cwd], True, {})
        with mock.patch.object(sandbox_mod, "bwrap_available", return_value=False):
            with self.assertRaises(RuntimeError):
                box.run("true", 5)

    def test_bwrap_available_is_cached(self) -> None:
        bwrap_available.cache_clear()
        self.addCleanup(bwrap_available.cache_clear)
        done = subprocess.CompletedProcess([], 0, b"", b"")
        with mock.patch.object(sandbox_mod.subprocess, "run", return_value=done) as run:
            self.assertTrue(bwrap_available())
            self.assertTrue(bwrap_available())
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args[0][0], ["bwrap", "--unshare-pid", "--ro-bind", "/", "/", "--dev", "/dev",
                                               "--proc", "/proc", "true"])

    def test_bwrap_available_false_when_missing_or_failing(self) -> None:
        self.addCleanup(bwrap_available.cache_clear)
        for effect in (FileNotFoundError("bwrap"), subprocess.CompletedProcess([], 1, b"", b"no userns")):
            bwrap_available.cache_clear()
            kwargs = {"side_effect": effect} if isinstance(effect, Exception) else {"return_value": effect}
            with mock.patch.object(sandbox_mod.subprocess, "run", **kwargs):
                self.assertFalse(bwrap_available())


@unittest.skipUnless(bwrap_available(), "bubblewrap is not usable here")
class BwrapRunTest(SandboxTestCase):
    def test_writes_only_inside_writable_paths(self) -> None:
        other = self.tmp / "other"
        other.mkdir()
        box = self.sandbox("bwrap")
        code, _, _ = box.run("echo hi > inside.txt", 20)
        self.assertEqual(code, 0)
        self.assertEqual((self.cwd / "inside.txt").read_text(), "hi\n")
        code, _, _ = box.run(f"echo hi > {other}/outside.txt", 20)
        self.assertNotEqual(code, 0)
        self.assertFalse((other / "outside.txt").exists())
        self.addCleanup(lambda: Path("/usr/sf-bwrap-probe").unlink(missing_ok=True))
        code, out, _ = box.run("touch /usr/sf-bwrap-probe", 20)
        self.assertNotEqual(code, 0)
        self.assertFalse(Path("/usr/sf-bwrap-probe").exists())

    def test_tmp_is_private(self) -> None:
        marker = Path(tempfile.gettempdir()) / f"sf-bwrap-marker-{os.getpid()}"
        marker.write_text("host")
        self.addCleanup(marker.unlink)
        code, out, _ = self.sandbox("bwrap").run(f"test -e {marker} && echo visible || echo hidden", 20)
        self.assertEqual(out.strip(), "hidden")

    def test_network_flag(self) -> None:
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        self.addCleanup(server.close)
        port = server.getsockname()[1]
        probe = f"python3 -c \"import socket; socket.create_connection(('127.0.0.1', {port}), 2)\""
        self.assertEqual(self.sandbox("bwrap", network=True).run(probe, 20)[0], 0)
        self.assertNotEqual(self.sandbox("bwrap", network=False).run(probe, 20)[0], 0)

    def test_env_is_scrubbed_inside_bwrap(self) -> None:
        with mock.patch.dict(os.environ, {"LEAK_API_KEY": "secret-value"}):
            code, out, _ = self.sandbox("bwrap").run("echo ${LEAK_API_KEY:-unset} $CI", 20)
        self.assertEqual(out.strip(), "unset 1")

    def test_background_and_timeout_in_bwrap(self) -> None:
        box = self.sandbox("bwrap")
        start = time.monotonic()
        marker = f"30.{os.getpid()}{time.monotonic_ns() % 100000}"   # $! is a pid inside the sandbox's namespace
        code, out, _ = box.run(f"sleep {marker} & echo $!", 20)
        self.assertLess(time.monotonic() - start, 3)
        self.assertEqual(code, 0)
        pids = host_pids_running(["sleep", marker])
        self.assertEqual(len(pids), 1, "the background child should outlive the command until cleanup()")
        self.assertTrue(pid_alive(pids[0]))
        box.cleanup()
        self.assertTrue(wait_dead(pids[0]))
        code, _, timed_out = box.run("sleep 30", 0.5)
        self.assertEqual((code, timed_out), (None, True))

    def test_host_processes_are_out_of_reach(self) -> None:
        """--unshare-pid: the sandbox's /proc lists only its own processes, so /proc/<host pid>/root cannot
        reach the host's mount namespace, where paths bound read-only inside are writable (as root that
        wrote through to a repository's .git/config)."""
        protected = self.tmp / "protected"
        protected.mkdir()
        target = protected / "config"
        target.write_text("original\n")
        holder = subprocess.Popen(["sleep", "30"], cwd=protected)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        box = self.sandbox("bwrap")
        self.assertNotEqual(box.run(f"echo plain > {target}", 20)[0], 0)   # hidden or read-only inside
        for host_pid in (holder.pid, os.getpid()):
            code, out, _ = box.run(f"test -e /proc/{host_pid} && echo visible; "
                                   f"echo escaped > /proc/{host_pid}/root{target}", 20)
            self.assertNotEqual(code, 0, out)
            self.assertNotIn("visible", out)
            self.assertEqual(target.read_text(), "original\n")
        code, out, _ = box.run(f"for p in /proc/[0-9]*/root; do echo escaped > $p{target} 2>/dev/null && "
                               "echo wrote-via $p; done; ls /proc | grep -c '^[0-9]'", 20)
        self.assertEqual(code, 0, out)
        self.assertNotIn("wrote-via", out)
        self.assertLess(int(out.split()[-1]), 10, "the sandbox's /proc should list only its own processes")
        self.assertEqual(target.read_text(), "original\n")


if __name__ == "__main__":
    unittest.main()
