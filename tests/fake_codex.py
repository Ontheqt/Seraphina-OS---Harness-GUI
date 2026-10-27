#!/usr/bin/env python3
"""A stand-in for the pinned Codex binary, for the Codex worker's unit tests on a machine without it (DESIGN §44).

The launcher execs it with Codex's own environment only, so it reads its behaviour from files beside itself:
``scenario.json`` ``{"launches": [[step, ...], ...]}`` (the n-th launch plays the n-th list; the last list repeats)
and it appends each launch's ``{argv, env, stdin, cwd, codex_home_files}`` to ``launches.jsonl``. Steps:

- ``{"run": "<shell>"}``: runs the command in the working directory (no sandbox: this is not Codex) and reports it
  as a ``command_execution`` item; ``{out_dir}`` in it becomes the ``--add-dir`` folder, ``{named_out_dir}`` the
  out folder this launch's prompt names (as a model reading only its prompt would; ``/nonexistent-out`` when it
  names none);
- ``{"text": "..."}``: an ``agent_message`` item;
- ``{"sleep": seconds}``: waits; SIGINT then ends it as exec does (exit 1, no terminal event); every signal it
  gets is appended to ``signals`` beside it;
- ``{"die": true}``: SIGKILL itself (a crash mid-turn);
- ``{"fail": "message"}``: ``error`` then ``turn.failed``, exit 1;
- ``{"exit0": true}``: exit 0 at once, with no ``turn.completed`` (an exec that ended without its terminal event).

``resume <id>`` whose rollout is not in ``$CODEX_HOME/sessions`` (a home deleted at job end) fails as a lost thread
does: a line on stderr, exit 1, no event.

``--version`` prints ``codex-cli 0.163.0`` (or the content of ``version.txt`` beside it).
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent


def emit(event: dict) -> None:
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def main() -> int:
    argv = sys.argv[1:]
    if argv == ["--version"]:
        version = (HERE / "version.txt").read_text().strip() if (HERE / "version.txt").exists() else "0.163.0"
        print(f"codex-cli {version}")
        return 0
    stdin = sys.stdin.read() if argv[-1:] == ["-"] else ""
    home = os.environ.get("CODEX_HOME", "")
    record = {"argv": argv, "env": dict(os.environ), "stdin": stdin, "cwd": os.getcwd(),
              "codex_home_files": sorted(os.listdir(home)) if home and os.path.isdir(home) else []}
    with open(HERE / "launches.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")
    count = sum(1 for _ in open(HERE / "launches.jsonl", encoding="utf-8"))
    scenario = json.loads((HERE / "scenario.json").read_text()) if (HERE / "scenario.json").exists() else {}
    launches = scenario.get("launches") or [[{"text": "Done."}]]
    steps = launches[min(count, len(launches)) - 1]
    out_dir = argv[argv.index("--add-dir") + 1] if "--add-dir" in argv else ""
    named = re.search(r"(?:out folder is|Write build\.md in) (/[^\s:;()]+)", stdin)
    named_out_dir = named.group(1).rstrip(".") if named else "/nonexistent-out"
    thread = argv[argv.index("resume") + 1] if "resume" in argv else str(uuid.uuid4())
    if home and "resume" in argv and not list(Path(home).glob(f"sessions/**/rollout-{thread}.jsonl")):
        sys.stderr.write(f"Error: no rollout found for thread id {thread}\n")
        return 1
    if home:   # a rollout, as Codex writes one
        sessions = Path(home) / "sessions" / "2026" / "10" / "26"
        sessions.mkdir(parents=True, exist_ok=True)
        with open(sessions / f"rollout-{thread}.jsonl", "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "turn", "prompt_chars": len(stdin)}) + "\n")
    def stopped(signum: int, frame: object) -> None:   # exec turns SIGINT into turn/interrupt: exit 1, no event
        with open(HERE / "signals", "a", encoding="utf-8") as handle:
            handle.write(signal.Signals(signum).name + "\n")
        os._exit(1 if signum == signal.SIGINT else 128 + signum)

    signal.signal(signal.SIGINT, stopped)
    signal.signal(signal.SIGTERM, stopped)
    emit({"type": "thread.started", "thread_id": thread})
    emit({"type": "turn.started"})
    for number, step in enumerate(steps, 1):
        if "run" in step:
            command = step["run"].replace("{out_dir}", out_dir).replace("{named_out_dir}", named_out_dir)
            done = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True)
            emit({"type": "item.completed", "item": {"id": f"item_{number}", "type": "command_execution",
                                                     "command": command, "aggregated_output": done.stdout + done.stderr,
                                                     "exit_code": done.returncode, "status": "completed"}})
        elif "text" in step:
            emit({"type": "item.completed", "item": {"id": f"item_{number}", "type": "agent_message",
                                                     "text": step["text"]}})
        elif "sleep" in step:
            time.sleep(float(step["sleep"]))
        elif step.get("die"):
            os.kill(os.getpid(), signal.SIGKILL)
        elif step.get("exit0"):
            return 0
        elif "fail" in step:
            emit({"type": "error", "message": str(step["fail"])})
            emit({"type": "turn.failed", "error": {"message": str(step["fail"])}})
            return 1
    emit({"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5}})
    return 0


if __name__ == "__main__":
    sys.exit(main())
