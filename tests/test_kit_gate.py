"""Tests for kits/claude-code/gate.sh: the kit's merge gate, run against fixture repos and job folders.

The gate prints one line per reason a branch needs a human and exits 1, or prints nothing and exits 0.
With a job folder it also stops on every spec.md line marked (product) that the job's confirmed.md
(written only by a person) does not confirm; the web harness's swfactory/assumptions.py applies the
same rules, and tests/test_assumptions.py runs both on the same specs.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from swfactory.config import PROJECT_DIR
from tests.helpers import commit_file, make_repo, run_git

GATE = PROJECT_DIR / "kits" / "claude-code" / "gate.sh"
BRANCH = "factory/001-greeting"

SPEC = """# Greeting

> Feature: add a greeting

## Requirements
1. Show a greeting.

## Assumptions
- The greeting reuses the existing `Banner` component.
- The greeting says "Hello" on the free plan **(product)**.
2.   **The paid plan gets   no greeting.** (product)

## Risk
None.
"""
FREE = 'The greeting says "Hello" on the free plan (product).'
PAID = "The paid plan gets no greeting. (product)"


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class KitGateTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.repo = make_repo(self.tmp)
        run_git(self.repo, "checkout", "-q", "-b", BRANCH)
        commit_file(self.repo, "src/greeting.txt", "Hello\n", "Add a greeting")
        run_git(self.repo, "checkout", "-q", "main")
        self.job = self.repo / "factory" / "jobs" / "001-greeting"
        self.job.mkdir(parents=True)

    def gate(self, *args: str) -> tuple[int, list[str]]:
        result = subprocess.run(["bash", str(GATE), *args], cwd=self.repo, capture_output=True, text=True,
                                timeout=60)
        return result.returncode, result.stdout.splitlines()

    def test_without_a_job_folder_only_the_branch_counts(self) -> None:
        (self.job / "spec.md").write_text(SPEC)
        self.assertEqual(self.gate("main", BRANCH), (0, []))

    def test_a_spec_without_product_assumptions_passes(self) -> None:
        (self.job / "spec.md").write_text(SPEC.replace("(product)", ""))
        self.assertEqual(self.gate("main", BRANCH, "factory/jobs/001-greeting/"), (0, []))

    def test_unconfirmed_product_assumptions_need_a_human(self) -> None:
        (self.job / "spec.md").write_text(SPEC)
        code, lines = self.gate("main", BRANCH, "factory/jobs/001-greeting/")
        self.assertEqual(code, 1)
        self.assertEqual(lines, [f"product assumption not confirmed by a human: {FREE}",
                                 f"product assumption not confirmed by a human: {PAID}"],
                         "list markers, bold and spacing are normalised away")

    def test_confirmed_lines_pass_however_they_are_spelled(self) -> None:
        (self.job / "spec.md").write_text(SPEC)
        (self.job / "confirmed.md").write_text(
            '# Confirmed by a human\n\n- [x] The greeting says "Hello" on the free plan **(product)**.\n')
        self.assertEqual(self.gate("main", BRANCH, str(self.job)),
                         (1, [f"product assumption not confirmed by a human: {PAID}"]))
        with (self.job / "confirmed.md").open("a") as handle:
            handle.write("  * The paid plan gets no greeting.   (product)\r\n")
        self.assertEqual(self.gate("main", BRANCH, str(self.job)), (0, []))

    def test_a_changed_assumption_needs_confirming_again(self) -> None:
        (self.job / "spec.md").write_text(SPEC)
        (self.job / "confirmed.md").write_text(f"{FREE}\n{PAID}\n")
        self.assertEqual(self.gate("main", BRANCH, str(self.job)), (0, []))
        (self.job / "spec.md").write_text(SPEC.replace('"Hello"', '"Hi"'))
        self.assertEqual(self.gate("main", BRANCH, str(self.job)),
                         (1, ['product assumption not confirmed by a human: The greeting says "Hi" on the free '
                              'plan (product).']))

    def test_a_heading_mark_can_be_confirmed(self) -> None:
        (self.job / "spec.md").write_text("# Plans\n\n## Assumptions\n### Free tier gets 3 projects (product)\n")
        self.assertEqual(self.gate("main", BRANCH, str(self.job)),
                         (1, ["product assumption not confirmed by a human: Free tier gets 3 projects (product)"]))
        (self.job / "confirmed.md").write_text("# Confirmed by a human\n### Free tier gets 3 projects (product)\n")
        self.assertEqual(self.gate("main", BRANCH, str(self.job)), (0, []), "as /factory confirm copies it")

    def test_what_is_not_a_mark(self) -> None:
        """The verbatim feature line and a mention that says it is not one; the shipped template has none."""
        (self.job / "spec.md").write_text(
            "# Badge\n\n> Feature: add a (product) badge to cards\n\n## Assumptions\n"
            "- The badge reuses `Banner`; an engineering choice, so not (product).\n"
            "- Copy for the badge is never **(product)** here: it repeats the request.\n"
            "- The badge says \"New\" **(Product)**\n")
        self.assertEqual(self.gate("main", BRANCH, str(self.job)),
                         (1, ['product assumption not confirmed by a human: The badge says "New" (Product)']))
        agent = (PROJECT_DIR / "agents" / "spec-writer.md").read_text(encoding="utf-8")
        template = agent.split("```markdown", 1)[1].split("```", 1)[0]
        (self.job / "spec.md").write_text(template + "- The greeting reuses `Banner` (an engineering choice).\n")
        self.assertEqual(self.gate("main", BRANCH, str(self.job)), (0, []),
                         "a spec that keeps the template's guidance as written stops for nothing")

    def test_a_missing_spec_fails_closed(self) -> None:
        code, lines = self.gate("main", BRANCH, str(self.job))
        self.assertEqual(code, 1)
        self.assertEqual(lines, [f"cannot check product assumptions: no {self.job}/spec.md"])

    def test_product_reasons_come_first_beside_the_branch_rules(self) -> None:
        (self.job / "spec.md").write_text(SPEC)
        (self.job / "confirmed.md").write_text(FREE + "\n")
        run_git(self.repo, "checkout", "-q", BRANCH)
        commit_file(self.repo, "package.json", "{}\n", "Add a manifest")
        run_git(self.repo, "checkout", "-q", "main")
        code, lines = self.gate("main", BRANCH, str(self.job))
        self.assertEqual(code, 1)
        self.assertEqual(lines, [f"product assumption not confirmed by a human: {PAID}",
                                 "touches protected path package.json"])

    def test_usage(self) -> None:
        result = subprocess.run(["bash", str(GATE), "main"], cwd=self.repo, capture_output=True, text=True,
                                timeout=60)
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage:", result.stderr)
        self.assertEqual(self.gate("main", "factory/none", str(self.job)), (1, ["no branch factory/none"]))


class KitDocsTest(unittest.TestCase):
    def test_the_skill_runs_the_gate_with_the_job_folder_and_explains_confirm(self) -> None:
        skill = " ".join((PROJECT_DIR / "kits" / "claude-code" / "SKILL.md").read_text(encoding="utf-8").split())
        self.assertIn("`bash .claude/skills/factory/gate.sh <base> factory/<job-id> <job folder>`", skill)
        self.assertIn("## /factory confirm <job-id>", skill)
        self.assertIn("The user typing this command is the confirmation", skill)
        self.assertIn("no agent ever writes `confirmed.md`", skill)
        self.assertIn("confirm <job-id>", skill.split("---")[1], "the argument hint lists confirm")
        self.assertNotIn("grep -i -F '(product)'", skill, "the gate is the one definition of a marked line")
        self.assertEqual(skill.count("that start with `product assumption not confirmed by a human: `")
                         + skill.count("that starts with `product assumption not confirmed by a human: `"), 2)


if __name__ == "__main__":
    unittest.main()
