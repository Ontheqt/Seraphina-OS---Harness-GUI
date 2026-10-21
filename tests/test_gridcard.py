"""Tests for swfactory.gridcard: the Factory Grid Card built from the real pipeline, and its receipt."""

from __future__ import annotations

import hashlib
import unittest

from swfactory.config import PipelineConfig
from swfactory.gridcard import HERE, TITLE, factory_grid, grid_sha256, receipt

LINE = ["spec-writer", "builder", "reviewer-security", "reviewer-ux", "reviewer-ui", "reviewer-code", "approver"]


class GridCardTest(unittest.TestCase):
    def test_the_line_from_the_default_pipeline(self) -> None:
        card = factory_grid(PipelineConfig())
        self.assertTrue(card.startswith(TITLE + "\n"))
        self.assertFalse(card.endswith("\n"), "the prompt carries exactly these bytes")
        order = ["[1] feature", "[2] spec-writer", "[3] builder",
                 "[4] reviewers, in parallel: reviewer-security, reviewer-ux, reviewer-ui, reviewer-code",
                 "WAIT FOR ALL → [5]", "[5] round verdicts (code)", "every review VERDICT: PASS → [6]",
                 "any VERDICT: CHANGES → resume [3] builder with the reviews (at most 2 resumes)",
                 "VERDICT: CHANGES with no resumes left → [H]", "[6] approver", "APPROVE → [7]",
                 "ESCALATE → [H]", "[7] gate (code, no model)", "stops → [H]", "[H] needs-human",
                 "rework → [3] builder"]
        positions = [card.index(marker) for marker in order]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("STATUS: BLOCKED → [H]", card)
        self.assertNotIn(HERE, card, "no agent: nothing is marked")
        self.assertNotIn("You are", card)

    def test_each_agent_is_marked_where_it_works(self) -> None:
        pipeline = PipelineConfig()
        for name in LINE:
            with self.subTest(agent=name):
                card = factory_grid(pipeline, name)
                self.assertEqual(card.count(HERE), 1)
                marked = next(line for line in card.splitlines() if HERE in line)
                module = {"spec-writer": "[2]", "builder": "[3]", "approver": "[6]"}.get(name, "[4]")
                self.assertTrue(marked.startswith(module), marked)
                self.assertTrue(card.splitlines()[-1].startswith(f"You are {name}"))
        self.assertIn("Your output: round-N/review-ux.md, first line VERDICT: PASS or VERDICT: CHANGES",
                      factory_grid(pipeline, "reviewer-ux"))
        self.assertIn("Your output: decision.md, first line APPROVE or ESCALATE", factory_grid(pipeline, "approver"))

    def test_built_from_the_real_pipeline(self) -> None:
        job = {"reviewers": ["code", "perf"], "max_resumes": 1, "feature": "ignored"}
        card = factory_grid(job, "reviewer-perf")
        self.assertIn("[4] reviewers, in parallel: reviewer-code, reviewer-perf;", card)
        self.assertNotIn("reviewer-security", card)
        self.assertIn("(at most 1 resume)", card)
        self.assertIn(HERE, card)
        self.assertNotIn(HERE, factory_grid(job, "reviewer-security"), "a lens not in this pipeline is not on it")
        self.assertNotIn(HERE, factory_grid(job, "coder"))
        none = factory_grid({"reviewers": ["code"], "max_resumes": 0}, "reviewer-code")
        self.assertIn("any VERDICT: CHANGES → [H] (this pipeline allows no resumes)", none)
        self.assertNotIn("no resumes left", none)
        self.assertIn("your CHANGES sends the job to [H] needs-human", none)
        empty = factory_grid({"reviewers": [], "max_resumes": 2})
        self.assertIn("[4] reviewers: none in this pipeline", empty)
        self.assertNotIn("WAIT FOR ALL", empty)

    def test_same_pipeline_same_bytes(self) -> None:
        """Nothing run-specific goes in: two jobs on one pipeline get identical cards (prompt caching holds)."""
        first = factory_grid({"reviewers": ["security", "code"], "max_resumes": 2, "id": "001-a"}, "builder")
        second = factory_grid(PipelineConfig(reviewers=["security", "code"]), "builder")
        self.assertEqual(first, second)
        self.assertNotEqual(first, factory_grid(PipelineConfig(reviewers=["code", "security"]), "builder"))

    def test_receipt(self) -> None:
        card = factory_grid(PipelineConfig(), "builder")
        self.assertEqual(grid_sha256(card), hashlib.sha256(card.encode("utf-8")).hexdigest())
        self.assertEqual(receipt(card), {"sha256": grid_sha256(card), "bytes": len(card.encode("utf-8"))})
        self.assertGreater(receipt(card)["bytes"], len(card), "the arrows are multi-byte: bytes, not chars")


if __name__ == "__main__":
    unittest.main()
