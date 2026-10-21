"""The Factory Grid Card: the factory line as compact text, delivered to the line's agents (DESIGN.md §26).

factory_grid(pipeline, agent) builds the card from the real pipeline (its reviewers and max_resumes), so
what an agent reads is the line its job runs on: numbered modules joined by labelled routes (the parallel
reviewers and WAIT FOR ALL, the approver, the deterministic gate, needs-human, and the CHANGES loop back
to the builder), with the agent's own module marked and a line saying what it writes and where the job
goes next. While the Housekeeping Tracker holds jobs (items=True, DESIGN.md §32) the builder's route and
the gate name its must-do items too. The text depends on nothing else (no paths, ids or dates), so every
run of the same agent on the same pipeline gets the same bytes and a provider can keep caching the system
prompt.

receipt(text) is what agent.start and job.json record: {sha256, bytes}. Delivery is not proof of use:
the receipt says which card the prompt carried, never that the model read or followed it.
"""

from __future__ import annotations

import hashlib
from typing import Any

HERE = "◀ you are here"
TITLE = "# Factory Grid Card"


def factory_grid(pipeline: Any, agent: str | None = None, items: bool = True) -> str:
    """The Grid Card for pipeline (a PipelineConfig, or a dict with reviewers and max_resumes, such as a
    job.json), with agent's module marked when agent is on the line (spec-writer, builder, a reviewer of
    this pipeline, approver); any other agent, or None, gets the card unmarked. items: the Housekeeping
    Tracker holds jobs (housekeeping.enabled), so the build check and the gate check must-do items. No
    trailing newline: the prompt carries exactly these bytes, so the receipt matches what was delivered."""
    reviewers = [str(r) for r in _field(pipeline, "reviewers", []) or []]
    resumes = max(0, int(_field(pipeline, "max_resumes", 2) or 0))
    lenses = [f"reviewer-{r}" for r in reviewers]
    here = _module(agent, lenses)

    def mark(module: str) -> str:
        return f"   {HERE}" if module == here else ""

    lines = [
        TITLE,
        "",
        "The factory line your job runs on: numbered modules joined by labelled routes (label → module). "
        "Code moves the job from module to module; each agent writes one file, and only those files decide "
        "the route.",
        "",
        "[1] feature: the request, verbatim, in feature.md",
        "    → [2]",
        f"[2] spec-writer: reads the code, writes spec.md{mark('2')}",
        "    spec.md written → [3]",
        f"[3] builder: changes the code and commits on the job branch, writes build.md{mark('3')}",
        "    STATUS: READY, with commits and a clean tree" + (", answering every must-do item it owns"
                                                              if items else "") + " → [4]",
        "    STATUS: BLOCKED → [H]",
    ]
    if lenses:
        lines += [f"[4] reviewers, in parallel: {', '.join(lenses)}; each writes round-N/review-<lens>.md"
                  f"{mark('4')}",
                  "    WAIT FOR ALL → [5]"]
    else:
        lines += ["[4] reviewers: none in this pipeline", "    → [5]"]
    lines += ["[5] round verdicts (code)", "    every review VERDICT: PASS → [6]"]
    if resumes:
        most = _count(resumes, "resume")
        lines += [f"    any VERDICT: CHANGES → resume [3] builder with the reviews (at most {most})",
                  "    VERDICT: CHANGES with no resumes left → [H]"]
    else:
        lines.append("    any VERDICT: CHANGES → [H] (this pipeline allows no resumes)")
    lines += [
        "    a missing or invalid review → [H]",
        f"[6] approver: checks the whole job, writes decision.md{mark('6')}",
        "    APPROVE → [7]",
        "    ESCALATE → [H]",
        "[7] gate (code, no model): every final review PASS" + ("; every must-do item in items.md proven, "
                                                                "a product decision by a person" if items else "")
        + "; a merge also needs no conflicts, no protected paths and a diff within the size limit",
        "    passes → delivered: merged into the base branch, kept on its branch, or exported to a folder "
        "(the job's delivery mode)",
        "    stops → [H]",
        "[H] needs-human: a person decides",
        "    approve → delivered (a human override; a merge still needs no conflicts)",
        "    rework → [3] builder, with the note in rework-<k>.md",
        "    reject → the branch is dropped",
    ]
    if here is not None and agent is not None:
        lines += ["", _you(agent, here, resumes)]
    return "\n".join(lines)


def grid_sha256(text: str) -> str:
    """The hex sha256 of the card's UTF-8 bytes."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def receipt(text: str) -> dict:
    """The delivery receipt of one card: {sha256, bytes}."""
    return {"sha256": grid_sha256(text), "bytes": len(text.encode("utf-8"))}


def _module(agent: str | None, lenses: list[str]) -> str | None:
    if agent == "spec-writer":
        return "2"
    if agent == "builder":
        return "3"
    if agent in lenses:
        return "4"
    if agent == "approver":
        return "6"
    return None


def _you(agent: str, module: str, resumes: int) -> str:
    if module == "2":
        return ("You are spec-writer, module [2]. Your output: spec.md in the job folder. Next: [3] builder "
                "builds from it, and the reviewers and the approver check the branch against it.")
    if module == "3":
        return ("You are builder, module [3]. Your output: commits on the job branch and build.md. Next: "
                "STATUS: READY → [4] reviewers; STATUS: BLOCKED → [H] needs-human. After a CHANGES round you "
                "are resumed with the reviews.")
    if module == "4":
        lens = agent[len("reviewer-"):]
        back = "[3] builder" if resumes else "[H] needs-human"
        return (f"You are {agent}, one of the parallel reviewers in module [4]. Your output: "
                f"round-N/review-{lens}.md, first line VERDICT: PASS or VERDICT: CHANGES. Next: WAIT FOR ALL → "
                f"[5]; your CHANGES sends the job to {back}.")
    return ("You are approver, module [6]. Your output: decision.md, first line APPROVE or ESCALATE. Next: "
            "APPROVE → [7] gate; ESCALATE → [H] needs-human.")


def _field(pipeline: Any, name: str, default: Any) -> Any:
    if isinstance(pipeline, dict):
        return pipeline.get(name, default)
    return getattr(pipeline, name, default)


def _count(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")
