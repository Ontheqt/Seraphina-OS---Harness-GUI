#!/usr/bin/env python3
"""A fake agent CLI for the CLI driver tests. Behaviour is driven by environment variables.

FAKE_CLI_RECORD     write {"argv", "cwd", "env", "stdin", "pid"} as JSON to this path
FAKE_CLI_ENV_KEYS   comma-separated env var names to record (default FOO,BAR,BOTH)
FAKE_CLI_FORMAT     stream-json (Claude/Qwen style, default) | jsonl (Codex style) | text
FAKE_CLI_REPLAY     print this file's lines verbatim instead of generated output
FAKE_CLI_TEXT       the assistant's text (default "fake done")
FAKE_CLI_USAGE      JSON object used as the reported usage
FAKE_CLI_THREAD     Codex thread id for a new session
FAKE_CLI_ECHO       print the value of this env var as a line
FAKE_CLI_LONG       print one line of this many characters
FAKE_CLI_STDERR     print this line to stderr
FAKE_CLI_WRITE      "path=content": write a file (relative paths are below cwd)
FAKE_CLI_CHILD      start a background grandchild sleeping 60s; write its pid to this path
FAKE_CLI_IGNORE_TERM  "1": ignore SIGTERM
FAKE_CLI_SLEEP      seconds to sleep after printing
FAKE_CLI_EXIT       exit code (default 0)
FAKE_CLI_HOOKS      with --settings <file> in argv: play its Claude Code hooks as a headless run would (the
                    SessionStart "compact" hooks once, then Stop: when a Stop hook answers decision "block",
                    run FAKE_CLI_ON_BLOCK and call Stop again with stop_hook_active true) and write what each
                    hook printed, as JSON, to this path
FAKE_CLI_ON_BLOCK   "path=content": what the agent writes when a Stop hook sends it back
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_THREAD = "0199a000-0000-7000-8000-000000000001"


def arg_after(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv[:-1] else None


def stream_json_lines(argv: list[str], text: str, failed: bool) -> list[dict]:
    session = arg_after(argv, "--session-id") or arg_after(argv, "--resume") or "no-session"
    usage = json.loads(os.environ.get("FAKE_CLI_USAGE") or
                       '{"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 30}')

    def assistant(message_id: str, block: dict) -> dict:
        return {"type": "assistant", "session_id": session,
                "message": {"id": message_id, "role": "assistant", "content": [block]}}

    return [
        {"type": "system", "subtype": "init", "session_id": session, "cwd": os.getcwd()},
        assistant("m1", {"type": "thinking", "thinking": "thinking about it"}),
        assistant("m1", {"type": "tool_use", "id": "toolu_1", "name": "Write",
                         "input": {"file_path": "out.txt", "content": "x" * 600}}),
        {"type": "user", "session_id": session, "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": [{"type": "text", "text": "ok"}]}]}},
        assistant("m2", {"type": "text", "text": text}),
        {"type": "result", "subtype": "error_during_execution" if failed else "success",
         "is_error": failed, "num_turns": 3, "result": text, "session_id": session, "usage": usage},
    ]


def jsonl_lines(argv: list[str], text: str) -> list[dict]:
    thread = (argv[argv.index("resume") + 1] if "resume" in argv[:-1]
              else os.environ.get("FAKE_CLI_THREAD") or DEFAULT_THREAD)
    usage = json.loads(os.environ.get("FAKE_CLI_USAGE") or
                       '{"input_tokens": 100, "cached_input_tokens": 30, "output_tokens": 20}')
    command = {"id": "item_1", "type": "command_execution", "command": "bash -lc 'echo hi'",
               "aggregated_output": "", "exit_code": None, "status": "in_progress"}
    return [
        {"type": "thread.started", "thread_id": thread},
        {"type": "turn.started"},
        {"type": "item.started", "item": command},
        {"type": "item.completed", "item": {**command, "aggregated_output": "hi\n", "exit_code": 0,
                                            "status": "completed"}},
        {"type": "item.completed", "item": {"id": "item_2", "type": "reasoning", "text": "pondering"}},
        {"type": "item.completed", "item": {"id": "item_3", "type": "agent_message", "text": text}},
        {"type": "turn.completed", "usage": usage},
    ]


def run_hook(command: str, event: dict) -> str:
    """One hook command, as Claude Code runs it: through the shell, its input as JSON on stdin."""
    done = subprocess.run(command, shell=True, input=json.dumps(event), capture_output=True, text=True, timeout=60)
    return done.stdout


def play_hooks(argv: list[str], record_path: str) -> list[str]:
    """The --settings hooks: SessionStart (compact), then Stop until it no longer blocks (stop_hook_active the
    second time); returns extra assistant texts for the continuation."""
    settings = json.loads(Path(arg_after(argv, "--settings") or "").read_text())
    hooks = settings.get("hooks") or {}
    session = arg_after(argv, "--session-id") or arg_after(argv, "--resume") or "no-session"
    record: dict = {"session_start": [], "stop": []}
    for entry in hooks.get("SessionStart") or []:
        if entry.get("matcher") in (None, "", "compact"):
            for hook in entry.get("hooks") or []:
                record["session_start"].append(run_hook(hook["command"], {
                    "hook_event_name": "SessionStart", "source": "compact", "session_id": session}))
    extra = []
    active = False
    for _ in range(3):
        blocked = False
        for entry in hooks.get("Stop") or []:
            for hook in entry.get("hooks") or []:
                out = run_hook(hook["command"], {"hook_event_name": "Stop", "stop_hook_active": active,
                                                 "session_id": session})
                record["stop"].append(out)
                if out.strip() and json.loads(out).get("decision") == "block":
                    blocked = True
        if not blocked:
            break
        extra.append("continuing after the stop hook")
        if os.environ.get("FAKE_CLI_ON_BLOCK"):
            rel, _, content = os.environ["FAKE_CLI_ON_BLOCK"].partition("=")
            Path(rel).write_text(content)
        active = True
    Path(record_path).write_text(json.dumps(record))
    return extra


def start_grandchild(pid_path: str) -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    Path(pid_path).write_text(str(child.pid))


def main() -> int:
    argv = sys.argv[1:]
    exit_code = int(os.environ.get("FAKE_CLI_EXIT") or 0)
    record = os.environ.get("FAKE_CLI_RECORD")
    if record:
        keys = (os.environ.get("FAKE_CLI_ENV_KEYS") or "FOO,BAR,BOTH").split(",")
        Path(record).write_text(json.dumps({
            "argv": argv, "cwd": os.getcwd(), "env": {key: os.environ.get(key) for key in keys},
            "stdin": sys.stdin.read(), "pid": os.getpid()}))
    if os.environ.get("FAKE_CLI_IGNORE_TERM") == "1":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if os.environ.get("FAKE_CLI_CHILD"):
        start_grandchild(os.environ["FAKE_CLI_CHILD"])
    if os.environ.get("FAKE_CLI_WRITE"):
        rel, _, content = os.environ["FAKE_CLI_WRITE"].partition("=")
        Path(rel).write_text(content)

    text = os.environ.get("FAKE_CLI_TEXT", "fake done")
    if os.environ.get("FAKE_CLI_HOOKS") and "--settings" in argv:
        extra = play_hooks(argv, os.environ["FAKE_CLI_HOOKS"])
        text = " ".join([text, *extra])
    fmt = os.environ.get("FAKE_CLI_FORMAT", "stream-json")
    out: list[str] = ["not json {"]
    if os.environ.get("FAKE_CLI_REPLAY"):
        out = Path(os.environ["FAKE_CLI_REPLAY"]).read_text().splitlines()
    elif fmt == "stream-json":
        out += [json.dumps(line) for line in stream_json_lines(argv, text, exit_code != 0)]
    elif fmt == "jsonl":
        out += [json.dumps(line) for line in jsonl_lines(argv, text)]
    else:
        out += ["fake line 1", text]
    if os.environ.get("FAKE_CLI_ECHO"):
        out.append(os.environ.get(os.environ["FAKE_CLI_ECHO"], ""))
    if os.environ.get("FAKE_CLI_LONG"):
        out.append("L" * int(os.environ["FAKE_CLI_LONG"]))
    print("\n".join(out), flush=True)
    if os.environ.get("FAKE_CLI_STDERR"):
        print(os.environ["FAKE_CLI_STDERR"], file=sys.stderr, flush=True)
    if os.environ.get("FAKE_CLI_SLEEP"):
        time.sleep(float(os.environ["FAKE_CLI_SLEEP"]))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
