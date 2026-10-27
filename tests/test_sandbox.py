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
from typing import Any
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
                       "SENDGRID_APIKEY", "npm_config__authToken", "CREDENTIALS_DIRECTORY", "db_password",
                       "SSH_AUTH_SOCK", "GPG_AGENT_INFO"]   # H2: an agent socket hands its keys to every Bash
        plain = ["PATH", "HOME", "PWD", "OLDPWD", "TOKENIZER_PATH", "MAX_THINKING_TOKENS", "GIT_AUTHOR_NAME",
                 "GIT_CONFIG_KEY_0", "KEYBOARD_LAYOUT", "AUTHOR"]
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
        try:
            Path("/proc/sys/kernel/ns_last_pid").write_text(str(pgid - 1))   # the next new pid would be pgid
        except PermissionError:  # the file looks writable, but steering pids needs CAP_CHECKPOINT_RESTORE
            self.skipTest("needs root to steer pid allocation")
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
        with mock.patch.object(sandbox_mod, "setpriv_path", return_value="/usr/bin/setpriv"), \
                mock.patch.object(sandbox_mod, "no_new_privs_set", return_value=False):
            self.assertEqual(Sandbox("none", self.cwd, [], False, {}).argv("ls"),
                             ["/usr/bin/setpriv", "--no-new-privs", "bash", "-c", "ls"])
        with mock.patch.object(sandbox_mod, "setpriv_path", return_value=None):
            self.assertEqual(Sandbox("none", self.cwd, [], False, {}).argv("ls"), ["bash", "-c", "ls"])
        self.assertNotIn("setpriv", " ".join(Sandbox("bwrap", self.cwd, [], True, {}).argv("ls")),
                         "bwrap sets no-new-privileges itself")

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
                                               "--proc", "/proc", "--tmpfs", "/tmp", "--unshare-net", "true"],
                         "the form every command runs in, with the strictest option a run may add")

    def test_bwrap_available_false_when_missing_or_failing(self) -> None:
        self.addCleanup(bwrap_available.cache_clear)
        for effect in (FileNotFoundError("bwrap"), subprocess.CompletedProcess([], 1, b"", b"no userns")):
            bwrap_available.cache_clear()
            kwargs = {"side_effect": effect} if isinstance(effect, Exception) else {"return_value": effect}
            with mock.patch.object(sandbox_mod.subprocess, "run", **kwargs):
                self.assertFalse(bwrap_available())


