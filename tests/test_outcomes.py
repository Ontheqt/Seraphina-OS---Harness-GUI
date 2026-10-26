"""Tests for the outcome rule (DESIGN.md section 40, swfactory/outcomes.py): how an answer reads, what a record of
the other side's answer keeps, and that the section names exactly the calls the code retries by itself."""

from __future__ import annotations

import hashlib
import re
import unittest
from pathlib import Path

from swfactory import outcomes

ROOT = Path(__file__).resolve().parent.parent
DESIGN = ROOT / "DESIGN.md"
# where each call on outcomes.RETRIED is tried again by itself
RETRY_SITES = {"model call": "swfactory/harness/providers.py", "drift source read": "swfactory/driftsources.py",
               "ntfy subscription": "swfactory/channels.py", "Telegram getUpdates": "swfactory/channels.py",
               "Telegram bot menu": "swfactory/channels.py", "event-source snippet": "swfactory/notify.py",
               "SnapRAID daemon reads": "swfactory/snapraidd.py"}


class OutcomeTest(unittest.TestCase):
    def test_an_answer_reads_as_one_outcome(self) -> None:
        for status, wanted in ((200, "succeeded"), (201, "succeeded"), (204, "succeeded"), (400, "failed"),
                               (401, "failed"), (409, "failed"), (429, "failed"), (500, "unknown"), (502, "unknown"),
                               (503, "unknown"), (504, "unknown"), (302, "unknown"), (101, "unknown")):
            with self.subTest(status=status):
                self.assertEqual(outcomes.of_status(status), wanted)
        self.assertEqual(outcomes.of_status(None), "unknown", "no answer after sending: it may have arrived")
        self.assertEqual(outcomes.of_status(None, sent=False), "failed", "it never left the harness")
        self.assertEqual(outcomes.STATES, ("succeeded", "failed", "unknown"))
        self.assertEqual(outcomes.UNKNOWN_TITLE, "Outcome unknown: check before retrying")

    def test_a_response_record_keeps_what_the_other_side_answered(self) -> None:
        body = b'{"id": "abc"}'
        self.assertEqual(outcomes.response(201, ["abc", None, "", 7], body),
                         {"status": 201, "ids": ["abc", "7"], "body_sha256": hashlib.sha256(body).hexdigest()})
        self.assertEqual(outcomes.response(0, [], "text")["body_sha256"], hashlib.sha256(b"text").hexdigest())
        self.assertIsNone(outcomes.response(204, [], b"")["body_sha256"], "no body: no hash")
        many = outcomes.response(200, [f"id-{n}" for n in range(50)])
        self.assertEqual(len(many["ids"]), outcomes.IDS_KEPT)

    def test_the_section_names_exactly_the_calls_retried_by_themselves(self) -> None:
        text = DESIGN.read_text(encoding="utf-8")
        start = text.index("## 40. Outcomes: an unknown outcome is never replayed (added)")
        table = text[text.index("### The calls retried by themselves", start):text.index("### The audit", start)]
        named = {match.group(1).strip() for match in re.finditer(r"^\| ([^|]+?) \| [^-]", table, re.MULTILINE)}
        named.discard("call")
        self.assertEqual(named, set(outcomes.RETRIED))
        audit = text[text.index("### The audit", start):]
        self.assertIn("| the builtin agent loop's tool calls", audit)
        self.assertIn("| Open WebUI", audit)
        for call in ("merge into base", "push base", "ntfy publish", "Telegram sendMessage", "MCP tool calls",
                     "custody lease requests", "the memory outbox", "wake decisions", "CLI drivers"):
            self.assertIn(f"| {call}", audit)


    def test_the_code_marks_every_call_it_retries_by_itself(self) -> None:
        """Every RETRIED key is marked with outcomes.retried() where the code tries it again, and every mark names
        a key: the list and the code cannot drift apart."""
        marks: dict[str, set[str]] = {}
        for path in sorted((ROOT / "swfactory").rglob("*.py")):
            for match in re.finditer(r"\bretried\(\"([^\"]+)\"\)", path.read_text(encoding="utf-8")):
                marks.setdefault(match.group(1), set()).add(str(path.relative_to(ROOT)))
        self.assertEqual(set(marks), set(outcomes.RETRIED))
        self.assertEqual(set(RETRY_SITES), set(outcomes.RETRIED))
        for call, where in RETRY_SITES.items():
            self.assertIn(where, marks[call], call)
        self.assertEqual(outcomes.retried("model call"), "model call")
        with self.assertRaises(KeyError):
            outcomes.retried("ntfy publish")   # a notice is never sent twice by itself


if __name__ == "__main__":
    unittest.main()
