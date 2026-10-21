"""Claude Code hooks for CLI runs with tracked items (DESIGN.md section 32; housekeeping.cli_hooks, off by default).

A CLI driver cannot get the builtin Items tool, re-injection or the loop's stop guard. Behind
``housekeeping.cli_hooks`` the CLI driver gives Claude Code (``--settings``) two hooks that run
this module with a state file the runner wrote beside the session file (the scope's folder and
roles, the agent, its output files):

- ``session-start`` (event ``SessionStart``, matcher ``compact``): after the CLI compacted its
  context, the open list goes back in as ``additionalContext`` (the same note the builtin loop
  re-injects).
- ``stop`` (event ``Stop``): when a must-do item the agent owns, or a done item it must check,
  is not answered under ``## Items`` in its file, Claude Code is told to go on
  (``{"decision": "block", "reason": ...}``), once: with ``stop_hook_active`` set (the CLI is
  already continuing because of this hook) it lets the run end.

Whether both fire the same way in a headless ``-p`` run is UNVERIFIED against a real Claude Code;
tests/fake_cli.py plays them, and cli_hooks stays off until a real-CLI run passes. The hook only
reads the scope's files; it never writes a ledger (the manager takes the run's ``## Items`` lines
in after it).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, TextIO

from . import items as tracker

STATE_VERSION = 1


def write_state(path: Path, ctx_items: Any) -> None:
    """The hook state for one CLI run (ctx.items: its ItemsContext)."""
    store, scope = ctx_items.store, ctx_items.scope
    state = {
        "version": STATE_VERSION, "folder": str(store.folder), "name": store.path.stem,
        "md": store.md_path is not None,
        "scope": {"kind": scope.kind, "repo": scope.repo, "id": scope.id, "worker": scope.worker,
                  "approver": scope.approver, "verifier": scope.verifier, "host": scope.host,
                  "handoff": scope.handoff},
        "agent": ctx_items.agent, "output_files": [str(p) for p in ctx_items.output_files],
        "contract": ctx_items.contract, "builder": bool(ctx_items.builder), "answer_file": ctx_items.answer_file,
        "max_chars": ctx_items.max_chars, "stop_guard": bool(ctx_items.stop_guard),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp.replace(path)


def _load(state_path: str) -> tuple[dict, tracker.Ledger, tracker.ItemStore]:
    state = json.loads(Path(state_path).read_text(encoding="utf-8"))
    if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
        raise ValueError("not an items hook state file")
    scope = tracker.Scope(**{key: state["scope"].get(key) for key in (
        "kind", "repo", "id", "worker", "approver", "verifier")},
        host=state["scope"].get("host") or tracker.LOCAL_HOST, handoff=bool(state["scope"].get("handoff")))
    store = tracker.ItemStore(Path(state["folder"]), scope, name=str(state.get("name") or "items"),
                              md=bool(state.get("md")))
    return state, store.ledger(), store


def session_start(state_path: str, event: dict) -> dict | None:
    """additionalContext with the open list after a compaction (None when there is nothing to show)."""
    if event.get("source") not in (None, "compact"):
        return None
    state, ledger, store = _load(state_path)
    note = tracker.render_note(ledger, str(state["agent"]), md_path=store.md_path,
                               max_chars=int(state.get("max_chars") or 1200), has_tool=False,
                               answer_file=state.get("answer_file"))
    if not note:
        return None
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": note}}


def stop(state_path: str, event: dict) -> dict | None:
    """{"decision": "block", "reason": ...} once, while owned must-do items are unanswered; else None."""
    if event.get("stop_hook_active"):
        return None   # already continuing because of this hook: never twice
    state, ledger, _ = _load(state_path)
    if not state.get("stop_guard"):
        return None
    touched: set[str] | None = set()
    for name in state.get("output_files") or []:
        try:
            text = Path(name).read_text(encoding="utf-8", errors="replace")[: tracker.OUTPUT_READ_BYTES]
        except OSError:
            continue
        found = tracker.touched_in_file(ledger, text, str(state.get("contract") or "other"))
        if found is None:
            touched = None
            break
        touched |= found
    reason = tracker.guard_message(ledger, str(state["agent"]), touched, 1, 1, builder=bool(state.get("builder")),
                                   answer_file=state.get("answer_file"))
    return {"decision": "block", "reason": reason} if reason else None


def main(argv: list[str] | None = None, stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    """python -m swfactory.items_hook <session-start|stop> <state file>; the hook's JSON input on stdin."""
    args = list(sys.argv[1:] if argv is None else argv)
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    if len(args) != 2 or args[0] not in ("session-start", "stop"):
        print("usage: items_hook <session-start|stop> <state file>", file=sys.stderr)
        return 2
    try:
        event = json.loads(stdin.read() or "{}")
    except ValueError:
        event = {}
    try:
        result = (session_start if args[0] == "session-start" else stop)(args[1], event if isinstance(event, dict)
                                                                          else {})
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"items hook: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 0   # a broken hook never stops the agent's run
    if result is not None:
        stdout.write(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
