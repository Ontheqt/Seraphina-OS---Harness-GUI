"""A scripted model that plays every factory agent, for tests/mock_llm.py's MockLLM(handler=...).

``ScriptedLLM(scenario).handler(server)`` answers each request the way the agent
asking would: it recognises the agent from the system prompt (the first line of
each agents/*.md body is in it), reads the task from the conversation so far
(the paths line, "Build round 1.", "Round 1 reviews are in ...", "Rework: ...",
"Task: Plan." ...) and answers with tool calls in the builtin harness's tool
names (Read, Grep, Glob, Bash, Edit, Write), one step per model call. It is
stateless across requests the way a real model is: the step it is on is the
number of assistant messages since the last user message, and what it does next
depends on the tool results it got back (the spec it read, the files' contents,
the review files the glob listed).

What each agent does on the tiny todo fixture (src/todo.js, test/todo.test.js,
index.html, src/app.js, package.json):

- spec-writer reads the code (and the plan, for a plan's job; the branch diff,
  for a chat handoff) and writes spec.md;
- builder checks its branch, reads the spec and the files, edits them by recipe
  (clearCompleted in src/todo.js + a test; a "Clear completed" button; an npm
  test script in package.json), runs ``node --test`` with Bash, commits with
  git, writes build.md ``STATUS: READY`` (``BLOCKED`` when a button job finds no
  clearCompleted on its base). Resumed after a round it globs the round folder,
  reads every review, fixes each CHANGES finding (a doc comment), re-tests,
  commits, and rewrites build.md keeping its Log; for a rework it reads the note;
- reviewer-<lens> reads the diff, spec and build notes (and its previous review)
  and writes its review with the verdict the scenario scripts for that round;
- approver reads everything, spot-checks ``node --test`` and writes APPROVE or
  ESCALATE as scripted (ESCALATE anyway if a final review is not PASS);
- lead writes plan.md with two jobs (the second ``after: 1``), later a final review;
- coder (Chat) implements clearCompleted, then commits when asked;
- repo-scout writes the AGENTS.md draft.

Tracked items (DESIGN.md section 32): the scripted agents follow the ``## Items``
contract the way the agent files ask. The builder marks every item its prompt says
it owns done (``node --test`` as the evidence), a reviewer verifies (or, with
CHANGES, reopens) the items waiting for its check, and the approver verifies the
items waiting for its check and closes both spec criteria in its table. The
``<factory-items>`` notes the builtin harness appends to messages are read past,
as a model would, and a stop-guard reminder is answered with one line.

Protocol checks, collected in ``violations`` (and failing the request with HTTP
400, like a strict API, where the real one would): every OpenAI assistant
message with tool calls carries its reasoning back under the key the server sent
it in (``reasoning_content``, or ``reasoning`` for the "local" server) with the
same text; every Anthropic assistant message starts with its thinking block, its
signature intact; tool results answer exactly the calls of the turn before
(Anthropic: all in one user message); roles alternate. Tool results the agent
did not expect to fail are collected in ``tool_errors``. ``calls`` records every
request (agent, server, model id, step, the instruction, the user messages of
the history), so a test can check what each agent saw.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

AGENTS_DIR = Path(__file__).resolve().parents[2] / "agents"

# Orchestrator prompt lines (swfactory/orchestrator.py).
SPEC_FROM_BRANCH = "The work is already on the branch; describe what it does and what it must do."
SPEC_RETRY = "You did not write spec.md."
BUILD_MD_FIX = "build.md must start with STATUS: READY or STATUS: BLOCKED."
COMMIT_FIX = "Commit or discard your uncommitted changes, then update build.md."
REVIEW_RETRY = "Your review file is missing or its first line is not a verdict."
ITEMS_FIX = "These blocking items are not addressed in build.md: "
STOP_GUARD = "Before you finish:"
NOTE_RE = re.compile(r"\s*<factory-items\b[^>]*>.*?(?:</factory-items>|\Z)", re.DOTALL)
ITEMS_BLOCK = ("Items: ", "You own (", "Waiting for your check (", "Suggested, accept or dismiss (",
               "Answer each under ## Items", "Your last answer left these unresolved")

JOB_PATHS_RE = re.compile(r"^Job folder: (?P<job_dir>.+?)\. Worktree: (?P<worktree>.+?)\. "
                          r"Branch: (?P<branch>.+?)\. Base: (?P<base>.+?)\.$", re.M)
PLAN_PATHS_RE = re.compile(r"^Plan folder: (?P<plan_dir>.+?)\. Checkout: (?P<checkout>.+?) "
                           r"\(detached worktree of (?P<base>.+?) at (?P<commit>[0-9a-f]+)\)\. Base: .+?\.$", re.M)
SCOUT_RE = re.compile(r"^Repo: (?P<repo>.+?)\. Write the draft to (?P<draft>.+?)\.$", re.M)
CWD_RE = re.compile(r"^- Working directory: (?P<cwd>.+?)\. Relative paths", re.M)
FAILED_BASH_RE = re.compile(r"\[(exit code -?\d+|timed out after [^\]]+|cancelled)\]\s*$")
READ_LINE_RE = re.compile(r"^\s*\d+\t")

# The fixture's code, as the recipes edit it.
REMOVE_ITEM = ("export function removeItem(items, index) {\n"
               "  return items.filter((_, i) => i !== index);\n"
               "}")
CLEAR_COMPLETED = ("export function clearCompleted(items) {\n"
                   "  return items.filter((item) => !item.done);\n"
                   "}")
DOC_COMMENT = "// Drop the items that are done."
REWORK_COMMENT = "// Returns a new array; the original items are left untouched."
TEST_IMPORT_OLD = 'import { addItem, toggleItem, removeItem } from "../src/todo.js";'
TEST_IMPORT_NEW = 'import { addItem, toggleItem, removeItem, clearCompleted } from "../src/todo.js";'
LAST_TEST = ('test("removeItem removes by index", () => {\n'
             '  assert.deepEqual(removeItem([{ text: "a", done: false }], 0), []);\n'
             '});')
NEW_TEST = ('test("clearCompleted drops done items", () => {\n'
            '  const items = [{ text: "a", done: true }, { text: "b", done: false }];\n'
            '  assert.deepEqual(clearCompleted(items), [{ text: "b", done: false }]);\n'
            '});')
APP_IMPORT_OLD = 'import { addItem, toggleItem, removeItem } from "./todo.js";'
APP_IMPORT_NEW = 'import { addItem, toggleItem, removeItem, clearCompleted } from "./todo.js";'
APP_ANCHOR = 'document.getElementById("add-form").addEventListener("submit", (event) => {'
APP_LISTENER = ('document.getElementById("clear-completed").addEventListener("click", () => {\n'
                '  items = clearCompleted(items);\n'
                '  render();\n'
                '});\n\n')
HTML_ANCHOR = '    <ul id="list"></ul>'
HTML_BUTTON = '    <button type="button" id="clear-completed">Clear completed</button>'
PACKAGE_OLD = '{ "type": "module", "private": true }'
PACKAGE_NEW = '{ "type": "module", "private": true, "scripts": { "test": "node --test" } }'

PLAN_JOBS = (
    "Add clearCompleted(items) to src/todo.js that drops done items, with a test in test/todo.test.js",
    'Add a "Clear completed" button to index.html that calls clearCompleted from src/app.js',
)


class ProtocolError(AssertionError):
    """The request breaks a rule a real API enforces (the mock answers it with HTTP 400)."""


@dataclass
class Scenario:
    """What the scripted agents decide, per scenario.

    reviews: lens -> verdict per round ("PASS"/"CHANGES"); rounds past the list repeat its last entry
    (default PASS). approver: decision per approver run of a job (default APPROVE). lead_reviews: the
    lead's final-review verdict per review. builder_sleep: the first builder run of a job runs
    ``sleep <seconds>`` with Bash after reading (the cancel scenario). reviewer_delay: lens -> seconds
    the first reply of its first round-1 run waits before answering; reviewer_chunk_delay: lens -> seconds
    between the SSE chunks of that reply instead (a slow stream). coder_delay: seconds the coder's first
    reply of the first message waits. echo_secret: a value the builder's final reply of round 1 includes
    (a model that leaks a key it saw). probe_env: names the builder looks for with ``env`` in round 1.
    product_assumption: an Assumptions line the spec-writer marks (product) (a decision only a person answers,
    DESIGN §7 and §32); None writes "None." there.
    """

    reviews: dict = field(default_factory=dict)
    approver: list = field(default_factory=list)
    lead_reviews: list = field(default_factory=list)
    builder_sleep: float | None = None
    reviewer_delay: dict = field(default_factory=dict)
    reviewer_chunk_delay: dict = field(default_factory=dict)
    coder_delay: float = 0.0
    echo_secret: str | None = None
    probe_env: tuple = ()
    product_assumption: str | None = None

    def verdict(self, lens: str, round_: int) -> str:
        script = self.reviews.get(lens) or ["PASS"]
        return script[min(round_, len(script)) - 1]


def agent_signatures(agents_dir: Path = AGENTS_DIR) -> dict[str, str]:
    """{agent name: the first non-empty line of its body}, which the system prompt contains verbatim."""
    found = {}
    for path in sorted(agents_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        name, body = path.stem, text
        if text.startswith("---"):
            end = text.find("\n---", 3)
            for line in text[3:end].splitlines():
                if line.startswith("name:"):
                    name = line.split(":", 1)[1].strip()
            body = text[end + 4:]
        found[name] = next(line.strip() for line in body.splitlines() if line.strip())
    return found


# ---------------------------------------------------------------- the conversation, neutral


def _text_of(content: Any, notes: bool = False) -> str:
    """The text of a message's content, without the harness's <factory-items> notes (with them: notes=True, for
    the system prompt, which never carries a note but names the marker in its rules: "as a <factory-items ...>
    block", which the note pattern would read as an unclosed note running to the end, the agent's body with it)."""
    strip = (lambda text: text) if notes else (lambda text: NOTE_RE.sub("", text))
    if isinstance(content, str):
        return strip(content)
    if isinstance(content, list):
        texts = (strip(str(block.get("text") or "")) for block in content
                 if isinstance(block, dict) and block.get("type") == "text")
        return "\n\n".join(text for text in texts if text)
    return ""


@dataclass
class Entry:
    role: str                      # user | assistant | tool
    text: str = ""
    reasoning: str | None = None
    calls: list = field(default_factory=list)      # [{id, name, input}]
    call_id: str = ""              # tool
    is_error: bool | None = None   # tool (Anthropic only)


@dataclass
class Conversation:
    fmt: str                       # openai | anthropic
    system: str
    model: str
    entries: list
    raw: list                      # the request's messages as sent

    @classmethod
    def parse(cls, body: dict, path: str) -> "Conversation":
        if path.endswith("/messages"):
            return cls._anthropic(body)
        return cls._openai(body)

    @classmethod
    def _openai(cls, body: dict) -> "Conversation":
        system, entries = "", []
        raw = [m for m in body.get("messages") or [] if isinstance(m, dict)]
        for message in raw:
            role = message.get("role")
            if role == "system":
                system = _text_of(message.get("content"), notes=True)
            elif role == "user":
                entries.append(Entry("user", _text_of(message.get("content"))))
            elif role == "assistant":
                calls = []
                for call in message.get("tool_calls") or []:
                    function = call.get("function") or {}
                    try:
                        arguments = json.loads(function.get("arguments") or "{}")
                    except ValueError:
                        arguments = {"_invalid": function.get("arguments")}
                    calls.append({"id": call.get("id"), "name": function.get("name"), "input": arguments})
                reasoning = message.get("reasoning_content", message.get("reasoning"))
                entries.append(Entry("assistant", _text_of(message.get("content")), reasoning, calls))
            elif role == "tool":
                entries.append(Entry("tool", _text_of(message.get("content")), call_id=str(message.get("tool_call_id"))))
        return cls("openai", system, str(body.get("model")), entries, raw)

    @classmethod
    def _anthropic(cls, body: dict) -> "Conversation":
        system = body.get("system")
        system = system if isinstance(system, str) else _text_of(system, notes=True)
        entries = []
        raw = [m for m in body.get("messages") or [] if isinstance(m, dict)]
        for message in raw:
            content = message.get("content")
            if message.get("role") == "user":
                if isinstance(content, str):
                    entries.append(Entry("user", _text_of(content)))
                    continue
                for block in content or []:
                    if block.get("type") == "tool_result":
                        entries.append(Entry("tool", _text_of(block.get("content")), call_id=str(block.get("tool_use_id")),
                                             is_error=bool(block.get("is_error"))))
                text = _text_of(content)
                if text:
                    entries.append(Entry("user", text))
            else:
                blocks = content if isinstance(content, list) else [{"type": "text", "text": str(content or "")}]
                thinking = [b.get("thinking") for b in blocks if b.get("type") == "thinking"]
                calls = [{"id": b.get("id"), "name": b.get("name"), "input": b.get("input") or {}}
                         for b in blocks if b.get("type") == "tool_use"]
                entries.append(Entry("assistant", _text_of(blocks), thinking[0] if thinking else None, calls))
        return cls("anthropic", str(system or ""), str(body.get("model")), entries, raw)


def plain(read_output: str) -> str:
    """The file text inside a Read result (`cat -n` style lines)."""
    lines = []
    for line in read_output.splitlines():
        if READ_LINE_RE.match(line):
            lines.append(READ_LINE_RE.sub("", line, count=1))
    return "\n".join(lines)


def call(name: str, expect: str = "ok", **arguments: Any) -> dict:
    """A tool call; expect "ok" (an error is a tool_error), "fail" or "any"."""
    return {"name": name, "arguments": arguments, "_expect": expect}


class Turn:
    """One request, from the scripted agent's point of view."""

    def __init__(self, convo: Conversation, agent: str, server: str) -> None:
        self.convo = convo
        self.agent = agent
        self.server = server
        users = [i for i, e in enumerate(convo.entries) if e.role == "user"]
        self.user_texts = [convo.entries[i].text for i in users]
        self.first = self.user_texts[0] if users else ""
        self.instruction = self.user_texts[-1] if users else ""
        last = users[-1] if users else -1
        self.turn = convo.entries[last + 1:]
        self.step = sum(1 for e in self.turn if e.role == "assistant")
        results = {e.call_id: e for e in self.turn if e.role == "tool"}
        self.steps: list[list[tuple[dict, Entry | None]]] = [
            [(c, results.get(str(c["id"]))) for c in e.calls] for e in self.turn if e.role == "assistant"]
        self.paths = self._paths()

    def _paths(self) -> dict:
        for pattern in (JOB_PATHS_RE, PLAN_PATHS_RE, SCOUT_RE):
            match = pattern.search(self.instruction) or pattern.search(self.first)
            if match:
                return match.groupdict()
        match = CWD_RE.search(self.convo.system)
        return {"cwd": match.group("cwd")} if match else {}

    def out(self, step: int, index: int = 0) -> str:
        """The output of call `index` of step `step` of this turn ("" if missing)."""
        try:
            result = self.steps[step][index][1]
        except IndexError:
            return ""
        return result.text if result is not None else ""

    def outs(self, step: int) -> list[str]:
        return [result.text if result is not None else "" for _, result in self.steps[step]]

    def lines(self) -> list[str]:
        """The instruction's lines after the paths line (without "Port: N." and the tracked-items block)."""
        rest, kept, in_block = self.instruction.splitlines()[1:], [], False
        for line in rest:
            if line.startswith(ITEMS_BLOCK) or (in_block and line.startswith(" ")):
                in_block = True
                continue
            in_block = False
            if not re.fullmatch(r"Port: \d+\.", line.strip()):
                kept.append(line)
        return kept

    def item_ids(self, label: str) -> list[str]:
        """The ids the prompt's Items line lists after label ("you own", "waiting for your check")."""
        for text in (self.instruction, self.first):
            match = re.search(rf"^Items: .*?{label}:? ([^;)]*)", text, re.M)
            if match:
                return [part.strip() for part in match.group(1).split(",") if part.strip()]
        return []


def reply(reasoning: str, *calls: dict, text: str = "") -> dict:
    return {"reasoning": reasoning, "text": text, "tool_calls": list(calls)}


def final(text: str, reasoning: str = "The file is written; report in one line.") -> dict:
    return {"reasoning": reasoning, "text": text}


def signature(thinking: str) -> str:
    return "e2e-sig:" + hashlib.sha256(thinking.encode("utf-8")).hexdigest()[:24]


# ---------------------------------------------------------------- the scripted model


class ScriptedLLM:
    """Plays every factory agent; see the module docstring."""

    def __init__(self, scenario: Scenario | None = None) -> None:
        self.scenario = scenario or Scenario()
        self.signatures = agent_signatures()
        self.lock = threading.Lock()
        self.calls: list[dict] = []
        self.violations: list[str] = []
        self.tool_errors: list[dict] = []
        self._ids = itertools.count(1)
        self._expect: dict[str, dict] = {}       # tool call id -> {agent, name, expect, input}
        self._sent_reasoning: dict[str, str] = {}  # first tool call id of a reply -> its reasoning
        self._runs: dict[tuple, int] = {}         # (agent, key) -> runs started (step-0 requests)

    def handler(self, server: str = "main"):
        """A MockLLM handler for one mock server ("main", or "local": reasoning under the `reasoning` key)."""
        def handle(body: dict, path: str) -> dict:
            return self.respond(body, path, server)
        return handle

    # -- dispatch --------------------------------------------------------------

    def identify(self, system: str) -> str:
        for name, first_line in sorted(self.signatures.items(), key=lambda item: -len(item[1])):
            if first_line in system:
                return name
        raise ProtocolError("unknown agent: no agents/*.md body in the system prompt")

    def agent_of(self, body: dict, path: str) -> str:
        """The agent a recorded request came from (for checks on MockLLM.requests)."""
        try:
            return self.identify(Conversation.parse(body, path).system)
        except ProtocolError:
            return "?"

    def respond(self, body: dict, path: str, server: str) -> dict:
        try:
            convo = Conversation.parse(body, path)
            agent = self.identify(convo.system)
            turn = Turn(convo, agent, server)
            self._check_protocol(turn, server)
            self._check_results(turn)
            key = turn.paths.get("job_dir") or turn.paths.get("plan_dir") or turn.paths.get("draft") \
                or turn.paths.get("cwd") or ""
            with self.lock:
                if turn.step == 0:
                    self._runs[(agent, key, turn.instruction)] = self._runs.get((agent, key, turn.instruction), 0) + 1
                    self._runs[(agent, key)] = self._runs.get((agent, key), 0) + 1
                runs = self._runs.get((agent, key), 0)
            play = getattr(self, "play_" + agent.replace("-", "_"), None)
            if play is None:
                play = self.play_reviewer if agent.startswith("reviewer-") else None
            if play is None:
                raise ProtocolError(f"no script for agent {agent}")
            if turn.instruction.startswith(STOP_GUARD):
                answer = final("My file answers what I could; the rest is for a human.")
            else:
                answer = play(turn, runs)
            record = {"server": server, "fmt": convo.fmt, "agent": agent, "model": convo.model, "step": turn.step,
                      "instruction": turn.instruction, "first": turn.first, "user_texts": list(turn.user_texts),
                      "messages": len(convo.raw), "key": key, "time": time.time(), "run": runs,
                      "tools": [c["name"] for c in answer.get("tool_calls") or []]}
            with self.lock:
                self.calls.append(record)
            return self._finish(answer, convo.fmt, server, agent)
        except Exception as exc:
            with self.lock:
                self.violations.append(f"{type(exc).__name__}: {exc}")
            raise

    def _finish(self, answer: dict, fmt: str, server: str, agent: str) -> dict:
        """Give the reply's tool calls our ids (and remember what we expect of them), sign its thinking."""
        out = {k: v for k, v in answer.items() if k != "tool_calls"}
        calls = []
        prefix = "toolu_e2e_" if fmt == "anthropic" else "call_e2e_"
        for item in answer.get("tool_calls") or []:
            call_id = f"{prefix}{next(self._ids)}"
            with self.lock:
                self._expect[call_id] = {"agent": agent, "name": item["name"], "expect": item.get("_expect", "ok"),
                                         "input": item["arguments"]}
            calls.append({"id": call_id, "name": item["name"], "arguments": item["arguments"]})
        if calls:
            out["tool_calls"] = calls
            with self.lock:
                self._sent_reasoning[calls[0]["id"]] = answer.get("reasoning", "")
        if fmt == "anthropic" and "reasoning" in out:
            out["signature"] = signature(out["reasoning"])
        if fmt == "openai" and server == "local":
            out["reasoning_key"] = "reasoning"
        return out

    # -- checks -------------------------------------------------------------------

    def _check_protocol(self, turn: Turn, server: str) -> None:
        convo = turn.convo
        if convo.fmt == "openai":
            key = "reasoning" if server == "local" else "reasoning_content"
            roles = [m.get("role") for m in convo.raw if m.get("role") != "system"]
            for a, b in zip(roles, roles[1:]):  # noqa: B905 (uneven by design)
                if a == b == "user":
                    raise ProtocolError("two user messages in a row")
            for index, message in enumerate(convo.raw):
                if message.get("role") != "assistant" or not message.get("tool_calls"):
                    continue
                ids = [c.get("id") for c in message["tool_calls"]]
                sent = self._sent_reasoning.get(str(ids[0]))
                if key not in message:
                    raise ProtocolError(f"assistant tool-call message without {key} (passback dropped)")
                if sent is not None and message[key] != sent:
                    raise ProtocolError(f"{key} passed back changed: {message[key]!r} != {sent!r}")
                answered = []
                for later in convo.raw[index + 1:]:
                    if later.get("role") != "tool":
                        break
                    answered.append(later.get("tool_call_id"))
                if answered != ids:
                    raise ProtocolError(f"tool results {answered} do not answer the calls {ids}")
        else:
            roles = [m.get("role") for m in convo.raw]
            if roles and roles[0] != "user":
                raise ProtocolError("the first message is not a user message")
            for a, b in zip(roles, roles[1:]):  # noqa: B905 (uneven by design)
                if a == b:
                    raise ProtocolError(f"two {a} messages in a row")
            for index, message in enumerate(convo.raw):
                if message.get("role") != "assistant":
                    continue
                blocks = message.get("content")
                if not isinstance(blocks, list) or not blocks or blocks[0].get("type") != "thinking":
                    raise ProtocolError("assistant message does not start with its thinking block")
                for block in blocks:
                    if block.get("type") == "thinking" and block.get("signature") != signature(block.get("thinking", "")):
                        raise ProtocolError("thinking block signature missing or changed")
                ids = [b.get("id") for b in blocks if b.get("type") == "tool_use"]
                if not ids:
                    continue
                following = convo.raw[index + 1] if index + 1 < len(convo.raw) else None
                answered = [b.get("tool_use_id") for b in (following or {}).get("content") or []
                            if isinstance(b, dict) and b.get("type") == "tool_result"]
                if answered != ids:
                    raise ProtocolError(f"tool results {answered} (one user message) do not answer the calls {ids}")

    def _check_results(self, turn: Turn) -> None:
        """Record tool results that failed although the agent expected success."""
        entries = turn.convo.entries
        last_assistant = max((i for i, e in enumerate(entries) if e.role == "assistant"), default=-1)
        for entry in entries[last_assistant + 1:]:
            if entry.role != "tool":
                continue
            with self.lock:
                expected = self._expect.pop(entry.call_id, None)
            if expected is None:
                continue
            failed = _failed(expected["name"], entry)
            if failed and expected["expect"] == "ok":
                with self.lock:
                    self.tool_errors.append({"agent": expected["agent"], "tool": expected["name"],
                                             "input": expected["input"], "output": entry.text[:2000]})

    # -- agents -------------------------------------------------------------------

    def play_spec_writer(self, t: Turn, runs: int) -> dict:
        p = t.paths
        wt, job = p["worktree"], p["job_dir"]
        lines = t.lines()
        feature_lines = []
        for line in lines:
            if line in (SPEC_FROM_BRANCH, SPEC_RETRY):
                break
            feature_lines.append(line)
        feature = "\n".join(feature_lines)
        feature = feature[len("Feature: "):] if feature.startswith("Feature: ") else feature
        from_branch = SPEC_FROM_BRANCH in lines
        plan_md = re.search(r"Plan context: \S+ — see (\S+/plan\.md)", feature)
        if t.step == 0:
            calls = [call("Read", file_path=f"{wt}/src/todo.js"),
                     call("Grep", pattern="export function", path=f"{wt}/src", output_mode="content"),
                     call("Glob", pattern="test/*.test.js", path=wt)]
            if plan_md:
                calls.append(call("Read", file_path=plan_md.group(1)))
            if from_branch:
                calls.append(call("Bash", command=f"cd {wt} && git log --oneline {p['base']}..{p['branch']} && "
                                                  f"git diff {p['base']}...{p['branch']}"))
            return reply("Read the code the feature touches before writing the spec.", *calls)
        if t.step == 1:
            functions = re.findall(r"export function (\w+)", t.out(0, 1))
            first = feature.splitlines()[0] if feature else "the feature"
            diff_note = ""
            if from_branch:
                diff_note = ("\nThe work is already on the branch: " + (t.out(0, len(t.steps[0]) - 1).splitlines()
                                                                         or [""])[0] + "\n")
            product = self.scenario.product_assumption
            assumptions = f"- {product} (product)" if product else "None."
            spec = (f"# {first[:60]}\n\n> Feature: {first}\n\n## Context\n`src/todo.js` exports "
                    f"{', '.join(functions) or 'nothing yet'}; tests are in `test/todo.test.js` and run with "
                    f"`node --test` (AGENTS.md).{diff_note}\n\n## Requirements\n1. {first}.\n\n"
                    "## Acceptance criteria\n1. `node --test` passes, with a test for the new behaviour.\n"
                    "2. `node --check src/todo.js src/app.js` passes.\n\n## Out of scope\nAnything the feature "
                    "does not name.\n\n## Assumptions\n" + assumptions + "\n\n## Risk\nNone.\n")
            return reply("The spec is clear; write it.", call("Write", file_path=f"{job}/spec.md", content=spec))
        return final("Wrote spec.md.")

    # builder ------------------------------------------------------------------

    def play_builder(self, t: Turn, runs: int) -> dict:
        lines = t.lines()
        head = lines[0] if lines else ""
        if head.startswith("Round ") and " reviews are in " in head:
            return self._builder_fix(t, head)
        if head.startswith("Rework: read "):
            return self._builder_rework(t, head)
        if head == COMMIT_FIX:
            return self._builder_commit_fix(t)
        if head == BUILD_MD_FIX:
            return self._builder_build_md_fix(t)
        if head.startswith(ITEMS_FIX):
            return self._builder_items_fix(t, head)
        return self._builder_build(t, runs)

    def _builder_build(self, t: Turn, runs: int) -> dict:
        p = t.paths
        wt, job = p["worktree"], p["job_dir"]
        files = ["src/todo.js", "test/todo.test.js", "index.html", "src/app.js", "package.json"]
        if t.step == 0:
            calls = [call("Bash", command=f"cd {wt} && git branch --show-current"),
                     call("Read", file_path=f"{job}/spec.md")]
            calls += [call("Read", file_path=f"{wt}/{rel}") for rel in files]
            if self.scenario.probe_env:
                pattern = "|".join(self.scenario.probe_env)
                calls.append(call("Bash", expect="any", command=f"env | grep -E '{pattern}' || echo 'no such variables'"))
            return reply("Check the branch, then read the spec and the files it touches.", *calls)
        branch_ok = t.out(0, 0).strip() == p["branch"]
        spec = plain(t.out(0, 1))
        current = {rel: plain(t.out(0, 2 + i)) for i, rel in enumerate(files)}
        recipe = _recipe(spec)
        sleep = self.scenario.builder_sleep if runs == 1 else None
        blocked = None
        if not branch_ok:
            blocked = f"git branch --show-current printed {t.out(0, 0).strip()!r}, not {p['branch']}"
        elif recipe == "button" and "export function clearCompleted" not in current["src/todo.js"]:
            blocked = "src/todo.js on this branch has no clearCompleted; the job it comes after has not merged"
        edits = [] if blocked else _recipe_edits(recipe, wt, current)
        changed = sorted({e["arguments"]["file_path"][len(wt) + 1:] for e in edits})
        script: list[dict] = []
        if sleep:
            script.append(reply("Warm up the test runner first.",
                                call("Bash", expect="any", command=f"cd {wt} && sleep {sleep}", timeout=300)))
        if blocked:
            script.append(reply("I cannot build this; say why.", call(
                "Write", file_path=f"{job}/build.md",
                content=f"STATUS: BLOCKED\n\n# Build\n\n## Blocked\n{blocked}\n")))
            script.append(final("Blocked: " + blocked))
        else:
            check = "node --check src/app.js && node --test" if recipe == "button" else "node --test"
            message = {"helper": "Add clearCompleted", "button": "Add a Clear completed button",
                       "package": "Add an npm test script"}[recipe]
            script += [
                reply(f"Apply the {recipe} change.", *edits),
                reply("Run the repo's tests.", call("Bash", command=f"cd {wt} && {check}")),
                reply("Commit with explicit paths, then check the tree is clean.",
                      call("Bash", command=f"cd {wt} && git add {' '.join(changed)} && "
                                           f"git commit -q -m '{message}' && git log --oneline -1"),
                      call("Bash", command=f"cd {wt} && git status --porcelain")),
                reply("Write build.md.", call("Write", file_path=f"{job}/build.md", content=_with_items(_build_md(
                    spec, changed, check, [("Round 1", f"{message} ({', '.join(changed)}); {check} passes.")]),
                    t.item_ids("you own"), check))),
            ]
            text = f"Built round 1: {message.lower()}; STATUS: READY."
            if self.scenario.echo_secret:
                text += f" (debug: key {self.scenario.echo_secret})"
            script.append(final(text))
        return _pick(script, t.step - 1)

    def _builder_fix(self, t: Turn, head: str) -> dict:
        p = t.paths
        wt, job = p["worktree"], p["job_dir"]
        folder = head.split(" reviews are in ", 1)[1].split("/. ", 1)[0].rstrip("/")
        next_round = int(re.search(r"for round (\d+)\.", head).group(1))
        if t.step == 0:
            return reply("Resumed after a review round: list the reviews and re-read my notes.",
                         call("Glob", pattern="review-*.md", path=folder),
                         call("Read", file_path=f"{job}/build.md"),
                         call("Read", file_path=f"{wt}/src/todo.js"))
        reviews = [line for line in t.out(0, 0).splitlines() if line.endswith(".md")]
        if t.step == 1:
            return reply("Read every review.", *[call("Read", file_path=path) for path in reviews])
        texts = {Path(path).stem[len("review-"):]: plain(out)
                 for path, out in zip(reviews, t.outs(1))}  # noqa: B905
        changes = {lens: text for lens, text in texts.items() if text.startswith("VERDICT: CHANGES")}
        todo = plain(t.out(0, 2))
        edits = []
        if any("doc comment" in text for text in changes.values()) and DOC_COMMENT not in todo:
            edits.append(call("Edit", file_path=f"{wt}/src/todo.js", old_string="export function clearCompleted(items) {",
                              new_string=f"{DOC_COMMENT}\nexport function clearCompleted(items) {{"))
        elif changes:
            edits.append(call("Edit", file_path=f"{wt}/src/todo.js", old_string=REMOVE_ITEM,
                              new_string=f"{REMOVE_ITEM}\n// Addressed round {next_round - 1} reviews."))
        build_md = plain(t.out(0, 1))
        log = (f"Round {next_round - 1} CHANGES from {', '.join(sorted(changes)) or 'nobody'}: "
               f"added the doc comment the code review asked for; node --test passes.")
        script = [
            reply("Address every CHANGES finding.", *edits),
            reply("Re-run the tests.", call("Bash", command=f"cd {wt} && node --test")),
            reply("Commit the fix.", call("Bash", command=f"cd {wt} && git add src/todo.js && "
                                                       f"git commit -q -m 'Address round {next_round - 1} reviews'"),
                  call("Bash", command=f"cd {wt} && git status --porcelain")),
            reply("Update build.md, keeping its Log.", call("Write", file_path=f"{job}/build.md", content=_with_items(
                _append_log(build_md, f"Round {next_round}", log), t.item_ids("you own"), "node --test"))),
            final(f"Round {next_round}: addressed the review findings; STATUS: READY."),
        ]
        return _pick(script, t.step - 2)

    def _builder_rework(self, t: Turn, head: str) -> dict:
        p = t.paths
        wt, job = p["worktree"], p["job_dir"]
        note_path = head[len("Rework: read "):].split(" and address it", 1)[0]
        next_round = int(re.search(r"for round (\d+)\.", head).group(1))
        if t.step == 0:
            return reply("A human asked for rework: read the note, my notes and the code.",
                         call("Read", file_path=note_path), call("Read", file_path=f"{job}/build.md"),
                         call("Read", file_path=f"{wt}/src/todo.js"))
        note, build_md, todo = plain(t.out(0, 0)), plain(t.out(0, 1)), plain(t.out(0, 2))
        if "comment" in note.lower() and REWORK_COMMENT not in todo:
            anchor = "export function clearCompleted(items) {"
            if DOC_COMMENT in todo:
                anchor = f"{DOC_COMMENT}\n{anchor}"
            edit = call("Edit", file_path=f"{wt}/src/todo.js", old_string=anchor,
                        new_string=f"{REWORK_COMMENT}\n{anchor}")
        else:
            edit = call("Edit", file_path=f"{wt}/src/todo.js", old_string=REMOVE_ITEM,
                        new_string=f"{REMOVE_ITEM}\n// Rework: {note.splitlines()[0][:60] if note else 'done'}")
        script = [
            reply("Make the change the note asks for.", edit),
            reply("Re-run the tests.", call("Bash", command=f"cd {wt} && node --test")),
            reply("Commit the rework.", call("Bash", command=f"cd {wt} && git add src/todo.js && "
                                                          "git commit -q -m 'Rework: document clearCompleted'"),
                  call("Bash", command=f"cd {wt} && git status --porcelain")),
            reply("Update build.md.", call("Write", file_path=f"{job}/build.md", content=_with_items(_append_log(
                build_md, f"Round {next_round}", f"Rework ({Path(note_path).name}): {note.splitlines()[0] if note else ''}"),
                t.item_ids("you own"), "node --test"))),
            final(f"Rework done for round {next_round}; STATUS: READY."),
        ]
        return _pick(script, t.step - 1)

    def _builder_commit_fix(self, t: Turn) -> dict:
        p = t.paths
        wt, job = p["worktree"], p["job_dir"]
        script = [
            reply("Commit what is left.", call("Bash", expect="any", command=f"cd {wt} && git add -u && "
                                                                       "git commit -q -m 'Commit leftovers'; git status --porcelain"),
                  call("Read", file_path=f"{job}/build.md")),
            reply("build.md stays as it is.", call("Write", file_path=f"{job}/build.md", content=plain(t.out(0, 1))
                                                    if t.steps else "STATUS: READY\n")),
            final("Committed the leftovers."),
        ]
        return script[min(t.step, len(script) - 1)]

    def _builder_items_fix(self, t: Turn, head: str) -> dict:
        job = t.paths["job_dir"]
        ids = [part.strip() for part in head[len(ITEMS_FIX):].split(". ", 1)[0].split(",") if part.strip()]
        if t.step == 0:
            return reply("Re-read build.md to answer the items.", call("Read", file_path=f"{job}/build.md"))
        script = [reply("Answer every item under ## Items.", call("Write", file_path=f"{job}/build.md", content=_with_items(
                      plain(t.out(0, 0)), ids, "node --test"))),
                  final("Answered the items in build.md.")]
        return script[min(t.step - 1, len(script) - 1)]

    def _builder_build_md_fix(self, t: Turn) -> dict:
        job = t.paths["job_dir"]
        script = [reply("Rewrite build.md with a status line.", call("Write", file_path=f"{job}/build.md",
                                                                      content="STATUS: READY\n\n# Build\n")),
                  final("Fixed build.md.")]
        return script[min(t.step, len(script) - 1)]

    # reviewers ------------------------------------------------------------------

    def play_reviewer(self, t: Turn, runs: int) -> dict:
        lens = t.agent[len("reviewer-"):]
        p = t.paths
        wt, job = p["worktree"], p["job_dir"]
        n = int(re.search(r"Review round (\d+)\.", t.instruction).group(1))
        if t.step == 0:
            calls = [call("Bash", command=f"cd {wt} && git log --oneline {p['base']}..{p['branch']} && "
                                          f"git diff {p['base']}...{p['branch']}"),
                     call("Read", file_path=f"{job}/spec.md"),
                     call("Read", file_path=f"{job}/build.md")]
            if lens == "code":
                calls.append(call("Bash", expect="any", command=f"cd {wt} && node --test"))
            if n > 1:
                calls.append(call("Read", file_path=f"{job}/round-{n - 1}/review-{lens}.md"))
            answer = reply(f"Review round {n} through the {lens} lens: read the change, spec and build notes.", *calls)
            first_run = self._runs.get((t.agent, job, t.instruction), 0) == 1
            if n == 1 and first_run and lens in self.scenario.reviewer_delay:
                answer["delay"] = self.scenario.reviewer_delay[lens]
            if n == 1 and first_run and lens in self.scenario.reviewer_chunk_delay:
                answer["chunk_delay"] = self.scenario.reviewer_chunk_delay[lens]
                answer["reasoning"] = answer["reasoning"] * 4
            return answer
        if t.step == 1:
            diff = t.out(0, 0)
            verdict = self.scenario.verdict(lens, n)
            findings = "None."
            checks = "node --test: not run (not this lens)"
            if lens == "code":
                tests_ok = not FAILED_BASH_RE.search(t.out(0, 3))
                checks = f"node --test: {'pass' if tests_ok else 'fail'}"
                if not tests_ok:
                    verdict = "CHANGES"
            if n > 1 and lens == "code":
                previous = plain(t.out(0, len(t.steps[0]) - 1))
                if "doc comment" in previous and DOC_COMMENT not in diff:
                    verdict = "CHANGES"
            if verdict == "CHANGES":
                findings = ("1. src/todo.js: clearCompleted has no doc comment. Add a one-line comment above it "
                            "that says what it does.")
            changed = len([line for line in diff.splitlines() if line.startswith(("+", "-"))
                           and not line.startswith(("+++", "---"))])
            text = (f"VERDICT: {verdict}\n\n## Checks\n{checks}\n\n## Findings\n{findings}\n\n## Notes\n"
                    f"Round {n}, {lens} lens: read {changed} changed lines of {p['branch']}.\n")
            checking = t.item_ids("waiting for your check")
            if checking:
                answer = ("verified: re-read the diff; the doc comment is there" if verdict == "PASS"
                          else "not met: the doc comment is still missing")
                text += "\n## Items\n" + "".join(f"- {item} {answer}\n" for item in checking)
            return reply("Write the review.", call("Write", file_path=f"{job}/round-{n}/review-{lens}.md", content=text))
        verdict = re.search(r"VERDICT: (\w+)", json.dumps(t.steps[1][0][0]["input"])) if t.steps[1:] else None
        return final(f"Wrote review-{lens}.md: {verdict.group(1) if verdict else '?'}.")

    # approver -------------------------------------------------------------------

    def play_approver(self, t: Turn, runs: int) -> dict:
        p = t.paths
        wt, job = p["worktree"], p["job_dir"]
        n = int(re.search(r"Final round: (\d+)\.", t.instruction).group(1))
        if t.step == 0:
            return reply("Read the job folder and the change before deciding.",
                         call("Read", file_path=f"{job}/feature.md"), call("Read", file_path=f"{job}/spec.md"),
                         call("Read", file_path=f"{job}/build.md"),
                         call("Glob", pattern="round-*/review-*.md", path=job),
                         call("Bash", command=f"cd {wt} && git log --oneline {p['base']}..{p['branch']} && "
                                              f"git diff --stat {p['base']}...{p['branch']}"))
        finals = [line for line in t.out(0, 3).splitlines() if f"/round-{n}/review-" in line]
        if t.step == 1:
            return reply("Read the final round's reviews and spot-check the tests.",
                         *[call("Read", file_path=path) for path in finals],
                         call("Bash", command=f"cd {wt} && node --test"))
        if t.step == 2:
            outs = t.outs(1)
            reviews_pass = bool(finals) and all(plain(out).startswith("VERDICT: PASS") for out in outs[:-1])
            script = self.scenario.approver or ["APPROVE"]
            decision = script[min(runs, len(script)) - 1]
            if not reviews_pass:
                decision = "ESCALATE"
            criteria = ("## Acceptance criteria\n1. node --test passes: met, ran it.\n"
                        "2. node --check src/todo.js src/app.js passes: met, ran it.\n")
            if decision == "APPROVE":
                text = ("APPROVE\n\n## Why\nEvery final-round review passes and the tests pass; the change does "
                        f"what the feature asks.\n\n{criteria}")
            else:
                text = ("ESCALATE\n\n## Why\nThe spec leaves open whether clearing should be undoable; a human "
                        f"should decide.\n\n{criteria}\n"
                        "## For the human\nDecide whether clearCompleted needs an undo. Files: src/todo.js.\n")
            checking = t.item_ids("waiting for your check")
            if checking:
                text += "\n## Items\n" + "".join(f"- {item} verified: ran `node --test`, it passes\n"
                                                  for item in checking)
            return reply("Decide.", call("Write", file_path=f"{job}/decision.md", content=text))
        decision = re.search(r"(APPROVE|ESCALATE)", json.dumps(t.steps[2][0][0]["input"])) if len(t.steps) > 2 else None
        return final(f"Wrote decision.md: {decision.group(1) if decision else '?'}.")

    # lead -----------------------------------------------------------------------

    def play_lead(self, t: Turn, runs: int) -> dict:
        p = t.paths
        plan_dir, checkout = p["plan_dir"], p["checkout"]
        task = next((line for line in t.lines() if line.startswith("Task: ")), "")
        if task.startswith("Task: Plan."):
            target = re.search(r"and write (\S+)\.$", task).group(1)
            if t.step == 0:
                return reply("Read the request and the code it touches.",
                             call("Read", file_path=f"{plan_dir}/request.md"),
                             call("Read", file_path=f"{checkout}/src/todo.js"),
                             call("Read", file_path=f"{checkout}/index.html"),
                             call("Bash", command=f"cd {checkout} && git log --oneline -3"))
            if t.step == 1:
                text = (f"PLAN: READY\n\n## Jobs\n1. {PLAN_JOBS[0]}\n2. {PLAN_JOBS[1]} (after: 1)\n\n## Context\n"
                        "The helper lands first so the button can call it. Names: clearCompleted(items) returns "
                        "the items that are not done.\n\n## Risks\nNone.\n")
                return reply("Two thin slices: the helper, then the button that uses it.",
                             call("Write", file_path=target, content=text))
            return final("Wrote plan.md with 2 jobs.")
        match = re.search(r"Task: Final review (\d+)\. .* Write (\S+)\.$", task)
        k, target = int(match.group(1)), match.group(2)
        diff = re.search(r"`git diff (\S+)`", t.instruction).group(1)
        if t.step == 0:
            return reply("Review the combined change against the request.",
                         call("Read", file_path=f"{plan_dir}/request.md"),
                         call("Read", file_path=f"{plan_dir}/plan.md"),
                         call("Bash", command=f"cd {checkout} && git diff --stat {diff} && node --test"))
        if t.step == 1:
            script = self.scenario.lead_reviews or ["PASS"]
            verdict = script[min(k, len(script)) - 1]
            findings = ("None." if verdict == "PASS" else
                        "1. Add a test for clearCompleted on an empty list in test/todo.test.js: the empty case "
                        "is untested")
            return reply("Write the final review.", call("Write", file_path=target, content=(
                f"VERDICT: {verdict}\n\n## Findings\n{findings}\n\n## Notes\nThe helper and the button fit together.\n")))
        return final(f"Wrote review-{k}.md.")

    # coder (Chat) ---------------------------------------------------------------

    def play_coder(self, t: Turn, runs: int) -> dict:
        cwd = t.paths["cwd"]
        if "commit" in t.instruction.lower():
            script = [
                reply("Check what is uncommitted.", call("Bash", command="git status --porcelain")),
                reply("Commit with explicit paths.", call("Bash", command=(
                    "git add src/todo.js test/todo.test.js && git commit -q -m 'Add clearCompleted helper' "
                    "&& git log --oneline -1"))),
                final("Committed: Add clearCompleted helper. The tree is clean."),
            ]
            return script[min(t.step, len(script) - 1)]
        if t.step == 0:
            answer = reply("Look at the code first.", call("Read", file_path=f"{cwd}/src/todo.js"),
                           call("Read", file_path=f"{cwd}/test/todo.test.js"))
            if self.scenario.coder_delay and len(t.user_texts) == 1:
                answer["delay"] = self.scenario.coder_delay
            return answer
        current = {"src/todo.js": plain(t.out(0, 0)), "test/todo.test.js": plain(t.out(0, 1))}
        script = [
            reply("Add the helper and its test.", *_recipe_edits("helper", cwd, current)),
            reply("Run the tests.", call("Bash", command="node --test")),
            final("Added clearCompleted(items) to src/todo.js with a test; node --test passes. "
                  "Say the word and I'll commit it."),
        ]
        return _pick(script, t.step - 1)

    # repo-scout -----------------------------------------------------------------

    def play_repo_scout(self, t: Turn, runs: int) -> dict:
        repo, draft = t.paths["repo"], t.paths["draft"]
        if t.step == 0:
            return reply("Inspect the manifests, layout and history.",
                         call("Read", file_path=f"{repo}/package.json"),
                         call("Glob", pattern="**/*.js", path=repo),
                         call("Bash", command=f"cd {repo} && git log --oneline -5 && node --version"))
        if t.step == 1:
            files = [Path(line).relative_to(repo).as_posix() for line in t.out(0, 1).splitlines()
                     if line.startswith(repo)]
            text = ("# AGENTS.md\n\n## Overview\nA tiny todo app in plain ES modules: "
                    f"{', '.join(sorted(files))}.\n\n## Setup for a fresh checkout\nNone.\n\n## Dev server\n"
                    "`python3 -m http.server <port> --bind 127.0.0.1`\n\n## Validation\n- Tests: `node --test`\n\n"
                    "## Conventions\n- ES modules, 2-space indent, double quotes.\n\n## Styling\n`styles.css`.\n\n"
                    "## Hot spots\nNone.\n")
            return reply("Write the draft.", call("Write", file_path=draft, content=text))
        return final("Wrote the AGENTS.md draft.")

    # -- queries for tests ----------------------------------------------------------

    def of(self, agent: str, **match: Any) -> list[dict]:
        """Recorded calls of agent whose fields equal match (key may be a substring test via key_in=...)."""
        with self.lock:
            found = [dict(c) for c in self.calls if c["agent"] == agent]
        key_in = match.pop("key_in", None)
        if key_in is not None:
            found = [c for c in found if key_in in c["key"]]
        return [c for c in found if all(c.get(k) == v for k, v in match.items())]


def _pick(script: list[dict], index: int) -> dict:
    """Step `index` of a script whose tool steps with nothing to call are dropped (the last entry is the
    final reply, kept even without calls)."""
    steps = [answer for answer in script[:-1] if answer.get("tool_calls")] + script[-1:]
    return steps[min(index, len(steps) - 1)]


def _failed(name: str, entry: Entry) -> bool:
    if entry.is_error is not None:
        return entry.is_error
    text = entry.text
    if name == "Bash":
        return bool(FAILED_BASH_RE.search(text))
    if name == "Read":
        return not (READ_LINE_RE.match(text) or text.startswith("(file has"))
    if name == "Write":
        return not text.startswith("Wrote ")
    if name == "Edit":
        return not text.startswith("Edited ")
    if name == "Glob":
        return not all(line.startswith("/") for line in text.splitlines()) and text != "(no matches)"
    return text.startswith(("Access denied", "Path not found", "Invalid", "Not a directory", "Unknown tool"))


def _recipe(spec: str) -> str:
    feature = next((line for line in spec.splitlines() if line.startswith("> Feature:")), spec)
    if "package.json" in feature:
        return "package"
    if "button" in feature.lower():
        return "button"
    return "helper"


def _recipe_edits(recipe: str, root: str, current: dict) -> list[dict]:
    """Edit calls for recipe against the files' current text (nothing for what is already there)."""
    edits = []
    if recipe == "helper":
        if "export function clearCompleted" not in current["src/todo.js"]:
            edits.append(call("Edit", file_path=f"{root}/src/todo.js", old_string=REMOVE_ITEM,
                              new_string=f"{REMOVE_ITEM}\n\n{CLEAR_COMPLETED}"))
        if "clearCompleted" not in current["test/todo.test.js"]:
            edits.append(call("Edit", file_path=f"{root}/test/todo.test.js", old_string=TEST_IMPORT_OLD,
                              new_string=TEST_IMPORT_NEW))
            edits.append(call("Edit", file_path=f"{root}/test/todo.test.js", old_string=LAST_TEST,
                              new_string=f"{LAST_TEST}\n\n{NEW_TEST}"))
    elif recipe == "button":
        edits.append(call("Edit", file_path=f"{root}/index.html", old_string=HTML_ANCHOR,
                          new_string=f"{HTML_ANCHOR}\n{HTML_BUTTON}"))
        edits.append(call("Edit", file_path=f"{root}/src/app.js", old_string=APP_IMPORT_OLD, new_string=APP_IMPORT_NEW))
        edits.append(call("Edit", file_path=f"{root}/src/app.js", old_string=APP_ANCHOR,
                          new_string=f"{APP_LISTENER}{APP_ANCHOR}"))
    else:
        edits.append(call("Edit", file_path=f"{root}/package.json", old_string=PACKAGE_OLD, new_string=PACKAGE_NEW))
    return edits


def _build_md(spec: str, changed: list[str], check: str, log: list[tuple[str, str]]) -> str:
    title = next((line[2:] for line in spec.splitlines() if line.startswith("# ")), "the feature")
    entries = "\n\n".join(f"### {name}\n{text}" for name, text in log)
    return (f"STATUS: READY\n\n# Build: {title}\n\n## Summary\nChanged {', '.join(changed)}.\n\n"
            f"## Acceptance criteria\n1. [x] `{check}` passes: ran it, exit 0.\n\n## Validation\n- `{check}`: pass\n\n"
            f"## Log\n{entries}\n")


def _with_items(build_md: str, ids: list[str], check: str) -> str:
    """build.md with its ## Items section (replaced, before the Log) answering every id done, `check` as evidence."""
    lines = build_md.splitlines()
    kept, skipping = [], False
    for line in lines:
        if line.strip().lower() == "## items":
            skipping = True
            continue
        if skipping and line.startswith("## "):
            skipping = False
        if not skipping:
            kept.append(line)
    body = "\n".join(kept).rstrip("\n") + "\n"
    if not ids:
        return body
    section = "## Items\n" + "".join(f"- {item} done: `{check}` passes\n" for item in ids) + "\n"
    if "\n## Log\n" in body:
        head, _, tail = body.partition("\n## Log\n")
        return f"{head.rstrip()}\n\n{section}## Log\n{tail}"
    return f"{body.rstrip()}\n\n{section}"


def _append_log(build_md: str, name: str, text: str) -> str:
    body = build_md if build_md.startswith("STATUS: ") else "STATUS: READY\n\n## Log\n"
    if "## Log" not in body:
        body = body.rstrip("\n") + "\n\n## Log\n"
    return body.rstrip("\n") + f"\n\n### {name}\n{text}\n"
