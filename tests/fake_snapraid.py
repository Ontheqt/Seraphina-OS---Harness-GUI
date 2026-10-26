#!/usr/bin/env python3
"""A fake SnapRAID 13.0 command line for the helper's tests (docs/seraphina/BACKUPS_SNAPRAID.md §13.2).

    fake_snapraid.py --scenario <scenario.json> [-c CONF] [-l LOG] [options...] COMMAND
    fake_snapraid.py --scenario <scenario.json> --version

A test puts a two-line wrapper at the fixed binary path of its temporary tree (`exec python3 <this file> --scenario
<file> "$@"`), because the helper starts SnapRAID with a minimal environment. The scenario is JSON:

    {"calls": "<file>", "version": "13.0",
     "paths": {"/mnt/disk1": "<tmp>/mnt/disk1", ...},
     "commands": {"status": {"fixture": "status-ok", "exit": 0},
                  "diff": [{"fixture": "diff-partial", "exit": 2}, {"text": "...", "exit": 0}],
                  "smart": {"fixture": "smart-unsupported", "exit": 0, "sleep": 0.5}}}

Each call appends {argv} to calls (so a test can see that SnapRAID was never started, or which options it got).
A command's entry is one answer, or a list used in order (the last one repeats). The answer writes the recorded
13.0 log (tests/fixtures/snapraid13/<fixture>.log, or text) to the -l path with the placeholder paths replaced,
prints the recorded human output (<fixture>.out) and exits with exit. SIGINT ends it with 130, as SnapRAID does.
"""

from __future__ import annotations

import json
import signal
import sys
import time
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "snapraid13"


def substitute(text: str, paths: dict) -> str:
    for before, after in sorted(paths.items(), key=lambda pair: -len(pair[0])):
        text = text.replace(before, after)
    return text


def main(argv: list[str]) -> int:
    signal.signal(signal.SIGINT, lambda *_: sys.exit(130))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(130))
    if len(argv) < 2 or argv[0] != "--scenario":
        print("fake_snapraid: --scenario <file> first", file=sys.stderr)
        return 2
    scenario = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    args = argv[2:]
    calls = scenario.get("calls")
    if calls:
        with open(calls, "a", encoding="utf-8") as handle:
            handle.write(json.dumps({"argv": args}) + "\n")
    if args == ["--version"]:
        print(f"snapraid v{scenario.get('version', '13.0')} by Andrea Mazzoleni, https://www.snapraid.it")
        return 0
    command = args[-1] if args else ""
    log = args[args.index("-l") + 1] if "-l" in args else None
    entries = (scenario.get("commands") or {}).get(command)
    if entries is None:
        print(f"Unknown command '{command}'")
        return 1
    if isinstance(entries, list):
        state = Path(argv[1] + ".count")
        used = json.loads(state.read_text(encoding="utf-8")) if state.exists() else {}
        index = used.get(command, 0)
        used[command] = index + 1
        state.write_text(json.dumps(used), encoding="utf-8")
        entry = entries[min(index, len(entries) - 1)]
    else:
        entry = entries
    paths = scenario.get("paths") or {}
    if "fixture" in entry:
        text = (FIXTURES / f"{entry['fixture']}.log").read_text(encoding="utf-8", errors="surrogateescape")
        out = FIXTURES / f"{entry['fixture']}.out"
        human = out.read_text(encoding="utf-8", errors="surrogateescape") if out.exists() else ""
    else:
        text, human = entry.get("text", ""), entry.get("out", "")
    if log:
        Path(log).write_text(substitute(text, paths), encoding="utf-8", errors="surrogateescape")
    sys.stdout.write(substitute(human, paths))
    sys.stdout.flush()
    if entry.get("sleep"):
        time.sleep(float(entry["sleep"]))
    return int(entry.get("exit", 0))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
