"""Tests for swfactory.assumptions: the spec's product assumptions, read by the same rules as the kit's
gate.sh (both run on the same specs here), and the confirmed.md the web harness writes from its record."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from swfactory import items
from swfactory.assumptions import (CONFIRMED_HEADER, answers, clear, marked, normalize, on_record, read_spec,
                                   render, view)
from swfactory.config import PROJECT_DIR
from tests.helpers import make_repo, run_git

GATE = PROJECT_DIR / "kits" / "claude-code" / "gate.sh"
PREFIX = "product assumption not confirmed by a human: "

SPECS = {
    "list items": "## Assumptions\n- The greeting says \"Hello\" on the free plan **(product)**.\n"
                  "2.   **The paid plan gets   no greeting.** (product)\n- Reuses `Banner`.\n",
    "heading": "## Assumptions\n### Free tier gets 3 projects (product)\n",
    "not a mark": "# X\n\n> Feature: add a (product) badge\n\n## Assumptions\n- Reuses `Banner`, so not (product).\n"
                  "- Never **(product)**: it repeats the request.\n- The badge says \"New\" (PRODUCT)\n",
    "box and spacing": "* [x]  Annual   plan costs $90 (product)\r\n\n- Annual plan costs $90 (product)\n",
}
EXPECTED = {
    "list items": ['The greeting says "Hello" on the free plan (product).', "The paid plan gets no greeting. (product)"],
    "heading": ["Free tier gets 3 projects (product)"],
    "not a mark": ['The badge says "New" (PRODUCT)'],
    "box and spacing": ["Annual plan costs $90 (product)"],
}


class RulesTest(unittest.TestCase):
    def test_marked_lines(self) -> None:
        for name, spec in SPECS.items():
            with self.subTest(spec=name):
                self.assertEqual(marked(spec), EXPECTED[name])

    def test_normalize(self) -> None:
        for raw, want in (("  - [x] **A**  b (product)\r\n", "A b (product)"), ("## 1. A (product)", "A (product)"),
                          ("#hashtag (product)", "#hashtag (product)"), ("12) A", "A"), ("+ A", "A")):
            with self.subTest(raw=raw):
                self.assertEqual(normalize(raw), want)

    def test_view(self) -> None:
        spec = SPECS["list items"]
        self.assertEqual(view(spec, ["- [x] The paid plan gets no greeting. **(product)**"]),
                         {"marked": EXPECTED["list items"],
                          "confirmed": ["- [x] The paid plan gets no greeting. **(product)**"],
                          "unconfirmed": [EXPECTED["list items"][0]]})
        self.assertEqual(view(None, []), {"marked": [], "confirmed": [], "unconfirmed": []})

    def test_a_confirmed_line_answers_its_decision_item(self) -> None:
        """The Housekeeping Tracker's decision item quotes the whole Assumptions item (continuation lines
        joined); its marked line answers it, another marked line does not (DESIGN §7, §32)."""
        free = 'The greeting says "Hello" on the free plan (product).'
        self.assertTrue(answers(free, 'The greeting says "Hello" on the free plan **(product)**.'))
        self.assertTrue(answers("paid users unlimited (product).",
                                "Free users get three projects, paid users unlimited **(product)**."))
        self.assertFalse(answers("The paid plan gets no greeting. (product)", f"{free} **(product)**"))
        self.assertFalse(answers("", free))

    def test_the_tracker_reads_the_mark_the_same_way(self) -> None:
        for name in ("list items", "not a mark"):
            with self.subTest(spec=name):
                _, decisions = items.parse_spec(SPECS[name])
                self.assertEqual([normalize(text) for _, text in decisions], EXPECTED[name])

    def test_the_shipped_template_marks_nothing(self) -> None:
        agent = (PROJECT_DIR / "agents" / "spec-writer.md").read_text(encoding="utf-8")
        self.assertEqual(marked(agent.split("```markdown", 1)[1].split("```", 1)[0]), [])


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class GateParityTest(unittest.TestCase):
    """gate.sh and assumptions.marked agree on every spec above, and on what a confirmed.md confirms."""

    def test_the_gate_reads_specs_the_same_way(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = make_repo(Path(tmp))
            run_git(repo, "branch", "factory/001-x")
            job = repo / "factory" / "jobs" / "001-x"
            job.mkdir(parents=True)
            for name, spec in SPECS.items():
                with self.subTest(spec=name):
                    (job / "spec.md").write_text(spec, newline="")
                    (job / "confirmed.md").unlink(missing_ok=True)
                    self.assertEqual(self.gate(repo, job), [PREFIX + line for line in marked(spec)])
                    (job / "confirmed.md").write_text(render(marked(spec)))
                    self.assertEqual(self.gate(repo, job), [], "what the web harness writes, the gate accepts")

    def gate(self, repo: Path, job: Path) -> list[str]:
        result = subprocess.run(["bash", str(GATE), "main", "factory/001-x", str(job)], cwd=repo,
                                capture_output=True, text=True, timeout=60)
        return result.stdout.splitlines()


class FilesTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.path = self.dir / "confirmed.md"

    def test_render_and_on_record(self) -> None:
        self.assertEqual(render(["A (product)"]), f"{CONFIRMED_HEADER}\nA (product)\n")
        self.assertTrue(on_record(self.path, []), "no file, nothing confirmed")
        self.assertFalse(on_record(self.path, ["A (product)"]))
        self.path.write_text(render(["A (product)"]))
        self.assertTrue(on_record(self.path, ["A (product)"]))
        self.assertFalse(on_record(self.path, []), "a file nobody confirmed through the record")
        self.path.write_text(render(["A (product)"]) + "B (product)\n")
        self.assertFalse(on_record(self.path, ["A (product)"]))

    def test_links_and_folders_are_never_on_record(self) -> None:
        real = self.dir / "elsewhere.md"
        real.write_text(render(["A (product)"]))
        os.symlink(real, self.path)
        self.assertFalse(on_record(self.path, ["A (product)"]))
        clear(self.path)
        self.assertFalse(self.path.exists() or self.path.is_symlink())
        self.assertTrue(real.exists(), "clear removes the link, not what it points at")
        (self.path / "inner").mkdir(parents=True)
        self.assertFalse(on_record(self.path, ["A (product)"]))
        clear(self.path)
        self.assertFalse(self.path.exists())

    def test_read_spec(self) -> None:
        self.assertIsNone(read_spec(self.dir))
        (self.dir / "spec.md").write_text("# Spec\n")
        self.assertEqual(read_spec(self.dir), "# Spec\n")
        (self.dir / "spec.md").unlink()
        os.symlink(self.dir / "elsewhere", self.dir / "spec.md")
        self.assertIsNone(read_spec(self.dir), "a link is not the spec")


if __name__ == "__main__":
    unittest.main()