class NestedAndPrivilegesTest(SandboxTestCase):
    """A sandbox inside the sandbox (refused where AppArmor restricts user namespaces), and no new privileges for
    what runs outside bwrap (DESIGN §12 sandbox.py, §17)."""

    NESTED_REFUSAL = (b"bwrap: No permissions to create a new namespace, likely because the kernel does not allow "
                      b"non-privileged user namespaces. See <https://deb.li/bubblewrap> or "
                      b"<file:///usr/share/doc/bubblewrap/README.Debian.gz>.\n")

    def test_nested_probe_runs_bwrap_inside_bwrap_and_keeps_its_words(self) -> None:
        sandbox_mod.nested_bwrap.cache_clear()
        self.addCleanup(sandbox_mod.nested_bwrap.cache_clear)
        refused = subprocess.CompletedProcess([], 1, b"", self.NESTED_REFUSAL)
        with mock.patch.object(sandbox_mod.subprocess, "run", return_value=refused) as run:
            works, why = sandbox_mod.nested_bwrap()
            self.assertEqual(sandbox_mod.nested_bwrap(), (works, why), "cached")
        self.assertFalse(works)
        self.assertTrue(why.startswith("bwrap: No permissions to create a new namespace"), why)
        self.assertEqual(run.call_count, 1)
        base = ["--unshare-pid", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc"]
        self.assertEqual(run.call_args[0][0], ["bwrap", *base, "bwrap", *base, "true"])
        sandbox_mod.nested_bwrap.cache_clear()
        with mock.patch.object(sandbox_mod.subprocess, "run", side_effect=FileNotFoundError("bwrap")):
            self.assertEqual(sandbox_mod.nested_bwrap(), (False, "bwrap could not be started (FileNotFoundError)"))
        sandbox_mod.nested_bwrap.cache_clear()
        with mock.patch.object(sandbox_mod.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, b"", b"")):
            self.assertEqual(sandbox_mod.nested_bwrap(), (True, ""))

    def test_no_new_privs_is_read_from_proc(self) -> None:
        status = self.tmp / "status"
        status.write_text("Name:\tpython3\nNoNewPrivs:\t1\nSeccomp:\t0\n")
        self.assertTrue(sandbox_mod.no_new_privs_set(str(status)))
        status.write_text("Name:\tpython3\nNoNewPrivs:\t0\n")
        self.assertFalse(sandbox_mod.no_new_privs_set(str(status)))
        self.assertIsNone(sandbox_mod.no_new_privs_set(str(self.tmp / "missing")))

    def test_without_new_privileges(self) -> None:
        tool = self.tmp / "bin" / "tool"
        tool.parent.mkdir()
        tool.write_text("#!/bin/sh\n")
        tool.chmod(0o755)
        with mock.patch.object(sandbox_mod, "setpriv_path", return_value="/usr/bin/setpriv"), \
                mock.patch.object(sandbox_mod, "no_new_privs_set", return_value=False):
            wrap = sandbox_mod.without_new_privileges
            self.assertEqual(wrap(["tool", "-x"], str(tool.parent)), ["/usr/bin/setpriv", "--no-new-privs", "tool", "-x"])
            self.assertEqual(wrap([str(tool)]), ["/usr/bin/setpriv", "--no-new-privs", str(tool)])
            self.assertEqual(wrap(["bin/tool"], None, self.tmp), ["/usr/bin/setpriv", "--no-new-privs", "bin/tool"],
                             "a relative path is found below cwd")
            self.assertEqual(wrap(["no-such-tool"], str(tool.parent)), ["no-such-tool"],
                             "a missing program is left to fail as it did")
            self.assertEqual(wrap([]), [])
        with mock.patch.object(sandbox_mod, "setpriv_path", return_value="/usr/bin/setpriv"), \
                mock.patch.object(sandbox_mod, "no_new_privs_set", return_value=True):
            self.assertEqual(sandbox_mod.without_new_privileges([str(tool)]), [str(tool)], "inherited already")
        with mock.patch.object(sandbox_mod, "setpriv_path", return_value=None), \
                mock.patch.object(sandbox_mod, "no_new_privs_set", return_value=False):
            self.assertEqual(sandbox_mod.without_new_privileges([str(tool)]), [str(tool)], "no setpriv here")

    @unittest.skipUnless(sandbox_mod.setpriv_path(), "setpriv (util-linux) is not installed here")
    def test_a_plain_command_runs_without_new_privileges(self) -> None:
        if sandbox_mod.no_new_privs_set():
            self.skipTest("this test process has no-new-privileges already")
        code, out, _ = self.sandbox("none").run("grep NoNewPrivs /proc/self/status", 10)
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"NoNewPrivs:\s+1", "a setuid sudo can no longer give the command root")

    @unittest.skipUnless(bwrap_available(), "bubblewrap is not usable here")
    def test_a_bwrap_command_runs_without_new_privileges(self) -> None:
        code, out, _ = self.sandbox("bwrap").run("grep NoNewPrivs /proc/self/status", 20)
        self.assertEqual(code, 0, out)
        self.assertRegex(out, r"NoNewPrivs:\s+1", "bwrap sets the flag itself")


class UnitFileTest(unittest.TestCase):
    """The shipped systemd units (DESIGN §17): the system unit leaves NoNewPrivileges off (it stops sudo-rs and
    classic sudo alike, so the root helpers could not run) and never hides the data disks from a helper; the user
    unit adds no sandboxing, which in a user unit would make every bwrap a sandbox inside a sandbox."""

    ROOT = Path(__file__).resolve().parents[1] / "systemd"

    def directives(self, name: str) -> dict[str, str]:
        """{directive: value} of a unit's uncommented lines."""
        lines = (self.ROOT / name).read_text().splitlines()
        return dict(line.strip().split("=", 1) for line in lines
                    if line.strip() and not line.lstrip().startswith("#") and "=" in line)

    def test_the_system_unit_lets_helpers_use_sudo(self) -> None:
        found = self.directives("factory@.service")
        for name in ("NoNewPrivileges", "TemporaryFileSystem", "InaccessiblePaths", "RestrictSUIDSGID"):
            self.assertNotIn(name, found)
        self.assertEqual(found.get("ProtectSystem"), "full", "never strict: the helper would lose /var/lib and /run")

    def test_the_user_unit_adds_no_sandboxing(self) -> None:
        found = self.directives("factory.service")
        for name in ("PrivateTmp", "ProtectSystem", "ProtectHome", "PrivateUsers", "NoNewPrivileges",
                     "PrivateDevices", "ProtectKernelTunables", "RestrictNamespaces"):
            self.assertNotIn(name, found)


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


