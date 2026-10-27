"""The harness's client of its narrow root helpers (docs/seraphina/YOUR_SERVER.md §6.2; DESIGN.md section 42):
``sudo -n <helper>`` with one JSON request line on standard input, through an injectable runner (like the Blueprint
collector's), and one JSON answer line back. Exit codes are mapped (0 answered, 2 refused, 1 the helper's own failure);
a refusal of sudo itself is told apart, with both sudo programs' words (classic sudo's "a password is required",
sudo-rs's "interactive authentication is required"), and its cause named when this process runs with NoNewPrivs.

Every verb of Your server's P1a only reads, so an answer that never came is asked again at the next refresh, never in
a loop. A changing request carries a ``rid`` and is never retried: a call whose outcome is unknown (a timeout after
the request was written, or a helper that ended without an answer) raises HelperError("unknown"), the shared outcome
rule of DESIGN §40. swfactory/snapraid.py keeps its own copy of this client for now; it moves here when that panel is
next changed.
"""

from __future__ import annotations

import contextlib
import json
import re
import signal
import subprocess
from pathlib import Path
from typing import Any, Callable

STOP_GRACE = 30
# What `sudo -n` says when it will not run a command: classic sudo and sudo-rs (Ubuntu 25.10 and later ship both).
SUDO_REFUSALS = ("a password is required", "interactive authentication is required", "is not allowed to execute",
                 "not allowed", "must be owned by uid 0", "no new privileges", "sudo:", "sudo-rs:")

Runner = Callable[[list[str], bytes, float], tuple[int | None, bytes, bytes]]


class HelperError(Exception):
    """A call that got no answer, or a refusal: code (no_sudo, sudo_refused, timeout, unknown, unreadable, or the
    helper's own refusal code), plain words, and the helper's answer when there was one."""

    def __init__(self, code: str, words: str, answer: dict | None = None) -> None:
        super().__init__(words)
        self.code = code
        self.words = words
        self.answer = answer or {}


def run_command(argv: list[str], data: bytes, timeout: float) -> tuple[int | None, bytes, bytes]:
    """The default runner: argv with data on standard input, no shell, a minimal environment. A call past timeout
    gets SIGINT, then SIGTERM (sudo passes them on); never SIGKILL. (exit status, stdout, stderr); exit None when it
    did not end."""
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C", "LANG": "C"}
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd="/",
                            env=env)
    try:
        out, err = proc.communicate(data, timeout=timeout)
    except subprocess.TimeoutExpired:
        out, err = b"", b""
        for signum in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                proc.send_signal(signum)
            try:
                out, err = proc.communicate(timeout=STOP_GRACE)
                break
            except subprocess.TimeoutExpired:
                continue
        return None, out or b"", (err or b"") + b"\n(timed out)"
    return proc.returncode, out, err


def no_new_privs(path: str = "/proc/self/status") -> bool:
    """True when this process runs with NoNewPrivs set: sudo's setuid bit is then ignored and no helper is reached."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    match = re.search(r"^NoNewPrivs:\s*(\d)", text, re.MULTILINE)
    return bool(match and match.group(1) == "1")


def sudo_refused(stderr: bytes | str) -> bool:
    text = stderr.decode("utf-8", "replace") if isinstance(stderr, bytes) else str(stderr)
    return any(word in text for word in SUDO_REFUSALS)


class HelperClient:
    """``sudo -n <helper>`` with one JSON request line; the answer, or HelperError. name: how the helper is called
    in words ("Seraphina's system access"); timeouts: seconds per verb."""

    def __init__(self, helper: str, runner: Runner | None = None, sudo: str = "sudo",
                 status_path: str = "/proc/self/status", name: str = "the helper",
                 timeouts: dict[str, float] | None = None, default_timeout: float = 120.0) -> None:
        self.helper = helper
        self.runner = runner or run_command
        self.sudo = sudo
        self.status_path = status_path
        self.name = name
        self.timeouts = dict(timeouts or {})
        self.default_timeout = default_timeout

    def call(self, verb: str, timeout: float | None = None, changing: bool = False, **fields: Any) -> dict:
        """One request. changing: the request changes something (it carries a rid); a timeout or a lost answer (no
        JSON line, and not sudo's own refusal) is then outcome unknown, never retried here (DESIGN §40)."""
        request = json.dumps({"v": 1, "verb": verb, **fields}, separators=(",", ":")).encode("utf-8") + b"\n"
        argv = [self.sudo, "-n", self.helper]
        waited = float(timeout or self.timeouts.get(verb, self.default_timeout))
        try:
            code, out, err = self.runner(argv, request, waited)
        except FileNotFoundError:
            raise HelperError("no_sudo", "sudo is not installed on this machine.") from None
        except OSError as exc:
            raise HelperError("no_sudo", f"sudo could not be started ({type(exc).__name__}).") from None
        text = (out or b"").decode("utf-8", "replace").strip()
        answer = None
        if text:
            with contextlib.suppress(ValueError):
                answer = json.loads(text.splitlines()[-1])
        if not isinstance(answer, dict):
            if code is None:
                if changing:
                    raise HelperError("unknown", f"{self.name[:1].upper() + self.name[1:]} did not answer within "
                                                 f"{int(waited)} seconds: it may or may not have done it. Check "
                                                 f"before trying again.")
                raise HelperError("timeout", f"{self.name[:1].upper() + self.name[1:]} did not answer within "
                                             f"{int(waited)} seconds; it is asked again at the next check.")
            if sudo_refused(err or b""):
                cause = (" This process runs with NoNewPrivs set, which stops sudo: run the harness from a unit "
                         "without NoNewPrivileges=yes." if no_new_privs(self.status_path) else
                         " Check that its sudoers file is installed (the setup page has the paste).")
                raise HelperError("sudo_refused", f"sudo would not run {self.name}." + cause)
            if changing:
                # it ended without an answer after the request was written (killed, or its own failure): it may
                # have run, so the outcome is unknown and the caller asks what happened, as after a timeout
                raise HelperError("unknown", f"{self.name[:1].upper() + self.name[1:]} ended without an answer "
                                             f"(exit {code}): it may or may not have done it. Check before trying "
                                             f"again.")
            raise HelperError("unreadable", f"The answer of {self.name} could not be read" +
                              (f" (exit {code})." if code is not None else "."))
        if answer.get("ok") is not True:
            raise HelperError(str(answer.get("error") or "refused"), str(answer.get("words") or "Refused."), answer)
        return answer
