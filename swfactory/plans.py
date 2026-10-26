"""Plans: a frontier "lead" agent above the factory line (DESIGN.md section 24).

The lead turns a large request into ordered factory jobs (``plan.md``). PlanManager
submits each job once the jobs it comes after have merged, follows them through
``job.updated`` events, and when all have merged asks the lead for a final review
of the combined change. A plan lives in ``<data_dir>/plans/<repo>/<plan-id>/``;
only PlanManager writes its ``plan.json``.

Stages: planning → ready (a human reads, edits and runs the plan) → running ⇄ blocked
→ final-review → done; needs-human, failed and cancelled stop it. Retry re-runs the
step recorded in ``failed_step`` (``plan``, ``final-review`` or ``run``).

A plan's ``routing`` (per-agent routes or a preset name) goes to every job it submits and
routes the lead too: its "lead" entry, or the preset's ``agents.lead``; its ``profile``
(resolved at create, like the delivery) goes to every job and places the lead by its "lead"
entry. The lead is placed like a job's run (chats.place_run): the first build of its model that
answers, else its overflow, which also takes the run when no slot comes free within the provider's
overflow_after_seconds. Its ``delivery``
(merge, branch or folder, resolved at create) goes to every job too; a job delivered as a
branch or folder has not merged, so it holds the plan ``blocked`` until a human approves
(merges) it, exactly like a job waiting in needs-human. After ``PLAN:
QUESTIONS`` the human may save plan.md with answers; Retry moves it to
``questions-<k>.md`` and points the lead at it.

A plan keeps tracked items too (DESIGN.md section 32: ``items.json``, ``items.log.jsonl``
and ``items.md`` in the plan folder, written only here): what a plan job left open when it
ended (the orchestrator hands it over through carry_in), and each final-review finding,
which closes when its follow-up job merges. The lead answers them under ``## Items`` in its
final review.
"""

from __future__ import annotations

import contextlib
import os
import queue
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import gitops
from . import items as tracker
from .chats import (
    ACTIVE_JOB_STAGES,
    INTERRUPTED,
    FactoryApi,
    Route,
    RunnerFactory,
    action_error,
    agent_context,
    agent_route,
    event_emitter,
    load_agent,
    next_log_path,
    place_run,
    read_json,
    run_agent,
    run_cost_of,
    write_atomic,
    write_json,
    zero_usage,
)
from .events import utc_now
from .jobs import REPO_NAME_RE, read_verdict, slugify
from .routing import add_cost, merged_placements, profile_routes, zero_cost
from .runners import AgentRun, add_usage, make_runner

if TYPE_CHECKING:
    from .agentdefs import AgentDef
    from .config import Config
    from .events import Event, EventBus

LEAD = "lead"
PLAN_FILE = "plan.md"
REQUEST_FILE = "request.md"
STATE_FILE = "plan.json"
PLAN_ID_RE = re.compile(r"^P\d{3}-[a-z0-9]+(-[a-z0-9]+)*$")
MAX_PLAN_NUMBER = 999
# A plan job in one of these waits for a human. "delivered" (a branch or folder delivery) has not merged into
# base, so later jobs cannot build on it: approving it merges it and the plan goes on.
BLOCKING_JOB_STAGES = frozenset({"needs-human", "failed", "rejected", "cancelled", "delivered"})
DEAD_JOB_STAGES = frozenset({"rejected", "cancelled"})   # can never merge; an edit gives the item a new job
EDITABLE_STAGES = ("ready", "needs-human", "blocked")
FINISHED_STAGES = ("done", "cancelled")
LEAD_STAGES = {"plan": "planning", "final-review": "final-review"}
POLL_SECONDS = 0.2