def _outside_tmp() -> Path | None:
    """A scratch base outside /tmp (bwrap gives every command a private /tmp, so a marker there proves nothing)."""
    for base in (Path("/var/tmp"), Path.home() / ".cache"):
        try:
            base.mkdir(parents=True, exist_ok=True)
            return Path(tempfile.mkdtemp(prefix="sf-h1-", dir=base)).resolve()
        except OSError:
            continue
    return None


class MaskPlanTest(unittest.TestCase):
    """H1 (DESIGN §34): every masked directory is hidden whole; what the run needs below one is bound back."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="sf-mask-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def test_directories_are_hidden_whole_and_needed_paths_bound_back(self) -> None:
        config_dir, data = self.tmp / "config", self.tmp / "data"
        for path in (config_dir / "personas" / "repo", config_dir / "skills", data / "wakes", data / "notify",
                     self.tmp / "work"):
            path.mkdir(parents=True)
        (config_dir / "config.json").write_text("{}")
        (config_dir / "env").write_text("")
        terms = self.tmp / "terms.txt"
        terms.write_text("x")
        fallback = [config_dir / "config.json", config_dir / "env", config_dir / "personas"]
        dirs, files, rebind = sandbox_mod.mask_plan([config_dir, data / "wakes", data / "notify", self.tmp / "missing"],
                                                    [terms], [self.tmp / "work"], fallback)
        self.assertEqual(dirs, [config_dir, data / "wakes", data / "notify"])
        self.assertEqual((files, rebind), ([terms], []))
        dirs, files, rebind = sandbox_mod.mask_plan([config_dir], [config_dir / "config.json"],
                                                    [config_dir / "skills", config_dir / "personas" / "repo"],
                                                    fallback)
        self.assertEqual(dirs, [config_dir], "a needed path inside no longer keeps the directory visible")
        self.assertEqual(rebind, [config_dir / "skills"], "a sensitive path is never bound back")
        self.assertEqual(files, [], "no bind over a file that the harness replaces atomically")

    def test_a_needed_masked_directory_gets_its_other_entries_back_one_by_one(self) -> None:
        config_dir = self.tmp / "config"
        (config_dir / "skills").mkdir(parents=True)
        (config_dir / "notes.txt").write_text("fine")
        (config_dir / "config.json").write_text("{}")
        (config_dir / "env").write_text("")
        (config_dir / "link").symlink_to(config_dir / "config.json")
        dirs, files, rebind = sandbox_mod.mask_plan([config_dir], [], [config_dir],
                                                    [config_dir / "config.json", config_dir / "env"])
        self.assertEqual(dirs, [config_dir])
        self.assertEqual(rebind, [config_dir / "notes.txt", config_dir / "skills"], "never config.json, env or a link")
        self.assertEqual(files, [])

    def test_argv_binds_writable_then_hides_then_binds_back_then_masks_files(self) -> None:
        hidden, skills, work = self.tmp / "config", self.tmp / "config" / "skills", self.tmp / "work"
        inner = hidden / "data" / "jobs" / "1"
        for path in (skills, work, inner):
            path.mkdir(parents=True)
        secret = self.tmp / "terms.txt"
        secret.write_text("x")
        argv = Sandbox("bwrap", work, [work, inner], False, {}, masked=[hidden], masked_files=[secret],
                       readonly_rebind=[skills, self.tmp / "elsewhere"]).argv("ls")
        joined = " ".join(argv)
        self.assertIn(f"--tmpfs {hidden}", joined)
        self.assertIn(f"--ro-bind {skills} {skills}", joined)
        self.assertNotIn("elsewhere", joined, "only what is under a hidden directory is bound back")
        position = {value: argv.index(value) for value in argv}
        self.assertLess(position[str(work)], position[str(hidden)], "a writable path outside a mask comes first")
        self.assertLess(position[str(hidden)], position[str(inner)], "a writable path inside comes after its mask")
        self.assertEqual(argv[position[str(inner)] - 1], "--bind", "bound back writable")
        self.assertLess(position[str(inner)], position[str(secret)], "the file masks come last")
        self.assertEqual(argv[position[str(secret)] - 2:position[str(secret)]], ["--ro-bind", "/dev/null"])
        self.assertEqual(argv[-8:-6], ["--remount-ro", str(hidden)], "the empty tmpfs is read-only, last")

    def test_agent_sockets_at_their_usual_places_are_masked(self) -> None:
        """H2: removing SSH_AUTH_SOCK does not stop a Bash from connecting to the socket itself."""
        from swfactory.config import agent_socket_paths

        base = _outside_tmp()
        if base is None:
            self.skipTest("no writable directory outside /tmp")
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        runtime, home = base / "run", base / "home"
        (home / ".gnupg").mkdir(parents=True)
        (home / ".gnupg" / "S.gpg-agent").write_text("")
        dirs, files = agent_socket_paths({"XDG_RUNTIME_DIR": str(runtime), "GNUPGHOME": str(home / ".gnupg"),
                                          "SSH_AUTH_SOCK": str(base / "agent.sock")})
        self.assertEqual(dirs, [runtime / "gnupg", runtime / "keyring", runtime / "gcr"])
        self.assertIn(home / ".gnupg" / "S.gpg-agent", files)
        self.assertIn(runtime / "ssh-agent.socket", files)
        _dirs, files = agent_socket_paths({"XDG_RUNTIME_DIR": str(runtime), "SSH_AUTH_SOCK": "/tmp/ssh-x/agent.1"})
        self.assertNotIn(Path("/tmp/ssh-x/agent.1"), files, "/tmp is private inside the sandbox already")

    def test_signing_is_off_for_commits_inside_bwrap(self) -> None:
        with mock.patch.dict(os.environ, {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.pager",
                                          "GIT_CONFIG_VALUE_0": "cat"}):
            env = Sandbox("bwrap", self.tmp, [self.tmp], False, {}).environment()
            plain = Sandbox("none", self.tmp, [self.tmp], False, {}).environment()
        self.assertEqual(env["GIT_CONFIG_COUNT"], "3")
        self.assertEqual((env["GIT_CONFIG_KEY_0"], env["GIT_CONFIG_KEY_1"], env["GIT_CONFIG_VALUE_1"]),
                         ("core.pager", "commit.gpgsign", "false"), "appended to the entries already there")
        self.assertEqual(plain["GIT_CONFIG_COUNT"], "1")


class TokenMaskTest(unittest.TestCase):
    """The Bearer tokens of custody registers and drift sources (DESIGN §37, §38) are among what H1 hides: this
    process's systemd credentials ($CREDENTIALS_DIRECTORY, whole) and each configured token_file."""

    def test_the_credentials_directory_and_every_token_file_are_masked(self) -> None:
        from swfactory.config import Config, sandbox_masks, token_masks

        tmp = Path(tempfile.mkdtemp())
        config = Config(tmp / "config.json", {"data_dir": str(tmp / "data"), "providers": {
            "tabby": {"lazy_load": True, "custody": {"url": "http://127.0.0.1:9", "token_file": "~/register.token"}},
            "plain": {"api_key_env": None}},
            "drift_sources": {"checker": {"url": "http://127.0.0.1:9/d", "token_file": str(tmp / "checker.token"),
                                          "schema_versions": ["1"]}}})
        with mock.patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": "/run/credentials/factory.service"}):
            masks = sandbox_masks(config)
        self.assertIn(Path("/run/credentials/factory.service"), masks["dirs"])
        self.assertIn(Path(os.path.expanduser("~/register.token")), masks["files"])
        self.assertIn(tmp / "checker.token", masks["files"])
        for value in ("/", "relative/dir", ""):   # never the whole machine, never a relative path
            with self.subTest(value=value):
                self.assertEqual(token_masks(config, {"CREDENTIALS_DIRECTORY": value})[0], [])


