"""Unit checks for the end-to-end test helpers (not gated): the scripted model (tests/e2e/scripted_llm.py) and the
GUI's real-backend launcher (tests/e2e/gui_backend.py, driven by tests/gui/e2e_real.mjs).

The gated suites (tests/e2e/test_e2e_server.py, tests/gui/e2e_real.mjs) only run with FACTORY_E2E=1, so a helper
that stops recognising the harness's own prompts fails them all without any ungated test noticing. These run with
the unit suite.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

from swfactory.harness.loop import ITEMS_PREAMBLE
from swfactory.settings import PLAIN_PROMPT, TOOL_PROMPT
from tests.e2e.gui_backend import plain_handler
from tests.e2e.scripted_llm import Conversation, ProtocolError, Scenario, ScriptedLLM, agent_signatures

ROOT = Path(__file__).resolve().parent.parent
JOB = "/srv/data/jobs/todo/001-x"
WORKTREE = "/srv/data/worktrees/todo/001-x"
PATHS = f"Job folder: {JOB}. Worktree: {WORKTREE}. Branch: factory/001-x. Base: main."
FEATURE = "Feature: Add clearCompleted(items) to src/todo.js"
OPENAI = "/v1/chat/completions"


def spec_writer_system() -> str:
    """A system prompt shaped like the builtin harness's (loop.build_system_prompt): its rules, the tracked-items
    rule that names the <factory-items ...> marker, then the agent's body."""
    body = (ROOT / "agents" / "spec-writer.md").read_text(encoding="utf-8").split("\n---", 1)[1]
    return "# Harness\n\nYou are a non-interactive coding agent.\n\n" + ITEMS_PREAMBLE + "\n\n" + body.strip() + "\n"


class ScriptedModelReadsTheHarnessPrompts(unittest.TestCase):
    def test_a_system_prompt_naming_the_note_marker_keeps_the_agent_body(self) -> None:
        """The harness's tracked-items rule says "as a <factory-items ...> block": read as an unclosed note it would
        swallow the rest of the system prompt, the agent's body with it, and no agent would be recognised (every
        gated server scenario then fails at its spec-writer)."""
        system = spec_writer_system()
        self.assertIn("<factory-items ...>", system)
        scripted = ScriptedLLM()
        openai = {"model": "m", "messages": [{"role": "system", "content": system},
                                             {"role": "user", "content": PATHS + "\n" + FEATURE}]}
        self.assertEqual(scripted.agent_of(openai, OPENAI), "spec-writer")
        blocks = {"model": "m", "system": [{"type": "text", "text": system}],
                  "messages": [{"role": "user", "content": PATHS + "\n" + FEATURE}]}
        self.assertEqual(scripted.agent_of(blocks, "/v1/messages"), "spec-writer")

    def test_a_note_at_the_end_of_a_message_is_still_read_past(self) -> None:
        note = '\n\n<factory-items job="todo/001-x" v="3">\n- I-1 To do: node --test passes\n</factory-items>'
        convo = Conversation.parse({"model": "m", "messages": [
            {"role": "system", "content": spec_writer_system()},
            {"role": "user", "content": PATHS + "\n" + FEATURE + note}]}, OPENAI)
        self.assertEqual(convo.entries[0].text, PATHS + "\n" + FEATURE)
        self.assertIn("You write the spec for one factory job.", convo.system)

    def test_the_spec_writer_marks_the_scenarios_product_assumption(self) -> None:
        line = "Clearing completed items cannot be undone"
        scripted = ScriptedLLM(Scenario(product_assumption=line))
        messages = [{"role": "system", "content": spec_writer_system()}, {"role": "user", "content": PATHS + "\n" + FEATURE}]
        first = scripted.respond({"model": "m", "messages": messages}, OPENAI, "main")
        calls = first["tool_calls"]
        messages.append({"role": "assistant", "content": "", "reasoning_content": first["reasoning"],
                         "tool_calls": [{"id": c["id"], "type": "function", "function": {
                             "name": c["name"], "arguments": json.dumps(c["arguments"])}} for c in calls]})
        outputs = {"Read": "     1\texport function addItem(items, text) {\n", "Grep": "src/todo.js:1:export function addItem",
                   "Glob": f"{WORKTREE}/test/todo.test.js"}
        for c in calls:
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": outputs.get(c["name"], "")})
        second = scripted.respond({"model": "m", "messages": messages}, OPENAI, "main")
        write = second["tool_calls"][0]
        self.assertEqual((write["name"], write["arguments"]["file_path"]), ("Write", f"{JOB}/spec.md"))
        self.assertIn(f"## Assumptions\n- {line} (product)\n", write["arguments"]["content"])
        self.assertEqual(scripted.violations, [])
        plain = ScriptedLLM().respond({"model": "m", "messages": messages}, OPENAI, "main")
        self.assertIn("## Assumptions\nNone.\n", plain["tool_calls"][0]["arguments"]["content"])


class GuiBackendModel(unittest.TestCase):
    """tests/e2e/gui_backend.py's local model: the scripted agents, plus a plain answer to a model Test's probes."""

    def setUp(self) -> None:
        self.scripted = ScriptedLLM()
        self.handle = plain_handler(self.scripted, "local")

    def test_a_model_tests_two_probes_get_plain_answers(self) -> None:
        plain = self.handle({"model": "m", "messages": [{"role": "user", "content": PLAIN_PROMPT}]}, OPENAI)
        self.assertEqual((plain["text"], plain["reasoning_key"]), ("OK", "reasoning"))
        tool = {"type": "function", "function": {"name": "get_time", "description": "", "parameters": {}}}
        called = self.handle({"model": "m", "tools": [tool], "messages": [{"role": "user", "content": TOOL_PROMPT}]}, OPENAI)
        self.assertEqual([c["name"] for c in called["tool_calls"]], ["get_time"])
        self.assertEqual(self.scripted.violations, [])

    def test_an_agent_goes_to_the_scripted_model_and_anything_else_is_a_violation(self) -> None:
        answer = self.handle({"model": "m", "messages": [{"role": "system", "content": spec_writer_system()},
                                                         {"role": "user", "content": PATHS + "\n" + FEATURE}]}, OPENAI)
        self.assertEqual({c["name"] for c in answer["tool_calls"]}, {"Read", "Grep", "Glob"})
        with self.assertRaises(ProtocolError):
            self.handle({"model": "m", "messages": [{"role": "user", "content": "Reply with OK, please"}]}, OPENAI)
        self.assertEqual(len(self.scripted.violations), 1)

    def test_every_agent_file_has_a_signature_the_model_can_find(self) -> None:
        signatures = agent_signatures()
        for name in ("spec-writer", "builder", "reviewer-security", "reviewer-ux", "reviewer-ui", "reviewer-code",
                     "approver", "lead", "coder", "repo-scout"):
            self.assertTrue(signatures.get(name), name)


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class RealBackendGuiTestIsGated(unittest.TestCase):
    def test_without_factory_e2e_it_says_so_and_passes(self) -> None:
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/nonexistent"}
        done = subprocess.run(["node", str(ROOT / "tests" / "gui" / "e2e_real.mjs")], capture_output=True, text=True,
                              env=env, timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("set FACTORY_E2E=1", done.stdout)


if __name__ == "__main__":
    unittest.main()