_PLAN_STATUSES = {"PLAN: READY": "READY", "PLAN: QUESTIONS": "QUESTIONS"}
_HEADING_RE = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)[\s#]*$")
_ITEM_RE = re.compile(r"^ ?(\d+)[.)]\s+(.*\S)\s*$")
_BULLET_RE = re.compile(r"^ ?(?:\d+[.)]|[-*+])\s+(.*\S)\s*$")
_AFTER_RE = re.compile(r"\s*\(\s*after\s*:?\s*([^()]*)\)\s*[.;]?$", re.IGNORECASE)
_PLAN_NUMBER_RE = re.compile(r"^P(\d{3})-")
_QUESTIONS_RE = re.compile(r"^questions-(\d+)\.md$")
_CONTEXT_RE = re.compile(r"^Plan context: (P\d{3}-[a-z0-9]+(?:-[a-z0-9]+)*) — see ", re.MULTILINE)
_BOM = "﻿"


# ---------------------------------------------------------------- plan.md and review files


@dataclass
class ParsedPlan:
    status: str                                       # "READY" or "QUESTIONS"
    jobs: list[dict] = field(default_factory=list)    # [{n, feature, after}]
    questions: list[str] = field(default_factory=list)


def plan_status(text: str) -> str | None:
    """READY or QUESTIONS from plan.md's first non-empty line (markdown wrapping ignored), else None."""
    for line in text.lstrip(_BOM).splitlines():
        stripped = line.strip()
        if stripped:
            while True:
                bare = stripped.lstrip("#").strip().strip("*_`").strip()
                if bare == stripped:
                    return _PLAN_STATUSES.get(bare)
                stripped = bare
    return None


def _section(text: str, title: str) -> list[str] | None:
    """The lines under the first heading named title (any level, any case), subsections included,
    up to the next heading of the same or a higher level; None if there is no such heading."""
    lines: list[str] | None = None
    level = 0
    for line in text.splitlines():
        heading = _HEADING_RE.match(line)
        if heading and lines is not None and len(heading.group(1)) <= level:
            break
        if heading and lines is None:
            if heading.group(2).strip().lower() == title:
                lines, level = [], len(heading.group(1))
        elif lines is not None and not heading:
            lines.append(line)
    return lines


def _bullets(lines: list[str]) -> list[str]:
    return [match.group(1) for match in map(_BULLET_RE.match, lines) if match]


def parse_plan(text: str) -> ParsedPlan:
    """Parse plan.md; ValueError naming the problem (first line, numbering, unknown refs, cycles).

    A READY plan lists its jobs under ``## Jobs`` as ``N. <feature> (after: a, b)``, numbered
    1, 2, 3, … in order; other lines there (sub-items, prose) are ignored.
    """
    status = plan_status(text)
    if status is None:
        raise ValueError("plan.md must start with PLAN: READY or PLAN: QUESTIONS")
    if status == "QUESTIONS":
        return ParsedPlan(status, questions=_bullets(_section(text, "questions") or []))
    lines = _section(text, "jobs")
    if lines is None:
        raise ValueError("plan.md has no '## Jobs' section")
    jobs: list[dict] = []
    for line in lines:
        item = _ITEM_RE.match(line)
        if not item:
            continue
        n, feature = int(item.group(1)), item.group(2)
        if n != len(jobs) + 1:
            raise ValueError(f"jobs must be numbered 1, 2, 3, … in order; found {n} after {len(jobs)}")
        after: list[int] = []
        suffix = _AFTER_RE.search(feature)
        if suffix:
            feature = feature[:suffix.start()].strip()
            for ref in filter(None, (part.strip() for part in suffix.group(1).split(","))):
                number = ref.lstrip("#")
                if not number.isdigit():
                    raise ValueError(f"job {n}: invalid reference {ref!r} in (after: …)")
                after.append(int(number))
        if not feature:
            raise ValueError(f"job {n} has no feature text")
        jobs.append({"n": n, "feature": feature, "after": sorted(set(after))})
    if not jobs:
        raise ValueError("the '## Jobs' section lists no numbered jobs")
    for job in jobs:
        unknown = [ref for ref in job["after"] if not 1 <= ref <= len(jobs)]
        if unknown:
            raise ValueError(f"job {job['n']} comes after unknown job {unknown[0]}")
    stuck = _never_ready(jobs)
    if stuck:
        raise ValueError(f"dependency cycle: jobs {', '.join(map(str, stuck))} can never start")
    return ParsedPlan(status, jobs=jobs)


def _never_ready(jobs: list[dict]) -> list[int]:
    """Numbers of the jobs that a dependency cycle keeps from ever starting (Kahn's algorithm)."""
    waiting = {job["n"]: set(job["after"]) for job in jobs}
    while True:
        free = {n for n, deps in waiting.items() if not deps}
        if not free:
            return sorted(waiting)
        for n in free:
            del waiting[n]
        for deps in waiting.values():
            deps -= free


def parse_findings(text: str) -> list[tuple[str, str]]:
    """(feature, why) per item under ``## Findings`` of a final review; the feature is the text
    before the first ": ". "None." items are skipped."""
    findings = []
    for item in _bullets(_section(text, "findings") or []):
        item = item.replace("**", "").replace("__", "").strip()
        if item.rstrip(".").strip().lower() == "none":
            continue
        feature, _, why = item.partition(": ")
        feature = feature.strip().strip("`").strip()
        if feature:
            findings.append((feature, why.strip()))
    return findings


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _housekeeping(config: Config) -> Any:
    from .config import HousekeepingConfig

    try:
        return config.housekeeping()
    except (ValueError, AttributeError):
        return HousekeepingConfig()


# ---------------------------------------------------------------- the manager


class PlanManager:
    """Plans under ``<data_dir>/plans/<repo>/``; Factory exposes the same methods.

    Subscribes to the bus at construction. Call start() once the factory has started
    (restart recovery and catching up on job changes) and stop() when it stops.
    Methods that change a plan hold one lock, also while calling factory.submit, so
    the Factory must not call them while holding a lock of its own.
    """

    def __init__(self, config: Config, bus: EventBus, factory: FactoryApi,
                 runner_factory: RunnerFactory = make_runner) -> None:
        self.config = config
        self.bus = bus
        self.factory = factory
        self.runner_factory = runner_factory
        self.root = Path(config.data_dir) / "plans"
        self._lock = threading.RLock()
        self._leads: dict[tuple[str, str], tuple[threading.Thread, threading.Event]] = {}
        self._index: dict[tuple[str, str], str] = {}     # (repo, job id) → plan id
        for plan in self.plans():
            self._reindex(plan)
        self._stopping = threading.Event()
        self._events = bus.subscribe()
        self._dispatcher = threading.Thread(target=self._dispatch, name="plans-events", daemon=True)
        self._dispatcher.start()

    def start(self) -> None:
        """Restart recovery: interrupted lead steps become failed (retryable); every other unfinished
        plan catches up with its jobs' current stages (the factory marks interrupted jobs failed
        before this manager exists, so those job.updated events were never seen), and running and
        blocked plans submit what became unblocked."""
        with self._lock:
            for plan in self.plans():
                key = (plan["repo"], plan["id"])
                step = next((s for s, stage in LEAD_STAGES.items() if stage == plan.get("stage")), None)
                if step is not None and key not in self._leads:
                    self._discard_checkout(plan)
                    self._fail(plan, step, INTERRUPTED)
                    self._save(plan)
                elif plan.get("stage") not in FINISHED_STAGES:
                    self._advance(plan, self._sync_jobs(plan))

    def stop(self, timeout: float = 10.0) -> None:
        """Stop following events, cancel running lead steps and wait up to timeout for the threads."""
        self._stopping.set()
        self.bus.unsubscribe(self._events)
        with contextlib.suppress(queue.Full):
            self._events.put_nowait(None)
        with self._lock:
            leads = list(self._leads.values())
        for _, cancel in leads:
            cancel.set()
        for thread, _ in leads:
            thread.join(timeout)
        self._dispatcher.join(timeout)

    # -- reading ----------------------------------------------------------------

    def plans(self) -> list[dict]:
        """Every plan.json, by repo then plan id (unreadable ones are skipped)."""
        found = []
        if self.root.is_dir():
            for repo_dir in sorted(self.root.iterdir()):
                if not repo_dir.is_dir():
                    continue
                for plan_dir in sorted(repo_dir.iterdir()):
                    if PLAN_ID_RE.fullmatch(plan_dir.name):
                        with contextlib.suppress(OSError, ValueError):
                            found.append(read_json(plan_dir / STATE_FILE))
        return found

    def plan(self, repo: str, plan_id: str) -> dict:
        """The plan's plan.json (KeyError if unknown)."""
        return self._load(repo, plan_id)

    def plan_dir(self, repo: str, plan_id: str) -> Path:
        """``<data_dir>/plans/<repo>/<plan-id>`` (ValueError for an invalid repo name or plan id)."""
        if not isinstance(plan_id, str) or not PLAN_ID_RE.fullmatch(plan_id):
            raise ValueError(f"invalid plan id {plan_id!r}")
        return self._repo_dir(repo) / plan_id

    def extra_read_roots_for_job(self, job: dict) -> list[Path]:
        """[plan folder] for a job a plan submitted (found by the "Plan context:" line of its feature),
        else []. Lock-free, so the orchestrator may call it from anywhere."""
        matches = list(_CONTEXT_RE.finditer(str(job.get("feature") or "")))
        if not matches:
            return []
        try:
            plan_dir = self.plan_dir(str(job.get("repo")), matches[-1].group(1))
        except ValueError:
            return []
        return [plan_dir] if plan_dir.is_dir() else []

    def plan_of_job(self, job: dict) -> str | None:
        """The id of the plan that submitted job (by the "Plan context:" line of its feature), else None.
        Lock-free, like extra_read_roots_for_job."""
        roots = self.extra_read_roots_for_job(job)
        return roots[0].name if roots else None

    # -- tracked items (section 32) -------------------------------------------------

    def _items_store(self, repo: str, plan_id: str) -> tracker.ItemStore:
        return tracker.ItemStore(self.plan_dir(repo, plan_id), tracker.plan_scope(repo, plan_id),
                                 max_proposals=_housekeeping(self.config).max_proposals_per_scope)

    def _item_secrets(self) -> list[str]:
        values = getattr(self.factory, "secret_values", None)
        found = list(values()) if callable(values) else list(self.config.secret_values())
        from .events import env_secret_values

        return found + env_secret_values()

    @contextlib.contextmanager
    def _plan_items(self, repo: str, plan_id: str, timeout: float | None = None) -> Any:
        """A transaction on the plan's ledger (its own lock, never the plan lock: the orchestrator calls
        carry_in from a job's worker); items.updated (with data.plan) after a change."""
        store = self._items_store(repo, plan_id)
        with store.transaction(timeout=timeout, secrets=self._item_secrets()) as ledger:
            yield ledger
        if ledger.dirty:
            self.bus.publish("items.updated", {"ref": store.scope.ref, "summary": ledger.summary(),
                                               "changed": list(ledger.changed), "plan": plan_id}, repo=repo)

    def carry_in(self, repo: str, plan_id: str, entries: list[dict]) -> list[str]:
        """Take in what a plan job left open when it ended (the orchestrator's carry-forward); their refs."""
        self._existing_dir(repo, plan_id)
        refs = []
        with self._plan_items(repo, plan_id) as ledger:
            for fields in entries:
                item = tracker.add_carried(ledger, dict(fields))
                refs.append(ledger.scope.item_ref(item["id"]))
        return refs

    def scope_items(self, repo: str, plan_id: str) -> dict:
        """{items, summary, jobs: [{job_id, summary}], log} of one plan (KeyError if unknown)."""
        plan = self._load(repo, plan_id)
        store = self._items_store(repo, plan_id)
        ledger = store.ledger()
        now = utc_now()
        jobs = []
        for entry in plan.get("jobs") or []:
            if not entry.get("job_id"):
                continue
            summary = None
            with contextlib.suppress(KeyError, ValueError, OSError):
                summary = self.factory.job(repo, entry["job_id"]).get("items")
            jobs.append({"job_id": entry["job_id"], "n": entry.get("n"), "stage": entry.get("stage"),
                         "summary": summary})
        return {"items": [tracker.decorate(item, store.scope, now=now, scope_stage=plan.get("stage"))
                          for item in ledger.items],
                "summary": ledger.summary(), "jobs": jobs, "log": store.log_tail()}

    def item_history(self, repo: str, plan_id: str, item_id: str) -> dict:
        """{item, log}: one plan item and every transition it went through (KeyError if unknown)."""
        plan = self._load(repo, plan_id)
        store = self._items_store(repo, plan_id)
        item = store.ledger().get(item_id)
        return {"item": tracker.decorate(item, store.scope, now=utc_now(), scope_stage=plan.get("stage")),
                "log": store.history(item_id)}

    def item_rows(self, repo: str | None, now: str) -> list[dict]:
        """Every plan's items as the tracker lists them (repo: only that repo's plans)."""
        rows: list[dict] = []
        for plan in self.plans():
            if repo is not None and plan.get("repo") != repo:
                continue
            store = self._items_store(plan["repo"], plan["id"])
            if not store.path.is_file():
                continue
            with contextlib.suppress(OSError, ValueError):
                rows += [tracker.decorate(item, store.scope, now=now, scope_stage=plan.get("stage"))
                         for item in store.ledger().items]
        return rows

    def item_action(self, repo: str, plan_id: str, item_id: str, action: str, **kwargs: Any) -> dict:
        """A person's action on one plan item; defer (and update to defer) moves it to the repo's follow-ups."""
        action = tracker.person_action(action, kwargs.get("to"))
        if action != "update":
            kwargs.pop("to", None)
        if action == "make-job":
            raise ValueError("make-job works on follow-ups; move this item to later first")
        plan = self._load(repo, plan_id)
        store = self._items_store(repo, plan_id)
        if not store.path.is_file():
            raise KeyError(f"unknown item {store.scope.item_ref(item_id)}")
        try:
            with self._plan_items(repo, plan_id, tracker.BUSY_SECONDS) as ledger:
                if action == "defer":
                    self.factory.defer_to_followups(repo, ledger, item_id, kwargs.get("reason") or kwargs.get("note")
                                                    or "", kwargs.get("due"))
                    item = ledger.get(item_id)
                else:
                    item = tracker.human_action(ledger, item_id, action, **kwargs)
                    self._move_later(repo, ledger)
                    item = ledger.get(item_id)
        except tracker.ItemsBusy as exc:
            raise action_error(str(exc)) from None
        return {"item": tracker.decorate(item, store.scope, now=utc_now(), scope_stage=plan.get("stage"))}

    def _move_later(self, repo: str, ledger: Any) -> list[str]:
        """Plan items made Later in place move to the repo's follow-ups, as a person's defer does (inside the
        plan's transaction). Best effort: one the follow-ups cannot take now stays Later here."""
        due = tracker.add_days(tracker.today(), _housekeeping(self.config).followup_due_days)
        try:
            return tracker.move_later(ledger, lambda copies: self.factory.add_followups(repo, copies), due)
        except (OSError, ValueError) as exc:
            self._log("warn", f"could not move Later plan items to the follow-ups yet: {exc}", repo)
            return []

    def _record_findings(self, plan: dict, k: int, entries: list[dict]) -> None:
        """Each final-review finding is also a plan item (owner builder, verifier the lead), linked to its
        follow-up job once that is submitted, and closed when it merges."""
        with self._plan_items(plan["repo"], plan["id"]) as ledger:
            for entry in entries:
                item = ledger.add(tracker.FACTORY, entry["feature"], kind="finding", detail=entry.get("why") or "",
                                  owner="builder", verifier=LEAD, blocking=False,
                                  source={"type": "plan-review", "agent": LEAD, "file": f"review-{k}.md",
                                          "n": entry["n"], "quote": entry["feature"]})
                entry["item"] = item["id"]

    def _settle_items(self, plan: dict) -> None:
        """Plan items made for follow-up jobs follow their job: Working on it once submitted, Proven (job
        evidence) when it merged, To do again when it was rejected or cancelled."""
        linked = [entry for entry in plan["jobs"] if entry.get("item") and entry.get("job_id")]
        if not linked:
            return
        with self._plan_items(plan["repo"], plan["id"]) as ledger:
            for entry in linked:
                item = ledger.find(entry["item"])
                if item is None:
                    continue
                job_id, stage = entry["job_id"], entry.get("stage")
                if stage == "merged" and item["status"] not in tracker.TERMINAL:
                    ledger.change(tracker.FACTORY, item["id"], "verified", note=f"job {job_id} merged",
                                  evidence=[{"kind": "job", "ref": job_id, "detail": str(entry.get("merged_commit") or ""),
                                             "check": "ok"}])
                elif stage in DEAD_JOB_STAGES and item["status"] == "doing":
                    ledger.change(tracker.FACTORY, item["id"], "open", reason=f"job {job_id} was {stage}")
                elif stage in ACTIVE_JOB_STAGES and item["status"] in ("open", "proposed"):
                    ledger.change(tracker.FACTORY, item["id"], "doing", note=f"job {job_id} works on it",
                                  links={"job": job_id})

    def _ingest_lead(self, plan: dict, target: Path, journal: Path) -> None:
        """After a lead run: its Items calls and the ## Items lines of the file it wrote, as the plan's judge."""
        store = self._items_store(plan["repo"], plan["id"])
        text = _read_text(target)
        if not store.path.is_file() and "## items" not in text.lower() and not journal.is_file():
            return
        try:
            with self._plan_items(plan["repo"], plan["id"]) as ledger:
                aliases: dict[str, str] = {}
                notices = tracker.apply_journal(ledger, journal, LEAD, aliases=aliases)
                found, _ = tracker.apply_section(ledger, text, LEAD, target.name, aliases=aliases)
                ledger.set_notices(LEAD, notices + found + self._move_later(plan["repo"], ledger))
        except (OSError, ValueError) as exc:
            self._log("warn", f"plan {plan['id']}: tracked items: {exc}", plan["repo"])

    def _items_lines(self, plan: dict, target: Path) -> list[str]:
        """The lead's prompt lines for the plan's items, with items.md rewritten first ([] when there are none)."""
        store = self._items_store(plan["repo"], plan["id"])
        try:
            ledger = store.ledger()
            if not ledger.items:
                return []
            store.write_md(tracker.render_md(ledger, LEAD, title=f"plan {plan['id']}"))
        except (OSError, ValueError):
            return []
        return tracker.prompt_lines(ledger, LEAD, store.md_path, answer_file=str(target))

    # -- actions ----------------------------------------------------------------

    def create(self, repo: str, request: str, routing: Any = None, delivery: str | None = None,
               profile: str | None = None) -> dict:
        """Create a plan for request on repo and start the lead's Plan step; returns the plan.

        routing (an object of per-agent routes, or a preset name) is passed to every job the plan
        submits and routes the lead: its "lead" entry, or the preset's route for the lead.
        profile (a routing profile; None: the default profile, "": none; resolved now and kept in
        plan.json) is passed to every job and places the lead by its "lead" entry.
        delivery (merge, branch or folder; None: the config's merge.delivery, resolved now and kept
        in plan.json) is passed to every job the plan submits.
        ValueError, before the lead runs, for routing that does not resolve for the lead or for
        the factory's agents (an unknown preset, driver or model, a malformed route), or for an
        unknown delivery: the factory checks them with check_routing and check_delivery, with the
        messages submit gives.
        """
        if not isinstance(request, str) or not request.strip():
            raise ValueError("request must be a non-empty string")
        if routing is not None and not isinstance(routing, (dict, str)):
            raise ValueError("routing must be an object of per-agent routes or a preset name")
        repo_cfg = self.config.repo(repo)
        # Fail now, not in the lead's thread or at the first submit, when a route does not resolve:
        # the factory's agents exactly as submit would check them, then the lead.
        self.factory.check_routing(repo, routing, profile)
        profile = self.factory.check_profile(profile, routing)   # the name, kept: the default may change later
        delivery = self.factory.check_delivery(delivery)
        self._lead_setup(repo_cfg.path, routing, profile)
        base = gitops.detect_base(repo_cfg.path, repo_cfg.base)
        base_commit = gitops.rev_parse(repo_cfg.path, base)
        with self._lock:
            plan_id, plan_dir = self._allocate(repo, request)
            write_atomic(plan_dir / REQUEST_FILE, request)
            now = utc_now()
            plan = {
                "id": plan_id, "repo": repo, "request": request, "stage": "planning", "reason": None,
                "failed_step": None, "base": base, "base_commit": base_commit, "plan_dir": str(plan_dir),
                "jobs": [], "reviews": [], "routing": routing, "profile": profile, "delivery": delivery,
                "usage": zero_usage(), "cost": zero_cost(), "created_at": now, "updated_at": now,
            }
            self._save(plan)
            self._start_lead(plan, "plan")
            return plan

    def update_plan_text(self, repo: str, plan_id: str, text: str) -> dict:
        """Replace plan.md with a human's edit (stage ready, needs-human or blocked) and re-parse it.

        A READY text makes the plan "ready". An item whose feature text is unchanged keeps the
        factory job it already has, unless that job was rejected or cancelled (the item then gets
        a new job on Run); follow-up jobs from final reviews are kept, except rejected or cancelled
        ones. ActionError if the edit drops or rewords the line of a job still in progress (cancel
        that job first). While the lead's questions wait for a human (needs-human after the Plan
        step), a PLAN: QUESTIONS text with the human's answers is saved as is for Retry to hand to
        the lead. ValueError if text does not parse as such (nothing is written then).
        """
        if not isinstance(text, str):
            raise ValueError("plan text must be a string")
        parsed = parse_plan(text)
        with self._lock:
            plan = self._load(repo, plan_id)
            if plan["stage"] not in EDITABLE_STAGES:
                raise action_error(f"plan {plan_id} is {plan['stage']}; it can be edited while "
                                   f"{', '.join(EDITABLE_STAGES)}")
            plan_file = Path(plan["plan_dir"]) / PLAN_FILE
            if parsed.status != "READY":
                if (plan["stage"], plan.get("failed_step")) != ("needs-human", "plan"):
                    raise ValueError("an edited plan must start with PLAN: READY and list its jobs (a PLAN: "
                                     "QUESTIONS text is taken only as answers to the lead's questions)")
                write_atomic(plan_file, text)
                plan["reason"] = "the human answered the lead's questions in plan.md; Retry re-plans with them"
                return self._save(plan)
            self._sync_jobs(plan)
            jobs, dropped = _merge_jobs(plan["jobs"], parsed.jobs)
            active = [entry for entry in dropped if entry.get("stage") in ACTIVE_JOB_STAGES]
            if active:
                names = "; ".join(f"{entry['n']}. {entry['feature']!r} ({entry['job_id']} is {entry['stage']})"
                                  for entry in active)
                raise action_error(f"plan {plan_id}: the edit drops or rewords jobs still in progress: {names}. "
                                   "Keep their lines as they are, or cancel those jobs first")
            write_atomic(plan_file, text)
            plan["jobs"] = jobs
            plan.update(stage="ready", reason=None, failed_step=None)
            self._reindex(plan)
            return self._save(plan)

    def run(self, repo: str, plan_id: str) -> dict:
        """Start a ready plan: submit every job whose dependencies have merged."""
        with self._lock:
            plan = self._load(repo, plan_id)
            if plan["stage"] != "ready":
                raise action_error(f"plan {plan_id} is {plan['stage']}; only a ready plan can run")
            self._sync_jobs(plan)
            plan.update(stage="running", reason=None)
            self._advance(plan, True)
            return plan

    def cancel(self, repo: str, plan_id: str) -> dict:
        """Cancel the plan: stop a running lead step and cancel the plan's active jobs."""
        with self._lock:
            plan = self._load(repo, plan_id)
            if plan["stage"] in FINISHED_STAGES:
                raise action_error(f"plan {plan_id} is already {plan['stage']}")
            plan.update(stage="cancelled", reason="cancelled", failed_step=None)
            lead = self._leads.get((repo, plan_id))
            if lead is not None:
                lead[1].set()
            active = [entry["job_id"] for entry in plan["jobs"]
                      if entry.get("job_id") and entry.get("stage") in ACTIVE_JOB_STAGES]
            self._save(plan)
        for job_id in active:
            try:
                self.factory.cancel(repo, job_id)
            except Exception as exc:  # the job may have just left its active stage
                self._log("warn", f"plan {plan_id}: could not cancel job {job_id}: {exc}", repo)
        return plan

    def retry(self, repo: str, plan_id: str) -> dict:
        """Re-run the step that stopped a failed or needs-human plan (its failed_step)."""
        with self._lock:
            plan = self._load(repo, plan_id)
            step = plan.get("failed_step")
            if plan["stage"] not in ("failed", "needs-human") or not step:
                raise action_error(f"plan {plan_id} is {plan['stage']}; nothing to retry")
            if (repo, plan_id) in self._leads:
                raise action_error(f"plan {plan_id}: the lead is still running")
            plan.update(reason=None, failed_step=None)
            if step in LEAD_STAGES:
                plan["stage"] = LEAD_STAGES[step]
                self._save(plan)
                self._start_lead(plan, step)
            else:
                self._sync_jobs(plan)
                plan["stage"] = "running"
                self._advance(plan, True)
            return plan

    # -- following jobs -------------------------------------------------------------

    def _dispatch(self) -> None:
        while not self._stopping.is_set():
            try:
                event = self._events.get(timeout=POLL_SECONDS)
            except queue.Empty:
                continue
            if event is None or event.type != "job.updated":   # None: stop() waking this thread
                continue
            try:
                self._on_job_updated(event)
            except Exception as exc:  # one bad plan must not stop the others from advancing
                self._log("error", f"plans: handling {event.job}: {type(exc).__name__}: {exc}", event.repo)

    def _on_job_updated(self, event: Event) -> None:
        job = event.data.get("job")
        if not isinstance(job, dict):
            return
        repo, job_id = job.get("repo") or event.repo, job.get("id") or event.job
        with self._lock:
            plan_id = self._index.get((repo, job_id))
            if plan_id is None:
                return
            plan = self._load(repo, plan_id)
            entry = next((e for e in plan["jobs"] if e.get("job_id") == job_id), None)
            if entry is not None:
                self._advance(plan, _sync_entry(entry, job))

    def _sync_jobs(self, plan: dict) -> bool:
        """Copy the factory's current stage of every submitted job into the plan; True if any changed.
        Jobs the factory no longer knows keep their last known stage."""
        changed = False
        for entry in plan["jobs"]:
            if entry.get("job_id"):
                with contextlib.suppress(KeyError):
                    changed |= _sync_entry(entry, self.factory.job(plan["repo"], entry["job_id"]))
        return changed

    def _advance(self, plan: dict, dirty: bool) -> None:
        """Derive a running/blocked plan's stage from its jobs, submit newly unblocked jobs and start
        the final review once all have merged; save the plan if anything changed (or dirty)."""
        final_review = False
        if plan["stage"] in ("running", "blocked"):
            blocking = next((e for e in plan["jobs"] if e.get("job_id") and e.get("stage") in BLOCKING_JOB_STAGES),
                            None)
            if blocking is not None:
                reason = f"job {blocking['job_id']} is {blocking['stage']}"
                if blocking["stage"] == "delivered":
                    reason += " (not merged): approve it to merge it, and the plan goes on"
                dirty |= (plan["stage"], plan["reason"]) != ("blocked", reason)
                plan.update(stage="blocked", reason=reason)
            else:
                dirty |= plan["stage"] == "blocked"
                plan.update(stage="running", reason=None)
                try:
                    dirty |= self._submit_ready(plan)
                except Exception as exc:
                    self._fail(plan, "run", f"could not submit a job: {exc}")
                    dirty = True
                if plan["stage"] == "running" and all(e.get("stage") == "merged" for e in plan["jobs"]):
                    plan["stage"] = "final-review"
                    final_review = dirty = True
        if dirty:
            self._save(plan)
            try:
                self._settle_items(plan)
            except (OSError, ValueError, tracker.ItemError) as exc:
                self._log("warn", f"plan {plan['id']}: tracked items: {exc}", plan["repo"])
        if final_review:
            self._start_lead(plan, "final-review")

    def _submit_ready(self, plan: dict) -> bool:
        stages = {entry["n"]: entry.get("stage") for entry in plan["jobs"]}
        submitted = False
        for entry in plan["jobs"]:
            if entry.get("job_id") or any(stages.get(dep) != "merged" for dep in entry["after"]):
                continue
            # A plan.json from before delivery modes (or profiles) has none: its jobs get the config's default.
            job = self.factory.submit(plan["repo"], _job_feature(plan, entry), routing=plan.get("routing"),
                                      delivery=plan.get("delivery"), **_plan_profile(plan))
            entry.update(job_id=job["id"], stage=job.get("stage") or "queued")
            self._index[(plan["repo"], job["id"])] = plan["id"]
            submitted = True
        return submitted

    # -- the lead -------------------------------------------------------------------

    def _start_lead(self, plan: dict, step: str) -> None:
        key = (plan["repo"], plan["id"])
        cancel = threading.Event()
        thread = threading.Thread(target=self._lead_step, args=(plan["repo"], plan["id"], step, cancel),
                                  name=f"lead-{plan['id']}", daemon=True)
        self._leads[key] = (thread, cancel)
        thread.start()

    def _lead_step(self, repo: str, plan_id: str, step: str, cancel: threading.Event) -> None:
        try:
            if step == "plan":
                self._plan_step(repo, plan_id, cancel)
            else:
                self._review_step(repo, plan_id, cancel)
        except Exception as exc:  # git or config trouble: the plan fails instead of hanging in its step
            with self._lock:
                self._release_lead(repo, plan_id)
                plan = self._load(repo, plan_id)
                if plan["stage"] == LEAD_STAGES[step] and not cancel.is_set():
                    self._fail(plan, step, f"{type(exc).__name__}: {exc}")
                    self._save(plan)
        finally:
            with self._lock:
                self._release_lead(repo, plan_id)

    def _release_lead(self, repo: str, plan_id: str) -> None:
        """Forget the calling thread's lead step (not a newer one a retry has started meanwhile)."""
        lead = self._leads.get((repo, plan_id))
        if lead is not None and lead[0] is threading.current_thread():
            del self._leads[(repo, plan_id)]

    def _plan_step(self, repo: str, plan_id: str, cancel: threading.Event) -> None:
        plan = self._load(repo, plan_id)
        plan_dir = Path(plan["plan_dir"])
        target = plan_dir / PLAN_FILE
        task = [f"Task: Plan. Read {plan_dir / REQUEST_FILE} and write {target}."]
        earlier = _archive_questions(target)
        if earlier:
            task.append(f"You asked questions before; they are in {', '.join(map(str, earlier))}, with the "
                        "human's answers where they added any. Plan with those answers and ask again only "
                        "what is still unclear.")
        cost: dict = {}
        result = self._run_lead(
            plan, plan["base_commit"], target, cancel, task,
            valid=lambda: plan_status(_read_text(target)) is not None,
            retry_note=f"You did not write {target}, or its first line is not PLAN: READY or PLAN: QUESTIONS.",
            cost=cost)
        with self._lock:
            plan = self._finish_lead(repo, plan_id, "planning", result, cancel, cost)
            if plan is None:
                return
            if result.ok:
                try:
                    parsed = parse_plan(_read_text(target))
                except ValueError as exc:
                    plan.update(stage="needs-human", reason=f"the lead's plan is invalid: {exc}",
                                failed_step="plan")
                else:
                    if parsed.status == "QUESTIONS":
                        plan.update(stage="needs-human", reason="the lead has questions (see plan.md)",
                                    failed_step="plan")
                    else:
                        plan["jobs"] = _merge_jobs(plan["jobs"], parsed.jobs)[0]
                        plan.update(stage="ready", reason=None)
                        self._reindex(plan)
            self._save(plan)

    def _review_step(self, repo: str, plan_id: str, cancel: threading.Event) -> None:
        plan = self._load(repo, plan_id)
        repo_path = self.config.repo(repo).path
        k = len(plan["reviews"]) + 1
        target = Path(plan["plan_dir"]) / f"review-{k}.md"
        with contextlib.suppress(FileNotFoundError):
            target.unlink()
        base, base_commit = plan["base"], plan["base_commit"]
        ours, others = self._merge_log(repo_path, plan)
        task = [f"Task: Final review {k}. Every job of the plan has merged into {base}. Write {target}.",
                f"The combined change is `git diff {base_commit}..{base}` (run it in the checkout).",
                "Merge commits of this plan (git log --oneline):", *(f"  {line}" for line in ours or ["(none)"])]
        if others:
            task += [f"Other merges in {base_commit[:12]}..{base} are not part of this plan:",
                     *(f"  {line}" for line in others)]
        task += self._items_lines(plan, target)
        cost: dict = {}
        result = self._run_lead(
            plan, gitops.rev_parse(repo_path, base), target, cancel, task,
            valid=lambda: read_verdict(target, "review") is not None,
            retry_note=f"Your review file {target} is missing or its first line is not a verdict.", cost=cost)
        with self._lock:
            plan = self._finish_lead(repo, plan_id, "final-review", result, cancel, cost)
            if plan is None:
                return
            if result.ok:
                self._apply_review(plan, k, target)
            self._advance(plan, True)

    def _apply_review(self, plan: dict, k: int, target: Path) -> None:
        verdict = read_verdict(target, "review")
        if verdict is None:
            plan.update(stage="needs-human", reason=f"the lead wrote no valid {target.name}",
                        failed_step="final-review")
            return
        plan["reviews"].append({"k": k, "verdict": verdict})
        if verdict == "PASS":
            plan.update(stage="done", reason=None)
        elif k > 1:
            plan.update(stage="needs-human", reason=f"final review {k} still asks for changes",
                        failed_step="final-review")
        else:
            findings = parse_findings(_read_text(target))
            if not findings:
                plan.update(stage="needs-human", reason=f"final review {k} asks for changes but lists no "
                                                        "findings", failed_step="final-review")
                return
            n = max((entry["n"] for entry in plan["jobs"]), default=0)
            added = []
            for offset, (feature, why) in enumerate(findings, start=1):
                added.append({"n": n + offset, "feature": feature, "after": [], "job_id": None,
                              "stage": None, "review": k, "why": why})
            try:
                self._record_findings(plan, k, added)
            except (OSError, ValueError, tracker.ItemError) as exc:
                self._log("warn", f"plan {plan['id']}: could not record the findings as items: {exc}", plan["repo"])
            plan["jobs"] += added
            plan.update(stage="running", reason=None)

    def _run_lead(self, plan: dict, commit: str, target: Path, cancel: threading.Event, task: list[str],
                  valid: Callable[[], bool], retry_note: str, cost: dict | None = None) -> AgentRun:
        """Run the lead on a detached checkout of commit (removed afterwards), writing only target;
        a run that ends without a valid target is re-run once with retry_note. The runs' cost is added
        to cost ({usd, priced, unpriced_runs}) when given."""
        repo_cfg = self.config.repo(plan["repo"])
        config, agent, route = self._lead_setup(repo_cfg.path, plan.get("routing"), plan.get("profile"))
        plan_dir = Path(plan["plan_dir"])
        checkout = self._checkout_path(plan)
        self._discard_checkout(plan)
        checkout.parent.mkdir(parents=True, exist_ok=True)
        gitops.git(repo_cfg.path, "worktree", "add", "--detach", "--", checkout, commit,
                   timeout=gitops.SLOW_TIMEOUT)
        paths = (f"Plan folder: {plan_dir}. Checkout: {checkout} (detached worktree of {plan['base']} "
                 f"at {commit[:12]}). Base: {plan['base']}.")
        prompt = "\n".join([paths, *task])
        usage = zero_usage()
        try:
            for attempt in range(2):
                log_path = next_log_path(plan_dir / "logs", LEAD)
                emit = event_emitter(self.bus, plan["repo"], LEAD, log_path, plan=plan["id"])
                journal = plan_dir / "sessions" / f"{LEAD}.items.jsonl"
                try:
                    items = tracker.ItemsContext(self._items_store(plan["repo"], plan["id"]), LEAD, journal,
                                                 settings=_housekeeping(config), answer_file=str(target),
                                                 secrets=self._item_secrets())
                except (OSError, ValueError):
                    items = None
                placed = place_run(config, route, getattr(self.factory, "reach", None), cancel, emit,
                                   f"lead {plan['repo']}/{plan['id']}")
                if placed is None:
                    result = AgentRun(ok=False, error="cancelled while waiting for a slot")
                    break
                model, info, notes, release = placed
                try:
                    ctx = agent_context(
                        config, repo_cfg.path, agent, model, job=plan,
                        cwd=checkout, job_dir=plan_dir, write_paths=[target], read_roots=[checkout, plan_dir],
                        log_path=log_path, emit=emit, cancel=cancel,
                        session_path=plan_dir / "sessions" / f"{log_path.stem}.json", items=items)
                    if notes:
                        emit("log", {"level": "warn" if "overflow" in info else "info",
                                     "message": f"lead runs on {model.provider.name}/{model.model}: " + "; ".join(notes)})
                    result = run_agent(self.runner_factory, config, route, ctx,
                                       prompt if attempt == 0 else f"{prompt}\n{retry_note}", labels=info)
                finally:
                    release()
                add_usage(usage, result.usage)
                if cost is not None:
                    add_cost(cost, *run_cost_of(ctx, result))
                if not result.ok or cancel.is_set() or valid():
                    break
            if result.ok and not cancel.is_set():
                self._ingest_lead(plan, target, plan_dir / "sessions" / f"{LEAD}.items.jsonl")
        finally:
            self._discard_checkout(plan)
        result.usage = usage
        return result

    def _finish_lead(self, repo: str, plan_id: str, stage: str, result: AgentRun,
                     cancel: threading.Event, cost: dict | None = None) -> dict | None:
        """The reloaded plan with the run's usage (and cost) added, failed if the run failed; None (after
        saving them) when the plan left stage meanwhile, e.g. because it was cancelled."""
        self._release_lead(repo, plan_id)
        plan = self._load(repo, plan_id)
        add_usage(plan["usage"], result.usage)
        if cost:
            total = plan.setdefault("cost", zero_cost())
            total["usd"] = round(float(total.get("usd") or 0.0) + float(cost.get("usd") or 0.0), 6)
            total["priced"] = bool(total.get("priced", True)) and bool(cost.get("priced", True))
            total["unpriced_runs"] = int(total.get("unpriced_runs") or 0) + int(cost.get("unpriced_runs") or 0)
        if plan["stage"] != stage or cancel.is_set():
            self._save(plan)
            return None
        if not result.ok:
            self._fail(plan, "plan" if stage == "planning" else "final-review",
                       result.error or "the lead run failed")
        return plan

    def _merge_log(self, repo_path: Path, plan: dict) -> tuple[list[str], list[str]]:
        """``git log --oneline`` lines of the merges into base since base_commit: the plan's, the others."""
        shas = {entry.get("merged_commit") for entry in plan["jobs"]}
        out = gitops.git(repo_path, "log", "--first-parent", "--merges", "--format=%H %h %s",
                         f"{plan['base_commit']}..{plan['base']}", "--")
        ours: list[str] = []
        others: list[str] = []
        for line in out.splitlines():
            full, _, oneline = line.partition(" ")
            (ours if full in shas else others).append(oneline)
        return ours, others

    def _lead_setup(self, repo_path: Path, routing: Any, profile: str | None = None) -> tuple[Config, AgentDef, Route]:
        """(config the lead runs in, the lead agent, its route): for a preset name the preset merged
        over the live config (§23: a per-run preset applies to every step), else the live config;
        the profile's "lead" placement, with routing's "lead" entry over it, as the override.
        ValueError if it does not resolve."""
        config = _routing_config(self.config, routing)
        agent = load_agent(config, repo_path, LEAD)
        placements = merged_placements(profile_routes(config.raw, profile or None),
                                       {LEAD: _lead_override(routing) or {}})
        return config, agent, agent_route(config, agent, placements.get(LEAD) or None)

    def _checkout_path(self, plan: dict) -> Path:
        return Path(self.config.data_dir) / "worktrees" / plan["repo"] / f"{plan['id']}-lead"

    def _discard_checkout(self, plan: dict) -> None:
        checkout = self._checkout_path(plan)
        if checkout.exists():
            try:
                gitops.remove_worktree(self.config.repo(plan["repo"]).path, checkout)
            except (KeyError, gitops.GitError, OSError) as exc:
                self._log("warn", f"plan {plan['id']}: could not remove {checkout}: {exc}", plan["repo"])

    # -- storage ----------------------------------------------------------------------

    def _repo_dir(self, repo: str) -> Path:
        if not isinstance(repo, str) or not REPO_NAME_RE.fullmatch(repo):
            raise ValueError(f"invalid repo name {repo!r}")
        return self.root / repo

    def _allocate(self, repo: str, request: str) -> tuple[str, Path]:
        repo_dir = self._repo_dir(repo)
        repo_dir.mkdir(parents=True, exist_ok=True)
        numbers = [int(m.group(1)) for m in map(_PLAN_NUMBER_RE.match, os.listdir(repo_dir)) if m]
        slug = slugify(request)
        for number in range(max(numbers, default=0) + 1, MAX_PLAN_NUMBER + 1):
            plan_id = f"P{number:03d}-{slug}"
            try:
                os.mkdir(repo_dir / plan_id)
            except FileExistsError:
                continue
            return plan_id, repo_dir / plan_id
        raise RuntimeError(f"no plan numbers left for repo {repo!r}")

    def _existing_dir(self, repo: str, plan_id: str) -> Path:
        try:
            plan_dir = self.plan_dir(repo, plan_id)
        except ValueError:
            raise KeyError(f"unknown plan {repo}/{plan_id}") from None
        if not plan_dir.is_dir():
            raise KeyError(f"unknown plan {repo}/{plan_id}")
        return plan_dir

    def _load(self, repo: str, plan_id: str) -> dict:
        try:
            return read_json(self._existing_dir(repo, plan_id) / STATE_FILE)
        except FileNotFoundError:
            raise KeyError(f"unknown plan {repo}/{plan_id}") from None

    def _save(self, plan: dict) -> dict:
        plan["updated_at"] = utc_now()
        write_json(Path(plan["plan_dir"]) / STATE_FILE, plan)
        self.bus.publish("plan.updated", {"plan": plan}, repo=plan["repo"])
        return plan

    def _reindex(self, plan: dict) -> None:
        for key in [key for key, plan_id in self._index.items() if key[0] == plan["repo"] and plan_id == plan["id"]]:
            del self._index[key]
        for entry in plan.get("jobs") or []:
            if entry.get("job_id"):
                self._index[(plan["repo"], entry["job_id"])] = plan["id"]

    @staticmethod
    def _fail(plan: dict, step: str, reason: str) -> None:
        plan.update(stage="failed", failed_step=step, reason=reason)

    def _log(self, level: str, message: str, repo: str | None = None) -> None:
        self.bus.publish("log", {"level": level, "message": message}, repo=repo)


def _plan_profile(plan: dict) -> dict:
    """submit's profile argument for a plan's jobs: the plan's own ("" when it has none), or nothing for
    a plan.json from before profiles (its jobs get the default profile)."""
    return {"profile": plan.get("profile") or ""} if "profile" in plan else {}


def _lead_override(routing: Any) -> dict | None:
    entry = routing.get(LEAD) if isinstance(routing, dict) else None
    return entry if isinstance(entry, dict) else None


def _routing_config(config: Config, routing: Any) -> Config:
    """The config routes resolve in: for a preset name, that preset merged over config (ValueError if
    the name is unknown), as Factory._routing_snapshot does; else config."""
    if not isinstance(routing, str):
        return config
    # Imported late: settings pulls in the providers, and only a preset needs it.
    from .settings import preset_config

    return preset_config(config, routing)


def _archive_questions(target: Path) -> list[Path]:
    """Before a Plan run: move a PLAN: QUESTIONS plan.md (with any answers the human added) to the
    next questions-<k>.md, delete any other plan.md, and return every questions-<k>.md in order."""
    folder = target.parent

    def archived() -> list[tuple[int, str]]:
        return sorted((int(m.group(1)), m.group(0)) for m in map(_QUESTIONS_RE.fullmatch, os.listdir(folder)) if m)

    if plan_status(_read_text(target)) == "QUESTIONS":
        k = max((n for n, _ in archived()), default=0) + 1
        os.replace(target, folder / f"questions-{k}.md")
    else:
        with contextlib.suppress(FileNotFoundError):
            target.unlink()
    return [folder / name for _, name in archived()]


def _sync_entry(entry: dict, job: dict) -> bool:
    """Copy the job's stage (and merge commit) into its plan entry; True if the entry changed."""
    before = (entry.get("stage"), entry.get("merged_commit"))
    entry["stage"] = job.get("stage")
    if job.get("merged_commit"):
        entry["merged_commit"] = job["merged_commit"]
    return (entry.get("stage"), entry.get("merged_commit")) != before


def _merge_jobs(old: list[dict], items: list[dict]) -> tuple[list[dict], list[dict]]:
    """(plan entries for freshly parsed items, the earlier submitted entries that are left out).

    An item whose feature matches an earlier submitted entry keeps that entry's job, unless the job
    was rejected or cancelled (it can never merge, so the item gets a new job). Follow-up entries
    (from final reviews) are kept, renumbered after the items, except rejected or cancelled ones.
    """
    def dead(entry: dict) -> bool:
        return bool(entry.get("job_id")) and entry.get("stage") in DEAD_JOB_STAGES

    submitted = [entry for entry in old if entry.get("job_id") and "review" not in entry and not dead(entry)]
    dropped = [entry for entry in old if dead(entry)]
    entries = []
    for item in items:
        entry = {**item, "job_id": None, "stage": None}
        match = next((e for e in submitted if e["feature"] == item["feature"]), None)
        if match is not None:
            submitted.remove(match)
            entry.update({key: match[key] for key in ("job_id", "stage", "merged_commit") if key in match})
        entries.append(entry)
    followups = [entry for entry in old if "review" in entry and not dead(entry)]
    for n, entry in enumerate(followups, start=len(entries) + 1):
        entries.append({**entry, "n": n})
    return entries, dropped + submitted


def _job_feature(plan: dict, entry: dict) -> str:
    """The feature text submitted for a plan job: its line, the review's reason, the plan context."""
    text = entry["feature"]
    if entry.get("why"):
        text += f"\n\nFrom final review {entry['review']} of the plan: {entry['why']}"
    return f"{text}\n\nPlan context: {plan['id']} — see {plan['plan_dir']}/{PLAN_FILE}"