@unittest.skipUnless(bwrap_available(), "bubblewrap is not usable here")
class SentinelTest(unittest.TestCase):
    """S46: under bwrap an agent's Bash cannot read the config (with the master token), the env file, the
    sensitive-terms file, a thread, a memory summary, a run log or the outbox; the user's skills still can. The
    run's context is built the way a chat builds it (the user skills directory among its read roots), and the
    config and the env file are replaced while a command runs."""

    MARKER = "sentinel-7f3a9c"

    def setUp(self) -> None:
        base = _outside_tmp()
        if base is None:
            self.skipTest("no writable directory outside /tmp")
        self.base = base
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        patch = mock.patch.dict(os.environ, {"HOME": str(base / "home")})
        patch.start()
        self.addCleanup(patch.stop)
        self.user_dir = base / "home" / ".config" / "software-factory"   # the default config directory
        self.skill = self.user_dir / "skills" / "demo" / "SKILL.md"
        self.skill.parent.mkdir(parents=True)
        self.skill.write_text("---\nname: demo\ndescription: d\n---\nskill-visible\n")

    def sandbox_for(self, raw: dict, read_roots: list[Path] | None = None) -> tuple[Any, Sandbox, Path]:
        from swfactory.chats import agent_context
        from swfactory.config import Config, sandbox_masks
        from swfactory.harness.loop import run_masks

        config = Config(self.user_dir / "config.json", {"server": {"token": self.MARKER + "-master"}, **raw})
        config.save()
        config = Config.load(config.path)
        work = config.data_dir / "chats" / "C-1" / "work"
        work.mkdir(parents=True, exist_ok=True)
        ctx = agent_context(config, None, None, None, job={}, cwd=work, job_dir=work.parent,  # type: ignore[arg-type]
                            write_paths=[work], read_roots=[work, *(read_roots or [])], log_path=work / "log.jsonl",
                            emit=lambda *a: None, cancel=threading.Event(), session_path=work / "s.json")
        self.assertIn(self.user_dir / "skills", [Path(p) for p in ctx.read_roots], "a chat's read roots")
        masked, masked_files, rebind = run_masks(config, ctx)
        box = Sandbox("bwrap", work, [work], False, {}, masked=masked, masked_files=masked_files,
                      readonly_rebind=[*sandbox_masks(config)["rebind"], *ctx.skill_dirs, *rebind])
        self.addCleanup(box.cleanup)
        return config, box, work

    def check_hidden(self, box: Sandbox, secrets_: dict[str, Path], config: Any, work: Path) -> None:
        for what, path in secrets_.items():
            _code, out, _ = box.run(f"cat {path} 2>&1; true", 20)
            self.assertNotIn(self.MARKER, out, f"Bash read the {what} under bwrap")
        code, out, _ = box.run(f"cat {self.skill}", 20)
        self.assertEqual((code, out.strip().splitlines()[-1]), (0, "skill-visible"), "skills are bound back")
        code, _, _ = box.run(f"touch {self.skill.parent}/new", 20)
        self.assertNotEqual(code, 0, "read-only")
        code, out, _ = box.run(f"touch {self.user_dir}/planted", 20)
        self.assertNotEqual(code, 0, "a masked directory is read-only too, so a write fails instead of vanishing")
        _code, out, _ = box.run(f"echo hi > {work}/x && cat {work}/x", 20)
        self.assertEqual(out.strip(), "hi", "the run's own paths still work")

    def plant(self, data: Path, terms: Path) -> dict[str, Path]:
        persona = data / "wakes" / "personas" / "seraphina"
        secrets_ = {"config": self.user_dir / "config.json", "env": self.user_dir / "env", "terms": terms,
                    "thread": persona / "thread.jsonl", "memory": persona / "memory.md",
                    "run log": persona / "logs" / "01-wake.jsonl",
                    "outbox": data / "notify" / "outbox" / "M-000001.json",
                    "persona card": self.user_dir / "personas" / "kate.md"}
        for what, path in secrets_.items():
            if what != "config":
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"{self.MARKER}\n")
        return secrets_

    def test_bash_under_bwrap_cannot_read_what_h1_masks(self) -> None:
        data = self.base / "data"
        terms = self.base / "elsewhere" / "sensitive-terms.txt"
        secrets_ = self.plant(data, terms)
        config, box, work = self.sandbox_for({"data_dir": str(data), "notify": {"sensitive_terms_file": str(terms)}})
        self.check_hidden(box, secrets_, config, work)

    def test_data_inside_the_config_directory_and_the_directory_itself_as_a_read_root(self) -> None:
        data = self.user_dir / "data"
        secrets_ = self.plant(data, self.user_dir / "sensitive-terms.txt")
        (self.user_dir / "notes.md").write_text("notes-visible\n")
        config, box, work = self.sandbox_for({"data_dir": str(data)}, read_roots=[self.user_dir])
        self.check_hidden(box, secrets_, config, work)
        _code, out, _ = box.run(f"cat {self.user_dir / 'notes.md'}", 20)
        self.assertEqual(out.strip(), "notes-visible", "a read root's other entries stay readable")

    def test_a_config_save_and_a_secret_set_while_a_command_runs_stay_hidden(self) -> None:
        from swfactory import envfile, settings

        data = self.base / "data"
        config, box, work = self.sandbox_for({"data_dir": str(data)})
        envfile.set_secret(config, "SERAPHINA_TELEGRAM_BOT_TOKEN", self.MARKER + "-before")
        found: dict[str, str] = {}

        def agent() -> None:   # an agent's long-running Bash command
            found["out"] = box.run(f"for i in $(seq 1 16); do cat {config.path} {self.user_dir / 'env'} 2>/dev/null; "
                                   "sleep 0.25; done; true", 30)[1]
        thread = threading.Thread(target=agent)
        thread.start()
        time.sleep(1.0)
        settings.update_config(config, {"limits": {"max_turns": 77}})   # os.replace of config.json
        envfile.set_secret(config, "SERAPHINA_TELEGRAM_BOT_TOKEN", self.MARKER + "-after")   # and of env
        thread.join(30)
        self.assertNotIn(self.MARKER, found.get("out", ""))

    def test_the_notice_rulebook_is_readable_never_writable_and_persona_files_stay_hidden(self) -> None:
        """Decision g: agents (and an outside notifier) may read quiet hours and limits, never change them; the
        step W2 persona files (memory versions, the write records, the outbox of memory proposals, the persona's
        inbox and the WakeManager's queue and reasons) are hidden like the rest of the wake data."""
        data = self.base / "data"
        persona = data / "wakes" / "personas" / "seraphina"
        hidden = {"versions": persona / "memory.versions" / "000001-x.md", "writes": persona / "memory-writes.jsonl",
                  "proposals": persona / "memory-proposals.jsonl", "inbox": persona / "inbox.jsonl",
                  "queue": data / "wakes" / "queue.json", "reason": data / "wakes" / "reasons" / "W-000001.txt",
                  "soul": self.user_dir / "personas" / "seraphina.md"}
        for path in hidden.values():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{self.MARKER}\n")
        rulebook = self.user_dir / "notice-rules.json"
        rulebook.write_text('{"version": 1, "quiet_hours": "rules-visible"}\n')
        config, box, work = self.sandbox_for({"data_dir": str(data)})
        for what, path in hidden.items():
            _code, out, _ = box.run(f"cat {path} 2>&1; true", 20)
            self.assertNotIn(self.MARKER, out, f"Bash read the persona's {what} under bwrap")
        code, out, _ = box.run(f"cat {rulebook}", 20)
        self.assertEqual((code, "rules-visible" in out), (0, True), "the rulebook is bound back read-only")
        code, _, _ = box.run(f"echo '{{}}' > {rulebook}", 20)
        self.assertNotEqual(code, 0, "an agent can never change the rulebook")
        self.assertIn("rules-visible", rulebook.read_text())

    def test_the_harness_s_credentials_and_token_files_stay_hidden(self) -> None:
        """An agent's Bash cannot read the harness's systemd credentials (the register's and the checker's
        tokens, its own LoadCredential copies) or a configured token_file: with them it could ask the operator's
        register for leases, or release them, as the harness."""
        credentials = self.base / "credentials"
        credentials.mkdir(mode=0o700)
        (credentials / "register-token").write_text(f"{self.MARKER}-credential\n")
        token_file = self.base / "tokens" / "checker.token"
        token_file.parent.mkdir()
        token_file.write_text(f"{self.MARKER}-file\n")
        with mock.patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": str(credentials)}):
            config, box, work = self.sandbox_for({
                "data_dir": str(self.base / "data"),
                "providers": {"tabby": {"lazy_load": True, "custody": {"url": "http://127.0.0.1:9",
                                                                       "token_credential": "register-token"}}},
                "drift_sources": {"checker": {"url": "http://127.0.0.1:9/d", "token_file": str(token_file),
                                              "schema_versions": ["1"]}}})
        for path in (credentials / "register-token", token_file):
            with self.subTest(path=path):
                _code, out, _ = box.run(f"cat {path} 2>&1; ls -a {path.parent} 2>&1; true", 20)
                self.assertNotIn(self.MARKER, out, f"Bash read {path} under bwrap")
        _code, out, _ = box.run(f"ls -a {credentials}", 20)
        self.assertNotIn("register-token", out, "the credentials directory is hidden whole")

    def test_an_agent_socket_outside_tmp_cannot_be_reached(self) -> None:
        from swfactory.config import Config, sandbox_masks

        runtime = self.base / "run"
        (runtime / "gnupg").mkdir(parents=True)
        sockets = [runtime / "gnupg" / "S.gpg-agent.ssh", runtime / "ssh-agent.socket", self.base / "custom.sock"]
        listeners = []
        for path in sockets:
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(path))
            server.listen(4)
            listeners.append(server)
            self.addCleanup(server.close)
        work = self.base / "work"
        work.mkdir()
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime), "SSH_AUTH_SOCK": str(sockets[2])}):
            config = Config(self.base / "cfg" / "config.json", {"data_dir": str(self.base / "data")})
            masks = sandbox_masks(config)
            dirs, files, rebind = sandbox_mod.mask_plan(masks["dirs"], masks["files"], [work], masks["fallback"])
            box = Sandbox("bwrap", work, [work], False, {}, masked=dirs, masked_files=files, readonly_rebind=rebind)
            self.addCleanup(box.cleanup)
            for path in sockets:
                code, out, _ = box.run(f"python3 -c \"import socket; s = socket.socket(socket.AF_UNIX); "
                                       f"s.connect('{path}')\" 2>&1 | tail -1", 20)
                self.assertRegex(out, "Error", f"{path} was reachable")

    def test_a_socket_that_gives_root_cannot_be_reached(self) -> None:
        """DESIGN §17: a container engine's or LXD's socket is root for whoever may connect, and a read-only bind of
        / does not stop a connect(); config.ROOT_SOCKETS that exist are hidden with /dev/null."""
        from swfactory import config as config_mod
        from swfactory.config import Config, sandbox_masks

        engine = self.base / "engine.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(engine))
        server.listen(4)
        self.addCleanup(server.close)
        link = self.base / "var-run-engine.sock"
        link.symlink_to(engine)
        work = self.base / "work"
        work.mkdir()
        connect = (f"python3 -c \"import socket; s = socket.socket(socket.AF_UNIX); s.connect('{engine}'); "
                   f"print('reached')\" 2>&1 | tail -1")
        plain = Sandbox("bwrap", work, [work], False, {})
        self.addCleanup(plain.cleanup)
        self.assertIn("reached", plain.run(connect, 20)[1], "without the mask the socket is reachable")
        with mock.patch.object(config_mod, "ROOT_SOCKETS", (str(link), str(engine), str(self.base / "absent.sock"))):
            self.assertEqual(config_mod.root_sockets(), [engine], "existing ones only, each once")
            config = Config(self.base / "cfg" / "config.json", {"data_dir": str(self.base / "data")})
            masks = sandbox_masks(config)
        self.assertIn(engine, masks["files"])
        dirs, files, rebind = sandbox_mod.mask_plan(masks["dirs"], masks["files"], [work], masks["fallback"])
        box = Sandbox("bwrap", work, [work], False, {}, masked=dirs, masked_files=files, readonly_rebind=rebind)
        self.addCleanup(box.cleanup)
        out = box.run(connect, 20)[1]
        self.assertNotIn("reached", out)
        self.assertRegex(out, "Error")
