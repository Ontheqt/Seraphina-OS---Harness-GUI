"""Factory: the job queue, the worker threads and the state machine of the factory line.

DESIGN.md section 6 is the contract. Each step of the line is one method below
(_setup, _spec, _build, _build_check, _review_round, _approve, _gate, _merge,
_deliver) that returns the name of the next step, or None once the job has stopped
(needs-human, merged, delivered). A job's delivery mode (section 25: merge, branch
or folder, snapshotted at submit) decides what happens after APPROVE. Every change
to a job goes through _set/_update, which write job.json and publish job.updated
with the whole job, so reshaping the line means editing those methods and the
STEPS table.

A job runs from the routing snapshot taken when it was submitted (section 23):
each route carries its whole model and provider entry, so editing models,
providers, drivers or agent routes in the config never changes a running job
(only secrets are read live); set_routing changes a stopped one. There is no
silent fallback between providers (section 20): a route switches servers only
the ways its snapshot says (its builds, its overflow, the builder's escalation
rungs; routing.py), and each run's agent.start and job.runs say which and why.

Every job has a ledger of tracked items (section 32, items.py): what the spec
requires, what reviewers found, what agents promised. The orchestrator is its only
writer: after each run it takes in the step's contract files, the agent's journal
of Items tool calls and its ``## Items`` lines, sweeps its durable text for
unrecorded promises and checks commit and file evidence against the branch. The
build check sends the builder back once for must-do items it left untouched, the
gate holds a job while a must-do item is not proven, and whatever is still open
when a job ends moves to the repo's follow-ups (or its plan).

The write policy of section 7 is checked after every agent run, whatever its
driver (the builtin harness enforces it for its file tools, but neither CLI
agents nor an unsandboxed Bash are contained): a change in the job folder
outside the run's write paths is reverted and stops the job. Reviewers run in
parallel, so their round is checked as a whole: anything but their own review
files and sessions, and any review file that changed after its own reviewer
finished. A write into a sibling's review file while that sibling still runs
cannot be told apart from the sibling's own write; only a sandbox (bwrap) or a
dedicated Unix user contains that (section 17). confirmed.md is the
orchestrator's own: a person's confirm action writes it, and it is put back
to that record before every approver run.

A builder whose driver cannot commit (the codex-worker preset, ``commits: harness``; DESIGN §44) is committed by the
harness after every run on it, corrective resumes included, after the write-policy check: protected paths refused,
the staged diff scanned for secrets (codex/work.py). A repository's ``verify`` command then runs, always under
bwrap, and the build check gives the builder one corrective resume when it fails. Every Codex run gets a receipt in
<data_dir>/receipts/, outside every root Codex can write, and a run that did not end cleanly is reconciled (git
status and the diff's hash) and never replayed (DESIGN §40).
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import hashlib
import os
import queue
import re
import shutil
import stat
import tempfile
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator, NamedTuple

from . import gitops
from . import items as tracker
from .codex import work as codex_work
from . import outcomes as rule
from .agentdefs import AgentDef, discover_agents, read_small_text, required_agents
from .config import (Config, HousekeepingConfig, ModelConfig, ProviderConfig, RepoConfig, agent_dirs, deep_merge,
                     expand_path, provider_secret_values, sandbox_masks)
from .delivery import check_delivery, export_folder
from .drivers.cli import cli_model
from .events import EventBus, env_secret_values, truncate, utc_now
from .gitops import GitError, MergeBusy
from .gridcard import factory_grid
from .gridcard import receipt as grid_receipt
from .housekeeping import AgentTokens, HousekeepingManager, agent_update
from .driftsources import DriftSources
from .custody import (Claims, CustodyDenied, custody_on_server, lease_of, leased, register_names, residency_matches,
                      server_of)
from .custody import REFUSAL_WORDS as CUSTODY_WORDS
from .custody import client_for as custody_client_for
from .custody import parse_settings as parse_custody
from .custody import lease_problem as custody_lease_problem
from .custody import needs_lease as custody_needs_lease
from .custody import settings_of as custody_settings_of
from .custody import set_journal as custody_journal
from .harness.providers import make_provider
from .harness.sandbox import Sandbox, mask_plan
from .jobs import JobStore, read_verdict
from .routing import (JUDGE_HINT, REACHABILITY, ROUTE_KEYS as PLACEMENT_KEYS, Build, Reachability, Role, add_cost,
                      alternate_text, check_placement, choose, choose_build, config_builds, effort_body,
                      escalation_start, estimate,
                      independence_map, is_judge, judge_choice, merged_placements, model_family, overflow_key,
                      placement_reason, price_table, profile_name, profile_routes, reason, route_family, run_cost,
                      shared_model, shared_status, take_slot, zero_cost)
from .runners import AgentRun, RunContext, Runner, add_usage, launch_model, make_runner, outcome_of
from .skills import skill_dirs

JobKey = tuple[str, str]
RunnerFactory = Callable[[Config, str], Runner]

ACTIVE_STAGES = frozenset({"queued", "spec", "build", "review", "approve", "merging"})
HUMAN_STAGES = frozenset({"needs-human", "failed"})
DELIVERED = "delivered"   # approved and delivered as a branch or a folder; a human approve merges it
REJECTABLE_STAGES = frozenset({"needs-human", "failed", "cancelled", DELIVERED})
END_STAGES = frozenset({"merged", DELIVERED, "rejected", "cancelled"})   # open items carry forward at each

# step -> (the stage the job shows while the step waits or runs, the failed_step it records)
STEPS: dict[str, tuple[str, str]] = {
    "setup": ("spec", "setup"),
    "spec": ("spec", "spec"),
    "build": ("build", "build"),
    "build_check": ("build", "build"),
    "review": ("review", "review"),
    "approve": ("approve", "approve"),
    "gate": ("approve", "gate"),
    "override": ("merging", "merge"),
    "merge": ("merging", "merge"),
    "deliver": ("approve", "deliver"),
}

PORT_OFFSETS = {"builder": 0, "reviewer-ux": 1, "reviewer-ui": 2}
REVIEWED = ("PASS", "CHANGES")
SPECIAL_MODES = {"120000": "symlink", "160000": "submodule"}
ALWAYS_PROTECTED = (".factory/**",)   # a branch must never change the factory's own agents unreviewed
ROUTE_KEYS = frozenset(PLACEMENT_KEYS)   # model, driver, extra_body, effort, escalate, reason, independent_alternate
# a job's independence (section 23) is its judge runs' worst: a run on the builder's family the routing accepted,
# code from a chat whose models are not recorded, a run on the builder's family with no way out
INDEPENDENCE_RANK = {"accepted": 1, "unknown": 2, "reduced": 3}
# What a routing snapshot keeps of the model and provider entries (section 23). Secrets are never
# kept: the api key is read from api_key_env/api_key_file at call time, and secret-looking headers
# come from the live provider of the same name and base_url.
MODEL_FIELDS = ("max_tokens", "stream", "context_tokens", "keep_reasoning", "allow_history_edits", "eager_tools")
PROVIDER_FIELDS = ("type", "base_url", "api_key_env", "api_key_file", "timeout", "auth", "max_concurrency",
                   "concurrency_scope")
# Kept in a snapshot only when set: prices (model entries) and the placement settings (provider entries).
PRICE_FIELDS = ("price_in", "price_out", "price_cached_in")
PLACEMENT_FIELDS = ("kind", "loaded_model_url", "allow_model_swap", "swap_back", "overflow_after_seconds",
                    "tokens_per_second", "lazy_load", "custody")
HANDOVER_CHARS = 8000               # each file a hand-over prompt quotes (build.md, a review)
HANDOVER_DIFF_CHARS = 40000         # the diff it quotes
SWAP_BACK_TIMEOUT = 600.0           # seconds a swap-back request may take (a model load)
USAGE_KEYS = ("input_tokens", "output_tokens", "cached_tokens", "calls")
SECRET_HEADER_RE = re.compile(r"key|token|secret|password|auth", re.IGNORECASE)
AUTH_VALUE_RE = re.compile(r"^(?:bearer|basic|token)\s+", re.IGNORECASE)
MIN_SECRET_CHARS = 8
# Write-policy check (section 7): the job folder minus what the orchestrator writes during runs.
# Top-level names: job.json is checked by _save, the items files by their ItemStore (a person may act on items
# while an agent runs, so they change during runs), confirmed.md by _restore_confirmed before the approver.
POLICY_SKIP = frozenset({"logs", "job.json", "items.json", "items.log.jsonl", "items.md", "confirmed.md"})
POLICY_MAX_FILES = 5000
POLICY_HASH_BYTES = 64 << 20        # larger files are compared by size and mtime
POLICY_KEEP_BYTES = 1 << 20         # files up to this size are kept so a change can be reverted
POLICY_KEEP_TOTAL = 32 << 20
POLICY_OVERFLOW = f"(more than {POLICY_MAX_FILES} files)"
POLICY_LIST = 5
SETUP_TIMEOUT = 1800.0
SETUP_LOG_CHARS = 4000
NOTE_CHARS = 300
SECRETS_TTL = 2.0                   # seconds Factory.secret_values reuses its last answer
BUSY_WAIT = tracker.BUSY_SECONDS     # a person's item action waits this long for a scope mid-ingest (then 409)
SCOUT_AGENT = "repo-scout"
SCOUT_DRAFT = "AGENTS.draft.md"

SPEC_FROM_BRANCH = "The work is already on the branch; describe what it does and what it must do."
SPEC_RETRY = "You did not write spec.md."
BUILD_MD_FIX = "build.md must start with STATUS: READY or STATUS: BLOCKED."
COMMIT_FIX = "Commit or discard your uncommitted changes, then update build.md."
VERIFY_FIX = ("The harness ran the repository's verify command on your latest commit and it failed (exit {code}). "
              "Fix the code so it passes, then update build.md. The end of its output:\n{tail}")
ITEMS_FIX = ("These blocking items are not addressed in build.md: {ids}. Add a ## Items line for each (done with "
             "evidence, or doing with what is left), then update build.md.")
REVIEW_RETRY = "Your review file is missing or its first line is not a verdict."
MERGE_BUSY = "approved; main checkout busy — approve again when clean"
INTERRUPTED = "interrupted (server restarted)"
STOPPED = "interrupted (server stopped)"
# Files git ignores below these folders that a Codex run made or changed are caches: the harness removes them after
# the run, so verify never reads one (one it cannot remove fails verify like any other ignored file the run left)
VERIFY_CACHES = frozenset({"__pycache__", ".pytest_cache"})
IGNORED_KEEP = 200                  # job.codex_ignored: at most this many paths (more: counted by time, see below)
IGNORED_SLACK = 1.0                 # seconds: a file's ctime runs on a coarser clock than time.time() (and the
                                    # wait after the harness's own ACL change, before a run's start)


class ActionError(Exception):
    """A human action the job's current stage does not allow (the server answers 409)."""


class OpenItemsError(ActionError):
    """A person's approve would move must-do items on without naming them (409 {open_blocking: [ids]})."""

    def __init__(self, message: str, open_blocking: list[str]) -> None:
        super().__init__(message)
        self.open_blocking = list(open_blocking)
        self.payload = {"open_blocking": list(open_blocking)}


class StepError(Exception):
    """A step cannot go on; the job goes to `failed` and a retry re-enters that step."""


@dataclasses.dataclass
class _Placement:
    """Where one run goes (routing.py): the route that runs (the agent's route, an escalation rung, a
    judge's independent alternate or the overflow), which of its builds, what agent.start and job.runs
    say about it (build, overflow, swap, escalation), why other builds or rungs were passed over, and
    (provider, seconds, overflow route) when the run waits at most that long for one of provider's slots
    before it overflows; claims the run's GPU custody request leases (section 37), taken while it is placed.

    The independence guard (section 23): guard is {family: the builder's model key} a judge run must not
    share, None otherwise; judge the judge's own route; left what the model it moved off shares with the
    builder when the run moved to its independent alternate (routing.shared_model); escape the alternate
    to take instead should the judge's own placement overflow onto one of guard's families (_off_guard);
    tried what that alternate did when it could not take over; unknown True when code it judges was
    written in a chat whose models the job does not record."""

    route: dict
    build: int = 0
    info: dict = dataclasses.field(default_factory=dict)
    notes: list[str] = dataclasses.field(default_factory=list)
    wait: tuple[ProviderConfig, float, dict] | None = None
    claims: Any = None   # custody.Claims: the run's request lease (section 37), released when the run ends
    guard: dict[str, str] | None = None
    judge: dict | None = None
    left: dict | None = None
    escape: dict | None = None
    tried: str = ""
    unknown: bool = False


class _Cancelled(Exception):
    """The job's cancel event is set: a human cancelled it or the factory is stopping.

    run: the agent run that ended when the cancel was noticed (None outside a run).
    """

    def __init__(self, run: AgentRun | None = None) -> None:
        super().__init__("cancelled")
        self.run = run


class Factory:
    """Owns the job queue, the worker threads and every change to a job.

    Locks: one RLock per job guards its dict (always taken before the factory lock,
    never while holding it), one RLock per repo serialises gate + merge, and the
    factory lock guards the bookkeeping maps.
    """

    def __init__(self, config: Config, bus: EventBus, runner_factory: RunnerFactory = make_runner,
                 store: JobStore | None = None, clock: Callable[[], float] | None = None,
                 reach: Reachability | None = None) -> None:
        self.config = config
        self.bus = bus
        self.runner_factory = runner_factory
        self.store = store or JobStore(config.data_dir)
        self.clock = clock or time.monotonic
        self.reach = reach or REACHABILITY      # cached provider probes for placements (routing.py)
        custody_journal(config.data_dir)        # what the custody clients note, for the operator's activity (§36)
        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._jobs: dict[JobKey, dict] = {}
        self._job_locks: dict[JobKey, threading.RLock] = {}
        self._cancels: dict[JobKey, threading.Event] = {}
        self._stages: dict[JobKey, str] = {}
        self._driving: set[JobKey] = set()
        self._merging: set[JobKey] = set()
        self._repo_locks: dict[str, threading.RLock] = {}
        self._job_json: dict[JobKey, _FileState] = {}          # job.json as _save last wrote it
        self._tampered: dict[JobKey, list[str]] = {}           # job.json or an items file changed by someone else
        self._items_full: dict[JobKey, str] = {}               # a scope reached its item cap during the step
        self._queue: queue.Queue = queue.Queue()
        self._workers: dict[int, threading.Thread] = {}
        self._worker_target = 0
        self._started = False
        self._stopping = threading.Event()
        self._scouts: dict[str, threading.Event] = {}
        self._chats: Any = None
        self._plans: Any = None
        self._notifier: Any = None      # notify.Notifier (Wake & Messages W1), made by _notify_manager()
        self._wakes: Any = None         # wakes.WakeManager (Wake & Messages W2, W3), made by _wake_manager()
        self._drift: Any = None         # driftsources.DriftSources (section 38), made by _drift_manager()
        self._snapraid: Any = None      # snapraid.SnapraidPanel (section 41), made by _snapraid_manager()
        self._yourserver: Any = None    # yourserver.YourServer (section 42), made by _yourserver_manager()
        self._system: HousekeepingManager | None = None
        self._tokens: AgentTokens | None = None
        self._receipts: Any = None   # receipts.Receipts, made on first use (DESIGN §36)
        self._looks: Any = None      # doctor.LookAround: the owner's look for model servers, made on first use
        self._secrets: tuple[float, list[str]] | None = None     # (when, values) cached by secret_values
        self._secrets_gen = 0                                    # bumped whenever that cache is dropped
        # models to load back (swap_back): (provider, the model it had, the custody residency before the swap)
        self._swaps: dict[JobKey, list[tuple[ProviderConfig, str, Any]]] = {}
        self._steps: dict[str, Callable[[dict], str | None]] = {
            "setup": self._setup, "spec": self._spec, "build": self._build,
            "build_check": self._build_check, "review": self._review_round, "approve": self._approve,
            "gate": self._gate, "override": lambda job: self._gate(job, override=True), "merge": self._merge,
            "deliver": self._deliver,
        }

    # -- lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        """Mark jobs a previous server left active as failed, start the workers, chats and plans, then the
        Notifier (messages to the owner's phone, DESIGN §34)."""
        with self._lock:
            if self._started:
                return
            self._started = True
        self._recover()
        self._ensure_workers()
        self._chat_manager().start()
        self._plan_manager().start()
        self._notify_manager().start()
        self._wake_manager().start()
        self._drift_manager().start()
        self._snapraid_manager().start()
        self._yourserver_manager().start()

    def start_jobs(self) -> None:
        """Restart recovery and the job workers only, without chats, plans or the Notifier (`factory run`)."""
        self._recover()
        self._ensure_workers()

    def stop(self, timeout: float = 10.0) -> None:
        """Stop plans, chats and workers, then the Notifier, against one shared deadline (S18: systemd's
        TimeoutStopSec must cover all of it); a job still running ends failed ("interrupted (server
        stopped)"). The Notifier records that a stop was asked for first and a clean stop last, so the
        next start can tell a hard stop from this one."""
        self._stopping.set()
        notifier, wakes = self._notifier, self._wakes
        for record in (wakes, notifier):   # S18: the stopping records first
            if record is not None:
                record.mark_stopping()
        deadline = time.monotonic() + timeout
        if self._drift is not None:
            self._drift.stop(min(2.0, timeout))
        if self._snapraid is not None:
            self._snapraid.stop(min(2.0, timeout))
        if self._yourserver is not None:
            self._yourserver.stop(min(2.0, timeout))
        if wakes is not None:   # no new wakes during shutdown; the turn in flight is cut (S17 retries it once)
            wakes.stop(deadline)
        for manager in (self._plans, self._chats):
            if manager is not None:
                manager.stop(max(0.0, deadline - time.monotonic()))
        with self._lock:
            events = [*self._cancels.values(), *self._scouts.values()]
            workers = list(self._workers.values())
        for event in events:
            event.set()
        for worker in workers:
            worker.join(max(0.0, deadline - time.monotonic()))
        if notifier is not None:
            notifier.stop(max(deadline, time.monotonic() + 1.0))   # sends in flight finish or become unknown

    def reload_config(self, new_config: Config) -> None:
        """Swap in a new config: later steps and jobs use it, running jobs keep their routing snapshot."""
        with self._lock:
            self.config = new_config
            custody_journal(new_config.data_dir)
            self._forget_secrets()
            for manager in (self._chats, self._plans, self._drift, self._snapraid, self._yourserver):
                if manager is not None:
                    manager.config = new_config
            if self._system is not None:
                self._system.max_proposals = self._max_proposals()
            started = self._started and not self._stopping.is_set()
            notifier = self._notifier
            wakes = self._wakes
        if notifier is not None:   # outside the factory lock: the Notifier rebuilds channels and fingerprints
            notifier.reload(new_config)
        if wakes is not None:
            wakes.reload(new_config)
        if getattr(self.bus, "instance", None) is not None:
            from .wakes import instance_name

            self.bus.instance = instance_name(new_config)
            tracker.INSTANCE["name"] = self.bus.instance
        if started:
            self._ensure_workers()

    def wait(self, repo: str, job_id: str, timeout: float | None = None) -> dict:
        """Block until the job has stopped (not active, no worker on it) and return it; TimeoutError."""
        key = (repo, job_id)
        job = self._entry(key)
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._changed:
            while job.get("stage") in ACTIVE_STAGES or key in self._driving:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise TimeoutError(f"{repo}/{job_id} is still {job.get('stage')}")
                self._changed.wait(remaining)
        return self.job(repo, job_id)

    def _recover(self) -> None:
        for listed in self.store.list():
            key = (listed.get("repo"), listed.get("id"))
            with self._lock:
                known = key in self._cancels
            if known:
                continue
            if listed.get("stage") not in ACTIVE_STAGES:
                if isinstance(listed.get("codex_run"), dict):
                    self._recover_stopped_codex_run(key)
                continue
            job = self._entry(key)
            with self._job_lock(key):
                if isinstance(job.get("codex_run"), dict):   # a Codex run was in flight: unknown, reconciled (§44)
                    self._codex_interrupted(job)
                self._mark_failed(job, self._interrupted_merge(job) if job.get("stage") == "merging" else INTERRUPTED)

    def _recover_stopped_codex_run(self, key: JobKey) -> None:
        """A job that left the active stages while its Codex run was in flight (Stop sets cancelled at once; the run's
        wind-down, which records it and takes the job folder's share back, never came because the harness was killed
        before it): the run is recorded unknown and reconciled as for an active job (_codex_interrupted), and a job
        that ended gets its end (_job_ended: the carry-forward, the follow-up, the Codex home and run folders) that
        _drive's finally never reached. The stage stays as it is."""
        job = self._entry(key)
        with self._job_lock(key):
            if not isinstance(job.get("codex_run"), dict):
                return
            self._codex_interrupted(job)
            if job.get("stage") in END_STAGES:
                self._job_ended(job)

    def _interrupted_merge(self, job: dict) -> str:
        """The failed reason of a job a previous server left merging (DESIGN §40): what it did is looked at again,
        read only, never assumed. A merge the job recorded is done (a retry never merges it again); else the
        repository says whether base already contains the branch. A push whose intent is on record without an
        answer is unknown, with a tracker item. Nothing is merged or pushed here."""
        reason, base = f"{INTERRUPTED} while merging", str(job.get("base") or "")
        try:
            repo = self._repo(job)
            sha = job.get("merged_commit")
            if sha and gitops.is_ancestor(repo.path, sha, f"refs/heads/{base}"):
                reason += f": {base} has the merge at {sha[:12]}; retry finishes the delivery"
            elif self._base_has_branch(repo, job):
                reason += f": {base} already contains {job['branch']} (looked again); retry finishes the delivery"
            else:
                reason += f": nothing was merged into {base} (looked again); retry merges it"
        except (GitError, KeyError, ValueError, OSError) as exc:
            reason += f": what it did could not be looked at ({exc}); check {base} before you retry"
        push = job.get("push")
        if isinstance(push, dict) and push.get("outcome") is None:
            self._push_done(job, dict(push), rule.UNKNOWN, "the server stopped while the push was out")
        return reason

    def _ensure_workers(self) -> None:
        with self._lock:
            self._worker_target = self.config.pipeline().max_parallel_jobs
            for index in range(self._worker_target):
                worker = self._workers.get(index)
                if worker is None or not worker.is_alive():
                    worker = threading.Thread(target=self._work, args=(index,), daemon=True,
                                              name=f"factory-worker-{index}")
                    self._workers[index] = worker
                    worker.start()

    def _work(self, index: int) -> None:
        while not self._stopping.is_set() and index < self._worker_target:
            try:
                key, step = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if index >= self._worker_target:   # the pool shrank while this worker waited
                self._queue.put((key, step))
                return
            self._drive(key, step)

    # -- reading -----------------------------------------------------------------

    def jobs(self) -> list[dict]:
        """Every job.json, by repo then id."""
        return self.store.list()

    def job(self, repo: str, job_id: str) -> dict:
        """A copy of one job (KeyError if unknown)."""
        key = (repo, job_id)
        with self._lock:
            cached = self._jobs.get(key)
        if cached is None:
            return self.store.load(repo, job_id)
        with self._job_lock(key):
            return copy.deepcopy(cached)

    def job_files(self, repo: str, job_id: str) -> list[str]:
        """Relative paths of the job folder's files (sessions/ left out)."""
        return self.store.files(repo, job_id)

    def agents(self, repo: str | None = None) -> dict[str, AgentDef]:
        """Agent definitions after overrides: project, user, then the repo's .factory/agents."""
        repo_path = self.config.repo(repo).path if repo else None
        return discover_agents(agent_dirs(self.config, repo_path))

    def secret_values(self) -> list[str]:
        """Values the event bus redacts (its secrets_provider; see new_factory): the live config's
        secret_values(), plus the keys of providers that only a routing snapshot knows, i.e. a
        per-run preset's: the provider_entry of every route of each job loaded in this Factory, and
        the providers of the preset a plan routes by (a plan's lead runs on them). Keys are read now
        from api_key_env (os.environ) or api_key_file. Chats run on the live config. The answer is
        reused for SECRETS_TTL seconds; submitting, re-routing, creating a plan and reloading the
        config start afresh. Never takes a job lock (the bus calls it while one may be held).
        """
        now = time.monotonic()
        with self._lock:
            cached = self._secrets
            if cached is not None and 0 <= now - cached[0] < SECRETS_TTL:
                return list(cached[1])
            config, jobs, plans, generation = self.config, list(self._jobs.values()), self._plans, self._secrets_gen
        values = list(config.secret_values())
        notifier = self._notifier
        if notifier is not None:   # the phone channels' tokens (Wake & Messages S26)
            values += [value for value in notifier.secret_values() if value not in values]
        entries: list[Any] = []
        for job in jobs:
            routing = job.get("routing")
            if isinstance(routing, dict):
                for route in list(routing.values()):
                    entries += _route_provider_entries(route)
        presets: set[str] = set()
        if plans is not None:
            try:
                presets = {str(plan["routing"]) for plan in plans.plans()
                           if isinstance(plan.get("routing"), str) and plan.get("stage") not in ("done", "cancelled")}
            except (OSError, ValueError, KeyError, AttributeError):
                presets = set()
        if presets:
            from .settings import load_preset

            for name in sorted(presets):
                try:
                    providers = load_preset(name).get("providers")
                except ValueError:
                    continue
                if isinstance(providers, dict):
                    entries += list(providers.values())
        for entry in entries:
            try:
                found = provider_secret_values(entry)
            except (OSError, TypeError, ValueError):
                continue
            values += [value for value in found if len(value) >= MIN_SECRET_CHARS and value not in values]
        with self._lock:
            if self._secrets_gen == generation:   # else something new arrived meanwhile: do not cache
                self._secrets = (now, values)
        return list(values)

    def _forget_secrets(self) -> None:
        """Make the next secret_values() look again (a new routing snapshot or plan may know a new key)."""
        with self._lock:
            self._secrets = None
            self._secrets_gen += 1

    # -- submitting --------------------------------------------------------------

    def submit(self, repo: str, feature: str, from_branch: str | None = None,
               routing: dict | str | None = None, delivery: str | None = None, profile: str | None = None,
               items: list[dict] | None = None, handoff_models: list[dict] | None = None) -> dict:
        """Create a queued job with the full routing snapshot of its agents and queue it.

        from_branch: start the worktree from that branch (a chat handoff) instead of
        building round 1. routing: per-agent {model, driver, extra_body, effort, escalate,
        reason} overrides, or a preset name. profile: a routing profile whose placements apply
        over the routing (None: the config's default_profile, "": none; recorded in job.json).
        delivery: merge, branch or folder (None: merge.delivery), kept in job.json. items: tracked
        items handed over with it (a chat's open items, items.transfer_copy), written into the job's
        items.json before the job is queued, so nothing races its worker (section 32).
        handoff_models: with from_branch, the models the chat's turns ran on ([{model, provider, model_id,
        family}], kept as job.handoff_models): the independence guard counts them as round 1's builder
        (section 23); a hand-off without them judges code of unknown models. ValueError for an invalid
        feature, branch, routing, profile, delivery or handoff_models.
        """
        repo_cfg = self.config.repo(repo)
        pipeline = self.config.pipeline()
        mode = self.check_delivery(delivery)
        if from_branch is not None:
            try:
                gitops.rev_parse(repo_cfg.path, f"refs/heads/{from_branch}")
            except GitError:
                raise ValueError(f"branch {from_branch!r} does not exist in {repo}") from None
        snapshot, name = self._routing(repo_cfg, pipeline.reviewers, routing, profile=profile)
        problem = self._codex_problem(repo, snapshot)
        if problem:   # a codex-worker job does not start (BROWSER_AND_CODEX.md §3.3, DESIGN §44)
            raise ValueError(f"this job's builder runs on codex-worker and cannot start: {problem}")
        if items is not None and not (isinstance(items, list) and all(isinstance(entry, dict) for entry in items)):
            raise ValueError("items must be a list of item fields")
        if handoff_models is not None and not (isinstance(handoff_models, list)
                                               and all(isinstance(entry, dict) for entry in handoff_models)):
            raise ValueError("handoff_models must be a list of {model, provider, model_id, family}")
        authors = None
        if from_branch is not None and handoff_models is not None:
            authors = [{key: str(entry[key]) for key in ("model", "provider", "model_id") if entry.get(key)}
                       | {"family": route_family(entry)} for entry in handoff_models]
        return self._create_job(repo, feature, pipeline.reviewers, pipeline.max_resumes, snapshot, from_branch, mode,
                                profile=name, items=items, handoff_models=authors)

    def check_delivery(self, delivery: str | None = None) -> str:
        """The delivery mode a job submitted now with delivery would get: delivery itself, or
        merge.delivery when it is None. ValueError (the message submit gives) unless it is merge,
        branch or folder. Plans and chat handoffs check theirs with it."""
        try:
            default = self.config.merge().delivery
        except ValueError:
            default = "merge"
        return check_delivery(delivery, default)

    def check_routing(self, repo: str, routing: dict | str | None = None, profile: str | None = None) -> dict:
        """The routing snapshot a job submitted now on repo with routing and profile would get ({agent:
        route} for every agent the pipeline runs; see _routing), without creating anything.

        Raises what submit would for the same routing: ValueError with the same message (a
        malformed route, an unknown preset, profile, driver or model, a missing agent definition),
        KeyError for an unknown repo. Plans check their routing with it before the lead runs.
        """
        return self._routing(self.config.repo(repo), self.config.pipeline().reviewers, routing, profile=profile)[0]

    def check_profile(self, profile: str | None = None, routing: dict | str | None = None) -> str | None:
        """The profile name a job submitted now with profile (and routing, which may be a preset that
        brings profiles) records: profile, the default_profile for None, None for "" or when there is
        no default. ValueError for an unknown profile. Plans keep theirs with it."""
        source = self._source_config(routing)
        return profile_name(source.raw, profile)

    def estimate(self, repo: str, kind: str = "feature", profile: str | None = None,
                 routing: dict | str | None = None) -> dict:
        """Expected tokens, cost and duration of a job (kind "feature") or a plan ("plan") on repo with
        routing and profile, from this repo's past jobs (routing.estimate: the median per agent once
        MIN_HISTORY jobs have it, else a default table). A judge is priced where the independence guard
        puts it while the builder runs on its own model (its alternate, with independence and instead_of
        on its row); independence (routing.independence_map) says what the guard does on the builder's
        other placements. Nothing runs; ValueError as submit would."""
        if kind not in ("feature", "plan"):
            raise ValueError("kind must be feature or plan")
        repo_cfg = self.config.repo(repo)
        reviewers = self.config.pipeline().reviewers
        extra = ("lead",) if kind == "plan" else ()
        snapshot, name = self._routing(repo_cfg, reviewers, routing, profile=profile, extra_agents=extra)
        stages = {"spec-writer": "spec", "builder": "build", "approver": "approve", "lead": "lead"}
        builder = snapshot.get("builder") if isinstance(snapshot.get("builder"), dict) else None
        roles = []
        for agent, route in snapshot.items():
            if builder is not None and is_judge(agent):   # where the guard puts it, the builder on its own model
                route = _estimated_judge(route, {route_family(builder): str(builder.get("model"))})
            roles.append(Role(agent, stages.get(agent, "review"), self._model_config(route), route))
        history: dict[str, list[dict]] = {}
        for past in self.store.list(repo):
            for agent, usage in _usage_by_agent(past).items():
                history.setdefault(agent, []).append(usage)
        lead_usage: list[dict] = []
        plan_jobs: list[int] = []
        if kind == "plan":
            try:
                plans = [plan for plan in self._plan_manager().plans() if plan.get("repo") == repo]
            except (AttributeError, TypeError, OSError, ValueError):
                plans = []
            for plan in plans:
                usage = plan.get("usage")
                if isinstance(usage, dict) and usage.get("calls"):
                    lead_usage.append(usage)
                if isinstance(plan.get("jobs"), list) and plan["jobs"]:
                    plan_jobs.append(len(plan["jobs"]))
            history["lead"] = lead_usage
        result = estimate(roles, history, kind, plan_jobs)
        result.update(repo=repo, profile=name, routing=routing if isinstance(routing, str) else None,
                      independence=independence_map(snapshot))
        return result

    def _create_job(self, repo: str, feature: str, reviewers: list[str], max_resumes: int, snapshot: dict,
                    from_branch: str | None, delivery: str = "merge", profile: str | None = None,
                    items: list[dict] | None = None, handoff_models: list[dict] | None = None) -> dict:
        """Write the new job (queued, with its routing snapshot, profile, delivery and handed-over items),
        announce it and queue its setup. Handed-over items are checked before anything is written (ValueError
        for a malformed one) and written before the job is announced: job.created comes first, then its
        items.updated; a job whose items cannot be written is removed again, never left half made."""
        if items:
            check = tracker.job_scope(repo, "000-check", handoff=bool(from_branch))
            scratch = tracker.Ledger(tracker.empty_doc(check), check, secrets=self._item_secrets())
            for fields in items:
                tracker.add_carried(scratch, dict(fields))
        job = self.store.new_job(repo, feature, reviewers, max_resumes)
        key = (repo, job["id"])
        changed: list[str] = []
        with self._job_lock(key):
            job.update(routing=snapshot, profile=profile, from_branch=from_branch, step="setup", delivery=delivery,
                       delivered=None, cost=zero_cost(), sendbacks=0)
            if handoff_models is not None:
                job["handoff_models"] = handoff_models
            if items:
                store = self._items_store(job)
                try:   # before the job is announced or queued: no worker or GUI reads it yet
                    with store.transaction(secrets=self._item_secrets()) as ledger:
                        for fields in items:
                            tracker.add_carried(ledger, dict(fields))
                        changed = list(ledger.changed)
                    job["items"] = ledger.summary()
                except BaseException:
                    store.forget()
                    shutil.rmtree(job["job_dir"], ignore_errors=True)
                    raise
            self.store.save(job)
            with self._lock:
                self._jobs[key] = job
                self._stages[key] = job["stage"]
                self._forget_secrets()   # the snapshot may name a provider the config does not have
            self.bus.publish("job.created", {"job": job}, repo=repo, job=job["id"])
            if changed:
                self.bus.publish("items.updated", {"ref": tracker.job_scope(repo, job["id"]).ref,
                                                   "summary": job["items"], "changed": changed},
                                 repo=repo, job=job["id"])
            self._enqueue(job, "setup")
            return copy.deepcopy(job)

    def backlog(self, repo: str) -> list[dict]:
        """The repo's backlog items [{text, done, job}]."""
        self.config.repo(repo)
        return self.store.backlog(repo)

    def add_backlog(self, repo: str, text: str) -> list[dict]:
        """Append an unchecked item to the repo's backlog and return the items."""
        self.config.repo(repo)
        items = self.store.add_backlog(repo, text)
        self.bus.publish("backlog.updated", {"repo": repo}, repo=repo)
        return items

    def next(self, repo: str) -> dict:
        """Submit the first unchecked backlog item and mark it '- [x] item → <job-id>'.

        The routing is resolved before the item is claimed (ValueError leaves the backlog as it
        was), and an item whose job still cannot be created goes back to '- [ ] item'.
        """
        repo_cfg = self.config.repo(repo)
        pipeline = self.config.pipeline()
        snapshot, name = self._routing(repo_cfg, pipeline.reviewers, None)
        mode = self.check_delivery(None)
        text = self.store.claim_next(repo)
        if text is None:
            raise ActionError(f"the backlog of {repo} has no unchecked items")
        try:
            job = self._create_job(repo, text, pipeline.reviewers, pipeline.max_resumes, snapshot, None, mode,
                                   profile=name)
        except BaseException:
            self._unclaim(repo, text)
            raise
        self.store.assign_backlog(repo, text, job["id"])
        self.bus.publish("backlog.updated", {"repo": repo}, repo=repo)
        return job

    def _unclaim(self, repo: str, text: str) -> None:
        """Turn '- [x] text → claimed' back into '- [ ] text' (best effort; a failure is logged, and so is
        a claimed line that is gone, e.g. because someone edited backlog.md meanwhile)."""
        wanted = text.strip()
        try:
            found = self.store.unclaim(repo, wanted)
        except (OSError, ValueError) as exc:
            problem = f"stays claimed: {exc}"
        else:
            if found:
                return
            problem = "was not put back: its '→ claimed' line is no longer in backlog.md"
        self.bus.publish("log", {"level": "warn", "message": f"backlog item {wanted!r} of {repo} {problem}"},
                         repo=repo)

    # -- human actions -----------------------------------------------------------

    def approve(self, repo: str, job_id: str, carry_items: list[str] | None = None) -> dict:
        """Human approve (needs-human, delivered, or failed at merge). A branch or folder job with no
        delivery on record goes to the deliver step (the review rule skipped; nothing merges, so base
        never moves before the delivery the job asked for). Any other job takes the human override:
        the review/path/size rules skipped, still no conflicts, then the merge (a delivered job too).
        Must-do items that are not proven are never waived (section 32): in both branches the person
        names every one in carry_items, and they move to follow-ups with reason "merged (delivered) by
        human override while open"; OpenItemsError (409, listing them) otherwise."""
        job, key = self._target(repo, job_id)
        with self._job_lock(key):
            merge_failed = job["stage"] == "failed" and job.get("failed_step") == "merge"
            if job["stage"] not in ("needs-human", DELIVERED) and not merge_failed:
                raise ActionError(f"cannot approve {repo}/{job_id} while it is {job['stage']}")
            self._require_idle(key)
            carried = self._check_carry(job, carry_items)
            if carried:
                self._log(job, "info", f"a person approved with {tracker.plural(len(carried), 'open must-do item')}"
                                       f" to carry to follow-ups: {', '.join(carried)}")
            mode = _delivery(job)
            if mode != "merge" and not job.get("delivered"):
                self._requeue(job, "deliver", approved_by="human", carried_items=carried or None,
                              detail=f"approved by a human: delivering as a {mode}")
            else:
                self._requeue(job, "override", approved_by="human", carried_items=carried or None,
                              detail="approved by a human")
            return copy.deepcopy(job)

    def _check_carry(self, job: dict, carry_items: list[str] | None) -> list[str]:
        """The must-do items a person's approve moves on (every one must be named in carry_items), or
        OpenItemsError. Nothing is required while housekeeping is off (the items then hold nothing)."""
        if carry_items is not None and not (isinstance(carry_items, list) and all(
                isinstance(entry, str) and tracker.ITEM_ID_RE.fullmatch(entry) for entry in carry_items)):
            raise ValueError("carry_items must be a list of item ids such as I-2")
        if not self._housekeeping().enabled:
            return []
        try:   # fail closed: a ledger that cannot be read waives nothing
            waiting = [item["id"] for item in self._items_store(job).ledger().items if tracker.holds_gate(item)]
        except (OSError, ValueError) as exc:
            raise ActionError(f"tracked items cannot be read, so nothing is approved: {exc}") from None
        missing = [item_id for item_id in waiting if item_id not in set(carry_items or [])]
        if missing:
            raise OpenItemsError(
                f"{tracker.plural(len(waiting), 'must-do item')} {'is' if len(waiting) == 1 else 'are'} not proven: "
                f"{', '.join(waiting)}. Approving moves them to follow-ups unproven: name every one in carry_items "
                "(factory approve --carry-open-items), or prove them first", waiting)
        return waiting

    def rework(self, repo: str, job_id: str, note: str) -> dict:
        """Write rework-<k>.md, reset resumes and send the builder back to work on it (needs-human)."""
        if not isinstance(note, str) or not note.strip():
            raise ValueError("rework needs a note")
        job, key = self._target(repo, job_id)
        with self._job_lock(key):
            if job["stage"] != "needs-human":
                raise ActionError(f"cannot rework {repo}/{job_id} while it is {job['stage']}")
            self._require_idle(key)
            k = self.store.next_rework_index(job)
            self.store.write_file(job, f"rework-{k}.md", note.strip() + "\n")
            round_ = _last_review_round(job) + 1
            with self._job_items(job) as ledger:
                tracker.ingest_rework(ledger, note.strip(), k, round=round_)   # never refused for the cap
            prompt = (f"Rework: read {job['job_dir']}/rework-{k}.md and address it, "
                      f"then update build.md for round {round_}.")
            if job.get("delivered"):   # the branch is about to move: that delivery is out of date
                self._log(job, "info", "rework: the earlier delivery is out of date; the job is delivered "
                                       "again after its next approval")
            self._requeue(job, "build", resumes=0, round=round_, decision=None, approved_by=None, delivered=None,
                          carried_items=None,
                          reviews=_pending(job), build_request={"prompt": prompt, "resume": True},
                          sendbacks=int(job.get("sendbacks") or 0) + 1, detail=f"round {round_}: rework {k}")
            return copy.deepcopy(job)

    def reject(self, repo: str, job_id: str, reason: str = "") -> dict:
        """Remove the worktree and branch of a stopped (or delivered) job and mark it rejected; a folder
        delivery's output folder stays."""
        job, key = self._target(repo, job_id)
        with self._job_lock(key):
            if job["stage"] not in REJECTABLE_STAGES:
                raise ActionError(f"cannot reject {repo}/{job_id} while it is {job['stage']}")
            self._require_idle(key)
            try:
                self._remove_checkout(job, self.config.repo(repo))
            except KeyError:
                self._log(job, "warn", f"repo {repo!r} is no longer configured; worktree and branch left")
            reason = (reason or "").strip() or "rejected by a human"
            self._update(job, stage="rejected", reason=reason, detail=reason, **_settled_reviews(job))
            self._job_ended(job)
            return copy.deepcopy(job)

    def retry(self, repo: str, job_id: str) -> dict:
        """Re-enter a failed job at its failed_step, with its routing snapshot."""
        job, key = self._target(repo, job_id)
        with self._job_lock(key):
            if job["stage"] != "failed":
                raise ActionError(f"cannot retry {repo}/{job_id} while it is {job['stage']}")
            self._require_idle(key)
            step = job.get("failed_step") if job.get("failed_step") in STEPS else "setup"
            reconciled = job.get("reconcile") if isinstance(job.get("reconcile"), dict) else None
            note = f": {reconciled['summary']}" if reconciled and reconciled.get("summary") else ""
            self._requeue(job, step, detail=f"retrying {step}{note}")
            return copy.deepcopy(job)

    def cancel(self, repo: str, job_id: str) -> dict:
        """Cancel an active job: its agents stop at their next check; the worktree is kept."""
        job, key = self._target(repo, job_id)
        with self._job_lock(key):
            if job["stage"] not in ACTIVE_STAGES:
                raise ActionError(f"cannot cancel {repo}/{job_id} while it is {job['stage']}")
            with self._lock:
                if key in self._merging:
                    raise ActionError(f"{repo}/{job_id} is merging right now; it can no longer be cancelled")
                event = self._cancels.setdefault(key, threading.Event())
            event.set()
            self._update(job, stage="cancelled", reason="cancelled by a human", detail="cancelled",
                         **_settled_reviews(job))
            with self._lock:
                driven = key in self._driving
            if not driven:   # else _drive carries forward once the run stopped and its journal is in
                self._job_ended(job)
            return copy.deepcopy(job)

    def set_routing(self, repo: str, job_id: str, routing: dict | str | None, profile: str | None = None) -> dict:
        """Change a stopped job's routing (needs-human or failed). A dict re-routes the agents it
        names and keeps the rest; a preset name (or None, the saved config) re-routes every agent.
        profile: None keeps the job's own profile (a job.json from before profiles: the default),
        a name applies that profile and "" none; naming one re-routes every agent."""
        job, key = self._target(repo, job_id)
        with self._job_lock(key):
            if job["stage"] not in HUMAN_STAGES:
                raise ActionError(f"cannot change the routing of {repo}/{job_id} while it is {job['stage']}")
            self._require_idle(key)
            keep = None if profile is not None else job.get("routing") or {}
            if profile is None and "profile" in job:
                profile = job["profile"] or ""   # the job's own profile, or none when it ran without one
            snapshot, name = self._routing(self.config.repo(repo), job["reviewers"], routing, keep=keep,
                                           profile=profile)
            # §3.3 holds for the rest of the job: the routing check as at submit, and once Codex ran, no route
            # (the builder's too) that reads its work off this machine (GETs only: the model may be unloaded now)
            problem = codex_work.routing_problem(self.config, repo, snapshot) or (
                self._codex_hosted_problem(repo, snapshot) if self._ran_codex(job) else None)
            if problem:
                raise ValueError(f"cannot route {repo}/{job_id} this way: {problem}")
            self._update(job, routing=snapshot, profile=name)
            self._forget_secrets()
            return copy.deepcopy(job)

    def assumptions(self, repo: str, job_id: str) -> dict:
        """The spec's product assumptions: {marked, confirmed, unconfirmed} (assumptions.view)."""
        from .assumptions import read_spec, view

        job, key = self._target(repo, job_id)
        with self._job_lock(key):
            return view(read_spec(Path(job["job_dir"])), _confirmed(job))

    def confirm(self, repo: str, job_id: str, lines: list[str] | None = None) -> dict:
        """A person confirms product assumptions of the job's spec: each of lines (None: every marked
        line not confirmed yet) must be a line spec.md marks (compared normalized). They join
        job.confirmed, and confirmed.md, which the approver reads, is rewritten from it; the decision
        items of the confirmed lines (section 32) are proven by the person too. Any stage but merged,
        rejected and cancelled, also while agents run: the write policy leaves confirmed.md to the
        orchestrator (_restore_confirmed). ValueError for no spec, nothing marked or a line spec.md
        does not mark; ActionError for the stage, or while the job's items are mid-ingest (retry)."""
        from .assumptions import marked, normalize, read_spec

        job, key = self._target(repo, job_id)
        with self._job_lock(key):
            if job["stage"] in ("merged", "rejected", "cancelled"):
                raise ActionError(f"cannot confirm assumptions of {repo}/{job_id} while it is {job['stage']}")
            spec = read_spec(Path(job["job_dir"]))
            if spec is None:
                raise ValueError(f"{repo}/{job_id} has no spec.md yet")
            lines_marked = marked(spec)
            if not lines_marked:
                raise ValueError("spec.md marks no assumption (product)")
            chosen = lines_marked if lines is None else []
            for line in lines or []:
                text = normalize(line) if isinstance(line, str) else ""
                if text not in lines_marked:
                    raise ValueError(f"spec.md does not mark this line (product): {line!r}")
                chosen.append(text)
            have = _confirmed(job)
            new = [line for line in dict.fromkeys(chosen) if line not in have]
            with self._busy_as_409():   # first, so a busy ledger changes nothing
                self._answer_decisions(job, have + new)
            if new:
                self._write_confirmed(job, have + new)
                self._update(job, confirmed={"lines": have + new, "at": utc_now()})
                self._log(job, "info", f"a person confirmed {len(new)} product assumption(s)")
            return copy.deepcopy(job)

    def _answer_decisions(self, job: dict, lines: list[str], ledger: tracker.Ledger | None = None) -> None:
        """The person's confirmed lines answer the job's (product) decision items too (sections 7, 32): each
        decision item from spec.md still waiting whose text a line matches (assumptions.answers) is proven
        by the person. ledger: inside its transaction, else one is opened (ItemsBusy past BUSY_WAIT)."""
        from .assumptions import answers

        if not lines:
            return
        if ledger is None:
            if not self._items_store(job).path.is_file():
                return
            with self._job_items(job, BUSY_WAIT) as opened:
                self._answer_decisions(job, lines, opened)
            return
        for item in ledger.items:
            source = item.get("source") or {}
            if item.get("kind") != "decision" or source.get("type") != "spec" or item.get("status") in tracker.CLOSED:
                continue
            line = next((line for line in lines if answers(line, str(source.get("quote") or item["title"]))), None)
            if line is not None:
                with contextlib.suppress(tracker.ItemError):   # refused: it stays open and the gate says so
                    ledger.change(tracker.HUMAN, item["id"], "verified", note=f"a person confirmed: {line}")

    def _follow_decision(self, job: dict, item: dict, action: str) -> None:
        """A person proved (verify) or reopened a (product) decision item from spec.md: job.confirmed and
        confirmed.md follow, so the approver reads the same answer (sections 7, 32). Nothing on a merged,
        rejected or cancelled job."""
        from .assumptions import answers, marked, read_spec

        source = item.get("source") or {}
        if action not in ("verify", "reopen") or item.get("kind") != "decision" or source.get("type") != "spec":
            return
        quote = str(source.get("quote") or item.get("title") or "")
        with self._job_lock(_key(job)):
            if job["stage"] in ("merged", "rejected", "cancelled"):
                return
            have = _confirmed(job)
            if action == "verify":
                lines = [*have, *(line for line in marked(read_spec(Path(job["job_dir"])) or "")
                                  if answers(line, quote) and line not in have)]
            else:
                lines = [line for line in have if not answers(line, quote)]
            if lines == have:
                return
            self._write_confirmed(job, lines)
            self._update(job, confirmed={"lines": lines, "at": utc_now()})
        if action == "verify":
            self._log(job, "info", f"a person confirmed {len(lines) - len(have)} product assumption(s) by "
                                   f"proving {item['id']}")
        else:
            self._log(job, "info", f"a person withdrew the confirmation of {len(have) - len(lines)} product "
                                   f"assumption(s) by reopening {item['id']}")

    def _write_confirmed(self, job: dict, lines: list[str]) -> None:
        """confirmed.md as lines render it, or no confirmed.md when there are none (caller holds the job lock)."""
        from .assumptions import CONFIRMED_FILE, clear, render

        path = Path(job["job_dir"]) / CONFIRMED_FILE
        try:
            is_file = stat.S_ISREG(path.lstat().st_mode)
        except FileNotFoundError:
            is_file = False
        if not lines or not is_file:
            clear(path)   # a link or a folder in its place goes; os.replace never follows a link
        if lines:
            self.store.write_file(job, CONFIRMED_FILE, render(lines))

    def _restore_confirmed(self, job: dict) -> None:
        """Before the approver reads confirmed.md: make it what job.confirmed renders (no file when
        nothing is confirmed). Factory.confirm is its only writer, so any other change (an agent's,
        or a hand edit) is undone here, and logged."""
        from .assumptions import CONFIRMED_FILE, on_record

        with self._job_lock(_key(job)):
            lines = _confirmed(job)
            if on_record(Path(job["job_dir"]) / CONFIRMED_FILE, lines):
                return
            self._write_confirmed(job, lines)
        self._log(job, "warn", "confirmed.md did not match the job's confirmations (only a person's confirm "
                               "action writes it: POST /api/jobs/<repo>/<id>/confirm or factory confirm); "
                               "restored before the approver")

    # -- repo scout --------------------------------------------------------------

    def scout(self, repo: str) -> None:
        """Start the repo-scout agent on the repo checkout (read-only); it writes AGENTS.draft.md."""
        repo_cfg = self.config.repo(repo)
        with self._lock:
            if repo in self._scouts:
                raise ActionError(f"repo-scout is already running on {repo}")
            cancel = self._scouts[repo] = threading.Event()
        threading.Thread(target=self._scout, args=(repo_cfg, cancel), name=f"scout-{repo}",
                         daemon=True).start()

    def scout_status(self, repo: str) -> dict:
        """{draft: the AGENTS.draft.md text or None, running}."""
        self.config.repo(repo)
        try:
            draft: str | None = read_small_text(self.store.repo_dir(repo) / SCOUT_DRAFT, 1 << 20)
        except (OSError, UnicodeDecodeError):
            draft = None
        with self._lock:
            running = repo in self._scouts
        return {"draft": draft, "running": running}

    def _scout(self, repo: RepoConfig, cancel: threading.Event) -> None:
        """One repo-scout run on the live config with its default profile's placement, placed and
        waiting for a slot like a job's run (a cancel while it waits ends it quietly)."""
        root = self.store.repo_dir(repo.name)
        work = root / "scout"
        release = None
        placement: _Placement | None = None
        try:
            agent = self.agents(repo.name).get(SCOUT_AGENT)
            if agent is None:
                raise StepError(f"no agent named {SCOUT_AGENT!r}")
            placements = profile_routes(self.config.raw, profile_name(self.config.raw, None))
            base = self._resolve_route(self.config, agent, placements.get(SCOUT_AGENT))
            placement = self._place(None, SCOUT_AGENT, base, purpose=f"scout {repo.name}", cancel=cancel, on_wait=(
                lambda provider, seconds, denied: self.bus.publish("log", {"level": "info", "message": (
                    f"repo-scout on {repo.name} waits up to {seconds:g}s for {provider}: GPU custody: {denied}")},
                    repo=repo.name)))
            if placement.claims is not None and placement.claims.waited and cancel.is_set():
                return   # cancelled while the placement waited out a HELD answer
            log_path = _next_log_path(work / "logs", SCOUT_AGENT)
            emit = self._emitter(repo.name, None, SCOUT_AGENT, log_path)
            if placement.wait is not None:
                waited = self._wait_for_slot(placement, cancel, emit)
                if waited is None:
                    return
                placement, release = waited
            route = placement.route
            model = self._model_config(route, placement.build)
            if placement.claims is not None:   # the run's request lease (section 37)
                model = placement.claims.attach(model)
                placement.info.update(placement.claims.info())
            ctx = self._context(agent, model, cwd=repo.path, job_dir=work, repo_path=repo.path,
                                job={"id": None, "repo": repo.name, "kind": "scout"},
                                write_paths=[root / SCOUT_DRAFT], roots=[repo.path, work],
                                log_path=log_path, emit=emit, cancel=cancel)
            prompt = f"Repo: {repo.path}. Write the draft to {root / SCOUT_DRAFT}."
            for note in placement.notes:
                self.bus.publish("log", {"level": "info", "message": f"repo-scout on {repo.name}: {note}"},
                                 repo=repo.name)
            labels: dict[str, Any] = {"round": None, **placement.info}
            reason_ = placement_reason(base.get("reason"), placement.info)
            if reason_:
                labels["reason"] = reason_
            result = self._invoke(route, ctx, prompt, None, emit, labels)
            if not result.ok:
                raise StepError(f"{SCOUT_AGENT} failed: {result.error}")
        except (StepError, KeyError, ValueError, OSError) as exc:
            self.bus.publish("log", {"level": "error", "message": f"repo-scout on {repo.name}: {_message(exc)}"},
                             repo=repo.name)
        finally:
            if release is not None:
                release()
            if placement is not None:
                self._release_claims(None, SCOUT_AGENT, placement, repo=repo.name)
            with self._lock:
                self._scouts.pop(repo.name, None)

    # -- doctor, settings, chats and plans (delegated) ---------------------------

    def doctor(self) -> list[dict]:
        """Environment and configuration checks (doctor.run_checks)."""
        from .doctor import run_checks

        return run_checks(self.config, factory=self)

    def config_view(self) -> dict:
        """The config for the GUI (no token; key presence only)."""
        from .settings import config_view

        return config_view(self.config)

    def update_config(self, patch: dict) -> dict:
        """Validate and save a partial config, swap it in, return the new view (ValueError if invalid).

        The swap runs as settings' on_saved callback, under its save lock: of two concurrent
        updates the one saved last is also the one installed last.
        """
        from .settings import update_config

        notifier = self._notifier
        # S47: the check, the save and the Notifier's new baseline (its reload) as one step, so no tamper check on
        # another thread compares the saved file against the baseline from before it
        with notifier.writing(None) if notifier is not None else contextlib.nullcontext():
            self._before_settings_save(patch)
            update_config(self.config, patch, on_saved=self.reload_config)
        return self.config_view()

    @contextlib.contextmanager
    def _config_write(self, patch: Any = None) -> Iterator[None]:
        """A save of config.json outside update_config (a preset, the offline snapshot's import), made as
        update_config makes its own: the S47 check, the save and the Notifier's new baseline (its reload) as one
        step under the settings lock (Notifier.writing), so a check on another thread can neither skip the
        hand-edit check nor compare in between."""
        notifier = self._notifier
        with notifier.writing(None) if notifier is not None else contextlib.nullcontext():
            self._before_settings_save(patch if patch is not None else {})
            yield

    def _before_settings_save(self, patch: Any) -> None:
        """S47: a hand edit of config.json must be caught before an API save folds it into the new baseline;
        S46: a patch may not turn a phone channel on while agents could read its messages."""
        notifier = self._notifier
        if notifier is not None:
            notifier.check_tamper()
            notifier.check_patch(patch)
        self._check_wake_patch(patch)

    def _check_wake_patch(self, patch: Any) -> None:
        """S46 for step W2/W3: a patch may not turn personas or chat check-ins on while agents run without bwrap or
        through a CLI driver, unless the owner typed the acknowledgement; and once notice-rules.json exists, the keys
        it holds are changed there only (one rulebook, operator decision g)."""
        from . import rules
        from .notify import sandbox_mode, uses_cli_driver

        if not isinstance(patch, dict):
            return
        notify = patch.get("notify") if isinstance(patch.get("notify"), dict) else {}
        moved = sorted(set(notify) & set(rules.CONFIG_KEYS))
        if moved and rules.rules_path(self.config).exists():
            raise ValueError(f"notify.{moved[0]} lives in {rules.RULES_FILE} now (the one rulebook): change it with "
                             "POST /api/notify/rules")
        wakes = patch.get("wakes") if isinstance(patch.get("wakes"), dict) else {}
        turning_on = wakes.get("enabled") is True or (isinstance(wakes.get("chat_wakes"), dict)
                                                      and wakes["chat_wakes"].get("enabled") is True)
        personas = wakes.get("personas") if isinstance(wakes.get("personas"), dict) else {}
        turning_on = turning_on or any(isinstance(entry, dict) and entry.get("enabled") is True
                                       for entry in personas.values())
        if not turning_on:
            return
        harness = patch.get("harness") if isinstance(patch.get("harness"), dict) else {}
        if harness.get("sandbox", sandbox_mode(self.config)) == "bwrap" and not uses_cli_driver(self.config):
            return
        if self._notify_manager().precondition().get("acknowledged_at"):
            return
        raise ValueError("personas and chat check-ins stay off while your AI agents run without the sandbox or "
                         "through a CLI driver (they could read what you tell Seraphina). Set harness.sandbox to bwrap "
                         "and use the builtin driver, or type the acknowledgement in Phone setup first")

    def apply_preset(self, name: str) -> dict:
        """Merge examples/<name>.json into the config, save it, swap it in (under the save lock, like
        update_config) and return the new view."""
        from .settings import apply_preset

        with self._config_write():
            apply_preset(self.config, name, on_saved=self.reload_config)
        return self.config_view()

    def test_model(self, key: str) -> dict:
        """Probe one model entry: {ok, latency_ms, text, error, tool_call, tokens_per_second}."""
        from .settings import test_model

        return test_model(self.config, key)

    def agent_detail(self, name: str) -> dict:
        """One agent file with its source and text (KeyError if unknown)."""
        from .settings import agent_detail

        return agent_detail(self.config, name)

    def save_agent(self, name: str, text: str) -> dict:
        """Write a user override of an agent file (publishes agent.updated)."""
        from .settings import save_agent

        return self._agent_updated(name, save_agent(self.config, name, text))

    def new_agent(self, name: str, from_: str = "reviewer-code") -> dict:
        """Create a user agent as a copy of another (publishes agent.updated)."""
        from .settings import new_agent

        return self._agent_updated(name, new_agent(self.config, name, from_))

    def reset_agent(self, name: str) -> dict | None:
        """Delete a user override (KeyError if there is none); the agent now in effect, or None when
        no agent of that name remains (it existed only as the override, which is still a reset).
        Publishes agent.updated."""
        from .settings import reset_agent

        return self._agent_updated(name, reset_agent(self.config, name))

    def _agent_updated(self, name: str, agent: dict | None) -> dict | None:
        """Publish agent.updated {name, agent} (agent: the detail now in effect, or None) and return agent."""
        self.bus.publish("agent.updated", {"name": name, "agent": agent}, agent=name)
        return agent

    # -- setup status, secrets and docs (delegated) -------------------------------

    def setup_status(self) -> dict:
        """{configured, steps: {providers, models, routing, repos, tools, output}: {ok, detail}, host} (doctor),
        offline: host carries the last look for model servers the owner asked for, never a new one."""
        from .doctor import setup_status

        return setup_status(self.config, factory=self, looked=self._look_around().last())

    def look_around(self, own_port: int | None = None) -> dict:
        """The owner's look for model servers, run once now: {at, model_servers, note} (doctor.LookAround; its
        LookAroundRefused when asked again too soon or while one runs). own_port: the port the server listens on,
        skipped like the config's server.port."""
        return self._look_around().ask(self.config, self.clock(), skip_ports={own_port} if own_port else frozenset())

    def _look_around(self) -> Any:
        from .doctor import LookAround

        with self._lock:
            if self._looks is None:
                self._looks = LookAround()
            return self._looks

    def secret_status(self) -> dict:
        """{env_file, secrets: [{name, set, in_env_file, providers}]}: names only, never values (envfile)."""
        from .envfile import secret_status

        return secret_status(self.config)

    def set_secret(self, name: str, value: str) -> dict:
        """Write NAME=value to <config dir>/env and set it in this process (envfile.set_secret); returns
        that name's status. The redaction list is rebuilt, so the new value never reaches an event."""
        from .envfile import set_secret

        self._forget_secrets()
        try:
            return set_secret(self.config, name, value)
        finally:
            self._forget_secrets()

    def procedures(self) -> dict:
        """Every agent's working procedure and the process maps, as data (docs.procedures)."""
        from .docs import procedures

        return procedures(self.config, self.agents())

    def procedures_html(self) -> str:
        """The procedures booklet: one self-contained HTML page (docs.procedures_html)."""
        from .docs import procedures_html

        return procedures_html(self.procedures())

    def maps(self) -> list[dict]:
        """The process maps as Mermaid source, [{id, title, mermaid, sha256}] (docs.mermaid_maps)."""
        from .docs import mermaid_maps

        return mermaid_maps(self.config)

    # -- messages to the owner's phone (delegated to notify.Notifier; DESIGN §34) ----------------------

    def _notify_manager(self) -> Any:
        with self._lock:
            if self._notifier is None:
                from .notify import Notifier

                self._notifier = Notifier(self.config, self.bus, self)
            return self._notifier

    def messages(self, **filters: Any) -> dict:
        """The inbox: {messages: [...newest first], unread} (filters: persona, state, channel, unread, before,
        limit, kind)."""
        return self._notify_manager().messages(**filters)

    def message(self, message_id: str) -> dict:
        """One notice: {message, deliveries: [{channel, rendered, why, state, ...}], inbound} (KeyError)."""
        return self._notify_manager().message(message_id)

    def message_action(self, message_id: str, action: str, minutes: int | None = None) -> dict:
        """ack | snooze (minutes) | retry | resend, from the cockpit."""
        return self._notify_manager().message_action(message_id, action, minutes=minutes)

    def notify_state(self) -> dict:
        """The Messages settings card: {state, channels, hooks}."""
        manager = self._notify_manager()
        return {**manager.state(), "channels": manager.channels(), "hooks": manager.hooks()}

    def notify_channels(self) -> list[dict]:
        return self._notify_manager().channels()

    def notify_test(self, channel: str) -> dict:
        return self._notify_manager().test(channel)

    def notify_prepare(self, channel: str) -> dict:
        return self._notify_manager().prepare_ntfy(channel)

    def notify_check(self, channel: str) -> dict:
        return self._notify_manager().check_ntfy(channel)

    def notify_pair(self, channel: str) -> dict:
        return self._notify_manager().pair(channel)

    def notify_pair_status(self, channel: str) -> dict:
        return self._notify_manager().pair_status(channel)

    def notify_pair_confirm(self, channel: str, code: str) -> dict:
        return self._notify_manager().pair_confirm(channel, code)

    def notify_unpair(self, channel: str, chat_id: Any) -> dict:
        return self._notify_manager().unpair(channel, chat_id)

    def notify_channel_action(self, channel: str, action: str, token: str | None = None) -> dict:
        """rotate | leaked | disable (S51); for Telegram, token is the new bot token from @BotFather."""
        self._forget_secrets()
        try:
            return self._notify_manager().channel_action(channel, action, token)
        finally:
            self._forget_secrets()

    def notify_lost_phone(self) -> dict:
        self._forget_secrets()
        try:
            return self._notify_manager().lost_phone()
        finally:
            self._forget_secrets()

    def notify_event(self, body: dict) -> dict:
        """POST /api/notify/events: the owner (master token) reports an event, as `factory notify event` does."""
        return self._notify_manager().owner_event(body)

    def notify_terms(self) -> dict:
        return self._notify_manager().terms()

    def notify_set_terms(self, text: str | None = None, none: bool = False) -> dict:
        return self._notify_manager().set_terms(text, none=none)

    def notify_inbound(self, limit: int = 100, kind: str | None = None) -> list[dict]:
        return self._notify_manager().inbound(limit=limit, kind=kind)

    def notify_apply_inbound(self, inbound_id: str) -> dict:
        """[Apply now] for a late command or tap."""
        return self._notify_manager().apply_inbound(inbound_id)

    def notify_hook_add(self, body: dict) -> dict:
        return self._notify_manager().hook_add(body)

    def notify_hook_action(self, hook: str, action: str) -> dict:
        return self._notify_manager().hook_action(hook, action)

    def hook_for_token(self, token: str) -> str | None:
        """The event source whose token this is (only its sha256 is stored), or None; never the master token."""
        return self._notify_manager().hook_for_token(token)

    def hook_auth_failed(self) -> None:
        self._notify_manager().hook_auth_failed()

    def hook_limits(self, hook: str) -> tuple[int, int]:
        """(per_minute, per_day) of an event source."""
        spec = next((h for h in self._notify_manager().hooks() if h["name"] == hook), None) or {}
        return int(spec.get("per_minute") or 10), int(spec.get("per_day") or 200)

    def hook_event(self, hook: str, body: dict, idempotency_key: str | None = None) -> dict:
        return self._notify_manager().hook_event(hook, body, idempotency_key)

    def wake_control(self, action: str, by: str = "human", via: str = "cockpit", persona: str | None = None,
                     until: str | None = None) -> dict:
        """pause | resume | mute | unmute (S43): all of Seraphina (the PAUSED file; the turn in flight is cut at once)
        or one persona, until a time. resume and unmute only from the cockpit or the CLI."""
        if action not in ("pause", "resume", "mute", "unmute"):
            raise ValueError("action must be pause, resume, mute or unmute")
        if action in ("resume", "unmute") and by not in ("human", "cli"):
            raise ValueError("Seraphina is restarted only in the cockpit or the CLI")
        manager = self._notify_manager()
        if persona is None and action == "pause":
            manager.pause(by=by, via=via)
        elif persona is None and action == "resume":
            manager.resume(by=by)
        result = self._wake_manager().control(action, persona=persona, until=until, by=by, via=via)
        return {"paused": manager.paused(), "personas": result.get("personas")}

    def wake_acknowledge(self, text: str | None = None, withdraw: bool = False) -> dict:
        """S46: the typed acknowledgement (or its withdrawal), audited."""
        return self._notify_manager().acknowledge(text, withdraw=withdraw)

    def wake_review(self, action: str) -> dict:
        """After S47: accept the current settings as the new baseline (accept), and restart (restart)."""
        return self._notify_manager().review(action)

    # -- personas that wake themselves (delegated to wakes.WakeManager; Wake & Messages W2, W3) ------------------

    def _wake_manager(self) -> Any:
        with self._lock:
            if self._wakes is None:
                from .wakes import WakeManager

                self._wakes = WakeManager(self.config, self.bus, self)
            return self._wakes

    def wakes(self) -> dict:
        """GET /api/wakes: personas, the queue, budgets, the last gate reasons, reminders, servers."""
        return self._wake_manager().state()

    def wakes_ready(self) -> bool:
        """Whether a phone's free text can go to a persona's thread (wakes on)."""
        wakes = self.config.raw.get("wakes")
        return isinstance(wakes, dict) and wakes.get("enabled") is True

    def wake_request(self, persona: str, source: str, **kwargs: Any) -> dict:
        return self._wake_manager().request(persona, source, **kwargs)

    def wake_run_now(self, persona: str, reason: str | None = None) -> dict:
        return self._wake_manager().run_now(persona, reason)

    def wake_cancel(self, wake_id: str) -> dict:
        return self._wake_manager().cancel(wake_id)

    def wake_event(self, rule: str, fields: dict | None = None, message: str | None = None) -> list[dict]:
        """A rule's notice was written (notify.py): the personas whose on_events list it wake (§2.3)."""
        if not self.wakes_ready():
            return []
        return self._wake_manager().event(rule, fields, message)

    def wake_persona_names(self) -> list[str]:
        """The wake personas by name (a phone's status lists them for mute <n>)."""
        return sorted(self._wake_manager()._personas())

    def wake_mute_target(self, reply_persona: str | None = None, index: int | None = None) -> str:
        return self._wake_manager().mute_target(reply_persona, index)

    def reminders(self) -> list[dict]:
        return self._wake_manager().reminders()

    def add_reminder(self, at: str, text: str, persona: str | None = None, show_on: Any = ()) -> dict:
        return self._wake_manager().add_reminder(at, text, persona=persona, show_on=show_on)

    def cancel_reminder(self, reminder_id: str) -> dict:
        return self._wake_manager().cancel_reminder(reminder_id)

    def wake_attest(self, provider: str, local: bool) -> dict:
        return self._wake_manager().attest_server(provider, local)

    def persona_soul(self, name: str, text: str, base_sha256: str) -> dict:
        return self._wake_manager().save_soul(name, text, base_sha256)

    def persona_privacy(self, name: str, what: str, ref: Any, clean: bool = True) -> dict:
        return self._wake_manager().confirm_clean(name, what, ref, clean)

    def persona_integrity(self, name: str, action: str) -> dict:
        return self._wake_manager().resolve_integrity(name, action)

    def persona_memory(self, name: str, text: str, base_version: int, clean: bool = False) -> dict:
        return self._wake_manager().set_memory(name, text, base_version, clean=clean)

    def persona_memory_rollback(self, name: str, version: int) -> dict:
        return self._wake_manager().memory_rollback(name, version)

    def persona_memory_version(self, name: str, version: int) -> dict:
        self._wake_manager()._require_persona(name)
        return self._wake_manager().store.memory_version(name, version)

    def persona_consolidation(self, name: str, action: str) -> dict:
        return self._wake_manager().consolidation(name, action)

    def persona_retract(self, name: str, entry: str) -> dict:
        return self._wake_manager().retract(name, entry)

    def persona_forget(self, name: str, before: str) -> dict:
        return self._wake_manager().forget(name, before)

    def persona_send(self, name: str, text: str, via: str = "cockpit", channel: str | None = None,
                     sent_at: str | None = None, forwarded: bool = False) -> dict:
        return self._wake_manager().persona_send(name, text, via=via, channel=channel, sent_at=sent_at,
                                                 forwarded=forwarded)

    def persona_sound(self, name: str) -> dict:
        return self._wake_manager().allow_sound(name)

    def persona_reimport(self, name: str, body: dict) -> dict:
        return self._wake_manager().reimport_stage(name, body)

    def persona_reimport_decide(self, name: str, action: str) -> dict:
        return self._wake_manager().reimport_decide(name, action, by="human")

    def persona_run_log(self, name: str, run: str) -> list[dict]:
        self._wake_manager()._require_persona(name)
        return self._wake_manager().store.run_log(name, run)

    def persona_external(self, name: str) -> dict:
        return self._wake_manager().external_ledger(name)

    def persona_external_version(self, name: str, digest: str) -> dict:
        return self._wake_manager().external_version(name, digest)

    def persona_external_diff(self, name: str, older: str, newer: str) -> dict:
        return self._wake_manager().external_diff(name, older, newer)

    def persona_external_restore(self, name: str, digest: str, file: str | None = None) -> dict:
        return self._wake_manager().external_restore(name, digest, file)

    def persona_external_erase(self, name: str, digest: str) -> dict:
        return self._wake_manager().external_erase(name, digest)

    def chat_wake_allow(self, chat_id: str, allow: bool) -> dict:
        self._chat_manager().chat(chat_id)   # KeyError for an unknown chat
        return self._wake_manager().chat_allow(chat_id, allow)

    def chat_wakes(self, chat_id: str) -> list[dict]:
        return self._wake_manager().chat_wakes(chat_id)

    def chat_wake_allowed(self, chat_id: str) -> bool:
        """The owner's switch "Let this chat schedule check-ins", as it stands."""
        return self._wake_manager().chat_allowed(chat_id)

    def wake_chat_context(self, chat_id: str, chain: int, *, checkin: bool) -> Any:
        if not self.wakes_ready():
            return None
        return self._wake_manager().chat_context(chat_id, chain, checkin=checkin)

    def wake_cancel_chat(self, chat_id: str) -> int:
        return self._wake_manager().cancel_chat(chat_id)

    def wake_chat_done(self, chat_id: str, wake_id: str, log_stem: str | None, ok: bool) -> None:
        self._wake_manager().chat_done(chat_id, wake_id, log_stem, ok)

    def hook_wake(self, hook: str, body: dict, idempotency_key: str | None = None) -> dict:
        return self._wake_manager().hook_wake(hook, body, idempotency_key)

    # the Notifier's side, for the WakeManager (neither module imports the other)

    def notify_notice(self, notice: Any) -> dict:
        return self._notify_manager().notify(notice)

    def notify_precondition(self) -> dict:
        return self._notify_manager().precondition()

    def notify_tamper(self) -> Any:
        return self._notify_manager().tamper()

    def notify_check_tamper(self) -> None:
        self._notify_manager().check_tamper()

    def notify_baseline(self, why: str) -> None:
        self._notify_manager().settings_written(why)

    def notify_writing(self, why: str | None) -> Any:
        """The context of one of the harness's own writes of a fingerprinted file (Notifier.writing, S47)."""
        return self._notify_manager().writing(why)

    def notify_pause(self, by: str = "human", via: str = "cockpit") -> dict:
        return self._notify_manager().pause(by=by, via=via)

    def notify_resume(self, by: str = "human") -> Any:
        return self._notify_manager().resume(by=by)

    def notify_rules(self) -> dict:
        from .rules import load_rules

        return load_rules(self.config)

    def notify_set_rules(self, change: dict) -> dict:
        """POST /api/notify/rules: the rulebook (quiet hours, rate limits, the data boundary) changed in the cockpit;
        written to notice-rules.json, audited, and the new S47 baseline."""
        from .rules import update_rules

        if not isinstance(change, dict) or "confirmed_profiles" in change:
            raise ValueError("send the rules to change; confirmations come from a persona's page")
        manager = self._notify_manager()
        with manager.writing("rules"):
            rules = update_rules(self.config, change)
            manager.audit.write("rules", keys=sorted(change))
        return rules

    def record_decline(self, persona: str, wake: str, run: str, item: str | None = None, title: str = "",
                       taint: Any = ()) -> dict:
        """Decision d: a persona's considered refusal, in the tracker (never as a failure)."""
        parsed = None
        if item:
            parsed = self._parse_ref(item, item=True)
            if parsed.kind != "system":
                parsed = None
        return self._system_manager().record_decline(persona, wake, run, parsed.item if parsed else None, title=title,
                                                     taint=taint)

    def _chat_manager(self) -> Any:
        with self._lock:
            if self._chats is None:
                from .chats import ChatManager

                self._chats = ChatManager(self.config, self.bus, self, runner_factory=self.runner_factory)
            return self._chats

    def _plan_manager(self) -> Any:
        with self._lock:
            if self._plans is None:
                from .plans import PlanManager

                self._plans = PlanManager(self.config, self.bus, self, runner_factory=self.runner_factory)
            return self._plans

    def chats(self) -> list[dict]:
        """Every chat.json."""
        return self._chat_manager().chats()

    def chat(self, chat_id: str) -> dict:
        """chat.json plus "messages" [{role, text}]."""
        return self._chat_manager().chat(chat_id)

    # -- the browser adapter (section 43) ---------------------------------------------------------------------

    def browser_state(self) -> dict:
        """GET /api/browser: the browser card (on or off, the runtime, the profiles, BC1's stated limits) and the
        newest session folders' receipts summarized (ids, counts and hashes; never page text)."""
        from .browser import runs as browser_runs

        return {**browser_runs.card(self.config), "sessions": browser_runs.recent_sessions(self.config.data_dir)}

    def browser_revoke(self, grant_id: str) -> dict:
        """A person revokes a browsing run's session lock (§2.6 rule 3): the grant says revoked and the locking run's
        MCP client is closed, which ends the shim's namespace. KeyError for an unknown grant."""
        from .browser import runs as browser_runs

        found = browser_runs.revoke(self.config.data_dir, grant_id)
        self.bus.publish("browser.revoked", {"grant": grant_id, "closed": found["closed"]})
        return found

    def browser_shot(self, chat_id: str, session_lock: str, name: str) -> Path:
        """A screenshot of one of the chat's browsing turns (KeyError when the chat has no such session)."""
        from .browser import runs as browser_runs

        chat = self._chat_manager().chat(chat_id)
        if not any(isinstance(r, dict) and r.get("session_lock") == session_lock for r in chat.get("browser") or []):
            raise KeyError("no such screenshot")
        return browser_runs.artifact_path(self.config.data_dir, session_lock, name)

    # -- the Codex worker (DESIGN §44; BROWSER_AND_CODEX.md §3) --------------------------------------------------

    def job_receipts(self, repo: str, job_id: str) -> dict:
        """GET …/receipts: the job's run receipts as the run card shows them (ids, outcomes, paths, the commit, the
        checks; never text), whether their hash chain holds, and the reconcile summary of an unclean end."""
        job = self.job(repo, job_id)
        store = codex_work.Receipts(self.config.data_dir)
        return {"receipts": store.summaries(repo, job_id), "chain_broken": store.verify_chain(repo, job_id),
                "reconcile": job.get("reconcile"), "verify": job.get("verify"),
                "preview": self._preview_offer(job)}

    def job_receipt(self, repo: str, job_id: str, run: str) -> dict:
        """One run receipt as written (KeyError for none)."""
        self.job(repo, job_id)
        return codex_work.Receipts(self.config.data_dir).read(repo, job_id, run)

    def _preview_offer(self, job: dict) -> dict:
        """Whether "Verify in the browser" is offered (§4.4): a repository with a preview, the job's worktree there,
        the job in review, approved or needs-human; with why not otherwise."""
        try:
            repo = self.config.repo(job["repo"])
        except KeyError:
            return {"offered": False, "why": "the repository is no longer configured"}
        if not repo.preview:
            return {"offered": False, "why": "the repository has no preview setting"}
        if job.get("stage") not in ("review", "approve", "needs-human"):
            return {"offered": False, "why": "offered while the job is in review, approved or needs you"}
        if not Path(job["worktree"]).is_dir():
            return {"offered": False, "why": "the job's worktree is gone"}
        return {"offered": True, "persona": codex_work.settings(self.config)["verify_persona"],
                "port": repo.preview.get("port")}

    def verify_in_browser(self, repo: str, job_id: str) -> dict:
        """POST …/verify-in-browser: a browsing chat for the persona of codex.verify_persona whose chat.json carries
        verify {repo, job, branch, preview}; its grant is built from that record (the preview from this job's branch
        in its sibling namespace, §2.9), never from the model. ActionError when it is not offered; ValueError (and no
        chat) when the persona's route could take the page off this machine on a repository not marked
        personal_data: false (§3.3; each turn checks where it was placed again)."""
        job = self.job(repo, job_id)
        offer = self._preview_offer(job)
        if not offer.get("offered"):
            raise ActionError(f"Verify in the browser is not offered: {offer.get('why')}")
        preview = dict(self.config.repo(repo).preview or {})
        record = {"repo": repo, "job": job_id, "branch": job.get("branch"), "preview": preview}
        manager = self._chat_manager()
        chat = manager.new_chat(None, agent=str(offer["persona"]))
        try:
            return manager.set_verify(chat["id"], record)
        except ValueError:
            with contextlib.suppress(Exception):
                manager.chat_delete(chat["id"])
            raise

    def attach_browser_receipt(self, repo: str, job_id: str, record: dict) -> list[str]:
        """At the end of a "Verify in the browser" turn the core (never the chat) adds the browser receipt as receipt
        evidence beside the job's criteria: check ok only when the turn checked an expected text and every check
        matched (a decoy outside the result element reads matched: false). Never a merge gate in BC1."""
        receipt = str(record.get("receipt") or "")
        if not receipt:
            return []
        expects = [row.get("expect") for row in record.get("rows") or [] if isinstance(row, dict)
                   and isinstance(row.get("expect"), dict)]
        matched = bool(expects) and all(expect.get("matched") is True for expect in expects)
        entry = {"kind": "receipt", "ref": receipt[:200], "check": "ok" if matched else "failed",
                 "detail": f"Verify in the browser: {len(expects)} expected text(s) checked, "
                           f"{'all matched' if matched else 'not all matched'}; grant {record.get('grant')}"}
        job = self.job(repo, job_id)
        return self._criteria_evidence(job, entry)

    def _criteria_evidence(self, job: dict, entry: dict) -> list[str]:
        """entry (code-written check or receipt evidence) beside every criterion item of the job; their ids."""
        added: list[str] = []
        try:
            with self._job_items(job) as ledger:
                for item in list(ledger.items):
                    if item.get("kind") == "criterion" and item.get("status") not in ("dropped",):
                        if ledger.add_evidence(tracker.FACTORY, item["id"], [dict(entry)]) is not None:
                            added.append(item["id"])
        except (tracker.ItemError, OSError) as exc:
            self._log(job, "warn", f"could not add {entry.get('kind')} evidence to the criteria: {exc}")
        return added

    def _codex_problem(self, repo: str, routing: dict) -> str | None:
        """Why a job whose builder (or a rung of it) runs on codex-worker may not start: the routing check, then the
        codex model blocker for each such route (GETs only, §3.3 (c)), then the host's containment (with a Codex user:
        this version's launcher, the uid egress rule loaded). None for other jobs."""
        if not codex_work.worker_agents(self.config, routing):
            return None
        problem = codex_work.routing_problem(self.config, repo, routing)
        if problem:
            return problem
        builder = routing.get("builder") if isinstance(routing.get("builder"), dict) else {}
        provider = None
        for route in [builder, *[rung for rung in builder.get("escalate") or [] if isinstance(rung, dict)]]:
            if not codex_work.is_worker(self.config, route.get("driver")):
                continue
            try:
                model = self._model_config(route)
            except StepError as exc:
                return str(exc)
            problem = codex_work.model_blocker(self.config, model.provider, model.model)
            if problem:
                return problem
            provider = provider or model.provider
        return codex_work.containment_blocker(self.config, provider)

    def _ran_codex(self, job: dict) -> bool:
        """Whether a Codex run has been in this job (one in flight, or a job.runs entry on codex-worker or with a
        receipt: only Codex runs get one)."""
        return bool(job.get("codex_run")) or any(
            isinstance(entry, dict) and ("receipt" in entry or codex_work.is_worker(self.config, entry.get("driver")))
            for entry in job.get("runs") or [])

    def _codex_hosted_problem(self, repo: str, routing: dict) -> str | None:
        """§3.3 for a job that ran Codex, whatever its routing says now: on a repository not marked
        personal_data: false, the first route of routing (the builder's too, once it is off codex-worker) that would
        send what it reads (Codex's diff, build.md, the verify output) off this machine, in the routing check's
        words; None when every route stays here."""
        if not codex_work.repo_work(self.config, repo).personal_data:
            return None
        for name, route in sorted((routing or {}).items()):
            why = codex_work.hosted_route(self.config, route)
            if why:
                return (f"{name} would read what Codex wrote, and {why}: on a repository not marked "
                        f"personal_data: false no role of a codex-worker job leaves this machine "
                        f"(BROWSER_AND_CODEX.md §3.3)")
        return None

    def _unfinished_codex_run(self, job: dict, agent_name: str) -> dict | None:
        """The agent's last run when it was a Codex run that did not complete (unknown, failed, timed out,
        cancelled, or cut off by a restart): its thread is never resumed and its prompt never sent again (§3.6,
        DESIGN §40), so the next run starts a new session. None otherwise."""
        last = _last_run(job, agent_name)
        if not last or last.get("outcome") in (None, "completed"):
            return None
        return last if "receipt" in last or codex_work.is_worker(self.config, last.get("driver")) else None

    def _unfinished_note(self, job: dict, unfinished: dict) -> str:
        """What the next builder run is told about a Codex run that did not complete: how it ended and the reconcile's
        summary of what it left."""
        reconciled = job.get("reconcile") if isinstance(job.get("reconcile"), dict) else {}
        summary = reconciled.get("summary") if reconciled.get("run") == unfinished.get("run") else None
        left = f"Reconciled: {summary}." if summary else "What it left is in the worktree."
        return (f"The previous builder run ({unfinished.get('run')}) ended {unfinished.get('outcome')}: its session is "
                f"never resumed and its request never sent again. {left} Check the worktree before you go on: it is "
                f"as that run left it.")

    def _codex_session_gone(self, job: dict, agent_name: str) -> bool:
        """Whether the agent ran on codex-worker in this job and its thread was dropped with the job's home at job end
        (_codex_job_ended): job.agents keeps no session for it."""
        record = (job.get("agents") or {}).get(agent_name) or {}
        return bool(record.get("runs")) and not record.get("session") and codex_work.is_worker(self.config,
                                                                                                record.get("driver"))

    def _ignored_files(self, worktree: Path) -> dict[str, tuple[int, int, int]] | None:
        """The worktree's ignored files (gitops.ignored_files: size, mtime, ctime), or None when git cannot list
        them."""
        try:
            return gitops.ignored_files(worktree)
        except (GitError, OSError):
            return None

    def _ignored_left(self, job: dict, before: dict[str, tuple[int, int, int]] | None = None,
                      since: float | None = None) -> list[str] | None:
        """The files git ignores that the job's Codex runs made or changed and left in the worktree. The commit does
        not hold them, so verify in the worktree would not be verify on the commit (§3.3); kept as
        job.codex_ignored, so a later verify still sees the ones that stay.

        since (the run's start, time.time()) is given after every Codex run, whatever its outcome. What the run
        made or changed is told by before (the worktree's ignored files before it, by size, mtime and ctime: ctime
        is the one time no process can set back, so a rewrite to the same size that puts the mtime back counts too);
        without one (a harness restart mid-run, or git could not list them) by time: every ignored file whose mtime
        or ctime is at or after since. The harness's own change to the worktree's ACLs (open_worktree) comes before
        since and the snapshot, past IGNORED_SLACK, so the ctimes it changes never count. job.codex_ignored_since
        keeps that time while the files cannot be listed, or are more than IGNORED_KEEP, so a later call counts them
        the same way. The caches among them (VERIFY_CACHES) are removed, so verify never reads one; one that cannot
        be removed counts. None when they cannot be listed (verify then fails closed)."""
        kept = [str(path) for path in job.get("codex_ignored") or []]
        pending = job.get("codex_ignored_since")
        pending = float(pending) if isinstance(pending, (int, float)) else None
        if since is None and pending is None and not kept:
            return []
        worktree = Path(str(job.get("worktree") or ""))
        now = self._ignored_files(worktree) if job.get("worktree") else None
        if since is not None and (before is None or now is None):   # this run is counted by time
            pending = since if pending is None else min(pending, since)
        if now is None:
            if pending != job.get("codex_ignored_since"):
                self._update(job, codex_ignored_since=pending)
            return None
        found = {path for path in kept if path in now}
        if since is not None and before is not None:
            found |= {path for path, seen in now.items() if tuple(before.get(path, ())) != tuple(seen)}
        if pending is not None:
            cutoff = int((pending - IGNORED_SLACK) * 1e9)
            found |= {path for path, seen in now.items() if max(seen[1], seen[2]) >= cutoff}
        caches = {path for path in found if set(PurePosixPath(path).parts) & VERIFY_CACHES}
        left = sorted(found - {path for path in caches if _remove_below(worktree, path)})
        times = [moment for moment in (pending, since) if moment is not None]
        pending = min(times) if times and len(left) > IGNORED_KEEP else None   # more than the list keeps: by time
        if left[:IGNORED_KEEP] != kept or pending != job.get("codex_ignored_since"):
            self._update(job, codex_ignored=left[:IGNORED_KEEP] or None, codex_ignored_since=pending)
        return left

    def _codex_after(self, job: dict, agent_name: str, round_: int | None, result: AgentRun, model: ModelConfig,
                     run_route: dict, log_path: Path, grid: Any, violation: str | None, started: str,
                     ignored_before: dict[str, tuple[int, int, int]] | None = None,
                     since: float = 0.0) -> str | None:
        """After a codex-worker run (§3.3, §3.6, §3.7): the ignored files it made or changed, whatever its outcome
        (ignored_before is the worktree's before this run, since its start: _ignored_left), the harness commit
        (after the write-policy check, and only when collect()'s share-worktree answered without an error: a
        check record otherwise, and the step fails), verify on that commit (failed without running while the job's
        runs left ignored files the commit does not hold; stopped by the job's Stop), the reconcile of an unclean
        end, and the run's receipt with the job.runs pointer. Returns why the step fails (a refused or skipped
        commit), else None. Never raises for the receipt itself."""
        run = log_path.stem
        outcome = outcome_of(result)
        details = result.details or {}
        checks: list[dict] = [{"kind": "write_policy", "ok": violation is None,
                               **({"reason": violation} if violation else {})}]
        commit_: dict | None = None
        reconciled: dict | None = None
        error: str | None = None
        artifacts: dict[str, Any] = {"paths": [], "diff_sha256": None, "final_message_sha256":
                                     codex_work.text_sha256(result.final_text)}
        left = self._ignored_left(job, ignored_before, since)
        if left:
            artifacts["ignored_left"] = left[:50]
        build_text = _read_text(Path(job["job_dir"]) / "build.md")
        artifacts["build_md_sha256"] = codex_work.text_sha256(build_text) if details.get("build_md_copied") else None
        share = details.get("share") if isinstance(details.get("share"), dict) else {}
        if share.get("error"):   # a folder Codex left closed would be missing from the commit, git says nothing
            checks.append({"kind": "share_worktree", "ok": False, "reason": str(share["error"])[:400]})
            if outcome == "completed" and violation is None:
                error = (f"what Codex wrote could not be made the group's (the launcher's share-worktree: "
                         f"{str(share['error'])[:300]}), so nothing was committed: a file or folder it left closed "
                         f"to the harness would be missing from the commit")
        if error is None and outcome == "completed" and violation is None and codex_work.commits_by_harness(
                self.config, run_route.get("driver")):
            try:
                commit_ = codex_work.commit(self.config, job, int(round_ or job.get("round") or 1),
                                            self.secret_values(), [build_text])
                checks.append({"kind": "secret_scan", "ok": True})
            except (codex_work.WorkError, GitError) as exc:
                checks.append({"kind": "secret_scan", "ok": False, "reason": str(exc)})
                error = f"the harness commit was refused: {exc}"
            if commit_:
                artifacts["paths"] = commit_.get("paths") or []
                with contextlib.suppress(GitError):
                    shown = gitops.git(Path(job["worktree"]), "show", "--binary", "--format=", "--no-color",
                                       "--no-ext-diff", "--no-textconv", commit_["sha"], timeout=gitops.SLOW_TIMEOUT)
                    artifacts["diff_sha256"] = hashlib.sha256(shown.encode("utf-8", "surrogateescape")).hexdigest()
                cancel = self._cancel_event(_key(job))
                check = codex_work.verify(self.config, job, run, commit_["sha"], ignored_left=left, cancel=cancel)
                if check:
                    checks.append(check)
                    if not cancel.is_set():   # a stopped verify is never the commit's result
                        self._note_verify(job, check, run)
        elif outcome != "completed":
            reconciled = codex_work.reconcile(self.config, job)
            artifacts["paths"] = list(reconciled.get("paths") or [])
            artifacts["diff_sha256"] = reconciled.get("diff_sha256")
            self._update(job, reconcile={"run": run, "outcome": outcome, "summary": codex_work.retry_summary(reconciled),
                                         "paths": artifacts["paths"][:50], "diff_sha256": reconciled.get("diff_sha256"),
                                         "at": codex_work.utc_now()})
        if outcome == "completed" and error is None:
            self._update(job, reconcile=None)
        what = (error or (f"committed {commit_['sha'][:12]}" if commit_ else "nothing to commit") if outcome == "completed"
                else f"run {outcome}: reconciled, nothing committed, never replayed")
        attempt = sum(1 for entry in job.get("runs") or [] if entry.get("agent") == agent_name)
        record = codex_work.run_record(
            job=job, run=run, agent=agent_name, attempt=max(1, attempt), grid=grid, version=None,
            thread_id=details.get("thread_id") or result.session_ref, model=str(model.model), provider=model.provider,
            home=details.get("home") if isinstance(details.get("home"), dict) else {}, started=started,
            ended=codex_work.utc_now(), outcome=outcome, error=result.error, artifacts=artifacts, checks=checks,
            commit_=commit_, reconciled=reconciled, result=what, exit_code=details.get("exit"))
        self._write_receipt(job, run, record, outcome)
        return error

    def _write_receipt(self, job: dict, run: str, record: dict, outcome: str) -> None:
        try:
            rel, digest = codex_work.Receipts(self.config.data_dir).write(job["repo"], job["id"], run, record)
        except (codex_work.WorkError, OSError) as exc:
            self._log(job, "error", f"could not write the receipt of {run}: {exc}")
            rel, digest = None, None
        with self._job_lock(_key(job)):
            for entry in reversed(job.get("runs") or []):
                if entry.get("run") == run:
                    entry.update(receipt=rel, sha256=digest, outcome=outcome)
                    break
            else:
                job.setdefault("runs", []).append({"run": run, "agent": record["request"]["agent"], "ok": False,
                                                   "outcome": outcome, "receipt": rel, "sha256": digest})
            job["codex_run"] = None
            self._save(job)

    def _note_verify(self, job: dict, check: dict, run: str | None) -> None:
        """Keep the last verify result on the job (the build check reads it for the commit it ran on) and put it
        beside the criteria as check evidence (code-written, with its check set explicitly)."""
        self._update(job, verify={key: check.get(key) for key in ("commit", "check", "exit", "seconds",
                                                                    "output_sha256", "tail", "reason", "not_run")}
                     | {"run": run})
        sha = str(check.get("commit") or "")[:12] or "the worktree"
        if check.get("not_run"):   # about the worktree, not the commit: a later run on the commit is evidence too
            ref, detail = f"verify@{sha} (not run)", f"verify did not run: {check.get('reason')}"
        else:
            ref = f"verify@{sha}"
            detail = (f"the harness ran `{check.get('argv', ['', '', ''])[-1]}` under bwrap: exit {check.get('exit')} "
                      f"in {check.get('seconds')}s, output sha256 {str(check.get('output_sha256') or '')[:16]}")
        self._criteria_evidence(job, {"kind": "check", "ref": ref, "check": check.get("check") or "failed",
                                      "detail": detail})

    def _verify_problem(self, job: dict, repo: RepoConfig) -> tuple[str, str] | None:
        """The build check's verify rule: (problem, the corrective prompt) when the repository's verify command fails
        on the branch's head, None when it passes or there is none. A result for the head commit is reused (the
        harness ran it right after its own commit) unless ignored files the job's Codex runs left were in the way
        then or are now; otherwise it runs now, under bwrap, until the job's Stop."""
        if not repo.verify:
            return None
        head = gitops.rev_parse(repo.path, job["branch"])
        left = self._ignored_left(job)
        found = job.get("verify") if isinstance(job.get("verify"), dict) else None
        if not found or found.get("commit") != head or found.get("not_run") or left != []:
            check = codex_work.verify(self.config, job, f"{int(job.get('round') or 1):02d}-verify", head,
                                      ignored_left=left, cancel=self._cancel_event(_key(job)))
            self._check_cancel(job)   # a stopped verify is never kept as the head's result
            self._note_verify(job, check, None)
            found = job.get("verify")
        if found.get("check") == "ok":
            return None
        tail = str(found.get("tail") or found.get("reason") or "")[-2000:]
        return (f"verify failed on {head[:12]} (exit {found.get('exit')})" + (f": {found['reason']}" if found.get("reason")
                                                                             else ""),
                VERIFY_FIX.format(code=found.get("exit"), tail=tail))

    def _codex_interrupted(self, job: dict) -> None:
        """Restart recovery of a Codex run that was in flight (§3.6): unknown, reconciled, recorded in a receipt;
        its prompt is never sent again. The run's own end never came, so first what the Codex user wrote in the
        worktree is made the group's (share-worktree), so that the count of its ignored files and the reconcile see
        every file: a folder Codex made 0700 is closed to the harness until then, and git lists past it without
        failing. The share only adds group bits to the Codex user's entries, and its ctime change falls on entries
        only the interrupted run can have made, so the count by time that follows (its snapshot went with the old
        process; an intent without its start counts every one) counts no fewer. Then the job folder's share is taken
        back (§3.11) and the worktree reconciled."""
        intent = job.get("codex_run") or {}
        run = str(intent.get("run") or "")
        since = intent.get("since")
        shared = codex_work.share_worktree(self.config, job)
        if shared.get("error"):   # the count below then fails closed where git cannot open a folder
            self._log(job, "warn", f"what Codex wrote could not be shared before its ignored files were counted and "
                                   f"the reconcile: {shared['error']}")
        left = self._ignored_left(job, None, float(since) if isinstance(since, (int, float)) else 0.0)
        codex_work.unshare_job(self.config, job)
        reconciled = codex_work.reconcile(self.config, job)
        self._update(job, reconcile={"run": run, "outcome": "unknown", "summary": codex_work.retry_summary(reconciled),
                                     "paths": list(reconciled.get("paths") or [])[:50],
                                     "diff_sha256": reconciled.get("diff_sha256"), "at": codex_work.utc_now()})
        if not codex_work.RUN_NAME_RE.fullmatch(run):
            self._update(job, codex_run=None)
            return
        route = (job.get("routing") or {}).get(intent.get("agent") or "builder") or {}
        record = codex_work.run_record(
            job=job, run=run, agent=str(intent.get("agent") or "builder"), attempt=1, grid=None, version=None,
            thread_id=None, model=str(route.get("model_id") or ""), provider=None, home={},
            started=str(intent.get("started") or ""), ended=codex_work.utc_now(), outcome="unknown", error=INTERRUPTED,
            artifacts={"paths": list(reconciled.get("paths") or []), "diff_sha256": reconciled.get("diff_sha256"),
                       **({"ignored_left": left[:50]} if left else {})},
            checks=[], commit_=None, reconciled=reconciled, result="the harness restarted mid-run: unknown, never replayed")
        self._write_receipt(job, run, record, "unknown")

    def _codex_share_worktree(self, job: dict) -> None:
        """Before the harness removes a Codex job's worktree: what the Codex user wrote there is made the group's
        once more (share-worktree, §3.11), so a file it left 0600 or a folder it left 0700 never stops the removal;
        nothing without a Codex user or for a job Codex never ran in."""
        if self._codex_ran(job):
            codex_work.share_worktree(self.config, job)

    def _codex_ran(self, job: dict) -> bool:
        """Whether a Codex run ever started for the job: one whose intent is still on record (codex_run: it never
        reached its end), a run on a codex-worker driver or with Codex's labels (the driver may have left the
        configuration since), or one with a receipt (the entry a restart's recovery writes for a run that was in
        flight names no driver)."""
        return isinstance(job.get("codex_run"), dict) or any(
            codex_work.is_worker(self.config, entry.get("driver")) or "codex" in entry or "receipt" in entry
            for entry in job.get("runs") or [])

    def _codex_job_ended(self, job: dict) -> None:
        """At job end: the job folder's share is taken back whatever its runs say (a no-op without codex.group), the
        job's CODEX_HOME is deleted (its rollouts' hash is in each run's receipt) and its run folders with it; old
        receipts are pruned within the caps the browser's sessions keep (O6). The Codex threads lived in that home,
        so none of them is resumed again: a delivered job that comes back (a conflicting merge, then rework) starts
        its builder in a new session with the full system text."""
        codex_work.unshare_job(self.config, job)
        if not self._codex_ran(job):
            return
        codex_work.remove_home(self.config, job)
        with self._job_lock(_key(job)):
            for record in (job.get("agents") or {}).values():
                if isinstance(record, dict) and record.get("session") and codex_work.is_worker(self.config,
                                                                                               record.get("driver")):
                    record["session"] = None
            self._save(job)
        runs = codex_work.work_root(self.config) / "runs" / codex_work.job_key(job)
        if runs.is_dir() and not runs.is_symlink():
            shutil.rmtree(runs, ignore_errors=True)
        from .browser.runs import settings as browser_settings

        caps = browser_settings(self.config)["caps"]
        codex_work.Receipts(self.config.data_dir).prune(
            int(float(caps["max_gb"]) * (1 << 30)), float(caps["max_days"]),
            keep=lambda repo, job_id: self._job_alive(repo, job_id))

    def codex_state(self) -> dict:
        """GET /api/codex: the worker's settings (no secret), what is checked and the owner's setup blocks."""
        from .codex import setup as codex_setup

        merged = codex_work.settings(self.config)
        return {"enabled": merged["enabled"], "pin": merged["pin"], "user": merged["user"], "via": merged["via"],
                "group": merged["group"], "binary": merged["binary"],
                "launcher": str(codex_work.launcher_path(self.config)),
                "work_root": str(codex_work.work_root(self.config)),
                "checks": codex_work.check_record(self.config), "setup": codex_setup.blocks(self.config),
                "risk": codex_setup.RESIDUAL_RISK}

    def new_chat(self, repo: str | None, model: str | None = None, mode: str = "worktree", agent: str = "coder",
                 profile: str | None = None, persona: str | None = None) -> dict:
        """Create a chat on repo (mode worktree or repo) with agent (any agent with tools; default coder), or
        without a repository (repo None, mode none: a scratch folder, and the agent may have no tools), placed
        by profile (None: the default profile; "": none). persona: a persona's chat (§33), without a repo."""
        extra: dict[str, Any] = {"profile": profile} if profile is not None else {}
        if persona is not None:
            extra["persona"] = persona
        return self._chat_manager().new_chat(repo, model, mode, agent, **extra)

    def chat_send(self, chat_id: str, text: str, origin: str = "user", wake_id: str | None = None,
                  wake_item: str | None = None) -> dict:
        """Start a chat turn in a thread (origin "wake": a scheduled check-in, Wake & Messages §5.8)."""
        if origin == "user" and wake_id is None and wake_item is None:
            return self._chat_manager().chat_send(chat_id, text)
        return self._chat_manager().chat_send(chat_id, text, origin=origin, wake_id=wake_id, wake_item=wake_item)

    def chat_cancel(self, chat_id: str) -> dict:
        """Cancel the chat's running turn."""
        return self._chat_manager().chat_cancel(chat_id)

    def chat_delete(self, chat_id: str) -> None:
        """Delete a chat with its worktree and branch; then (after ChatManager returns, so neither manager calls the
        other under its lock) its pending check-ins end."""
        self._chat_manager().chat_delete(chat_id)
        if self._wakes is not None or self.config.raw.get("wakes"):
            self._wake_manager().cancel_chat(chat_id)

    def chat_handoff(self, chat_id: str, feature: str, delivery: str | None = None,
                     profile: str | None = None) -> dict:
        """Hand the chat's branch to the factory (delivery: merge, branch or folder; profile: the job's
        routing profile, None the default, "" none); returns the job."""
        extra = {"profile": profile} if profile is not None else {}
        return self._chat_manager().chat_handoff(chat_id, feature, delivery, **extra)

    # -- personas and Open WebUI profiles (section 33; Wake & Messages adds memory and threads) ------------

    def personas(self) -> list[dict]:
        """Every persona (project and user agent dirs), as personas.persona_view rows, by name."""
        from . import personas

        found = personas.discover_personas(self.config)
        return [personas.persona_view(self.config, found[name]) for name in sorted(found)]

    def persona(self, name: str) -> dict:
        """{persona, card, drift, capability_map, import}: one persona, the saved profile card of its workspace
        model, the last drift observation, the card's capability map for this persona now and what the card says
        about its import (_import_record; null without a card; KeyError if unknown). A wake persona answers her
        page, with import for the workspace model her soul was imported from."""
        from . import personas

        found = personas.find_persona(self.config, name)
        if found.kind == "wake":   # a persona that wakes itself: her page (Wake & Messages §4.5)
            source = found.source or {}
            record = self._import_record(str(source["model"]), source.get("imported_at"), source.get("version_id")) \
                if source.get("model") else None
            return {**self._wake_manager().persona(name), "card": None, "drift": None, "import": record}
        model_id = (found.binding or {}).get("model")
        card = personas.load_profile(model_id) if model_id else None
        return {"persona": personas.persona_view(self.config, found), "card": card,
                "drift": personas.drift_state(self.config, model_id) if model_id else None,
                "capability_map": personas.capability_map(card, found, self.config) if card else None,
                "import": self._import_record(str(model_id), card.get("imported_at"), card.get("version_id"))
                if card else None}

    def persona_card(self, name: str) -> dict:
        """{persona, card, live, drift, appended_text, capability_map, import}: the profile card page of a persona
        bound to (or copied from) an Open WebUI workspace model: the saved card (null before an import), what the
        key sees live now, the card's capability map for this persona and the personas there are now, and what
        the card says about its import and the changes before it (section 33; both null before an import)."""
        from . import personas

        found = personas.find_persona(self.config, name)
        model_id = (found.binding or {}).get("model")
        if not model_id:
            raise ValueError(f"persona {name} is not bound to an Open WebUI workspace model")
        card = personas.load_profile(model_id)
        return {"persona": name, "card": card, "live": personas.live_card(self.config, found),
                "drift": personas.drift_state(self.config, model_id),
                "appended_text": personas.appended_text(found, self.config),
                "capability_map": personas.capability_map(card, found, self.config) if card else None,
                "import": self._import_record(model_id, (card or {}).get("imported_at"), (card or {}).get("version_id"))
                if card else None}

    def _import_record(self, model_id: str, imported_at: Any, version_id: Any) -> dict:
        """What the persona card says about its import (DESIGN §33 "Changes before import"): the harness had no copy
        before its first import, so that import is the baseline and there is no diff of what came before; the
        changes before it are only the operator's receipts the owner or the operator attached here by id, each
        with its time, intent, not_verified, verified and outcome (with the outcome the receipt itself reports, DESIGN
        §40), labelled reported by the operator."""
        from . import personas
        from .receipts import REPORTED

        reader = self._receipt_reader()
        rows = []
        for row in reader.attached(model_id):
            try:
                receipt = reader.get(row["id"])["receipt"]
            except KeyError:
                rows.append({**row, "missing": True, "label": REPORTED,
                             "note": "the receipts index no longer holds this receipt"})
                continue
            rows.append({**row, **{key: receipt.get(key) for key in ("time", "intent", "not_verified", "verified",
                                                                      "outcome", "reported_outcome", "file", "gone")},
                         "label": REPORTED})
        return {"model": model_id, "imported_at": imported_at, "version_id": version_id, "diff": None,
                "baseline": personas.IMPORT_BASELINE, "changes_before_import": rows}

    def persona_before_import(self, name: str, *, attach: list[str] | None = None, detach: list[str] | None = None,
                              by: str = "owner") -> dict:
        """Attach the operator's receipts (by id) to the import of a persona's workspace model as its changes
        before import, or take them off (DESIGN §33). Ids the receipts index does not hold are refused (her
        receipts are scanned first, at most every operator.scan_seconds); nothing is chosen by a receipt's text.
        Returns {import} as the persona card shows it."""
        from . import personas

        found = personas.find_persona(self.config, name)
        model_id = personas.workspace_model(found)
        if not model_id:
            raise ValueError(f"persona {name} is not bound to or imported from an Open WebUI workspace model")
        reader = self._receipt_reader()
        self._operator_prescan(reader)
        reader.attach(model_id, attach=attach or [], detach=detach or [], by=by)
        self.bus.publish("persona.updated", {"persona": name, "what": "before_import"})
        if found.kind == "wake":
            source = found.source or {}
            return {"import": self._import_record(model_id, source.get("imported_at"), source.get("version_id"))}
        card = personas.load_profile(model_id) or {}
        return {"import": self._import_record(model_id, card.get("imported_at"), card.get("version_id"))}

    def persona_import(self, body: dict) -> dict:
        """Import an Open WebUI workspace model's profile card: {export: [records], model_id?} (an uploaded Export
        file) or {source: "openwebui", model_id, provider?} (GET /api/v1/models/export with the owner's key);
        snapshot: {name, model?, user_name?} only when the owner asks for the offline mode. Returns
        personas.import_profile's {card, path, unavailable, unknown_variables, fingerprint, snapshot}; publishes
        persona.imported (and agent.updated for a snapshot)."""
        from . import personas

        if not isinstance(body, dict):
            raise ValueError("the import must be a JSON object")
        if "name" in body and "snapshot" not in body:   # a wake persona's soul (Wake & Messages §2.2)
            unknown = sorted(set(body) - {"export", "source", "key", "model_id", "provider", "name", "user_name",
                                          "description", "display"})
            if unknown:
                raise ValueError(f"unknown field {', '.join(unknown)}")
            result = self._wake_manager().import_persona(body)
            self.bus.publish("persona.imported", {"model": body.get("model_id"), "persona": result["persona"],
                                                  "fingerprint": result["fingerprint"], "wake": True})
            return result
        unknown = sorted(set(body) - {"export", "source", "model_id", "provider", "snapshot"})
        if unknown:
            raise ValueError(f"unknown field {', '.join(unknown)} (use export or source, model_id, provider, snapshot)")
        records, source = body.get("export"), body.get("source")
        if (records is None) == (source is None):
            raise ValueError("give either export (an Open WebUI Export file's records) or source: \"openwebui\"")
        if source is not None and source != "openwebui":
            raise ValueError("source must be \"openwebui\"")
        model_id = body.get("model_id")
        if model_id is not None and (not isinstance(model_id, str) or not model_id.strip()
                                     or len(model_id) > personas.MAX_MODEL_ID_CHARS):
            raise ValueError("model_id must be the workspace model's id")
        provider = body.get("provider")
        if provider is not None and not isinstance(provider, str):
            raise ValueError("provider must be a provider name")
        snapshot = body.get("snapshot")
        if snapshot is not None and not isinstance(snapshot, dict):
            raise ValueError("snapshot must be an object like {\"name\": \"kate-offline\"} (only for the offline mode)")
        result = personas.import_profile(self.config, records=records, model_id=model_id, provider=provider,
                                         snapshot=snapshot, on_saved=self.reload_config,
                                         writing=self._config_write if snapshot is not None else None)
        card = result["card"]
        made = result.get("snapshot")
        self.bus.publish("persona.imported", {"model": card["model_id"], "version_id": card["version_id"],
                                              "fingerprint": card["fingerprint"], "source": card["source"],
                                              "snapshot": made["persona"] if made else None})
        if made:
            self._agent_updated(made["persona"], self.agent_detail(made["persona"]))
        return result

    def persona_check(self, name: str | None = None) -> dict:
        """The drift check, now (the GUI's "Check now", `factory personas check`, a timer): every persona bound
        to an Open WebUI workspace model or copied from one (or the one called name) against the live preset,
        each workspace model once; a change that alters behaviour, or cannot be told apart, opens one
        system-scope item. {checked: [{persona, model, kind, version_id, previous_version_id, item, stale?
        (a snapshot of another version), error?}]}; persona.drift for each persona of a changed model."""
        from . import personas

        found = personas.discover_personas(self.config)
        if name is not None:
            found = {name: personas.find_persona(self.config, name)}
        rows: list[dict] = []
        listings: dict[str, Any] = {}
        results: dict[tuple[str, str], dict] = {}
        for persona in [found[key] for key in sorted(found)]:
            model_id = (persona.binding or {}).get("model")
            if persona.kind not in ("openwebui", "snapshot") or not model_id:
                continue
            row: dict[str, Any] = {"persona": persona.name, "model": model_id}
            try:
                api = personas.client(self.config, persona)
                if api.provider.name not in listings:
                    listings[api.provider.name] = api.models()
                entry = personas.listed(listings[api.provider.name], model_id)
                if entry is None:
                    raise personas.OpenWebUIError(f"Open WebUI does not list {model_id!r} for this key")
                info = entry.get("info") or {}
                key = (api.provider.name, str(model_id))
                if key not in results:
                    results[key] = personas.check_drift(self.config, model_id, info,
                                                        name=str(entry.get("name") or model_id),
                                                        provider=api.provider.name, api=api,
                                                        open_item=self.system_item)
                result = results[key]
            except (KeyError, ValueError, personas.OpenWebUIError) as exc:
                row["error"] = str(exc.args[0] if exc.args else exc)
            else:
                row.update({key: result[key] for key in ("kind", "version_id", "previous_version_id", "item")})
                if persona.kind == "snapshot":
                    row["stale"] = info.get("version_id") != (persona.binding or {}).get("version_id")
                if result["changed"]:
                    self.bus.publish("persona.drift", {"persona": persona.name, **row})
            rows.append(row)
        return {"checked": rows}

    def openwebui_models(self, provider: str | None = None) -> dict:
        """What an Open WebUI provider lists for its key (the import picker, "Add model from template")."""
        from . import personas

        return personas.workspace_models(self.config, provider)

    def plans(self) -> list[dict]:
        """Every plan.json."""
        return self._plan_manager().plans()

    def plan(self, repo: str, plan_id: str) -> dict:
        """One plan.json (KeyError if unknown)."""
        return self._plan_manager().plan(repo, plan_id)

    def create_plan(self, repo: str, request: str, routing: dict | str | None = None,
                    delivery: str | None = None, profile: str | None = None) -> dict:
        """Create a plan and start the lead (delivery: merge, branch or folder, for each of its jobs;
        profile: the routing profile of its jobs and lead, None the default, "" none)."""
        extra = {"profile": profile} if profile is not None else {}
        try:
            return self._plan_manager().create(repo, request, routing, delivery, **extra)
        finally:
            self._forget_secrets()   # a preset routing may bring providers the config does not have

    def update_plan_text(self, repo: str, plan_id: str, text: str) -> dict:
        """Replace plan.md with a human's edit."""
        return self._plan_manager().update_plan_text(repo, plan_id, text)

    def run_plan(self, repo: str, plan_id: str) -> dict:
        """Start a ready plan."""
        return self._plan_manager().run(repo, plan_id)

    def cancel_plan(self, repo: str, plan_id: str) -> dict:
        """Cancel a plan and its active jobs."""
        return self._plan_manager().cancel(repo, plan_id)

    def retry_plan(self, repo: str, plan_id: str) -> dict:
        """Re-run the step that stopped a plan."""
        return self._plan_manager().retry(repo, plan_id)

    # -- the Housekeeping Tracker (section 32) ---------------------------------------------

    def items(self, repo: str | None = None, scope: str | None = None, status: list[str] | None = None,
              owner: str | None = None, waiting: bool = False, overdue: bool = False, limit: int = 200,
              for_agent: str | None = None) -> dict:
        """Every tracked item across jobs, chats, plans, follow-ups and the system scope: {items: [item + ref,
        scope, word, waiting, overdue, age_days], counts, total}. scope is job, chat, plan, followups or
        system; status a list of statuses; waiting / overdue keep only those. Counts cover repo and scope,
        before the other filters. Rows waiting for a person come first, then must-do items, then the most
        recently changed. for_agent (an outside agent's token name): only the items that agent owns in the
        system scope and the repos' follow-ups (those it added or a person assigned to it), never a persona's
        item nor one a private turn wrote (items.agent_visible)."""
        if scope is not None and scope not in tracker.PREFIXES:
            raise ValueError("scope must be job, chat, plan, followups or system")
        wanted = set(status or [])
        unknown = sorted(wanted - set(tracker.STATUSES))
        if unknown:
            raise ValueError(f"unknown status {', '.join(unknown)} (use {', '.join(tracker.STATUSES)})")
        now = utc_now()
        rows: list[dict] = []
        if for_agent is not None:
            if scope not in (None, "system", "followups"):
                raise ValueError("an agent token reads its own items in the system scope and the follow-ups only")
            if scope in (None, "system"):
                rows += self._system_manager().rows(now, for_agent=for_agent)
            if scope in (None, "followups"):
                for name in self._item_repos(repo):
                    store = self._followups_store(name)
                    if store.path.is_file():
                        with contextlib.suppress(OSError, ValueError):
                            rows += [tracker.decorate(item, store.scope, now=now) for item in store.ledger().items
                                     if tracker.agent_visible(item, for_agent)]
        else:
            if scope in (None, "job"):
                for job in self.store.list(repo):
                    if not job.get("job_dir") or not (Path(job["job_dir"]) / "items.json").is_file():
                        continue
                    with contextlib.suppress(OSError, ValueError):
                        ledger = self._items_store(job).ledger()
                        rows += [tracker.decorate(item, ledger.scope, now=now, scope_stage=job.get("stage"))
                                 for item in ledger.items]
            if scope in (None, "chat"):
                rows += _rows(self._chat_manager().item_rows(repo, now))
            if scope in (None, "plan"):
                rows += _rows(self._plan_manager().item_rows(repo, now))
            if scope in (None, "followups"):
                for name in self._item_repos(repo):
                    store = self._followups_store(name)
                    if store.path.is_file():
                        with contextlib.suppress(OSError, ValueError):
                            rows += [tracker.decorate(item, store.scope, now=now) for item in store.ledger().items]
            if scope in (None, "system") and repo is None:
                with contextlib.suppress(OSError, ValueError):
                    rows += self._system_manager().rows(now)
        counts = tracker.counts(rows, now)
        found = [row for row in rows if (not wanted or row.get("status") in wanted)
                 and (owner is None or row.get("owner") == owner)
                 and (not waiting or row.get("waiting")) and (not overdue or row.get("overdue"))]
        found.sort(key=lambda row: row.get("updated_at") or "", reverse=True)
        found.sort(key=lambda row: (not row.get("waiting"), not (row.get("blocking")
                                                                and row.get("status") not in tracker.CLOSED)))
        return {"items": found[:max(0, int(limit))], "counts": counts, "total": len(found)}

    def scope_items(self, ref: str) -> dict:
        """One scope's items: job {items, summary, unparsed, notices, log}; chat {items, summary, log}; plan
        {items, summary, jobs: [{job_id, summary}], log}; followups and system {items, summary, log}. KeyError
        if the scope is unknown (or on another host), ValueError if ref is malformed."""
        parsed = self._parse_ref(ref, item=False)
        if parsed.kind == "system":
            return self._system_manager().scope_items()
        if parsed.kind == "chat":
            return self._chat_manager().scope_items(parsed.id)
        if parsed.kind == "plan":
            return self._plan_manager().scope_items(parsed.repo, parsed.id)
        now = utc_now()
        if parsed.kind == "followups":
            self.store.repo_dir(parsed.repo)
            store = self._followups_store(parsed.repo)
            ledger = store.ledger()
            return {"items": [tracker.decorate(item, store.scope, now=now) for item in ledger.items],
                    "summary": ledger.summary(), "log": store.log_tail()}
        job = self.job(parsed.repo, parsed.id)
        store = self._items_store(job)
        ledger = store.ledger()
        return {"items": [tracker.decorate(item, store.scope, now=now, scope_stage=job.get("stage"))
                          for item in ledger.items],
                "summary": ledger.summary(), "unparsed": list(ledger.doc.get("unparsed") or []),
                "notices": dict(ledger.doc.get("notices") or {}), "log": store.log_tail()}

    def add_item(self, scope_ref: str, by: str = tracker.HUMAN, **fields: Any) -> dict:
        """A person's new item in a job, chat, the repo's follow-ups or the system scope ({title, detail?,
        blocking?, owner?, verifier?, due?, kind?}); returns it as the API serves it. by agent:<name> (an
        outside agent's token): only in the system scope or a repo's follow-ups, owned by that agent and
        verified by a person. ValueError for a malformed or refused one."""
        parsed = self._parse_ref(scope_ref, item=False)
        if by != tracker.HUMAN and parsed.kind not in ("system", "followups"):
            raise ValueError("an agent token adds items to the system scope or a repo's follow-ups only")
        if parsed.kind == "system":
            with self._busy_as_409():
                return self._system_manager().add_item(fields, by=by)
        if parsed.kind == "chat":
            return self._chat_manager().add_item(parsed.id, fields)
        if parsed.kind == "plan":
            raise ValueError("a plan's items come from its jobs and its lead; add a follow-up or a job item instead")
        if parsed.kind == "followups":
            self.config.repo(parsed.repo)
            due = fields.get("due") or tracker.add_days(tracker.today(), self._housekeeping().followup_due_days)
            store = self._followups_store(parsed.repo)
            roles = {"owner": by, "verifier": tracker.HUMAN} if by != tracker.HUMAN else {
                "owner": fields.get("owner") or "human"}
            with self._busy_as_409():
                with self._scope_items(store, BUSY_WAIT) as ledger:
                    item = tracker.human_add(ledger, {**fields, "due": due, **roles}, kind="followup", by=by)
            return tracker.decorate(item, store.scope, now=utc_now())
        job = self._entry((parsed.repo, parsed.id))
        if job.get("stage") in END_STAGES:   # nobody in the job will work it now: it would only sit there
            raise ValueError(f"job {parsed.repo}/{parsed.id} has ended ({job['stage']}): add a follow-up instead "
                             f"(repo:{parsed.repo})")
        with self._busy_as_409():
            with self._job_items(job, BUSY_WAIT) as ledger:
                item = tracker.human_add(ledger, fields)
        return tracker.decorate(item, self._items_store(job).scope, now=utc_now(), scope_stage=job.get("stage"))

    def item_action(self, ref: str, action: str, note: str | None = None, reason: str | None = None,
                    owner: str | None = None, due: str | None = None, to: str | None = None,
                    evidence: list | None = None, by: str = tracker.HUMAN) -> dict:
        """A person's action on one item (accept dismiss verify reopen drop defer assign block unblock
        make-job update): {item} (+ {job} for make-job). update moves it by an agent-facing verb (to,
        with evidence for done). by agent:<name> (an outside agent's token): update only, of its own items
        in the system scope or a repo's follow-ups. ValueError with the rule when it is refused, KeyError for
        an unknown item, ActionError while the scope is mid-ingest (retry). defer moves a job, chat or plan item
        to the repo's follow-ups (due: today + followup_due_days unless given); make-job submits a job for a
        follow-up, which is proven when that job merges. On a job that has ended, reopen is refused (reopen
        its follow-up instead) and whatever another action leaves open moves on as at the job's end."""
        parsed = self._parse_ref(ref, item=True)
        if action not in tracker.HUMAN_ACTIONS:
            raise ValueError(f"unknown action {action!r} (use {', '.join(tracker.HUMAN_ACTIONS)})")
        if by != tracker.HUMAN and (action != "update" or parsed.kind not in ("system", "followups")):
            raise ValueError("an agent token may only update its own items in the system scope or a repo's "
                             "follow-ups (doing, done with evidence, or defer)")
        if by == tracker.HUMAN:
            action = tracker.person_action(action, to)   # update to defer is defer, to verified is verify, ...
        kwargs = {"note": note or "", "reason": reason or "", "owner": owner, "due": due}
        if action == "update":
            kwargs.update(to=to, evidence=evidence)
        elif action == "verify" and evidence is not None:
            kwargs.update(evidence=evidence)
        if parsed.kind == "system":
            with self._busy_as_409():
                return self._system_manager().item_action(parsed.item, action, by=by, **kwargs)
        if parsed.kind == "chat":
            return self._chat_manager().item_action(parsed.id, parsed.item, action, **kwargs)
        if parsed.kind == "plan":
            return self._plan_manager().item_action(parsed.repo, parsed.id, parsed.item, action, **kwargs)
        if parsed.kind == "followups":
            return self._followup_action(parsed.repo, parsed.item, action, by=by, **kwargs)
        job = self._entry((parsed.repo, parsed.id))
        if action == "make-job":
            raise ValueError("make-job works on a follow-up (repo:<repo>/F-<n>); move this item to later first")
        store = self._items_store(job)
        ended = job.get("stage") in END_STAGES
        if ended and action == "reopen":
            followup = (store.ledger().get(parsed.item).get("links") or {}).get("followup")
            where = f"reopen its follow-up {followup}" if followup else f"add a follow-up (repo:{job['repo']})"
            raise ValueError(f"job {job['repo']}/{job['id']} has ended ({job['stage']}): {where} instead")
        with self._busy_as_409():
            if action == "defer":
                with self._job_items(job, BUSY_WAIT) as ledger:
                    self.defer_to_followups(job["repo"], ledger, parsed.item, reason or note or "", due)
                    item = ledger.get(parsed.item)
            else:
                with self._job_items(job, BUSY_WAIT) as ledger:
                    item = tracker.human_action(ledger, parsed.item, action, **kwargs)
                    self._move_later(job, ledger)
                    item = ledger.get(parsed.item)
        self._learn_dismissal(job["repo"], item, action)
        self._follow_decision(job, item, action)
        if ended:
            self._job_ended(job)   # anything still open in the ended job moves on (idempotent)
            item = self._items_store(job).ledger().get(parsed.item)
        return {"item": tracker.decorate(item, store.scope, now=utc_now(), scope_stage=job.get("stage"))}

    def dismiss_items(self, refs: list[str], reason: str) -> dict:
        """A person dismisses several suggestions with one reason (a bulk "not needed"): {items, refused:
        [{ref, error}]}; each is dismissed on its own, so one refusal leaves the others done."""
        if not isinstance(refs, list) or not refs or len(refs) > tracker.MAX_ITEMS or not all(
                isinstance(ref, str) for ref in refs):
            raise ValueError(f"refs must be a list of 1 to {tracker.MAX_ITEMS} item references")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("a bulk dismiss needs one reason")
        done, refused = [], []
        for ref in dict.fromkeys(refs):
            try:
                done.append(self.item_action(ref, "dismiss", reason=reason)["item"])
            except (KeyError, ValueError, ActionError) as exc:
                refused.append({"ref": ref, "error": str(exc.args[0]) if isinstance(exc, KeyError) and exc.args
                                else str(exc)})
        return {"items": done, "refused": refused}

    def propose_item(self, scope: str, by: str = tracker.HUMAN, **fields: Any) -> dict:
        """POST /api/items/propose: an item in the system scope ("system") or a repo's follow-ups
        ("repo:<repo>"), owned by its proposer (an outside agent: agent:<token name>), verified by a person."""
        if scope != "system" and not (isinstance(scope, str) and scope.startswith("repo:")):
            raise ValueError('scope must be "system" or "repo:<repo>"')
        return self.add_item(scope, by=by, **fields)

    def item_history(self, ref: str) -> dict:
        """One item and every transition it went through, oldest first (the whole log, not a scope's last 100):
        {item, log}. KeyError for an unknown item, ValueError if ref is malformed."""
        parsed = self._parse_ref(ref, item=True)
        if parsed.kind == "system":
            return self._system_manager().item_history(parsed.item)
        if parsed.kind == "chat":
            return self._chat_manager().item_history(parsed.id, parsed.item)
        if parsed.kind == "plan":
            return self._plan_manager().item_history(parsed.repo, parsed.id, parsed.item)
        if parsed.kind == "followups":
            self.store.repo_dir(parsed.repo)
            store, stage = self._followups_store(parsed.repo), None
        else:
            job = self.job(parsed.repo, parsed.id)
            store, stage = self._items_store(job), job.get("stage")
        item = store.ledger().get(parsed.item)
        return {"item": tracker.decorate(item, store.scope, now=utc_now(), scope_stage=stage),
                "log": store.history(parsed.item)}

    # -- personas and wakes (Wake & Messages §2.3): the WakeManager's delegations ---------------------

    def open_persona_item(self, persona: str, title: str, **fields: Any) -> dict:
        """A persona's tracker item in the system scope (HousekeepingManager.open_persona_item)."""
        return self._system_manager().open_persona_item(persona, title, **fields)

    def close_wake_item(self, ref: str, run: str) -> dict:
        """A wake ran: code proves the system item made for it, with the run as its receipt."""
        parsed = self._parse_ref(ref, item=True)
        if parsed.kind != "system":
            raise ValueError("a chat's wake item is closed through the chat turn's ItemsContext")
        return self._system_manager().close_wake_item(parsed.item, run)

    def drop_wake_item(self, ref: str, reason: str = "the persona cancelled its wake") -> dict:
        """A persona cancelled its pending wake: code drops the system item made for it."""
        parsed = self._parse_ref(ref, item=True)
        if parsed.kind != "system":
            raise ValueError("a chat's wake item is closed through the chat turn's ItemsContext")
        return self._system_manager().drop_wake_item(parsed.item, reason)

    # -- GPU custody and drift sources (sections 37, 38) --------------------------------------------------

    def custody(self, refresh: bool = False) -> dict:
        """GET /api/custody: {providers: [{provider, lazy_load, allow_model_swap, needs_lease, custody_from (the
        entry on the same server whose custody this one follows, else None), held_wait_seconds (how long a run
        waits out a HELD answer: the provider's overflow_after_seconds, else None: no wait), settings (no token),
        register: {url, generation, epoch, fenced, residency, leases, history}}], reason_codes: {code: plain
        words}}; refresh reads residency now. Read-only: the harness never mutates the register's state; it only
        asks, and the register (the operator's) decides."""
        rows = []
        read: set[int] = set()   # one residency GET per register, however many providers name it
        for name in sorted(self.config.raw.get("providers") or {}):
            try:
                provider = self.config.provider(str(name))
            except (KeyError, ValueError):
                continue
            settings = custody_settings_of(provider)
            if settings is None:
                continue
            client = custody_client_for(settings)
            if refresh and id(client) not in read:
                read.add(id(client))
                client.reread()
            entry = (self.config.raw.get("providers") or {}).get(name)
            owner = None
            if isinstance(entry, dict) and entry.get("custody") is None:   # it follows another entry on its server
                found = custody_on_server(self.config.raw.get("providers"), provider.base_url, skip=str(name))
                owner = found[0] if found else None
            rows.append({"provider": provider.name, "lazy_load": provider.lazy_load,
                         "allow_model_swap": provider.allow_model_swap, "needs_lease": custody_needs_lease(provider),
                         "custody_from": owner, "held_wait_seconds": provider.overflow_after_seconds or None,
                         "settings": settings.to_dict(), "register": client.view()})
        return {"providers": rows, "reason_codes": dict(CUSTODY_WORDS)}

    def drift_sources(self) -> list[dict]:
        """GET /api/drift-sources: each source's settings (never its token), status and findings."""
        return self._drift_manager().views()

    def check_drift_source(self, name: str) -> dict:
        """POST /api/drift-sources/<name>/check: check it now (KeyError for an unknown source)."""
        return self._drift_manager().check(name)

    def drift_manifest(self, name: str) -> dict | None:
        """GET /api/drift-sources/<name>/manifest: its generated role manifest, or None (KeyError if unknown)."""
        return self._drift_manager().manifest(name)

    # -- disk protection with SnapRAID (section 41: the daemon route, and the read-only reader) -------------

    def snapraid_view(self) -> dict:
        """GET /api/snapraid: the disk protection card (snapraid.SnapraidPanel.view)."""
        return self._snapraid_manager().view()

    def snapraid_refresh(self, what: str = "status", wait: bool = False) -> dict:
        """POST /api/snapraid/refresh: a fresh read (status, health, or diff: a guard report). ValueError when the
        panel is not reading (absent, no reader, off)."""
        return self._snapraid_manager().refresh(what, wait=wait)

    def snapraid_jobs(self) -> dict:
        return self._snapraid_manager().jobs()

    def snapraid_job(self, job: str, log: bool = False, offset: int = 0) -> dict:
        return self._snapraid_manager().job(job, log=log, offset=offset)

    def snapraid_guard(self, guard: str, **page: Any) -> dict:
        return self._snapraid_manager().guard_page(guard, **page)

    def snapraid_files(self, **page: Any) -> dict:
        return self._snapraid_manager().files_page(**page)

    def snapraid_setup(self) -> dict:
        return self._snapraid_manager().setup()

    def snapraid_setup_action(self, action: str) -> dict:
        """stage (the files the copy blocks install) or check (the reader's info)."""
        panel = self._snapraid_manager()
        if action == "stage":
            return panel.stage()
        if action == "check":
            return panel.check()
        if action == "link-stage":
            return panel.stage_link()
        if action == "link-check":
            return panel.check_link()
        raise ValueError("action must be stage, check, link-stage or link-check")

    # the SnapRAID Daemon route (BACKUPS_SNAPRAID.md §3 to §7): approvals, the review page, runs, reports

    def snapraid_action_preview(self, action: str, **fields: Any) -> dict:
        """POST /api/snapraid/actions: a preview A-<n> (snapraidd.DaemonRoute.preview); ValueError when refused."""
        return self._snapraid_manager().daemon.preview(action, **fields)

    def snapraid_action_approve(self, approval: str, shown_sha256: str, typed: Any = None) -> dict:
        """POST /api/snapraid/actions/<A-n>/approve: one send through the link (snapraidd.Changed: 409)."""
        return self._snapraid_manager().daemon.approve(approval, shown_sha256, typed)

    def snapraid_action_cancel(self, approval: str) -> dict:
        return self._snapraid_manager().daemon.cancel(approval)

    def snapraid_actions(self) -> dict:
        daemon = self._snapraid_manager().daemon
        return {"actions": [daemon.public(record) for record in daemon.approvals()[-50:]]}

    def snapraid_action(self, approval: str) -> dict:
        daemon = self._snapraid_manager().daemon
        return daemon.public(daemon.approval(approval))

    def snapraid_review(self, **page: Any) -> dict:
        return self._snapraid_manager().daemon.review(**page)

    def snapraid_fixes(self, offset: int = 0) -> dict:
        return self._snapraid_manager().daemon.fixes(offset)

    def snapraid_runs(self) -> dict:
        return self._snapraid_manager().daemon.runs()

    def snapraid_run(self, number: int) -> dict:
        return self._snapraid_manager().daemon.run(number)

    # the owner's checklist for a whole failed disk (BACKUPS_SNAPRAID.md §9.4): read-only checks; he runs the blocks

    def snapraid_rebuild(self) -> dict:
        return self._snapraid_manager().rebuild.page()

    def snapraid_rebuild_start(self, disk: str) -> dict:
        return self._snapraid_manager().rebuild.start(disk)

    def snapraid_rebuild_step(self, step: str, confirm: list | None = None) -> dict:
        return self._snapraid_manager().rebuild.step(step, confirm)

    def snapraid_rebuild_prepare(self, by_id: str, typed: str) -> dict:
        return self._snapraid_manager().rebuild.prepare(by_id, typed)

    def snapraid_rebuild_close(self) -> dict:
        return self._snapraid_manager().rebuild.close()

    def snapraid_report(self, subject: Any, detail: Any) -> None:
        """A parity.report event from the daemon's forwarder (notify.PANEL_HOOK_KINDS): read the daemon now, in the
        background (the hook answers at once)."""
        panel = self._snapraid_manager()
        threading.Thread(target=panel.daemon.report_event, args=(subject, detail), name="snapraid-report",
                         daemon=True).start()

    # -- Your server (section 42; docs/seraphina/YOUR_SERVER.md, phase P1a: it only looks) --------------------------

    def yourserver_view(self) -> dict:
        """GET /api/yourserver: the host strip, the helper's state, the apps' summary, Beszel, Needs you."""
        return self._yourserver_manager().view()

    def yourserver_apps(self) -> dict:
        return self._yourserver_manager().apps_view()

    def yourserver_app(self, app: str) -> dict:
        return self._yourserver_manager().app_view(app)

    def yourserver_app_update(self, app: str, body: dict) -> dict:
        return self._yourserver_manager().app_update(app, body)

    def yourserver_address(self, app: str, body: dict) -> dict:
        return self._yourserver_manager().address(app, body)

    def yourserver_qr(self, app: str) -> dict:
        return self._yourserver_manager().qr(app)

    def yourserver_ask(self, app: str, question: Any) -> dict:
        return self._yourserver_manager().ask(app, question)

    def yourserver_review(self) -> dict:
        return self._yourserver_manager().review_view()

    def yourserver_review_confirm(self, rows: Any, tailnet: Any = None) -> dict:
        return self._yourserver_manager().review_confirm(rows, tailnet)

    def yourserver_groups(self, body: dict) -> dict:
        return self._yourserver_manager().groups(body)

    def yourserver_beszel(self, body: dict) -> dict:
        """POST /api/yourserver/beszel: the read-only Beszel login's password (kept in the env file) or its check."""
        return self._yourserver_manager().beszel_login(body)

    def yourserver_refresh(self) -> dict:
        """POST /api/yourserver/refresh: read the server again now (every read only looks)."""
        area = self._yourserver_manager()
        area.numbers(force=True)
        area.host(force=True)
        if area.settings().get("enabled") and area.helper_present():
            area.refresh_map()
        return area.view()

    def yourserver_setup(self) -> dict:
        return self._yourserver_manager().setup()

    def yourserver_setup_action(self, action: str, body: dict | None = None) -> dict:
        """stage (the files the one paste installs, with the classes ticked) or check (the helper's info)."""
        area = self._yourserver_manager()
        body = body or {}
        if action == "stage":
            if set(body) - {"classes"}:
                raise ValueError("stage takes classes")
            return area.stage(classes=body.get("classes"))
        if action == "check":
            return area.check()
        raise ValueError("action must be stage or check")

    def yourserver_checks(self) -> dict:
        """GET /api/yourserver/checks: the Check buttons' last results (memory only)."""
        return self._yourserver_manager().checks_view()

    def yourserver_check(self, check: str, body: dict) -> dict:
        """POST /api/yourserver/checks/<check>: one Check now; it only looks (refresh: the list of updates)."""
        return self._yourserver_manager().run_check(check, body)

    def yourserver_logs(self, app: str, member: str | None, lines: str | None, before: str | None) -> dict:
        """GET /api/yourserver/apps/<id>/logs: one redacted page, kept nowhere, or why it waits."""
        return self._yourserver_manager().logs(app, member, lines, before)

    def yourserver_keep(self) -> dict:
        """POST /api/yourserver/keep: the keep pass now (it only adds names)."""
        return self._yourserver_manager().keep_now()

    def yourserver_jobs(self) -> dict:
        return self._yourserver_manager().jobs_view()

    def yourserver_job(self, job: str) -> dict:
        return self._yourserver_manager().job_view(job)

    def yourserver_receipts(self, cursor: str | None = None) -> dict:
        return self._yourserver_manager().receipts_view(cursor)

    def yourserver_receipt(self, receipt: str) -> dict:
        return self._yourserver_manager().receipt_view(receipt)

    def yourserver_root(self) -> dict:
        """GET /api/yourserver/root: who can act as root (view only), or why the page waits."""
        return self._yourserver_manager().root_view()

    def yourserver_root_check(self) -> dict:
        return self._yourserver_manager().root_check()

    def yourserver_root_mark(self, row: str, body: dict) -> dict:
        return self._yourserver_manager().root_mark(row, body)

    def _yourserver_manager(self) -> Any:
        from .yourserver import YourServer

        system = self._system_manager()
        notifier = self._notify_manager()
        with self._lock:
            if self._yourserver is None:
                self._yourserver = YourServer(self.config, system, notifier, self.bus, protection=self._protection,
                                              operator_receipts=self._yourserver_operator_receipts)
                self._yourserver.update_config = self.update_config
                self._yourserver.set_secret = self.set_secret
            return self._yourserver

    def _yourserver_operator_receipts(self) -> list[dict]:
        """The operator's reported receipts for Your server's receipts page (none when no operator is set up)."""
        try:
            return self.operator_receipts(limit=50).get("receipts") or []
        except Exception:  # noqa: BLE001 - no operator, or her folder unreadable: the helper's receipts alone
            return []

    def _protection(self) -> dict | None:
        """The disk protection panel's protected folders and its line, for Your server's backup lines (§2.9)."""
        panel = self._snapraid_manager()
        if not panel.settings().get("enabled"):
            return None
        view = panel.view()
        dirs = [row.get("mount") for row in view.get("disks") or [] if row.get("role") == "data" and row.get("mount")]
        reason = str((view.get("light") or {}).get("reason") or "")
        return {"dirs": dirs, "words": reason[:1].lower() + reason[1:]} if dirs else None

    # -- the factory's own system items (§33 drift, §15 a deleted chat without a repository) ------------

    def add_system_items(self, entries: list[dict]) -> list[str]:
        """Add items to the system scope, by the factory (a deleted chat without a repository carries its open
        items here); their refs."""
        return self._system_manager().add_carried(entries)

    def system_item(self, fields: dict, dedupe: str | None = None) -> dict:
        """Open one item in the system scope, by the factory (an Open WebUI profile that changed, §33): {title,
        detail?, kind?, source?, due?}. With dedupe, an item still open whose source.hash is dedupe is returned
        instead of a new one. Returns {ref, item, added}."""
        return self._system_manager().open_once(fields, dedupe)

    # -- operator personas: receipts, activity, open items (section 36) -------------------------------

    def _receipt_reader(self) -> Any:
        from .receipts import Receipts

        with self._lock:
            if self._receipts is None:
                self._receipts = Receipts(lambda: self.config, self.config.data_dir, secrets=self._item_secrets,
                                          system=self._system_manager, bus=self.bus)
            return self._receipts

    def operator_state(self) -> dict:
        """{operators, marked, several, problem, receipts_dir, scan}: who the owner's config marks as the operator and
        what the last scan of the receipts directory found (no scan is run here)."""
        from . import operator as op

        raw = self.config.raw
        reader = self._receipt_reader()
        summary = reader.summary()
        return {"operators": op.operators(self.config), "marked": op.marked(raw),
                "several": list(op.section(raw).get("several") or []), "problem": op.rule_problem(raw),
                "receipts_dir": reader.settings()["receipts_dir"], "label": op.REPORTED,
                "scan": {key: summary[key] for key in ("scanned_at", "read", "cannot_read", "refused", "past_limit",
                                                       "unreadable", "writable", "gone", "problem")}}

    def operator_scan(self, force: bool = True) -> dict:
        """Read the operator's receipts directory now (only what changed is read again) and bring the tracker items
        of the checks she reports as not done up to date; the scan's summary. force False: at most every
        operator.scan_seconds."""
        return self._receipt_reader().scan(force=force)

    def operator_receipts(self, limit: int = 200, since: str | None = None, until: str | None = None,
                          rescan: bool = False) -> dict:
        """{receipts, refused, unreadable, scan}: the receipts the harness read, newest first (a scan first, at most
        every operator.scan_seconds unless rescan), each labelled "reported by the operator" and carrying its actor
        as written (null: not stated), whoever that is: every actor's receipt is listed. unreadable: what the
        harness's user may not read there, by name, left as it is. A scan that fails (the tracker was busy) still
        serves what the index holds, with scan.problem saying why."""
        from .activity import parse_when
        from .receipts import unreadable

        reader = self._receipt_reader()
        try:
            scan = reader.scan(force=rescan)
        except Exception as exc:  # noqa: BLE001 - what was read before is still served
            scan = {**reader.summary(), "skipped": False, "problem": f"the scan failed ({type(exc).__name__}): "
                                                                      "what was read before is shown"}
        index = reader.index()
        return {"receipts": reader.rows(limit=limit, since=parse_when(since), until=parse_when(until)),
                "refused": index.get("refused") or [], "unreadable": unreadable(index), "scan": scan}

    def operator_receipt(self, receipt_id: str) -> dict:
        """{receipt, duplicates}: one receipt by its id, with before and after (KeyError when unknown)."""
        return self._receipt_reader().get(receipt_id)

    def activity(self, agent: str, since: Any = None, until: Any = None, limit: int = 500,
                 after: Any = None) -> dict:
        """What the agent or persona did in [since, until], oldest first, from durable files (activity.activity);
        the operator's receipts are scanned first (at most every operator.scan_seconds). A scan that fails is
        listed in errors and never hides the rest."""
        from .activity import activity

        reader = self._receipt_reader()
        problem = self._operator_prescan(reader)
        result = activity(self.config, agent=agent, since=since, until=until, limit=limit, after=after,
                          receipts=reader, secrets=self._item_secrets)
        if problem:
            result["errors"] = [*result.get("errors", []), f"receipt scan: {problem}"]
        return result

    def _operator_prescan(self, reader: Any) -> str | None:
        """An automatic scan before an activity query or an operator's turn; why it failed, or None. Never raises:
        what the index already holds is still used."""
        if reader.directory() is None:
            return None
        try:
            reader.scan(force=False)
        except Exception as exc:  # noqa: BLE001 - a failed scan never stops a query or a turn
            return f"{type(exc).__name__}: the scan failed; what was read before is used"
        return None

    def operator_items_view(self, name: str, titles: bool = True) -> tracker.ItemsView | None:
        """The read-only view of an operator persona's open system-scope items for her turn (None when she is not
        the operator, or the tracker cannot be read): her receipts are scanned first, so what she reported as not
        verified is on it (DESIGN §36)."""
        from .operator import is_operator, owner

        if not is_operator(self.config, name):
            return None
        self._operator_prescan(self._receipt_reader())
        try:
            return tracker.ItemsView(self._system_manager().store(), owner(name), settings=self._housekeeping(),
                                     titles=titles)
        except (OSError, ValueError):
            return None

    # -- outside agents' tokens ------------------------------------------------------------------

    def agent_tokens(self) -> list[dict]:
        """[{name, created, last_used}] of the outside agents' tokens."""
        return self._agent_tokens().list()

    def create_agent_token(self, name: str) -> dict:
        """{name, token}: a new outside agent's token, shown once (only its hash is kept)."""
        return self._agent_tokens().create(name)

    def revoke_agent_token(self, name: str) -> None:
        self._agent_tokens().revoke(name)

    def agent_token_name(self, token: str) -> str | None:
        """The name of an outside agent's token, or None."""
        return self._agent_tokens().match(token)

    def add_followups(self, repo: str, entries: list[dict]) -> list[str]:
        """Add items to the repo's follow-ups, by the factory (carry-forward, a deferred item); their refs."""
        store = self._followups_store(repo)
        refs = []
        with self._scope_items(store) as ledger:
            for fields in entries:
                refs.append(store.scope.item_ref(tracker.add_carried(ledger, dict(fields))["id"]))
        return refs

    def defer_to_followups(self, repo: str, ledger: tracker.Ledger, item_id: str, reason: str,
                           due: str | None = None, by: str = tracker.HUMAN) -> str:
        """Move one item of ledger (a job's, chat's or plan's, inside its transaction) to the repo's follow-ups:
        the copy is added there and the item becomes Later with links.followup; returns the copy's ref."""
        item = ledger.get(item_id)
        problem = tracker.refusal(item, "deferred", by, ledger.scope, reason=reason)
        if problem:
            raise tracker.ItemError(problem)
        days = self._housekeeping().followup_due_days
        fields = tracker.carry_copy(item, ledger.scope, due=tracker.check_due(due) or tracker.add_days(
            tracker.today(), days), reason=reason)
        ref = self.add_followups(repo, [fields])[0]
        ledger.change(by, item_id, "deferred", reason=reason, links={"followup": ref})
        return ref

    # -- driving a job -------------------------------------------------------------

    def _drive(self, key: JobKey, step: str | None) -> None:
        job = self._entry(key)
        with self._job_lock(key):
            if job.get("stage") not in ACTIVE_STAGES:
                return   # cancelled while it waited in the queue
            with self._lock:
                self._driving.add(key)
        try:
            while step is not None:
                self._check_cancel(job)
                step = self._steps[step](job)
                full = self._take_items_full(key)
                if full is not None:   # after the step's own checks (branch moved, policy, discarded edits)
                    self._stop_items_full(job, full)
                    step = None
        except _Cancelled:
            self._take_items_full(key)
            self._fail(job, STOPPED)
        except (StepError, GitError) as exc:
            self._fail(job, _with_full(str(exc), self._take_items_full(key)))
        except Exception as exc:  # a bug in one step must not kill the worker or strand the job
            self._fail(job, _with_full(f"internal error: {type(exc).__name__}: {exc}", self._take_items_full(key)))
        finally:
            try:
                self._swap_back(job)
                self._take_items_full(key)
                if job.get("stage") in END_STAGES:
                    self._job_ended(job)
            finally:
                with self._lock:
                    self._driving.discard(key)
                    self._changed.notify_all()

    def _setup(self, job: dict) -> str:
        """Resolve base, create the worktree on factory/<id> (from base or from_branch), run repo setup."""
        self._set(job, stage="spec", step="setup", detail="creating the worktree")
        repo = self._repo(job)
        worktree = Path(job["worktree"])
        if not job.get("base") or not worktree.exists():
            base = gitops.detect_base(repo.path, repo.base)
            if not worktree.exists() and gitops.branch_exists(repo.path, job["branch"]):
                gitops.checkout_worktree(repo.path, worktree, job["branch"])   # a retry: the branch survived
            elif not worktree.exists():
                gitops.add_worktree(repo.path, worktree, job["branch"], job.get("from_branch") or base)
            self._set(job, base=base, base_commit=gitops.rev_parse(repo.path, base))
        if repo.setup:
            self._run_setup(job, repo)
        return "spec"

    def _spec(self, job: dict) -> str | None:
        """spec-writer writes spec.md (one fresh retry if it did not)."""
        self._set(job, stage="spec", step="spec", detail="spec-writer")
        repo = self._repo(job)
        self._ensure_worktree(job)
        spec = Path(job["job_dir"]) / "spec.md"
        lines = [f"Feature: {job['feature']}"] + ([SPEC_FROM_BRANCH] if job.get("from_branch") else [])
        head = gitops.rev_parse(repo.path, job["branch"])
        self._run_agent(job, "spec-writer", self._prompt(job, "spec-writer", *lines))
        if not _non_empty(spec):
            self._run_agent(job, "spec-writer", self._prompt(job, "spec-writer", *lines, SPEC_RETRY))
            if not _non_empty(spec):
                raise StepError("spec-writer wrote no spec.md")
        self._ingest_spec(job)   # only a spec the step accepted (never a failed run's draft)
        if gitops.rev_parse(repo.path, job["branch"]) != head:
            return self._needs_human(job, "the branch moved while the spec-writer ran (it may write only spec.md)")
        problem = self._discard_edits(job, "spec-writer edits")
        if problem:
            return self._needs_human(job, problem)
        first = None if job.get("from_branch") else {"prompt": "Build round 1.", "resume": False}
        self._set(job, round=1, build_request=first)
        return "build"

    def _build(self, job: dict) -> str:
        """Run (or resume) the builder with the round's build request; a handoff writes build.md itself."""
        request = job.get("build_request")
        who = "built in chat" if request is None else "builder"
        self._set(job, stage="build", step="build", detail=f"round {job['round']}: {who}")
        if request is None:
            self.store.write_file(job, "build.md", _handoff_build_md(job))
            return "build_check"
        self._ensure_worktree(job)
        self._run_agent(job, "builder", self._prompt(job, "builder", request["prompt"]),
                        round=job["round"], resume=request["resume"])
        return "build_check"

    def _build_check(self, job: dict) -> str | None:
        """build.md READY with commits and a clean tree, else one corrective resume per problem."""
        n = job["round"]
        self._set(job, step="build_check", detail=f"round {n}: checking the build")
        repo = self._repo(job)
        build_md = Path(job["job_dir"]) / "build.md"
        asked: set[str] = set()
        while True:
            verdict = read_verdict(build_md, "build")
            if verdict == "BLOCKED":
                blocked = _first_line_under(build_md, "Blocked")
                return self._needs_human(job, "builder blocked" + (f": {blocked}" if blocked else ""))
            fix: str | None
            kind = None
            if verdict is None:
                problem, fix = "builder wrote no valid build.md", BUILD_MD_FIX
            elif not gitops.is_clean(Path(job["worktree"]), repo.path):
                problem, fix = "builder left uncommitted changes", COMMIT_FIX
            elif gitops.commits_ahead(repo.path, job["base"], job["branch"]) == 0:
                problem, fix = "builder made no commits", None
            elif (failed_verify := self._verify_problem(job, repo)) is not None:
                problem, fix, kind = failed_verify[0], failed_verify[1], "verify"
            else:
                untouched = self._untouched_blocking(job, n)
                if not untouched:
                    self._count_stale(job, n)
                    return "review"
                ids = ", ".join(untouched)
                problem, fix, kind = f"builder left blocking items open: {ids}", ITEMS_FIX.format(ids=ids), "items"
            if fix is None or (kind or fix) in asked:
                return self._needs_human(job, problem)
            asked.add(kind or fix)
            self._run_agent(job, "builder", self._prompt(job, "builder", fix), round=n, resume=True)
            if self._has_items_full(job):
                return None   # _drive stops the job for a human: the job's items are at the cap

    def _review_round(self, job: dict) -> str | None:
        """Every reviewer in parallel; WAIT FOR ALL, discard their edits, then decide the round."""
        n = job["round"]
        repo = self._repo(job)
        round_dir = Path(job["job_dir"]) / f"round-{n}"
        round_dir.mkdir(exist_ok=True)
        reviews = {}
        for reviewer in job["reviewers"]:
            # A retried round keeps the verdicts it already has: the code has not changed since.
            kept = job["reviews"].get(reviewer)
            on_file = read_verdict(round_dir / f"review-{reviewer}.md", "review")
            reviews[reviewer] = kept if kept in REVIEWED and kept == on_file else "pending"
        self._set(job, stage="review", step="review", reviews=reviews, detail=_review_detail(n, reviews))
        self._ensure_worktree(job)
        head = gitops.rev_parse(repo.path, job["branch"])
        errors: dict[str, str] = {}
        own: dict[str, _FileState | None] = {}   # each review file as its own reviewer left it
        ran = [r for r in job["reviewers"] if reviews[r] == "pending"]
        self._render_items_md(job, None)
        before = _scan(Path(job["job_dir"]), keep=True)
        finals: dict[str, str] = {}
        journals: dict[str, _FileState | None] = {}   # each reviewer's items journal as it left it
        threads = [threading.Thread(target=self._review, args=(job, r, n, errors, own, finals, journals),
                                    name=f"review-{r}", daemon=True) for r in ran]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        forged_journals: set[str] = set()
        policy = self._review_policy(job, n, before, ran, own, journals, forged_journals)
        self._ingest_round(job, n, finals, forged_journals)
        self._check_cancel(job)
        history = [h for h in job["history"] if h.get("round") != n] + [{"round": n, "reviews": dict(job["reviews"])}]
        self._set(job, history=history)
        if policy:
            self._discard_edits(job, f"reviewer edits round {n}")
            return self._needs_human(job, policy)
        if gitops.rev_parse(repo.path, job["branch"]) != head:
            return self._needs_human(job, f"the branch moved during review round {n} (a reviewer committed?)")
        problem = self._discard_edits(job, f"reviewer edits round {n}")
        if problem:
            return self._needs_human(job, problem)
        if errors:
            raise StepError("; ".join(errors[r] for r in job["reviewers"] if r in errors))
        failed = [r for r in job["reviewers"] if job["reviews"][r] == "ERROR"]
        if failed:
            return self._needs_human(job, f"reviewer {', '.join(failed)} failed")
        if self._has_items_full(job):
            return None   # _drive stops the job for a human: the job's items are at the cap
        changes = [r for r in job["reviewers"] if job["reviews"][r] == "CHANGES"]
        if not changes:
            return "approve"
        if job["resumes"] < job["max_resumes"]:
            prompt = (f"Round {n} reviews are in {job['job_dir']}/round-{n}/. Address every review whose "
                      f"first line is VERDICT: CHANGES, commit, and update build.md for round {n + 1}.")
            self._set(job, resumes=job["resumes"] + 1, round=n + 1, reviews=_pending(job), stage="build",
                      build_request={"prompt": prompt, "resume": True}, sendbacks=int(job.get("sendbacks") or 0) + 1)
            return "build"
        waiting = self._blocking_waiting(job)
        open_items = f" Open blocking items: {', '.join(waiting)}." if waiting else ""
        self.store.write_file(job, "decision.md", f"ESCALATE\n\n## Why\nReview rounds exhausted. Outstanding "
                                                  f"CHANGES in round-{n}/ from: {', '.join(changes)}.{open_items}\n")
        return self._needs_human(job, f"review rounds exhausted; CHANGES from {', '.join(changes)}",
                                 decision="ESCALATE")

    def _review(self, job: dict, reviewer: str, n: int, errors: dict[str, str],
                own: dict[str, _FileState | None], finals: dict[str, str] | None = None,
                journals: dict[str, _FileState | None] | None = None) -> None:
        """One reviewer thread: run, re-run once fresh if no valid verdict, save the verdict as it lands.

        The review file is removed before each run, and own[reviewer] (journals[reviewer]: its items
        journal) records it as the run left it (the round's write-policy check compares against that);
        finals[reviewer] keeps the run's final text for the sweep. A review that finished before a
        cancel still counts; an interrupted one goes back to pending.
        """
        agent = f"reviewer-{reviewer}"
        path = Path(job["job_dir"]) / f"round-{n}" / f"review-{reviewer}.md"
        verdict = None
        try:
            for extra in ((), (REVIEW_RETRY,)):
                _remove_path(path)
                self._set_review(job, reviewer, "running", n)
                try:
                    result = self._run_agent(job, agent, self._prompt(job, agent, f"Review round {n}.", *extra),
                                             round=n, guard=False)
                    if finals is not None:
                        finals[reviewer] = result.final_text
                finally:
                    own[reviewer] = _state_of(path)
                    if journals is not None:
                        journals[reviewer] = _state_of(Path(job["job_dir"]) / _journal_rel(agent))
                verdict = read_verdict(path, "review")
                if verdict is not None:
                    break
        except _Cancelled as exc:
            verdict = read_verdict(path, "review") if exc.run is not None and exc.run.ok else None
        except StepError as exc:
            errors[reviewer] = str(exc)
        except Exception as exc:  # a bug here must still let the round finish
            errors[reviewer] = f"{agent}: internal error: {type(exc).__name__}: {exc}"
        self._set_review(job, reviewer, verdict or "ERROR", n)

    def _approve(self, job: dict) -> str | None:
        """approver writes decision.md; anything but APPROVE stops for a human (fail closed)."""
        n = job["round"]
        self._set(job, stage="approve", step="approve", detail=f"round {n}: approver")
        repo = self._repo(job)
        decision = Path(job["job_dir"]) / "decision.md"
        decision.unlink(missing_ok=True)
        self._restore_confirmed(job)
        self._ensure_worktree(job)
        head = gitops.rev_parse(repo.path, job["branch"])
        self._run_agent(job, "approver", self._prompt(job, "approver", f"Final round: {n}."), round=n)
        if gitops.rev_parse(repo.path, job["branch"]) != head:
            return self._needs_human(job, "the branch moved while the approver ran")
        problem = self._discard_edits(job, "approver edits")
        if problem:
            return self._needs_human(job, problem)
        verdict = read_verdict(decision, "decision")
        if verdict is None:
            return self._needs_human(job, "approver wrote no valid decision")
        if verdict == "ESCALATE":
            why = _first_line_under(decision, "Why")
            return self._needs_human(job, "approver escalated" + (f": {why}" if why else ""), decision="ESCALATE")
        self._set(job, decision="APPROVE", approved_by="approver")
        return "gate"

    def _gate(self, job: dict, override: bool = False) -> str | None:
        """The deterministic merge gate, then the merge, under the repo's merge lock.

        A human override (approve of a merge job, or of a branch or folder job already
        delivered) skips the review, path, mode and size rules but still requires a
        conflict-free merge. A branch or folder delivery only needs every final review
        PASS (nothing is merged), then goes to the deliver step.
        """
        mode = _delivery(job)
        delivers = not override and mode != "merge"
        detail = ("approved by a human: checking for conflicts" if override
                  else f"checking the gate for a {mode} delivery" if delivers else "checking the merge gate")
        self._set(job, step="override" if override else "gate", detail=detail)
        if delivers:
            problem = _review_problem(job) or self._items_problem(job)
            return self._needs_human(job, problem) if problem else "deliver"
        with self._repo_lock(job["repo"]):
            try:
                problem = self._gate_problem(job, self._repo(job), override)
            except GitError as exc:
                raise StepError(f"merge gate: {exc}") from None
            if problem:
                return self._needs_human(job, problem)
            return self._merge(job)

    def _gate_problem(self, job: dict, repo: RepoConfig, override: bool) -> str | None:
        base, branch, tmp_dir = job["base"], job["branch"], self._tmp_dir()
        if not override:
            problem = _review_problem(job) or self._items_problem(job)
            if problem:
                return problem
        conflicted = gitops.conflicts(repo.path, base, branch, tmp_dir=tmp_dir)
        if conflicted:
            return f"merge conflicts with {base}: {', '.join(conflicted)}"
        if override:
            return None
        globs = [*self.config.escalate_paths(repo), *ALWAYS_PROTECTED]
        for path in gitops.changed_files(repo.path, base, branch, tmp_dir=tmp_dir):
            if gitops.matches_any(path, globs):
                return f"touches protected path {path}"
        special = _special_paths(repo.path, base, branch)
        if special:
            path, kind = special[0]
            return f"touches {kind} {path}"
        pipeline = self.config.pipeline()
        lines = gitops.changed_lines(repo.path, base, branch, tmp_dir=tmp_dir)
        if lines > pipeline.max_auto_merge_lines:
            return f"changes {lines} lines (max_auto_merge_lines is {pipeline.max_auto_merge_lines})"
        if not pipeline.auto_merge:
            return "auto-merge disabled"
        return None

    def _merge(self, job: dict) -> None:
        """Merge the branch into base (no cancel from here on), then remove the worktree and branch."""
        key = (job["repo"], job["id"])
        repo = self._repo(job)
        with self._repo_lock(job["repo"]):
            self._set(job, stage="merging", step="merge", detail=f"merging into {job['base']}")
            with self._lock:
                self._merging.add(key)
            try:
                with self._job_lock(key):
                    self._check_cancel(job)   # a cancel that landed before the flag
                sha = job.get("merged_commit")
                if not (sha and gitops.is_ancestor(repo.path, sha, f"refs/heads/{job['base']}")):
                    try:   # (a merge the job recorded before a restart is done: never merged twice, DESIGN §40)
                        sha = gitops.merge(repo.path, job["base"], job["branch"], _merge_message(job),
                                           self._tmp_dir())
                    except MergeBusy:
                        return self._needs_human(job, MERGE_BUSY, decision="APPROVE")
                    except GitError as exc:
                        if not exc.timed_out:
                            return self._needs_human(job, f"merge failed: {exc}")
                        sha = self._timed_out_merge(job, repo, exc)
                        if sha is None:
                            return None
                    self._update(job, merged_commit=sha)
                self._remove_checkout(job, repo)
                self._push(job, repo)
                self._update(job, stage="merged", reason=None, error=None,
                             detail=f"merged into {job['base']} at {sha[:12]}")
            finally:
                with self._lock:
                    self._merging.discard(key)

    def _timed_out_merge(self, job: dict, repo: RepoConfig, exc: GitError) -> str | None:
        """git merge was killed at its timeout, so what it did is unknown (it may have moved base, then been killed
        while it printed): looked at again, read only, never assumed (DESIGN §40). base's head when base already
        contains the branch (the merge landed: the delivery carries on); else None, with the job stopped for a
        person and what the look found, never "merge failed"."""
        base = job["base"]
        try:
            if self._base_has_branch(repo, job):
                sha = gitops.rev_parse(repo.path, base)
                self._log(job, "warn", f"git merge timed out ({exc}), but {base} already contains {job['branch']} "
                                       f"(looked again): the merge landed")
                return sha
            found = f"nothing was merged into {base} (looked again); approve again to merge it"
        except GitError as look:
            found = f"what it did could not be looked at ({look}); check {base} before you approve again"
        self._needs_human(job, f"merge timed out: {found}")
        return None

    @staticmethod
    def _base_has_branch(repo: RepoConfig, job: dict) -> bool:
        """Whether base already contains the job's branch (read only); GitError when git cannot say."""
        return gitops.branch_exists(repo.path, job["branch"]) and gitops.is_ancestor(
            repo.path, f"refs/heads/{job['branch']}", f"refs/heads/{job['base']}")

    def _deliver(self, job: dict) -> None:
        """A branch or folder delivery: the branch is kept and its worktree removed; a folder delivery
        first exports changes.patch, files/ and REPORT.md to <output_dir>/<repo>/<job-id>/. The job
        stops at stage delivered, where a human approve merges it."""
        mode = _delivery(job)
        repo = self._repo(job)
        self._set(job, stage="approve", step="deliver", detail=f"delivering as a {mode}")
        tip = gitops.rev_parse(repo.path, job["branch"])
        record: dict[str, Any] = {"mode": mode, "branch": job["branch"], "base": job["base"], "commit": tip,
                                  "by": "human" if job.get("approved_by") == "human" else "approver"}
        if mode == "folder":
            try:
                record.update(export_folder(job, repo.path, self.config.output_dir(), tip))
            except (GitError, OSError, ValueError) as exc:
                raise StepError(f"folder delivery failed: {exc}") from None
        self._check_cancel(job)
        self._codex_share_worktree(job)
        try:
            gitops.remove_worktree(repo.path, Path(job["worktree"]))
        except GitError as exc:
            self._log(job, "warn", f"could not remove the worktree: {exc}")
        record["at"] = utc_now()
        where = f"to {record['path']}" if mode == "folder" else f"on branch {job['branch']}"
        self._set(job, stage=DELIVERED, delivered=record, reason=None, error=None,
                  detail=f"delivered {where} at {tip[:12]}; approve to merge it into {job['base']}")

    def _ensure_worktree(self, job: dict) -> None:
        """Check the job's branch out again when its worktree is gone (removed at delivery, then sent
        back to work by a human); StepError if git cannot."""
        worktree = Path(job["worktree"])
        if worktree.exists():
            return
        repo = self._repo(job)
        try:
            gitops.checkout_worktree(repo.path, worktree, job["branch"])
        except GitError as exc:
            raise StepError(f"cannot re-create the worktree from {job['branch']}: {exc}") from None
        self._log(job, "info", f"re-created the worktree {worktree} from {job['branch']}")

    def _needs_human(self, job: dict, reason: str, **changes: Any) -> None:
        """Stop the line for a human, with a one-line reason for the card."""
        self._set(job, stage="needs-human", reason=reason, detail=reason, **changes)

    # -- step helpers ----------------------------------------------------------------

    def _run_setup(self, job: dict, repo: RepoConfig) -> None:
        """The repo's setup command: bash in the worktree, secrets scrubbed, 1800s timeout."""
        self._set(job, detail="running the repo's setup command")
        worktree = Path(job["worktree"])
        common_dir = (repo.path / gitops.git(repo.path, "rev-parse", "--git-common-dir").strip()).resolve()
        harness = _section(self.config.raw, "harness")
        masks = sandbox_masks(self.config)
        hidden, hidden_files, rebind = mask_plan(masks["dirs"], masks["files"],
                                                 [worktree, Path(job["job_dir"]), common_dir, repo.path],
                                                 masks["fallback"])
        try:
            sandbox = Sandbox(str(harness.get("sandbox") or "none"), worktree,
                              [worktree, Path(job["job_dir"]), common_dir], True, {},
                              secret_names=self._key_names(), masked=hidden, masked_files=hidden_files,
                              readonly_rebind=[*masks["rebind"], *rebind])
            try:
                code, output, timed_out = sandbox.run(repo.setup, SETUP_TIMEOUT, self._cancel_event(_key(job)))
            finally:
                sandbox.cleanup()
        except (ValueError, RuntimeError, OSError) as exc:
            raise StepError(f"setup command: {exc}") from None
        self._check_cancel(job)
        self._log(job, "info" if code == 0 else "error", f"setup command output:\n{truncate(output, SETUP_LOG_CHARS)}")
        if timed_out:
            raise StepError(f"setup command timed out after {SETUP_TIMEOUT:.0f}s")
        if code != 0:
            last = next((line.strip() for line in reversed(output.splitlines()) if line.strip()), "")
            raise StepError(f"setup command failed (exit {code})" + (f": {last[:NOTE_CHARS]}" if last else ""))

    def _discard_edits(self, job: dict, what: str) -> str | None:
        """Stash anything a non-builder agent left in the worktree; the error text if git cannot (or if the
        worktree's git pointers no longer lead to the repo)."""
        repo = self._repo(job)
        try:
            if gitops.stash_all(Path(job["worktree"]), f"factory: discarded {what}", repo.path):
                self._log(job, "warn", f"discarded {what} (kept in git stash)")
        except GitError as exc:
            return f"cannot discard {what}: {exc}"
        return None

    def _remove_checkout(self, job: dict, repo: RepoConfig) -> None:
        """Remove the job's worktree and branch; failures are logged, not raised."""
        self._codex_share_worktree(job)
        try:
            gitops.remove_worktree(repo.path, Path(job["worktree"]))
            gitops.delete_branch(repo.path, job["branch"], force=True)
        except GitError as exc:
            self._log(job, "warn", f"could not remove the worktree or branch: {exc}")

    def _push(self, job: dict, repo: RepoConfig) -> None:
        """Push base to merge.remote when merge.push is true (one branch, never forced), recording the outcome in
        job.push (DESIGN §40): the intent first, on disk before the call; succeeded or failed as the remote's own
        line for the ref says (git push --porcelain: a refusal is failed); failed too when nothing left (git never
        ran, or git's own words say it never reached the remote: gitops.push_never_left); with no confirming line
        (a timeout, a connection cut, "[remote failure]"), whatever a read-only look at the remote finds:
        succeeded when it has the commit, else unknown, with a tracker item. A push that already succeeded is not
        repeated, and nothing is pushed again by itself (a person's retry may: a repeat ends in the same state or is
        refused)."""
        merge = _section(self.config.raw, "merge")
        if merge.get("push") is not True or (job.get("push") or {}).get("outcome") == rule.SUCCEEDED:
            return
        remote, base = str(merge.get("remote") or "origin"), job["base"]
        ref = f"refs/heads/{base}"
        record: dict[str, Any] = {"remote": remote, "ref": ref, "commit": job.get("merged_commit"), "outcome": None,
                                  "at": utc_now()}
        try:
            record["commit"] = gitops.rev_parse(repo.path, base)
        except GitError:
            pass
        self._update(job, push=dict(record))   # the intent, before anything leaves
        try:
            proc = gitops.push(repo.path, remote, base)
        except GitError as exc:
            if not exc.timed_out:   # git never ran, or the remote name was refused: nothing left this machine
                return self._push_done(job, record, rule.FAILED, str(exc))
            detail = str(exc)
        else:
            line = gitops.push_line(proc.stdout, ref)
            record["response"] = rule.response(proc.returncode, [line[1]] if line else [], proc.stdout)
            said = gitops.complaint(proc.stderr)
            if gitops.push_refused(line):
                return self._push_done(job, record, rule.FAILED, f"the remote refused it: {line[1]}")
            if gitops.push_never_left(proc, ref):
                return self._push_done(job, record, rule.FAILED, f"it never reached {remote}: {said}")
            if line is not None and line[0] in gitops.PUSH_DONE_FLAGS:
                return self._push_done(job, record, rule.SUCCEEDED, f"the remote answered {line[1]}")
            detail = f"no confirming answer for {ref} ({line[1]})" if line is not None else \
                said or f"git push ended with exit {proc.returncode} and no answer for {ref}"
        try:   # the outcome is unknown: look again, read only, and record what is there
            seen = gitops.remote_head(repo.path, remote, base)
        except GitError as exc:
            record["observed"] = {"remote_commit": None, "at": utc_now(), "error": truncate(str(exc), NOTE_CHARS)}
            return self._push_done(job, record, rule.UNKNOWN, f"{detail}; the remote could not be asked afterwards")
        record["observed"] = {"remote_commit": seen, "at": utc_now()}
        if seen and seen == record["commit"]:
            return self._push_done(job, record, rule.SUCCEEDED, f"{detail}; looked again: {remote} has {base} at "
                                                                f"{seen[:12]}")
        where = f"at {seen[:12]}" if seen else "without it"
        self._push_done(job, record, rule.UNKNOWN, f"{detail}; looked again: {remote} has {base} {where}")

    def _push_done(self, job: dict, record: dict, outcome: str, detail: str) -> None:
        """Record a push's outcome on the job and in its log; an unknown one also opens one system item, "Outcome
        unknown: check before retrying", for a person (DESIGN §40)."""
        record.update(outcome=outcome, detail=truncate(detail, NOTE_CHARS), done_at=utc_now())
        remote, base = record.get("remote"), job.get("base")
        if outcome == rule.UNKNOWN:
            title = f"{rule.UNKNOWN_TITLE} (push of {base} to {remote}, job {job['repo']}/{job['id']})"
            digest = hashlib.sha256(f"push|{job['repo']}|{job['id']}|{record.get('commit')}".encode()).hexdigest()
            detail = (f"The push of {base} ({str(record.get('commit') or '')[:12]}) to {remote} may or may not have "
                      f"landed: {record['detail']}. Look at the remote before you push again (DESIGN §40).")
            try:
                opened = self.system_item({"title": title, "kind": "gap", "detail": detail,
                                           "source": {"type": "push", "job": f"{job['repo']}/{job['id']}"}},
                                          dedupe=digest[:16])
                record["item"] = opened["ref"]
            except Exception as exc:  # noqa: BLE001 - the tracker being busy never undoes the record
                self._log(job, "warn", f"could not open the tracker item for the unknown push: {exc}")
        self._update(job, push=record)
        level, words = {rule.SUCCEEDED: ("info", "pushed"), rule.FAILED: ("warn", "push failed"),
                        rule.UNKNOWN: ("warn", "push outcome unknown")}[outcome]
        tail = "; check the remote before pushing again" if outcome == rule.UNKNOWN else ""
        self._log(job, level, f"{words} ({base} to {remote}): {record['detail']}{tail}")

    def _prompt(self, job: dict, agent_name: str, *lines: str) -> str:
        """The paths line, the job's tracked items for this agent (when there are any, section 32), the
        step's lines, then "Port: <p>." for builder, reviewer-ux, reviewer-ui."""
        paths = (f"Job folder: {job['job_dir']}. Worktree: {job['worktree']}. "
                 f"Branch: {job['branch']}. Base: {job['base']}.")
        parts = [paths, *self._items_prompt(job, agent_name), *lines]
        if agent_name in PORT_OFFSETS:
            parts.append(f"Port: {job['port'] + PORT_OFFSETS[agent_name]}.")
        return "\n".join(parts)

    # -- running one agent -------------------------------------------------------

    def _run_agent(self, job: dict, agent_name: str, prompt: str, round: int | None = None,
                   resume: bool = False, guard: bool = True, ingest: bool | None = None) -> AgentRun:
        """Run (or resume) one agent from the job's routing snapshot and record it in job.agents and job.runs.

        The route is placed first (_place: the builder's escalation rung, the first build that
        answers, else the overflow; a wait for a slot past the provider's overflow_after_seconds
        overflows too). resume continues the agent's session when the runner supports it and the
        session was made by the same driver, model and served model id; when the model changed (an
        escalation, another build, an overflow, a re-route) the builder starts a fresh session with
        a hand-over prompt instead (other agents with the same prompt): a session cannot move
        between models, whose reasoning and thinking passback differs (section 23).
        The run's system prompt carries the Factory Grid Card of the job's own pipeline (its
        reviewers and max_resumes, section 26; its items rules while housekeeping.enabled holds jobs);
        agent.start, job.runs and job.agents record its receipt.
        guard checks the write policy after the run (section 7): every change in the job folder
        outside the run's write paths is reverted and fails the step. Parallel reviewers pass
        False; _review_policy checks their round as a whole. ingest (default: guard) takes the run's
        items in afterwards (section 32), unless the policy check failed (a run that did not end
        normally gives only its Items calls); a review round is taken in as a whole after WAIT FOR ALL.
        Raises StepError unless the run ended normally within its write paths, _Cancelled (with
        the run) if the job was cancelled.
        """
        route = (job.get("routing") or {}).get(agent_name)
        if route is None:
            raise StepError(f"the job has no route for {agent_name}")
        agents = self._agent_defs(job["repo"])
        if agent_name not in agents:
            raise StepError(f"no agent definition named {agent_name!r}")
        early: list[Path] = []   # the run's log, made early when a GPU custody wait must say so (section 37)

        def held(provider: str, seconds: float, denied: CustodyDenied) -> None:
            if not early:
                early.append(self.store.next_log_path(job, agent_name))
            self._emitter(job["repo"], job["id"], agent_name, early[0])(
                "agent.waiting", {"provider": provider, "overflow_after_seconds": seconds, "custody": denied.code})
            self._log(job, "info", f"{agent_name} waits up to {seconds:g}s for {provider}: GPU custody: {denied}")

        placement = self._place(job, agent_name, route, round, f"job {job['repo']}/{job['id']} {agent_name}",
                                on_wait=held)
        release = None
        try:
            if placement.claims is not None and placement.claims.waited and self._cancel_event(_key(job)).is_set():
                raise _Cancelled()   # cancelled while the placement waited out a HELD answer
            log_path = early[0] if early else self.store.next_log_path(job, agent_name)
            emit = self._emitter(job["repo"], job["id"], agent_name, log_path)
            if placement.wait is not None:
                waited = self._wait_for_slot(placement, self._cancel_event(_key(job)), emit)
                if waited is None:
                    raise _Cancelled()
                placement, release = waited
            return self._run_placed(job, agent_name, prompt, round, resume, guard, ingest, placement, agents,
                                    log_path, emit)
        finally:
            if release is not None:
                release()
            self._release_claims(job, agent_name, placement)

    def _run_placed(self, job: dict, agent_name: str, prompt: str, round: int | None, resume: bool, guard: bool,
                    ingest: bool | None, placement: _Placement, agents: dict[str, AgentDef], log_path: Path,
                    emit: Callable[[str, dict], None]) -> AgentRun:
        """_run_agent once the placement is final."""
        run_route = placement.route
        model = self._model_config(run_route, placement.build)
        if placement.claims is not None:   # the run's request lease on its provider (section 37)
            model = placement.claims.attach(model)
            placement.info.update(placement.claims.info())
        independence = self._independence(placement, run_route, model)
        self._note_placement(job, agent_name, placement, model, independence, round)
        repo = self._repo(job)
        worktree, job_dir = Path(job["worktree"]), Path(job["job_dir"])
        with self._job_lock(_key(job)):
            snapshot = copy.deepcopy(job)
        extra_roots = self._plans.extra_read_roots_for_job(snapshot) if self._plans is not None else []
        write_paths = _write_paths(job, agent_name, round)
        ingest = guard if ingest is None else ingest
        output = _output_file(job, agent_name, round)
        if ingest:
            self._render_items_md(job, agent_name)
        ctx = self._context(agents[agent_name], model, cwd=worktree, job_dir=job_dir, repo_path=repo.path,
                            job=snapshot, write_paths=write_paths,
                            roots=[worktree, job_dir, *extra_roots], log_path=log_path, emit=emit,
                            cancel=self._cancel_event(_key(job)),
                            items=self._items_context(job, agent_name, round, output))
        ctx.grid = factory_grid(snapshot, agent_name, items=self._housekeeping().enabled)
        grid = grid_receipt(ctx.grid)
        session = self._resumable_session(job, agent_name, run_route, model) if resume else None
        labels: dict[str, Any] = {"round": round, "grid": grid, **placement.info, "family": route_family(run_route),
                                  **independence}
        reason_ = placement_reason((job.get("routing") or {}).get(agent_name, {}).get("reason"),
                                   {**placement.info, **independence})
        if reason_:
            labels["reason"] = reason_
        worker = codex_work.is_worker(self.config, run_route.get("driver"))
        unfinished = self._unfinished_codex_run(job, agent_name)
        why = self._unfinished_note(job, unfinished) if unfinished is not None else None
        if why is None and worker and resume and session is None and self._codex_session_gone(job, agent_name):
            why = ("The earlier Codex session is gone with the job's Codex home, which is deleted when the job ends "
                   "(here: when it was delivered).")
        if resume and session is None and agent_name == "builder" and (
                why is not None or self._model_changed(job, agent_name, run_route, model)):
            prompt = self._handover_prompt(job, prompt, why)
            labels["handover"] = True
        elif unfinished is not None:   # a fresh run after an unclean end (§3.6): told what that run left
            prompt += "\n\n" + str(why)
        ignored_before: dict[str, tuple[int, int, int]] | None = None
        since = 0.0
        if worker:   # BC1b (DESIGN §44): the routing check, the codex model blocker, the run's lease and the host's
            # containment (the launcher; the uid egress rule, gone after a reboot unless its unit loads it), then its
            # intent
            problem = (codex_work.routing_problem(self.config, job["repo"], job.get("routing") or {})
                       or codex_work.model_blocker(self.config, model.provider, model.model)
                       or custody_lease_problem(model.provider, model.model)
                       or codex_work.containment_blocker(self.config, model.provider))
            if problem:
                raise StepError(f"{agent_name} cannot start on codex-worker: {problem}")
            from .codex import APPROVAL_POLICY, CODEX_PIN, PROFILE, SURFACE

            labels["codex"] = {"version": codex_work.settings(self.config)["pin"] or CODEX_PIN, "surface": SURFACE,
                               "permissions_profile": PROFILE, "approval_policy": APPROVAL_POLICY}
            ctx.codex_job = True   # the driver runs Codex only for a run cleared here (K20)
            if codex_work.open_worktree(self.config, job):   # its setfacl changes ctimes: before the run's start,
                time.sleep(IGNORED_SLACK)                    # and past the slack, so none is counted as the run's
            since = time.time()   # before the snapshot: what changes after it is this run's (_ignored_left)
            ignored_before = self._ignored_files(worktree)
            self._update(job, codex_run={"run": log_path.stem, "agent": agent_name, "round": round,
                                         "started": codex_work.utc_now(), "since": since})
        elif self._ran_codex(job):   # a job that ran Codex: no later run reads its work off this machine (§3.3)
            hosted = self._codex_hosted_problem(job["repo"], {agent_name: run_route})
            if hosted:
                raise StepError(f"{agent_name} cannot start: {hosted}")
        started_at = codex_work.utc_now()
        before = _scan(job_dir, keep=True) if guard else None
        result = self._invoke(run_route, ctx, prompt, session, emit, labels)
        codex_error = None
        try:
            violation = self._record_run(job, agent_name, result, model, run_route, log_path, labels, grid, session,
                                         before, write_paths)
            if worker:
                codex_error = self._codex_after(job, agent_name, round, result, model, run_route, log_path, grid,
                                                violation, started_at, ignored_before, since)
        except BaseException:
            if ingest:   # whatever ended the run, its Items calls are taken in (section 32)
                with contextlib.suppress(Exception):
                    self._ingest_run(job, agent_name, round, result, output, log_path.stem, complete=False)
            raise
        if ingest:   # a run that broke the write policy gives only its journal (its files are not its word)
            self._ingest_run(job, agent_name, round, result, output, log_path.stem,
                             complete=result.ok and violation is None)
        if self._cancel_event(_key(job)).is_set():
            raise _Cancelled(result)
        if violation:
            raise StepError(violation)
        if not result.ok:
            ended = outcome_of(result)
            how = f" (outcome {ended})" if worker and ended != "failed" else ""
            raise StepError(f"{agent_name} failed{how}: {result.error or 'the run did not finish'}")
        if codex_error:
            raise StepError(codex_error)
        return result

    def _record_run(self, job: dict, agent_name: str, result: AgentRun, model: ModelConfig, run_route: dict,
                    log_path: Path, labels: dict, grid: Any, session: str | None,
                    before: dict[str, _FileState] | None, write_paths: list[Path]) -> str | None:
        """Record a finished run in job.json (agents, usage, cost, runs), then check its write policy; returns
        the violation or None."""
        cost, priced = run_cost(result.usage, price_table(model))
        with self._job_lock(_key(job)):
            record = job.setdefault("agents", {}).setdefault(agent_name, {})
            kept = record.get("session") if session else None
            record.update(driver=run_route["driver"], model=run_route["model"],
                          session=_stored_session(job, result.session_ref) or kept,
                          runs=int(record.get("runs") or 0) + 1, grid=grid)
            add_usage(job.setdefault("usage", {}), result.usage or {})
            add_cost(job.setdefault("cost", zero_cost()), cost, priced)
            status = labels.get("independence")
            if status in INDEPENDENCE_RANK:   # section 23: the job says so too, for its badge
                if status != "unknown":
                    shared = {"round": labels.get("round"), "agent": agent_name, **labels.get("shared_model", {}),
                              **({"accepted": True} if status == "accepted" else {})}
                    listed = job.setdefault("shared_models", [])
                    if shared not in listed:
                        listed.append(shared)
                if INDEPENDENCE_RANK[status] > INDEPENDENCE_RANK.get(str(job.get("independence")), 0):
                    job["independence"] = status
            job.setdefault("runs", []).append({
                "run": log_path.stem, "agent": agent_name, "round": labels.get("round"), "driver": run_route["driver"],
                "model": run_route["model"], "provider": model.provider.name, "model_id": model.model,
                **{key: value for key, value in labels.items() if key not in ("round",)},
                "ok": result.ok, "outcome": outcome_of(result),
                "usage": {key: int((result.usage or {}).get(key) or 0) for key in USAGE_KEYS},
                "cost_usd": cost, "priced": priced})
            self._save(job)
        return self._run_policy(job, agent_name, before, write_paths) if before is not None else None

    # -- placement (section 23, routing.py) ---------------------------------------------

    def _place(self, job: dict | None, agent_name: str, route: dict, round_: int | None = None,
               purpose: str = "", cancel: threading.Event | None = None,
               on_wait: Callable[[str, float, CustodyDenied], None] | None = None) -> _Placement:
        """Where a run of route goes: the builder's escalation rung for the job's sendbacks (skipping
        rungs none of whose builds answers); a judge's independent alternate when its own model is of a
        family the builder ran on (the independence guard, routing.judge_choice and _guard); then the
        first build that answers, else the overflow. A CLI driver reaches its own endpoint, so its route
        is never probed: only its rung or alternate changes.
        On a server under GPU custody the build's request lease is taken here (purpose: what the run is,
        for the register): a build the register gives none for is passed over (section 37), and a judge's
        alternate it gives none for counts as not answering (the judge then stays on its own model, reduced).
        A HELD answer sends the run at once to another build that can take it (another card); only when none can
        does it wait, for at most the provider's overflow_after_seconds, until cancel (default: the job's cancel
        event) is set; on_wait(provider, seconds, the refusal) is told once when it starts. FULL passes the build
        over at once. A CLI route whose provider needs a lease takes it too, and fails the step
        without one (it has no overflow)."""
        if cancel is None and job is not None:
            cancel = self._cancel_event(_key(job))
        claims = Claims(purpose or f"{agent_name} run", cancel=cancel, on_wait=on_wait)
        try:
            return self._place_with(job, agent_name, route, round_, claims)
        except BaseException:   # a placement that fails gives back any lease it took (section 37)
            claims.release("the placement failed")
            raise

    def _place_with(self, job: dict | None, agent_name: str, route: dict, round_: int | None,
                    claims: Claims) -> _Placement:
        """_place with the run's Claims."""
        cli = self._is_cli(route.get("driver"))
        notes: list[str] = []
        info: dict[str, Any] = {}
        chosen = route
        judged = job is not None and is_judge(agent_name)
        guard = self._builder_families(job, round_, agent_name) if judged else {}
        status, tried = None, ""
        if guard:
            chosen, status = judge_choice(route, guard)
            if status == "alternate":
                cli = self._is_cli(chosen.get("driver"))
                answers, why = self._answers(chosen, cli, claims)
                if not answers:   # an alternate that cannot run is no way out: the judge's own model, reduced
                    notes += why
                    chosen, status, tried = route, shared_status(route), "does not answer"
                    cli = self._is_cli(route.get("driver"))
        rungs = [rung for rung in route.get("escalate") or [] if isinstance(rung, dict)]
        sendbacks = int(job.get("sendbacks") or 0) if job is not None and agent_name == "builder" else 0
        start = escalation_start(len(rungs), sendbacks)
        if start is not None:
            rung = start
            if not cli:
                for index in range(start, len(rungs)):
                    found, _, why = choose_build(self._builds(rungs[index]), self.reach)
                    if found is not None:
                        rung = index
                        break
                    notes.append(f"escalation rung {index + 1} ({rungs[index].get('model')}) skipped: "
                                 + "; ".join(why))
                else:
                    notes.append(f"no escalation rung from {start + 1} on answers; trying rung {start + 1} anyway")
            chosen = rungs[rung]
            info["escalation"] = {"rung": rung + 1, "of": len(rungs), "sendbacks": sendbacks,
                                  "from": route.get("model"), "model": chosen.get("model")}
        placement = self._place_on(chosen, info, notes, cli, claims, agent_name)
        placement.tried = tried
        if judged:
            placement.unknown = self._unknown_authors(job, round_, agent_name)   # type: ignore[arg-type]
        return self._guard(placement, route, guard, status) if guard else placement

    def _answers(self, route: dict, cli: bool, claims: Claims | None) -> tuple[bool, list[str]]:
        """(whether a build of route, else of its overflow, answers, and under GPU custody gets its request
        lease from claims; why the others were passed over). A CLI route reaches its own endpoint and counts as
        answering once it holds the lease its provider needs."""
        if cli:
            provider = self._builds(route)[0].provider
            if claims is not None and custody_needs_lease(provider):
                granted, why = claims(provider, str(route.get("model_id")))
                if not granted:
                    return False, [f"{route.get('model')} is unavailable: GPU custody: {why}"]
            return True, []
        found, _, why = choose_build(self._builds(route), self.reach, claims)
        overflow = route.get("overflow") if isinstance(route.get("overflow"), dict) else None
        if found is None and overflow is not None:
            found, _, more = choose_build(self._builds(overflow), self.reach, claims)
            why += more
        return found is not None, why

    def _place_on(self, chosen: dict, info: dict, notes: list[str], cli: bool, claims: Claims | None,
                  agent_name: str = "the run") -> _Placement:
        """chosen's placement: on a CLI driver chosen itself (with its request lease when its provider needs
        one: StepError without it); else its first build that answers (and gets its lease), else its
        overflow's, with the slot wait (wait) when its provider has max_concurrency and
        overflow_after_seconds and chosen has an overflow."""
        if cli:
            provider = self._builds(chosen)[0].provider
            if claims is not None and custody_needs_lease(provider):
                granted, why = claims(provider, str(chosen.get("model_id")))
                if not granted:
                    raise StepError(f"{agent_name} cannot start: GPU custody: {why}")
            return _Placement(route=chosen, info=info, notes=notes, claims=claims)
        builds = self._builds(chosen)
        overflow = chosen.get("overflow") if isinstance(chosen.get("overflow"), dict) else None
        choice = choose(builds, self._builds(overflow) if overflow else None, self.reach,
                        str(overflow.get("model")) if overflow else "", claims)
        notes += choice.notes
        if choice.swap:
            info["swap"] = choice.swap
        if choice.overflowed and overflow is not None:
            info["overflow"] = {"from": chosen.get("model"), "to": overflow.get("model"), "why": choice.why}
            return _Placement(route=overflow, build=choice.index, info=info, notes=notes, claims=claims)
        if len(builds) > 1:
            info["build"] = {"index": choice.index, "of": len(builds), "provider": builds[choice.index].provider.name,
                             "model_id": builds[choice.index].model_id}
        placement = _Placement(route=chosen, build=choice.index, info=info, notes=notes, claims=claims)
        provider = builds[choice.index].provider
        if overflow is not None and provider.overflow_after_seconds and provider.max_concurrency:
            placement.wait = (provider, float(provider.overflow_after_seconds), overflow)
        return placement

    def _guard(self, placement: _Placement, route: dict, guard: dict[str, str], status: str | None) -> _Placement:
        """A judge's placement under the independence guard (section 23): placement with guard and judge set,
        left when it moved to its alternate (status "alternate"). When the judge's own model is independent
        but its placement overflowed onto one of guard's families, its independent alternate takes over if
        it is placed off them (_off_guard); with a slot wait still to come, the alternate is kept for then
        (escape)."""
        placement.guard, placement.judge = guard, route
        if status == "alternate":
            placement.left = shared_model(route, guard)
            return placement
        if status is not None:   # reduced or accepted on its own model
            return placement
        alternate = route.get("independent_alternate")
        if isinstance(alternate, dict) and route_family(alternate) not in guard:
            placement.escape = alternate
        if placement.wait is None and placement.escape is not None and route_family(placement.route) in guard:
            return self._off_guard(placement)
        return placement

    def _off_guard(self, placement: _Placement) -> _Placement:
        """placement overflowed onto a family the builder ran on: the judge's independent alternate
        (placement.escape) instead, when it is placed off those families on a build or overflow that
        answers (and, under GPU custody, gets its lease from the placement's claims: a lease taken for the
        build left behind is given back when the run starts, Claims.attach); else placement as it is, saying
        what the alternate did (tried)."""
        guard, landed, escape = placement.guard or {}, placement.route, placement.escape
        placement.escape = None
        if escape is None:
            return placement
        cli = self._is_cli(escape.get("driver"))
        answers, why = self._answers(escape, cli, placement.claims)   # placing one build alone never probes it
        if not answers:
            placement.notes += why
            placement.tried = "does not answer"
            return placement
        moved = self._place_on(escape, {}, [], cli, placement.claims)
        if route_family(moved.route) not in guard:
            moved.guard, moved.judge, moved.unknown = placement.guard, placement.judge, placement.unknown
            moved.left = shared_model(landed, guard, self._build_model_id(landed, placement.build))
            moved.notes = placement.notes + [f"its overflow {landed.get('model')} is of the {route_family(landed)} "
                                             f"family the builder ran on"] + moved.notes
            return moved
        placement.tried = f"would overflow to {moved.route.get('model')}, of a family the builder ran on too"
        return placement

    def _build_model_id(self, route: dict, build: int) -> str | None:
        """The model id of route's build-th build (None when its builds cannot be read)."""
        try:
            return self._builds(route)[build].model_id
        except (IndexError, StepError):
            return None

    def _release_claims(self, job: dict | None, agent_name: str, placement: _Placement, repo: str | None = None) -> None:
        """Give back the run's request leases when it ends, however it ended (section 37), and log what the
        register says is resident afterwards: a release is never taken as evidence that VRAM is free."""
        claims = placement.claims
        if claims is None or not claims.leases:
            return
        cancelled = job is not None and self._cancel_event(_key(job)).is_set()
        for view in claims.release("cancelled" if cancelled else "the run ended"):
            if view["state"] == "held":   # another run of this process still uses the shared lease
                continue
            after = ", ".join(model for _, model in view.get("resident_after") or []) or "nothing"
            line = (f"{agent_name}: GPU custody lease {view['lease_id']} on {view['provider']}/{view['model']} "
                    f"{view['state']}" + (f" ({view['error']})" if view.get("error") else "")
                    + (f"; resident on {view['register_provider']} afterwards: {after}"
                       if view.get("resident_after") is not None else "; residency could not be read afterwards"))
            if job is not None:
                self._log(job, "info" if view["state"] in ("released", "held") else "warn", line)
            else:
                self.bus.publish("log", {"level": "info", "message": line}, repo=repo)

    def _wait_for_slot(self, placement: _Placement, cancel: threading.Event, emit: Callable[[str, dict], None]
                       ) -> tuple[_Placement, Callable[[], None] | None] | None:
        """Wait for a slot of the placed provider for at most its overflow_after_seconds (agent.waiting
        when it is busy); past that the run overflows (a judge's onto its independent alternate instead
        when the overflow is of a family the builder ran on: _off_guard), its overflow's build under GPU
        custody only with a lease from the placement's claims. Returns the final placement and the slot's
        release (None after an overflow), or None when cancel is set meanwhile (no slot is then held:
        take_slot gives back one that came free after the cancel)."""
        provider, seconds, overflow = placement.wait   # type: ignore[misc]
        release = take_slot(provider, seconds, cancel,
                            on_wait=lambda: emit("agent.waiting", {"provider": provider.name,
                                                                   "overflow_after_seconds": seconds}))
        if release is not None:
            return placement, release
        if cancel.is_set():
            return None
        if isinstance(placement.claims, Claims):   # the run waited its time: the overflow's HELD answers do not wait
            placement.claims.stop_waiting(f"waited {seconds:g}s for a slot of {provider.name}")
        builds = self._builds(overflow)
        found, swap, notes = choose_build(builds, self.reach, placement.claims)
        if found is None:   # nowhere to go: keep waiting for the slot, as without an overflow
            placement.notes += notes + [f"the overflow {overflow.get('model')} is unavailable too; waiting for "
                                        f"{provider.name}"]
            release = take_slot(provider, float("inf"), cancel)
            return (placement, release) if release is not None else None
        info = {key: value for key, value in placement.info.items() if key not in ("build", "swap")}
        info["overflow"] = {"from": placement.route.get("model"), "to": overflow.get("model"),
                            "why": f"no slot of {provider.name} came free within {seconds:g}s"}
        if swap:
            info["swap"] = swap
        moved = dataclasses.replace(placement, route=overflow, build=found, info=info, notes=placement.notes + notes,
                                    wait=None)   # the same claims: its leases are given back when the run ends
        if moved.guard and moved.escape is not None and route_family(overflow) in moved.guard:
            other = self._off_guard(moved)
            if other is not moved:
                return self._wait_for_slot(other, cancel, emit) if other.wait is not None else (other, None)
        return moved, None

    def _builds(self, route: dict) -> list[Build]:
        """A snapshot route's builds as routing.Build (index 0: the route's own provider and model)."""
        builds = route.get("builds") if isinstance(route.get("builds"), list) else None
        if not builds:
            return [Build(self._model_config(route).provider, str(route.get("model_id")))]
        found = []
        for item in builds:
            try:
                found.append(Build(self._provider_from_entry(str(item["provider"]), item["provider_entry"]),
                                   str(item["model_id"]), _positive(item.get("max_tokens")),
                                   _positive(item.get("context_tokens"))))
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                raise StepError(f"the job's route on model {route.get('model')!r} has a malformed build: "
                                f"{_message(exc)}; change the job's routing") from None
        return found

    def _builder_families(self, job: dict, round_: int | None, agent_name: str = "") -> dict[str, str]:
        """{family: model key} a judge of round_ must not share (section 23): the families of the builder's
        runs in round_ (job.runs), and of a chat hand-off's models (job.handoff_models) in round 1, whose code
        the chat wrote. The approver judges the whole branch once, so it is compared with every round up to
        round_. {} without a round."""
        if not round_:
            return {}
        approver = agent_name == "approver"
        with self._job_lock(_key(job)):
            runs = [dict(run) for run in job.get("runs") or [] if isinstance(run, dict)
                    and run.get("agent") == "builder" and isinstance(run.get("round"), int)
                    and (run["round"] <= round_ if approver else run["round"] == round_)]
            chat = [dict(entry) for entry in job.get("handoff_models") or [] if isinstance(entry, dict)]
        found: dict[str, str] = {}
        for entry in [*(chat if approver or round_ == 1 else []), *runs]:
            found.setdefault(route_family(entry), str(entry.get("model")))
        return found

    @staticmethod
    def _unknown_authors(job: dict, round_: int | None, agent_name: str) -> bool:
        """Whether a judge of round_ judges code a chat wrote on models the job does not record: a
        hand-off job (from_branch) without handoff_models, in round 1 or for the approver."""
        return (bool(job.get("from_branch")) and not isinstance(job.get("handoff_models"), list)
                and (round_ == 1 or agent_name == "approver"))

    @staticmethod
    def _independence(placement: _Placement, run_route: dict, model: ModelConfig) -> dict:
        """The independence fields agent.start and job.runs carry for a judge run (section 23): "reduced"
        (or "accepted", when the judge's route says independent_alternate: false) and the model it shares
        with the builder when it runs on a family the builder ran on (no alternate, an alternate of such a
        family too, or an overflow onto one with no way out); "alternate" and the model it left when it
        moved to its independent alternate; "unknown" when code it judges was written in a chat whose
        models the job does not record; {} otherwise (and for every other run)."""
        guard = placement.guard
        if guard and route_family(run_route) in guard:
            return {"independence": shared_status(placement.judge or {}),
                    "shared_model": shared_model(run_route, guard, model.model)}
        if guard and placement.left:
            return {"independence": "alternate", "shared_model": dict(placement.left)}
        if placement.unknown:
            return {"independence": "unknown"}
        return {}

    @staticmethod
    def _shared_why(placement: _Placement) -> str:
        """Why a judge run on the builder's family had no way out, for its log line."""
        judge = placement.judge or {}
        guard = placement.guard or {}
        alternate = judge.get("independent_alternate")
        if alternate is False:
            return "the routing accepts it: independent_alternate is false"
        overflow = placement.info.get("overflow") if isinstance(placement.info.get("overflow"), dict) else {}
        if placement.left:   # it moved to its alternate, and that overflowed onto a builder family
            return (f"{placement.left.get('model')} is of a family the builder ran on too, and its independent "
                    f"alternate {overflow.get('from')} overflowed to {overflow.get('to')}")
        if route_family(judge) in guard:
            return ("it has no independent_alternate" if not isinstance(alternate, dict) else
                    f"its independent alternate {alternate.get('model')} does not answer" if placement.tried else
                    f"its independent alternate {alternate.get('model')} is of a family the builder ran on too")
        head = f"its own model {judge.get('model')} overflowed to {placement.route.get('model')}"
        if not isinstance(alternate, dict):
            return head + ", and it has no independent_alternate"
        what = ("is of a family the builder ran on too" if route_family(alternate) in guard
                else placement.tried or "did not take over")
        return head + f", and its independent alternate {alternate.get('model')} {what}"

    def _is_cli(self, driver: Any) -> bool:
        try:
            return self.config.driver(str(driver)).type == "cli"
        except (KeyError, ValueError):
            return False

    def _note_placement(self, job: dict, agent_name: str, placement: _Placement, model: ModelConfig,
                        independence: dict | None = None, round_: int | None = None) -> None:
        """Log why a run does not go to its route's first build (passed-over builds and rungs, an
        overflow, a swap, an independent alternate or a reduced independence), and remember a swap to
        undo when the job's step loop ends (swap_back)."""
        info = placement.info
        lines = list(placement.notes)
        if "escalation" in info:
            esc = info["escalation"]
            lines.append(f"escalated to rung {esc['rung']} of {esc['of']} ({esc['model']}) after "
                         f"{esc['sendbacks']} send-back(s)")
        shared = (independence or {}).get("shared_model") or {}
        status = (independence or {}).get("independence")
        when = "in the rounds so far" if agent_name == "approver" else f"in round {round_}"
        if status == "alternate":
            lines.append(f"runs on its independent alternate {placement.route.get('model')} instead of "
                         f"{shared.get('model')}: the builder ran on the {shared.get('family')} family "
                         f"({shared.get('builder_model')}) {when}")
        elif status in ("reduced", "accepted"):
            lines.append(f"independence {status}: {shared.get('model')} is of the {shared.get('family')} family, "
                         f"like the builder's {shared.get('builder_model')} {when} ({self._shared_why(placement)})")
        elif status == "unknown":
            lines.append("independence unknown: the code it judges was written in a chat whose models the job "
                         "does not record")
        if "overflow" in info:
            lines.append(f"overflows from {info['overflow']['from']} to {info['overflow']['to']}: "
                         f"{info['overflow']['why']}")
        swap = info.get("swap")
        if swap:
            lines.append(f"{swap['provider']} switches from {swap['from'] or 'an unknown model'} to {swap['to']} "
                         f"(allow_model_swap)")
            self.reach.forget(str(swap["provider"]))
            if model.provider.swap_back and swap.get("from"):
                lease = lease_of(model.provider)
                with self._lock:
                    swaps = self._swaps.setdefault(_key(job), [])
                    if all(provider.name != model.provider.name for provider, _, _ in swaps):
                        swaps.append((dataclasses.replace(model.provider, lease=None), str(swap["from"]),
                                      lease.before if lease is not None else None))
        if lines:
            level = "warn" if "overflow" in info or swap else "info"
            self._log(job, level, f"{agent_name} runs on {model.provider.name}/{model.model}: " + "; ".join(lines))

    def _swap_back(self, job: dict) -> None:
        """Load back the model a swap replaced on each swap_back provider (best effort; logged): one tiny
        request naming the original model, which a server with inline model loading (TabbyAPI) loads. Under
        GPU custody the request holds its own lease (purpose "swap_back"), and afterwards the register's
        residency on that server is compared with the residency read before the swap: exact restoration is
        checked, never assumed (section 37)."""
        with self._lock:
            swaps = self._swaps.pop(_key(job), [])
        for provider, original, before in swaps:
            try:
                loaded, _ = self.reach.loaded(provider, self.reach.timeout)
                if loaded == original:
                    continue
                cfg = dataclasses.replace(provider, timeout=min(provider.timeout, SWAP_BACK_TIMEOUT))
                target = ModelConfig(key="swap-back", provider=cfg, model=original, max_tokens=1, stream=False)
                with leased(target, f"swap_back {job['repo']}/{job['id']}") as call:
                    client = make_provider(call.provider)
                    client.complete(call, "", [client.user_message("Reply with OK")], [], cancel=self._stopping)
                self._log(job, "info", f"swap_back: {provider.name} has {original} loaded again")
                self._check_restored(job, provider, original, before)
            except CustodyDenied as exc:
                self._log(job, "warn", f"swap_back: GPU custody gave no lease to load {original} on {provider.name} "
                                       f"again ({exc}); it was not loaded back")
            except Exception as exc:  # noqa: BLE001 - the job is over; a failed swap-back is reported only
                self._log(job, "warn", f"swap_back: could not load {original} on {provider.name} again: "
                                       f"{_message(exc)}")
            finally:
                self.reach.forget(provider.name)

    def _check_restored(self, job: dict, provider: ProviderConfig, original: str, before: Any) -> None:
        """Under custody: is the residency on provider's server what it was before the swap? (logged)"""
        settings = custody_settings_of(provider)
        if settings is None or before is None:
            return
        after = custody_client_for(settings).reread()
        name = register_names(provider, original)[0]
        if residency_matches(before, after, name):
            self._log(job, "info", f"swap_back: GPU custody residency on {name} matches what it was before the swap "
                                   f"({', '.join(model for _, model in before.on(name)) or 'nothing resident'})")
            return
        now = "could not be read" if after is None else (", ".join(model for _, model in after.on(name)) or "nothing")
        self._log(job, "warn", f"swap_back: GPU custody residency on {name} differs from before the swap: before "
                               f"{', '.join(model for _, model in before.on(name)) or 'nothing'}, now {now}")

    def _model_changed(self, job: dict, agent_name: str, route: dict, model: ModelConfig) -> bool:
        """Whether the agent's last run in this job ran on another driver, model or served model id."""
        record = (job.get("agents") or {}).get(agent_name) or {}
        if not record.get("runs"):
            return False
        last = _last_run(job, agent_name)
        if record.get("driver") != route["driver"] or record.get("model") != route["model"]:
            return True
        return bool(last and last.get("model_id") and last["model_id"] != model.model)

    def _handover_prompt(self, job: dict, prompt: str, why: str | None = None) -> str:
        """The builder's first prompt in a new session mid-job: on a new model, or (why: the reason, said first) when
        the Codex session cannot go on (its last run did not complete, §3.6; its home was deleted at job end): the
        paths line, what the earlier runs built and why it was sent back (the spec, build.md, the last round's
        CHANGES reviews and any rework note, the diff so far), then the request it would have resumed with."""
        paths, _, request = prompt.partition("\n")
        job_dir = Path(job["job_dir"])
        last = _last_run(job, "builder") or {}
        before = f"{last.get('provider')}/{last.get('model_id')}" if last.get("model_id") else "another model"
        if why:
            opening = (f"Hand-over: you take over this job's build in a new session. {why} Everything committed is "
                       f"on the branch {job['branch']}.")
        else:
            opening = (f"Hand-over: you take over this job's build from {before}. Its session cannot continue on "
                       f"another model, so you start fresh: everything it did is on the branch {job['branch']}.")
        parts = [opening, f"The spec is {job_dir / 'spec.md'}; read it first."]
        quoted = [("The previous builder's notes", job_dir / "build.md")]
        n = _last_review_round(job)
        rounds = [entry for entry in job.get("history") or [] if entry.get("round") == n]
        reviews = (rounds[-1].get("reviews") or {}) if rounds else {}
        quoted += [(f"Review findings from {reviewer}", job_dir / f"round-{n}" / f"review-{reviewer}.md")
                   for reviewer, verdict in reviews.items() if verdict == "CHANGES"]
        notes = re.findall(r"rework-(\d+)\.md", request)   # the last: the step's line, after the items lines
        if notes:
            quoted.append(("The human's rework note", job_dir / f"rework-{notes[-1]}.md"))
        for title, path in quoted:
            text = _quoted(path)
            if text:
                parts.append(f"{title} ({path}):\n{text}")
        try:
            repo = self._repo(job)
            stat_text = gitops.diffstat(repo.path, job["base"], job["branch"]).strip()
            diff, cut = gitops.diff_truncated(repo.path, job["base"], job["branch"], HANDOVER_DIFF_CHARS)
            rest = f"\n[the diff is cut short: run `git diff {job['base']}...HEAD` for the rest]" if cut else ""
            parts.append(f"The diff so far ({job['base']}...{job['branch']}):\n{stat_text}\n\n{diff.rstrip()}{rest}")
        except (GitError, StepError) as exc:
            parts.append(f"(the diff could not be read: {_message(exc)}; run `git diff {job['base']}...HEAD`)")
        parts.append(f"Your task now:\n{request}")
        return paths + "\n" + "\n\n".join(parts)

    # -- the write policy (section 7) -------------------------------------------------

    def _run_policy(self, job: dict, agent_name: str, before: dict[str, _FileState],
                    write_paths: list[Path]) -> str | None:
        """After one (not parallel) run: revert what changed in the job folder outside write_paths
        and the agent's own session files; the violation, also logged as a warning, else None."""
        job_dir = Path(job["job_dir"])
        allowed = _policy_allows(job_dir, write_paths, [agent_name])
        changed = [rel for rel in _changed(before, _scan(job_dir)) if not allowed(rel)]
        self._check_items_files(job)
        tampered = self._take_tampered(_key(job))
        if not changed and not tampered:
            return None
        stuck = [rel for rel in changed if not _revert(job_dir, rel, before.get(rel))]
        paths = changed + tampered
        message = (f"write policy: {agent_name} changed {_listing(paths)} in the job folder, outside its write "
                   f"paths {_revert_note(stuck)}")
        self._log(job, "warn", message)
        return message

    def _review_policy(self, job: dict, n: int, before: dict[str, _FileState], ran: list[str],
                       own: dict[str, _FileState | None], journals: dict[str, _FileState | None] | None = None,
                       forged_journals: set[str] | None = None) -> str | None:
        """After review round n: revert what changed in the job folder besides the review files and
        sessions of the reviewers that ran; a review file, or an items journal, that is not what its
        reviewer left becomes ERROR (a forged journal is also added to forged_journals and is never
        applied). The violation (also logged as a warning), else None."""
        job_dir = Path(job["job_dir"])
        after = _scan(job_dir)
        files = {f"round-{n}/review-{r}.md": r for r in ran}
        allowed = _policy_allows(job_dir, [], [f"reviewer-{r}" for r in ran])
        changed = [rel for rel in _changed(before, after) if rel not in files and not allowed(rel)]
        self._check_items_files(job)
        tampered = self._take_tampered(_key(job))
        forged = [r for rel, r in files.items() if not _same(after.get(rel), own.get(r))]
        journal_forged = [r for r in ran if journals is not None
                          and not _same(after.get(_journal_rel(f"reviewer-{r}")), journals.get(r))]
        if forged_journals is not None:
            forged_journals.update(f"reviewer-{r}" for r in journal_forged)
        if not changed and not tampered and not forged and not journal_forged:
            return None
        parts = []
        if changed or tampered:
            stuck = [rel for rel in changed if not _revert(job_dir, rel, before.get(rel))]
            paths = changed + tampered
            parts.append(f"{_listing(paths)} changed outside the review files {_revert_note(stuck)}")
        parts += [f"round-{n}/review-{r}.md changed after reviewer-{r} finished" for r in forged]
        parts += [f"{_journal_rel(f'reviewer-{r}')} changed after reviewer-{r} finished" for r in journal_forged
                  if r not in forged]
        forged = list(dict.fromkeys(forged + journal_forged))
        if forged:
            with self._job_lock(_key(job)):
                job["reviews"].update({r: "ERROR" for r in forged})
                self._save(job)
        message = f"write policy, review round {n}: " + "; ".join(parts)
        self._log(job, "warn", message)
        return message

    def _take_tampered(self, key: JobKey) -> list[str]:
        """The job-folder files (once each) that _save or an items store found changed by someone else
        since the orchestrator last wrote them: job.json, items.json, items.log.jsonl, items.md."""
        with self._lock:
            return self._tampered.pop(key, [])

    def _check_items_files(self, job: dict) -> None:
        """Put back the job's items files if an agent changed them, and remember them for the report."""
        try:
            names = self._items_store(job).check_tamper()
        except OSError as exc:
            self._log(job, "warn", f"could not check the items files: {exc}")
            return
        if names:
            with self._lock:
                found = self._tampered.setdefault(_key(job), [])
                found += [name for name in names if name not in found]
            self._log(job, "warn", f"{', '.join(names)} changed outside the orchestrator; put back")

    def _invoke(self, route: dict, ctx: RunContext, prompt: str, session: str | None,
                emit: Callable[[str, dict], None], labels: dict) -> AgentRun:
        """One run between agent.start and agent.end: a resume of session when the runner can, else a
        fresh run. An unknown driver or a runner exception becomes a failed run, never a dead worker."""
        runner: Runner | None = None
        failure = None
        try:
            runner = self.runner_factory(self.config, route["driver"])
        except Exception as exc:  # reported as the run's error
            failure = f"cannot start driver {route['driver']!r}: {_message(exc)}"
        resume = bool(session) and runner is not None and runner.supports_resume
        model_id, provider = launch_model(runner, ctx, (route["model_id"], route["provider"]))
        emit("agent.start", {"agent": ctx.agent.name, "driver": route["driver"], "model": route["model"],
                             "model_id": model_id, "provider": provider, "resume": resume, **labels})
        started = self.clock()
        if runner is None:
            result = AgentRun(ok=False, error=failure)
        else:
            try:
                result = runner.resume(ctx, str(session), prompt) if resume else runner.run(ctx, prompt)
            except Exception as exc:  # reported as the run's error
                result = AgentRun(ok=False, error=f"{type(exc).__name__}: {exc}")
        cost, priced = run_cost(result.usage, price_table(ctx.model))
        emit("agent.end", {"ok": result.ok, "error": result.error, "usage": result.usage, "turns": result.turns,
                           "seconds": max(0.0, self.clock() - started), "cost_usd": cost, "priced": priced,
                           "outcome": outcome_of(result)})
        return result

    def _context(self, agent: AgentDef, model: ModelConfig, *, cwd: Path, job_dir: Path, repo_path: Path,
                 job: dict, write_paths: list[Path], roots: list[Path], log_path: Path,
                 emit: Callable[[str, dict], None], cancel: threading.Event,
                 items: tracker.ItemsContext | None = None) -> RunContext:
        harness = _section(self.config.raw, "harness")
        dirs = skill_dirs(self.config, repo_path, cwd)
        extra = [expand_path(root, self.config.path.parent) for root in harness.get("extra_read_roots") or []]
        (job_dir / "sessions").mkdir(parents=True, exist_ok=True)
        return RunContext(
            job=job, agent=agent, cwd=cwd, job_dir=job_dir, write_paths=write_paths,
            read_roots=[*roots, *dirs, *extra], log_path=log_path, emit=emit, cancel=cancel, model=model,
            limits=self.config.limits(), session_path=job_dir / "sessions" / f"{agent.name}.json",
            repo_path=repo_path, skill_dirs=dirs, mcp_servers=self.config.mcp_servers(),
            sandbox=str(harness.get("sandbox") or "none"), sandbox_network=harness.get("sandbox_network") is not False,
            secrets=[*self.config.secret_values(), *_provider_secrets(model.provider)], items=items,
        )

    def _emitter(self, repo: str, job_id: str | None, agent_name: str, log_path: Path) -> Callable[[str, dict], None]:
        """emit(type, data) for one run: data.run = the log stem; also appended to the run's log."""
        run = log_path.stem

        def emit(event_type: str, data: dict) -> None:
            self.bus.publish(event_type, {**(data or {}), "run": run}, repo=repo, job=job_id, agent=agent_name,
                             log_path=log_path)

        return emit

    def _resumable_session(self, job: dict, agent_name: str, route: dict, model: ModelConfig | None = None) -> str | None:
        """The agent's session to resume: same driver and model (and, when job.runs knows it, the same
        served model id: another build of the model starts fresh), and (for a session file) still there."""
        record = (job.get("agents") or {}).get(agent_name) or {}
        ref = record.get("session")
        if not ref or record.get("driver") != route["driver"] or record.get("model") != route["model"]:
            return None
        if self._unfinished_codex_run(job, agent_name) is not None:   # never the old prompt in the old thread
            return None
        last = _last_run(job, agent_name)
        if model is not None and last and last.get("model_id") and last["model_id"] != model.model:
            return None
        ref = str(ref)
        if ref.startswith("sessions/") or os.path.isabs(ref):
            path = Path(job["job_dir"]) / ref
            return str(path) if path.is_file() else None
        return ref

    # -- routing -------------------------------------------------------------------

    def _routing(self, repo: RepoConfig, reviewers: list[str], routing: dict | str | None,
                 keep: dict | None = None, profile: str | None = None,
                 extra_agents: tuple[str, ...] = ()) -> tuple[dict, str | None]:
        """({agent: route} for every agent the job runs, the profile applied); see _resolve_route.

        routing: None (the saved config), a preset name (examples/<name>.json merged over the
        config) or {agent: {model?, driver?, extra_body?, effort?, escalate?, reason?}}. profile:
        a key of the (preset-merged) config's profiles whose placements apply under routing's
        (None: its default_profile; "": none). With keep, agents a dict does not name keep their
        route. A route's model and provider may exist only in the preset (the snapshot carries
        them); its driver must exist in the live config. ValueError if a route does not resolve.
        extra_agents (e.g. lead, for an estimate) are resolved too when they have a definition.
        """
        if routing is not None and not isinstance(routing, (dict, str)):
            raise ValueError("routing must be an object of per-agent routes or a preset name")
        overrides = _check_overrides(routing) if isinstance(routing, dict) else {}
        source = self._source_config(routing)
        name = profile_name(source.raw, profile)
        try:
            placements = merged_placements(profile_routes(source.raw, name), overrides)
        except ValueError as exc:
            raise ValueError(f"profile {name!r}: {exc}") from None
        agents = discover_agents(agent_dirs(source, repo.path))
        names = required_agents(reviewers)
        missing = [agent for agent in names if agent not in agents]
        if missing:
            raise ValueError(f"no agent definition for {', '.join(missing)} (add <name>.md to an agents dir)")
        snapshot = {}
        for agent in [*names, *(extra for extra in extra_agents if extra in agents)]:
            if keep and isinstance(routing, dict) and agent not in overrides and agent in keep:
                snapshot[agent] = keep[agent]
                continue
            try:
                route = self._resolve_route(source, agents[agent], placements.get(agent))
                for part in [route, *route.get("escalate", []), *([route["overflow"]] if "overflow" in route else []),
                             *([route["independent_alternate"]] if isinstance(route.get("independent_alternate"), dict)
                               else [])]:
                    self._model_config(part)
                self.config.driver(route["driver"])
            except (KeyError, ValueError, StepError) as exc:
                raise ValueError(f"cannot route {agent}: {_message(exc)}") from None
            snapshot[agent] = route
        return snapshot, name

    def _source_config(self, routing: dict | str | None) -> Config:
        """The config routes resolve in: a preset name merged over the live config, else the live config."""
        if isinstance(routing, str):
            from .settings import preset_config

            return preset_config(self.config, routing)
        return self.config

    @staticmethod
    def _resolve_route(config: Config, agent: AgentDef, override: dict | None = None) -> dict:
        """The agent's full route in config: override's placement over its agents entry.

        agents.<name> extra_body, effort, escalate, reason and independent_alternate apply only when
        the model comes from config; the resolved extra_body is the model entry's with the route's
        deep-merged over it (an effort becomes output_config.effort on Anthropic providers,
        reasoning_effort on OpenAI ones). model_entry and provider_entry are the resolved entries'
        settings (no secrets), so the job runs on exactly them however the config changes later;
        builds, overflow, the escalation rungs and a judge's independent alternate (each a whole route
        of its own, on the model entry's own extra_body; false when the routing accepts the judge on the
        builder's family) come along (section 23). ValueError for an alternate on an agent that is not a
        judge, or one of the route's own family.
        """
        override = override or {}
        driver = str(override.get("driver") or config.agent_driver(agent.name))
        config.driver(driver)
        if override.get("model"):
            model, own = config.model(str(override["model"])), {}
        else:
            entry = _section(config.raw, "agents").get(agent.name)
            model, own = config.agent_model(agent), entry if isinstance(entry, dict) else {}
        configured = own.get("extra_body") if isinstance(own.get("extra_body"), dict) else {}
        effort = override.get("effort") or own.get("effort")
        extra_body = deep_merge(deep_merge(deep_merge(model.extra_body, configured),
                                           effort_body(model.provider.type, effort)),
                                override.get("extra_body") or {})
        route = Factory._model_route(config, model, driver, extra_body)
        rungs = override["escalate"] if "escalate" in override else own.get("escalate")
        if rungs:
            route["escalate"] = [Factory._model_route(config, config.model(str(key)), driver) for key in rungs]
        found = reason(override["reason"] if "reason" in override else own.get("reason"))
        if found:
            route["reason"] = found
        alternate = (override["independent_alternate"] if "independent_alternate" in override
                     else own.get("independent_alternate"))
        if alternate is False:   # the routing accepts this judge on the builder's family
            if not is_judge(agent.name):
                raise ValueError(f"{agent.name}: {JUDGE_HINT}")
            route["independent_alternate"] = False
        elif alternate:
            if not is_judge(agent.name):
                raise ValueError(f"{agent.name}: {JUDGE_HINT}")
            other = Factory._model_route(config, config.model(str(alternate)), driver)
            if route_family(other) == route_family(route):
                raise ValueError(f"independent_alternate {alternate!r} is of the {route_family(route)} family, like "
                                 f"{route['model']!r}: name a model of another family")
            route["independent_alternate"] = other
        return route

    @staticmethod
    def _model_route(config: Config, model: ModelConfig, driver: str, extra_body: dict | None = None,
                     overflow: bool = True) -> dict:
        """The route of model on driver: {model, driver, provider, model_id, family, extra_body,
        model_entry, provider_entry}, cli_model on a CLI driver, builds when the entry has more than one,
        and the overflow model's route (without an overflow of its own) when it has one."""
        route = {"model": model.key, "driver": driver, "provider": model.provider.name,
                 "model_id": model.model, "family": model_family(model),
                 "extra_body": copy.deepcopy(model.extra_body if extra_body is None else extra_body),
                 "model_entry": _model_entry(model), "provider_entry": _provider_entry(model.provider)}
        driver_cfg = config.driver(driver)
        if driver_cfg.type == "cli":
            route["cli_model"] = cli_model(driver_cfg, model.key, model.model)
        builds = config_builds(config, model)
        if len(builds) > 1:
            route["builds"] = [{"provider": build.provider.name, "model_id": build.model_id,
                                "provider_entry": _provider_entry(build.provider),
                                **({"max_tokens": build.max_tokens} if build.max_tokens else {}),
                                **({"context_tokens": build.context_tokens} if build.context_tokens else {})}
                               for build in builds]
        key = overflow_key(config, model) if overflow else None
        if key:
            route["overflow"] = Factory._model_route(config, config.model(key), driver, overflow=False)
        return route

    def _model_config(self, route: dict, build: int = 0) -> ModelConfig:
        """The ModelConfig a route runs on (on its build-th build), with no fallback (section 20).

        A snapshot route runs on its own model_entry and provider_entry (a build's on that build);
        only secrets are read live (the api key at call time, secret-looking headers from the live
        provider of the same name and base_url). A route from an older job.json without them uses
        the live config's entry for route.model with the route's provider, model id and extra_body,
        and StepError when that entry or provider is gone.
        """
        if isinstance(route.get("model_entry"), dict) and isinstance(route.get("provider_entry"), dict):
            return self._snapshot_model(route, build)
        ref = str(route["model"])
        if not isinstance(_section(self.config.raw, "models").get(ref), dict) and route.get("model_id") != ref:
            raise StepError(f"model {ref!r} is no longer in the config; add it back or change the job's routing")
        try:
            base = self.config.model(ref)
            provider = self.config.provider(str(route["provider"]))
        except (KeyError, ValueError) as exc:
            raise StepError(f"cannot run on model {ref!r}: {_message(exc)}") from None
        history_edits = base.allow_history_edits if provider.type == base.provider.type \
            else provider.type != "anthropic"
        return dataclasses.replace(base, provider=provider, model=str(route["model_id"]),
                                   extra_body=copy.deepcopy(route.get("extra_body") or {}),
                                   allow_history_edits=history_edits)

    def _snapshot_model(self, route: dict, build: int = 0) -> ModelConfig:
        """The ModelConfig a snapshot route describes, on its build-th build (StepError if malformed)."""
        entry = route["model_entry"]
        try:
            name, saved, model_id, limits = str(route["provider"]), route["provider_entry"], route["model_id"], {}
            builds = route.get("builds")
            if build and isinstance(builds, list) and 0 < build < len(builds):
                limits = builds[build]
                name, saved, model_id = str(limits["provider"]), limits["provider_entry"], limits["model_id"]
            provider = self._provider_from_entry(name, saved)
            context = limits.get("context_tokens") or entry.get("context_tokens")
            history_edits = entry.get("allow_history_edits")
            if provider.type != route["provider_entry"].get("type"):
                history_edits = None   # resolved for the entry's own provider type: a build of another type
                                       # gets that type's default
            return ModelConfig(
                key=str(route["model"]), provider=provider, model=str(model_id),
                max_tokens=int(limits.get("max_tokens") or entry.get("max_tokens") or 32768),
                stream=entry.get("stream") is not False,
                extra_body=copy.deepcopy(route.get("extra_body") or {}),
                context_tokens=int(context) if context is not None else None,
                keep_reasoning=entry.get("keep_reasoning") is not False,
                allow_history_edits=history_edits if isinstance(history_edits, bool) else None,
                eager_tools=entry.get("eager_tools") is True,
                family=str(route["family"]) if isinstance(route.get("family"), str) and route["family"] else None,
                **{field: float(entry[field]) for field in PRICE_FIELDS if entry.get(field) is not None})
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise StepError(f"the job's route on model {route.get('model')!r} is malformed: {_message(exc)}; "
                            f"change the job's routing") from None

    def _provider_from_entry(self, name: str, saved: dict) -> ProviderConfig:
        """A snapshot's provider_entry as a ProviderConfig, with the live provider's secret-looking headers."""
        base_url = str(saved["base_url"])
        headers = {str(k): str(v) for k, v in (saved.get("headers") or {}).items()}
        concurrency = saved.get("max_concurrency")
        wait, speed = saved.get("overflow_after_seconds"), saved.get("tokens_per_second")
        return ProviderConfig(
            name=name, type=str(saved["type"]), base_url=base_url,
            api_key_env=saved.get("api_key_env") or None, timeout=float(saved.get("timeout") or 900),
            headers={**headers, **self._live_secret_headers(name, base_url)},
            auth=str(saved.get("auth") or ""), api_key_file=saved.get("api_key_file") or None,
            max_concurrency=int(concurrency) if concurrency is not None else None,
            concurrency_scope=str(saved.get("concurrency_scope") or "call"),
            kind=saved.get("kind") or None, loaded_model_url=saved.get("loaded_model_url") or None,
            allow_model_swap=saved.get("allow_model_swap") is True, swap_back=saved.get("swap_back") is True,
            overflow_after_seconds=float(wait) if wait is not None else None,
            tokens_per_second=float(speed) if speed is not None else None,
            **self._custody_of(name, base_url, saved))

    def _custody_of(self, name: str, base_url: str, saved: dict) -> dict:
        """{lazy_load, custody} for a snapshot's provider: the live provider's while it is the same server, else
        the custody of any live provider entry on that server (custody follows the server, not the job or a
        name: a server put under custody mid-job, or a provider renamed since, is not bypassed by an older
        snapshot), else the snapshot's own (section 37)."""
        try:
            live = self.config.provider(name)
        except (KeyError, ValueError):
            live = None
        if live is not None and server_of(live.base_url) == server_of(base_url):
            return {"lazy_load": live.lazy_load, "custody": live.custody}
        found = custody_on_server(self.config.raw.get("providers"), base_url)
        if found is not None:
            _, settings, loads = found
            return {"lazy_load": saved.get("lazy_load") is True or loads, "custody": settings}
        try:
            settings = parse_custody(saved.get("custody"), f"the snapshot's provider {name}.custody")
        except ValueError as exc:
            raise StepError(f"{exc}; change the job's routing") from None
        return {"lazy_load": saved.get("lazy_load") is True, "custody": settings}

    def _live_secret_headers(self, name: str, base_url: str) -> dict[str, str]:
        """Secret-looking headers of the live provider `name`, only while it still points at base_url."""
        try:
            live = self.config.provider(name)
        except (KeyError, ValueError):
            return {}
        if live.base_url.rstrip("/") != base_url.rstrip("/"):
            return {}
        return {k: v for k, v in live.headers.items() if _secret_header(k, v)}

    # -- tracked items (section 32) ------------------------------------------------------

    def _housekeeping(self) -> HousekeepingConfig:
        try:
            return self.config.housekeeping()
        except ValueError:
            return HousekeepingConfig()

    def _items_store(self, job: dict) -> tracker.ItemStore:
        scope = tracker.job_scope(job["repo"], job["id"], handoff=bool(job.get("from_branch")))
        return tracker.ItemStore(Path(job["job_dir"]), scope, max_proposals=self._max_proposals())

    def _followups_store(self, repo: str) -> tracker.ItemStore:
        return tracker.ItemStore(self.store.followups_dir(repo), tracker.followups_scope(repo), name="followups",
                                 md=False, max_proposals=self._max_proposals())

    def _max_proposals(self) -> int:
        return self._housekeeping().max_proposals_per_scope

    def _item_repos(self, repo: str | None) -> list[str]:
        if repo is not None:
            self.store.repo_dir(repo)
            return [repo]
        root = self.store.jobs_root
        return sorted(p.name for p in root.iterdir() if p.is_dir()) if root.is_dir() else []

    def _item_secrets(self) -> list[str]:
        return [*self.secret_values(), *env_secret_values()]

    def _system_manager(self) -> HousekeepingManager:
        with self._lock:
            if self._system is None:
                self._system = HousekeepingManager(self.config.data_dir, self.bus, self._item_secrets,
                                                   max_proposals=self._max_proposals())
            return self._system

    def _drift_manager(self) -> DriftSources:
        system = self._system_manager()
        with self._lock:
            if self._drift is None:
                self._drift = DriftSources(self.config, system, self.bus)
            return self._drift

    def _snapraid_manager(self) -> Any:
        from .snapraid import SnapraidPanel

        system = self._system_manager()
        notifier = self._notify_manager()
        with self._lock:
            if self._snapraid is None:
                self._snapraid = SnapraidPanel(self.config, system, notifier, self.bus)
            return self._snapraid

    def _agent_tokens(self) -> AgentTokens:
        with self._lock:
            if self._tokens is None:
                self._tokens = AgentTokens(self.config.data_dir)
            return self._tokens

    @staticmethod
    def _parse_ref(ref: str, item: bool | None = None) -> tracker.Ref:
        """items.parse_ref, refusing a ref of another host (KeyError: this harness holds host local's items)."""
        parsed = tracker.parse_ref(ref, item=item)
        if parsed.host != tracker.LOCAL_HOST:
            raise KeyError(f"host {parsed.host!r}: this harness keeps only this machine's items (host local)")
        return parsed

    @contextlib.contextmanager
    def _scope_items(self, store: tracker.ItemStore, timeout: float | None = None) -> Iterator[tracker.Ledger]:
        """A transaction on a scope the Factory writes (follow-ups); items.updated after a change."""
        with store.transaction(timeout=timeout, secrets=self._item_secrets()) as ledger:
            yield ledger
        if ledger.dirty:
            self.bus.publish("items.updated", {"ref": store.scope.ref, "summary": ledger.summary(),
                                               "changed": list(ledger.changed)}, repo=store.scope.repo)

    @contextlib.contextmanager
    def _job_items(self, job: dict, timeout: float | None = None) -> Iterator[tracker.Ledger]:
        """A transaction on the job's ledger; after a change, job.items and items.updated follow."""
        store = self._items_store(job)
        with store.transaction(timeout=timeout, secrets=self._item_secrets()) as ledger:
            yield ledger
        if ledger.dirty:
            self._items_changed(job, ledger.summary(), list(ledger.changed))

    @contextlib.contextmanager
    def _busy_as_409(self) -> Iterator[None]:
        try:
            yield
        except tracker.ItemsBusy as exc:
            raise ActionError(str(exc)) from None

    def _items_changed(self, job: dict, summary: dict, changed: list[str], **extra: Any) -> None:
        with self._job_lock(_key(job)):
            if job.get("items") != summary:
                self._update(job, items=summary)
        self.bus.publish("items.updated", {"ref": tracker.job_scope(job["repo"], job["id"]).ref, "summary": summary,
                                           "changed": changed, **extra}, repo=job["repo"], job=job["id"])

    def _items_context(self, job: dict, agent_name: str, round_: int | None,
                       output: Path | None) -> tracker.ItemsContext | None:
        """The run's view of the job's items (journal sessions/<agent>.items.jsonl), or None when the ledger
        cannot be read (the run goes on without it, and the next ingest reports why)."""
        store = self._items_store(job)
        def changed(ledger: tracker.Ledger) -> None:
            self._items_changed(job, ledger.summary(), list(ledger.changed))

        try:
            return tracker.ItemsContext(store, agent_name, Path(job["job_dir"]) / _journal_rel(agent_name),
                                        settings=self._housekeeping(), round=round_,
                                        answer_file=str(output) if output is not None else None,
                                        builder=agent_name == "builder", contract=_contract_kind(agent_name),
                                        output_files=[output] if output is not None else [],
                                        secrets=self._item_secrets(), on_change=changed)
        except (OSError, ValueError) as exc:
            self._log(job, "warn", f"{agent_name} runs without its tracked items: {exc}")
            return None

    def _render_items_md(self, job: dict, agent_name: str | None) -> None:
        """Rewrite items.md for the run about to start (agent None: a review round); none while no item exists."""
        store = self._items_store(job)
        try:
            ledger = store.ledger()
            if ledger.items or (store.md_path is not None and store.md_path.exists()):
                store.write_md(tracker.render_md(ledger, agent_name, title=f"job {job['id']}"))
        except (OSError, ValueError) as exc:
            self._log(job, "warn", f"could not write items.md: {exc}")

    def _items_prompt(self, job: dict, agent_name: str) -> list[str]:
        """The prompt's items lines for agent_name ([] while the job has no items)."""
        try:
            ledger = self._items_store(job).ledger()
        except (OSError, ValueError):
            return []
        output = _output_file(job, agent_name, job.get("round"))
        return tracker.prompt_lines(ledger, agent_name, Path(job["job_dir"]) / "items.md",
                                    answer_file=str(output) if output is not None else None)

    def _evidence_checker(self, job: dict) -> Callable[[dict], tuple[str, str]]:
        try:
            repo = self.config.repo(job["repo"])
        except KeyError:
            return lambda entry: ("unchecked", "the repo is no longer configured")
        branch = job.get("branch") if gitops.branch_exists(repo.path, str(job.get("branch") or "")) else None
        return tracker.git_checker(repo.path, job.get("base"), branch, Path(job["worktree"]))

    def _ingest_run(self, job: dict, agent_name: str, round_: int | None, result: AgentRun, output: Path | None,
                    run: str, complete: bool = True) -> None:
        """After one run: the step's contract table (build.md's, decision.md's; the spec step takes spec.md in
        itself), the agent's journal, its ## Items lines, the sweep of its file and final text, then the
        evidence checks; refusals, and its items a failed check reopened, are kept for its next prompt. A run
        that did not end normally (complete False) gives only its journal: each Items call was validated when
        it was made, but its files are not its final word. A full scope is recorded for _drive, which stops
        the job once the step's own checks have run."""
        hk = self._housekeeping()
        job_dir = Path(job["job_dir"])
        text = _read_text(output) if output is not None and complete else ""
        rel = output.relative_to(job_dir).as_posix() if output is not None else "reply"
        exists = _path_exists(Path(job["worktree"]))
        unparsed: list[tuple[int, str]] = []
        full: tracker.ItemsFull | None = None
        try:
            with self._job_items(job) as ledger:
                notices: list[str] = []
                try:
                    if complete and agent_name == "builder":
                        notices += tracker.ingest_build(ledger, text, agent_name, rel, round=round_, path_exists=exists)
                    elif complete and agent_name == "approver":
                        notices += tracker.ingest_decision(ledger, text, agent_name, rel, round=round_)
                    aliases: dict[str, str] = {}
                    notices += tracker.apply_journal(ledger, job_dir / _journal_rel(agent_name), agent_name,
                                                     round=round_, aliases=aliases)
                    if complete:
                        found, unparsed = tracker.apply_section(ledger, text, agent_name, rel, round=round_,
                                                                path_exists=exists, aliases=aliases)
                        notices += found
                    if complete and hk.sweep != "off":   # the final text and the file's newest Log entry only
                        texts = ([] if agent_name == "spec-writer" else [(rel, tracker.newest_log_entry(text))]) \
                            + [("reply", result.final_text)]
                        tracker.apply_sweep(ledger, texts, agent_name, round=round_, limit=hk.max_proposals_per_run,
                                            suppress=self.store.sweep_suppressed(job["repo"]), run=run)
                except tracker.ItemsFull as exc:
                    full = exc
                notices += self._move_later(job, ledger)
                reopened = tracker.check_evidence(ledger, self._evidence_checker(job))
                notices += [f"{item_id} is To do again: {ledger.get(item_id).get('reason')}" for item_id in reopened
                            if ledger.get(item_id).get("owner") == agent_name]
                ledger.set_notices(agent_name, notices)
        except (OSError, ValueError) as exc:
            raise StepError(f"tracked items: {exc}") from None
        self._publish_unparsed(job, [(agent_name, rel, line, raw) for line, raw in unparsed])
        if full is not None:
            self._note_items_full(job, str(full))

    def _move_later(self, job: dict, ledger: tracker.Ledger) -> list[str]:
        """Items made Later in place in the job's ledger (an agent's own defer) move to the repo's follow-ups,
        as a person's defer does (inside the ledger's transaction); what the agent should hear. Best effort:
        when the follow-ups cannot be written the item stays Later here and moves at the job's end."""
        due = tracker.add_days(tracker.today(), self._housekeeping().followup_due_days)
        try:
            return tracker.move_later(ledger, lambda copies: self.add_followups(job["repo"], copies), due)
        except (OSError, ValueError) as exc:
            self._log(job, "warn", f"could not move Later items to the follow-ups yet: {exc}")
            return []

    def _ingest_spec(self, job: dict) -> None:
        """spec.md's acceptance criteria and (product) decisions, once the spec step has a spec; a spec written
        again (a retried step) is followed: re-worded criteria take the new text, removed ones are dropped. A
        decision whose line a person already confirmed is proven by that confirmation."""
        text = _read_text(Path(job["job_dir"]) / "spec.md")
        with self._job_lock(_key(job)):
            confirmed = _confirmed(job)
        try:
            with self._job_items(job) as ledger:
                try:
                    tracker.ingest_spec(ledger, text, "spec.md")
                except tracker.ItemsFull as exc:
                    self._note_items_full(job, str(exc))
                self._answer_decisions(job, confirmed, ledger)   # a person may have confirmed before the ingest
        except (OSError, ValueError) as exc:
            raise StepError(f"tracked items: {exc}") from None

    def _note_items_full(self, job: dict, message: str) -> None:
        with self._lock:
            self._items_full.setdefault(_key(job), message)

    def _has_items_full(self, job: dict) -> bool:
        with self._lock:
            return _key(job) in self._items_full

    def _take_items_full(self, key: JobKey) -> str | None:
        with self._lock:
            return self._items_full.pop(key, None)

    def _stop_items_full(self, job: dict, message: str) -> None:
        """The job's items reached the cap during the step that just ended: it stops for a human (nothing is
        dropped). A stop the step made itself keeps its reason, which then names the cap too."""
        with self._job_lock(_key(job)):
            stage = job.get("stage")
            if stage in ACTIVE_STAGES:
                self._update(job, stage="needs-human", reason=message, detail=message, **_settled_reviews(job))
            elif stage == "needs-human" and message not in str(job.get("reason") or ""):
                reason = _with_full(str(job.get("reason") or ""), message)
                self._update(job, reason=reason, detail=reason)

    def _ingest_round(self, job: dict, n: int, finals: dict[str, str], forged: set[str]) -> None:
        """After WAIT FOR ALL: each valid review's findings and notes, then its reviewer's journal (unless
        forged), ## Items lines and sweep, then the evidence checks. A full scope is recorded, not raised."""
        hk = self._housekeeping()
        job_dir = Path(job["job_dir"])
        unparsed: list[tuple[str, str, int, str]] = []
        full: tracker.ItemsFull | None = None
        suppress = self.store.sweep_suppressed(job["repo"])
        try:
            with self._job_items(job) as ledger:
                try:
                    for reviewer in job["reviewers"]:
                        agent = f"reviewer-{reviewer}"
                        journal = job_dir / _journal_rel(agent)
                        verdict = job["reviews"].get(reviewer)
                        if agent in forged or verdict == "ERROR":
                            tracker.skip_journal(ledger, journal, agent)
                            continue
                        if verdict not in REVIEWED:
                            continue
                        rel = f"round-{n}/review-{reviewer}.md"
                        text = _read_text(job_dir / rel)
                        tracker.ingest_review(ledger, text, reviewer, round=n, verdict=verdict, file=rel)
                        aliases: dict[str, str] = {}
                        must = verdict == "CHANGES"   # a review that passes requires nothing: its adds are suggestions
                        notices = tracker.apply_journal(ledger, journal, agent, round=n, aliases=aliases,
                                                        blocking_allowed=must)
                        found, lines = tracker.apply_section(ledger, text, agent, rel, round=n, aliases=aliases,
                                                             blocking_allowed=must)
                        unparsed += [(agent, rel, line, raw) for line, raw in lines]
                        if hk.sweep != "off":
                            tracker.apply_sweep(ledger, [(rel, tracker.newest_log_entry(text)),
                                                         ("reply", finals.get(reviewer, ""))], agent,
                                                round=n, limit=hk.max_proposals_per_run, suppress=suppress)
                        ledger.set_notices(agent, notices + found)
                except tracker.ItemsFull as exc:
                    full = exc
                self._move_later(job, ledger)
                tracker.check_evidence(ledger, self._evidence_checker(job))
        except (OSError, ValueError) as exc:
            raise StepError(f"tracked items: {exc}") from None
        self._publish_unparsed(job, unparsed)
        if full is not None:
            self._note_items_full(job, str(full))   # _review_round finishes its checks, then _drive stops

    def _publish_unparsed(self, job: dict, lines: list[tuple[str, str, int, str]]) -> None:
        ref = tracker.job_scope(job["repo"], job["id"]).ref
        for agent, file, line, raw in lines:
            self.bus.publish("items.unparsed", {"ref": ref, "agent": agent, "file": file, "line": line,
                                                "text": tracker.clip(raw, tracker.QUOTE_CHARS)},
                             repo=job["repo"], job=job["id"], agent=agent,
                             log_path=Path(job["job_dir"]) / "logs" / "events.jsonl")

    def _untouched_blocking(self, job: dict, n: int) -> list[str]:
        """Must-do items the builder owns that it did not touch in round n (the build check's items rule);
        [] when housekeeping is off or the round was not built by the builder (a chat hand-off)."""
        if not self._housekeeping().enabled or job.get("build_request") is None:
            return []
        try:
            ledger = self._items_store(job).ledger()
        except (OSError, ValueError) as exc:
            raise StepError(f"tracked items: {exc}") from None
        return [item["id"] for item in ledger.items
                if item.get("owner") == "builder" and item.get("blocking")
                and item.get("status") in ("proposed", "open", "doing") and item.get("touched_round") != n]

    def _count_stale(self, job: dict, n: int) -> None:
        threshold = self._housekeeping().stale_rounds
        try:
            with self._job_items(job) as ledger:
                ledger.count_stale("builder", n, threshold)
        except (OSError, ValueError) as exc:
            self._log(job, "warn", f"could not count stale items: {exc}")

    def _blocking_waiting(self, job: dict) -> list[str]:
        try:
            ledger = self._items_store(job).ledger()
        except (OSError, ValueError):
            return []
        return [item["id"] for item in ledger.items if tracker.holds_gate(item)]

    def _items_problem(self, job: dict) -> str | None:
        """The gate's items rule: no must-do item may be open, doing, done but not proven, or suggested, and a
        (product) decision counts as proven only when a person answered it (items.holds_gate)."""
        if not self._housekeeping().enabled:
            return None
        try:
            ledger = self._items_store(job).ledger()
        except (OSError, ValueError) as exc:
            return f"tracked items cannot be read: {exc}"
        waiting = [item for item in ledger.items if tracker.holds_gate(item)]
        if not waiting:
            return None

        def label(item: dict) -> str:
            if item.get("status") in tracker.CLOSED:
                return f"{item['id']} (product decision proven by {item.get('verified_by')}, not a person)"
            claim = f": {tracker.MET_WITHOUT_EVIDENCE}" if item.get("met_claimed") else ""
            return f"{item['id']} ({tracker.WORDS[item['status']]}, {item['owner']}{claim})"

        shown = ", ".join(label(item) for item in waiting[:5])
        more = " …" if len(waiting) > 5 else ""
        return f"{tracker.plural(len(waiting), 'blocking item')} not proven: {shown}{more}"

    def _job_ended(self, job: dict) -> None:
        """A job reached merged, delivered, rejected or cancelled: its open items move on (carry-forward) and a
        follow-up it was made for is proven (merged) or opened again (rejected, cancelled). Never raises."""
        try:
            self._carry_forward(job)
        except Exception as exc:  # noqa: BLE001 - the job has ended either way; nothing is lost, it stays open
            self._log(job, "error", f"could not move the job's open items on: {type(exc).__name__}: {exc}")
        try:
            self._settle_followups(job)
        except Exception as exc:  # noqa: BLE001
            self._log(job, "error", f"could not update the follow-up of this job: {type(exc).__name__}: {exc}")
        try:
            self._codex_job_ended(job)
        except Exception as exc:  # noqa: BLE001
            self._log(job, "error", f"could not remove the job's Codex home: {type(exc).__name__}: {exc}")

    def _carry_forward(self, job: dict) -> None:
        """Copy every item still open to the repo's follow-ups (or, for a plan's job, the plan); the job's
        item becomes Later with links.followup. An item already Later that never moved on (unmoved_later) goes
        with them. Idempotent: closed items are left alone."""
        stage = job.get("stage")
        store = self._items_store(job)
        if stage not in END_STAGES or not store.path.is_file():
            return
        override = job.get("approved_by") == "human"
        reason = {"merged": "merged by human override while open" if override else "job merged",
                  DELIVERED: "delivered by human override while open" if override
                  else f"job delivered as a {_delivery(job)}", "rejected": "job rejected",
                  "cancelled": "job cancelled"}[stage]
        plan_id = None
        if self._plans is not None:
            found = self._plans.plan_of_job(job)
            plan_id = found if isinstance(found, str) else None
        due = tracker.add_days(tracker.today(), self._housekeeping().followup_due_days)
        moved = 0
        target = ""
        with self._job_items(job) as ledger:
            later = {item["id"] for item in tracker.unmoved_later(ledger)}   # made Later in place: moves too
            waiting = [item for item in ledger.items if item.get("status") not in tracker.CLOSED
                       or item["id"] in later]
            if not waiting:
                return
            copies = [tracker.carry_copy(item, ledger.scope, due=(item.get("due") or due) if item["id"] in later
                                         else due, reason=reason) for item in waiting]
            refs: Any = None
            if plan_id is not None:
                refs = self._plans.carry_in(job["repo"], plan_id, copies)
                target = f"plan {plan_id}"
            if not (isinstance(refs, list) and len(refs) == len(copies)):
                refs = self.add_followups(job["repo"], copies)
                target = "follow-ups"
            for item, ref in zip(waiting, refs):
                if item["id"] in later:
                    ledger.set_links(item["id"], followup=ref)
                else:
                    ledger.change(tracker.FACTORY, item["id"], "deferred", reason=reason, links={"followup": ref})
            moved = len(waiting)
        self._log(job, "info", f"{tracker.plural(moved, 'open item')} moved to {target} ({reason})")
        self.bus.publish("items.carried", {"ref": tracker.job_scope(job["repo"], job["id"]).ref, "count": moved,
                                           "to": target, "reason": reason}, repo=job["repo"], job=job["id"])

    def _settle_followups(self, job: dict) -> None:
        """The follow-ups being worked on by this job: proven with job evidence when it merged, To do again
        when it was rejected or cancelled."""
        stage = job.get("stage")
        if stage not in ("merged", "rejected", "cancelled"):
            return
        store = self._followups_store(job["repo"])
        if not store.path.is_file():
            return
        with self._scope_items(store) as ledger:
            for item in ledger.items:
                if item.get("status") != "doing" or (item.get("links") or {}).get("job") != job["id"]:
                    continue
                if stage == "merged":
                    ledger.change(tracker.FACTORY, item["id"], "verified", note=f"job {job['id']} merged",
                                  evidence=[{"kind": "job", "ref": job["id"],
                                             "detail": str(job.get("merged_commit") or ""), "check": "ok"}])
                else:
                    ledger.change(tracker.FACTORY, item["id"], "open", reason=f"job {job['id']} was {stage}")

    def _followup_action(self, repo: str, item_id: str, action: str, by: str = tracker.HUMAN,
                         **kwargs: Any) -> dict:
        self.store.repo_dir(repo)
        store = self._followups_store(repo)
        if not store.path.is_file():
            raise KeyError(f"unknown item {store.scope.item_ref(item_id)}")
        with self._busy_as_409():
            if action == "make-job":
                return self._make_job(repo, store, item_id)
            with self._scope_items(store, BUSY_WAIT) as ledger:
                if by == tracker.HUMAN:
                    item = tracker.human_action(ledger, item_id, action, **kwargs)
                else:
                    item = agent_update(ledger, by, item_id, kwargs.get("to"), evidence=kwargs.get("evidence"),
                                        reason=kwargs.get("reason") or kwargs.get("note"))
        self._learn_dismissal(repo, item, action)
        return {"item": tracker.decorate(item, store.scope, now=utc_now())}

    def _make_job(self, repo: str, store: tracker.ItemStore, item_id: str) -> dict:
        """Submit a job for a follow-up; the item becomes Working on it with links.job. All of it under the
        follow-ups' lock, so a second click waits and is refused while that job has not ended, and a submit
        that fails changes nothing."""
        with self._scope_items(store, BUSY_WAIT) as ledger:
            item = ledger.get(item_id)
            status = item.get("status")
            if status in tracker.CLOSED or status == "done":
                raise ValueError(f"{item_id} is {tracker.WORDS[status]}: reopen it before making a job of it")
            linked = (item.get("links") or {}).get("job")
            if status == "doing" and linked and self._job_alive(repo, linked):
                raise ValueError(f"{item_id} is already being worked on by job {linked}; cancel or reject that job "
                                 "before making another")
            origin = _origin_job(item)
            feature = f"Follow-up {item_id}{f' from job {origin}' if origin else ''}: {item.get('title')}"
            if item.get("detail") and item.get("detail") != item.get("title"):
                feature += f"\n\n{item['detail']}"
            job = self.submit(repo, feature)
            if status == "doing":   # its earlier job is gone: this one takes its place
                ledger.set_links(item_id, job=job["id"])
            else:
                ledger.change(tracker.HUMAN, item_id, "doing", note=f"job {job['id']} works on it",
                              links={"job": job["id"]})
            done = ledger.get(item_id)
        return {"item": tracker.decorate(done, store.scope, now=utc_now()), "job": job}

    def _job_alive(self, repo: str, job_id: str) -> bool:
        """Whether a job exists and has not ended (lock-free: the caller may hold a scope's lock)."""
        try:
            return self._entry((repo, str(job_id))).get("stage") not in END_STAGES
        except (KeyError, ValueError, OSError):
            return False

    def _learn_dismissal(self, repo: str, item: dict, action: str) -> None:
        """A dismissed sweep suggestion ("not a promise") is not proposed again in this repo."""
        source = item.get("source") or {}
        if action == "dismiss" and source.get("type") == "sweep" and source.get("hash"):
            try:
                self.store.suppress_sweep(repo, str(source["hash"]))
            except (OSError, ValueError):
                pass

    # -- job state -------------------------------------------------------------------

    def _set(self, job: dict, **changes: Any) -> None:
        """Change and save a job from a worker step; _Cancelled if the job has been cancelled."""
        with self._job_lock(_key(job)):
            self._check_cancel(job)
            self._update(job, **changes)

    def _update(self, job: dict, **changes: Any) -> None:
        with self._job_lock(_key(job)):
            job.update(changes)
            self._save(job)

    def _save(self, job: dict) -> None:
        """Write job.json and publish job.updated (caller holds the job lock).

        job.json found different from what the last save wrote was changed by someone else
        (only the orchestrator writes it): it is overwritten, and the next write-policy check
        reports it. Best effort: a write landing between that check and the save is overwritten
        unseen, which also undoes it.
        """
        key = _key(job)
        path = Path(job["job_dir"]) / "job.json"
        with self._lock:
            written = self._job_json.get(key)
        if written is not None and not _same(_state_of(path), written):
            with self._lock:
                found = self._tampered.setdefault(key, [])
                if "job.json" not in found:
                    found.append("job.json")
            self._log(job, "warn", "job.json was changed outside the orchestrator; it is rewritten")
        self.store.save(job)
        written = _state_of(path)
        with self._lock:
            if written is None:
                self._job_json.pop(key, None)
            else:
                self._job_json[key] = written
        self.bus.publish("job.updated", {"job": job}, repo=job["repo"], job=job["id"])
        if self._stages.get(key) != job.get("stage"):
            self._stages[key] = job.get("stage")
            self._log(job, "info", f"stage {job.get('stage')}" + (f": {job['detail']}" if job.get("detail") else ""))
        with self._lock:
            self._changed.notify_all()

    def _set_review(self, job: dict, reviewer: str, verdict: str, n: int) -> None:
        """Record one reviewer's state. Once the job is cancelled or stopping, a finished verdict
        (PASS/CHANGES) is still recorded and anything else leaves the reviewer pending."""
        with self._job_lock(_key(job)):
            if self._cancel_event(_key(job)).is_set() and verdict not in REVIEWED:
                verdict = "pending"
            job["reviews"][reviewer] = verdict
            if job.get("stage") == "review":
                job["detail"] = _review_detail(n, job["reviews"])
            self._save(job)

    def _fail(self, job: dict, reason: str) -> None:
        """failed (retryable at the step it was in), unless a human already stopped the job."""
        with self._job_lock(_key(job)):
            if job.get("stage") in ACTIVE_STAGES:
                self._mark_failed(job, reason)

    def _mark_failed(self, job: dict, reason: str) -> None:
        failed_step = STEPS.get(job.get("step") or "setup", STEPS["setup"])[1]
        self._update(job, stage="failed", failed_step=failed_step, reason=reason, error=reason, detail=reason,
                     **_settled_reviews(job))

    def _requeue(self, job: dict, step: str, **changes: Any) -> None:
        """Put a stopped job back on the line at step (caller holds the job lock)."""
        self._update(job, stage=STEPS[step][0], step=step, reason=None, error=None, failed_step=None, **changes)
        self._enqueue(job, step)

    def _enqueue(self, job: dict, step: str) -> None:
        key = _key(job)
        with self._lock:
            self._cancels[key] = threading.Event()
        self._queue.put((key, step))

    def _check_cancel(self, job: dict) -> None:
        if self._cancel_event(_key(job)).is_set():
            raise _Cancelled()

    def _require_idle(self, key: JobKey) -> None:
        with self._lock:
            if key in self._driving:
                raise ActionError(f"{key[0]}/{key[1]} is still finishing its last step; try again")

    def _target(self, repo: str, job_id: str) -> tuple[dict, JobKey]:
        key = (repo, job_id)
        return self._entry(key), key

    def _entry(self, key: JobKey) -> dict:
        """The live job dict (loaded from job.json once; KeyError if unknown)."""
        with self._lock:
            job = self._jobs.get(key)
            if job is None:
                job = self._jobs[key] = self.store.load(*key)
                self._stages[key] = job.get("stage")
            return job

    def _job_lock(self, key: JobKey) -> threading.RLock:
        with self._lock:
            return self._job_locks.setdefault(key, threading.RLock())

    def _repo_lock(self, repo: str) -> threading.RLock:
        with self._lock:
            return self._repo_locks.setdefault(repo, threading.RLock())

    def _cancel_event(self, key: JobKey) -> threading.Event:
        with self._lock:
            return self._cancels.setdefault(key, threading.Event())

    def _repo(self, job: dict) -> RepoConfig:
        try:
            return self.config.repo(job["repo"])
        except KeyError:
            raise StepError(f"repo {job['repo']!r} is no longer configured") from None

    def _agent_defs(self, repo: str) -> dict[str, AgentDef]:
        try:
            return self.agents(repo)
        except (KeyError, ValueError) as exc:
            raise StepError(_message(exc)) from None

    def _tmp_dir(self) -> Path:
        return self.store.data_dir / "tmp"

    def _key_names(self) -> list[str]:
        return [str(entry["api_key_env"]) for entry in _section(self.config.raw, "providers").values()
                if isinstance(entry, dict) and entry.get("api_key_env")]

    def _log(self, job: dict, level: str, message: str) -> None:
        """A `log` event for the job, also appended to its logs/events.jsonl."""
        self.bus.publish("log", {"level": level, "message": message}, repo=job["repo"], job=job["id"],
                         log_path=Path(job["job_dir"]) / "logs" / "events.jsonl")


def new_factory(config: Config, **kwargs: Any) -> tuple[Factory, EventBus]:
    """A Factory and the EventBus it publishes on, the bus redacting factory.secret_values().

    The bus has to exist before the Factory, so its secrets_provider defers to the factory once
    it is set (and to config.secret_values() before that, or for a stand-in without
    secret_values). `factory serve` and `factory run` build theirs here.
    """
    holder: list[Any] = []

    def secrets() -> list[str]:
        provider = getattr(holder[0], "secret_values", None) if holder else None
        return list(provider()) if callable(provider) else config.secret_values()

    bus = EventBus(secrets_provider=secrets)
    from .wakes import instance_name

    bus.instance = instance_name(config)
    tracker.INSTANCE["name"] = bus.instance
    holder.append(Factory(config, bus, **kwargs))
    return holder[0], bus


# -- module helpers ---------------------------------------------------------------


def _origin_job(item: dict) -> str | None:
    """The job a carried follow-up came from (its source ref), not a job made for it since."""
    source = item.get("source") or {}
    if source.get("type") == "carry":
        with contextlib.suppress(ValueError):
            ref = tracker.parse_ref(source.get("ref"))
            return ref.id if ref.kind == "job" else None
    return None


def _with_full(reason: str, full: str | None) -> str:
    """A stop reason that also names the item cap the step ran into, when it did."""
    return f"{reason}; {full}" if full and reason else full or reason


def _key(job: dict) -> JobKey:
    return job["repo"], job["id"]


def _message(exc: BaseException) -> str:
    """str(exc), without the quotes KeyError adds."""
    return str(exc.args[0]) if isinstance(exc, KeyError) and exc.args else str(exc)


def _section(raw: dict, name: str) -> dict:
    value = raw.get(name)
    return value if isinstance(value, dict) else {}


def _check_overrides(routing: dict) -> dict:
    """routing as {agent: {model?, driver?, extra_body?, effort?, escalate?, reason?}}; ValueError for
    anything else."""
    for name, entry in routing.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            raise ValueError("routing must map agent names to {model, driver, extra_body} objects")
        unknown = sorted(set(entry) - ROUTE_KEYS)
        if unknown:
            raise ValueError(f"routing for {name}: unknown keys {', '.join(map(str, unknown))}")
        for field in ("model", "driver"):
            if entry.get(field) is not None and not isinstance(entry[field], str):
                raise ValueError(f"routing for {name}: {field} must be a string")
        if entry.get("extra_body") is not None and not isinstance(entry["extra_body"], dict):
            raise ValueError(f"routing for {name}: extra_body must be an object")
        check_placement(entry, f"routing for {name}", agent=name)
    return routing


def _output_file(job: dict, agent_name: str, round_: int | None) -> Path | None:
    """The one job-folder file the agent writes (its contract file), None for other agents."""
    job_dir = Path(job["job_dir"])
    if agent_name == "spec-writer":
        return job_dir / "spec.md"
    if agent_name == "builder":
        return job_dir / "build.md"
    if agent_name == "approver":
        return job_dir / "decision.md"
    if agent_name.startswith("reviewer-") and round_:
        return job_dir / f"round-{round_}" / f"review-{agent_name[len('reviewer-'):]}.md"
    return None


def _rows(found: Any) -> list[dict]:
    return [row for row in found if isinstance(row, dict)] if isinstance(found, list) else []


def _contract_kind(agent_name: str) -> str:
    return {"builder": "build", "approver": "decision", "spec-writer": "spec"}.get(
        agent_name, "review" if agent_name.startswith("reviewer-") else "other")


def _journal_rel(agent_name: str) -> str:
    """An agent's items journal (Items tool calls), relative to the job folder: one of its session siblings."""
    return f"sessions/{agent_name}.items.jsonl"


def _read_text(path: Path) -> str:
    try:
        return read_small_text(path, 1 << 20)
    except (OSError, UnicodeDecodeError):
        return ""


def _path_exists(root: Path) -> Callable[[str], bool]:
    """path → True when it names an existing file or directory inside root (relative to root, or absolute)."""
    base = root.resolve()

    def exists(path: str) -> bool:
        candidate = Path(path)
        target = (candidate if candidate.is_absolute() else base / candidate).resolve()
        return (target == base or base in target.parents) and target.exists()

    return exists


def _write_paths(job: dict, agent_name: str, round_: int | None) -> list[Path]:
    """DESIGN.md section 7: what each factory agent may write."""
    job_dir = Path(job["job_dir"])
    if agent_name == "spec-writer":
        return [job_dir / "spec.md"]
    if agent_name == "builder":
        return [Path(job["worktree"]), job_dir / "build.md"]
    if agent_name == "approver":
        return [job_dir / "decision.md"]
    if agent_name.startswith("reviewer-"):
        return [job_dir / f"round-{round_}" / f"review-{agent_name[len('reviewer-'):]}.md"]
    return []


def _delivery(job: dict) -> str:
    """The job's delivery mode (a job.json from before delivery modes merges)."""
    mode = job.get("delivery")
    return mode if isinstance(mode, str) and mode else "merge"


def _review_problem(job: dict) -> str | None:
    """The gate's first rule: every final-round review is PASS."""
    not_passed = [r for r in job["reviewers"] if job["reviews"].get(r) != "PASS"]
    return f"final review not PASS: {', '.join(not_passed)}" if not_passed else None


def _settled_reviews(job: dict) -> dict:
    """{"reviews": ...} with "running" turned back into "pending", for a job that stops; {} if none."""
    reviews = job.get("reviews")
    if not isinstance(reviews, dict):
        return {}
    return {"reviews": {r: "pending" if v == "running" else v for r, v in reviews.items()}}


# -- routing snapshot helpers --------------------------------------------------------


def _secret_header(name: str, value: Any) -> bool:
    """A header that may carry a credential: a secret-looking name, or a value with an auth scheme."""
    return bool(SECRET_HEADER_RE.search(str(name)) or AUTH_VALUE_RE.match(str(value).strip()))


def _model_entry(model: ModelConfig) -> dict:
    """The model entry's settings for a routing snapshot, its prices only when set."""
    entry = {field: getattr(model, field) for field in MODEL_FIELDS}
    entry.update({field: getattr(model, field) for field in PRICE_FIELDS if getattr(model, field) is not None})
    return entry


def _provider_entry(provider: ProviderConfig) -> dict:
    """The provider's settings for a routing snapshot: no secret-looking header is kept; the placement
    settings only when set."""
    entry = {field: getattr(provider, field) for field in PROVIDER_FIELDS}
    entry["headers"] = {k: v for k, v in (provider.headers or {}).items() if not _secret_header(k, v)}
    entry.update({field: getattr(provider, field) for field in PLACEMENT_FIELDS
                  if getattr(provider, field, None) not in (None, False)})
    if entry.get("custody") is not None:   # the register's settings (no token: only where it is read from)
        entry["custody"] = entry["custody"].to_dict()
    return entry


def _estimated_judge(route: dict, builder: dict[str, str]) -> dict:
    """A judge's route for the estimate when the builder runs on builder's families (routing.judge_choice):
    its own route, marked with its independence when that is reduced or accepted; its independent alternate,
    with independence "alternate", instead_of (the model it moves off) and the alternate's reason."""
    chosen, status = judge_choice(route, builder)
    if status == "alternate":
        left = shared_model(route, builder)
        return {**chosen, "independence": "alternate", "instead_of": route.get("model"),
                "reason": {"kind": "stage-fit", "text": alternate_text(left)}}
    return {**route, "independence": status} if status else route


def _route_provider_entries(route: Any) -> list[Any]:
    """Every provider entry a snapshot route can run on: its own, its builds', its overflow's, each
    escalation rung's and its independent alternate's (with theirs), for redaction."""
    if not isinstance(route, dict):
        return []
    entries = [route.get("provider_entry")]
    entries += [build.get("provider_entry") for build in route.get("builds") or [] if isinstance(build, dict)]
    for nested in [route.get("overflow"), *(route.get("escalate") or []), route.get("independent_alternate")]:
        entries += _route_provider_entries(nested)
    return entries


def _last_run(job: dict, agent_name: str) -> dict | None:
    """The agent's latest entry in job.runs (None before its first run, or in a job.json from before runs)."""
    for entry in reversed(job.get("runs") or []):
        if isinstance(entry, dict) and entry.get("agent") == agent_name:
            return entry
    return None


def _usage_by_agent(job: dict) -> dict[str, dict]:
    """{agent: its usage over the whole job} from job.runs (empty for a job.json from before runs)."""
    totals: dict[str, dict] = {}
    for entry in job.get("runs") or []:
        if isinstance(entry, dict) and isinstance(entry.get("usage"), dict) and entry.get("agent"):
            add_usage(totals.setdefault(str(entry["agent"]), {}), entry["usage"])
    return totals


def _quoted(path: Path) -> str:
    """A file's text for a prompt (at most HANDOVER_CHARS, the cut marked), "" when missing or empty."""
    try:
        text = read_small_text(path, 1 << 20).strip()
    except (OSError, UnicodeDecodeError):
        return ""
    return text if len(text) <= HANDOVER_CHARS else text[:HANDOVER_CHARS] + "\n[… cut short]"


def _positive(value: Any) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _provider_secrets(provider: ProviderConfig) -> list[str]:
    """Values a run must redact that the live config may not know: the provider's key and secret headers."""
    values = [provider.api_key(), *(AUTH_VALUE_RE.sub("", str(v).strip()) for k, v in (provider.headers or {}).items()
                                    if _secret_header(k, v))]
    return [v for v in values if isinstance(v, str) and len(v) >= MIN_SECRET_CHARS]


# -- write-policy helpers (section 7) ------------------------------------------------


class _FileState(NamedTuple):
    """One path of the job folder as the write-policy check saw it."""

    kind: str               # "file", "link" or "other" (FIFO, socket, device)
    size: int
    mtime_ns: int
    digest: str | None      # sha256 of the content (of a link: of its target); None when too big
    data: bytes | None      # the content (of a link: its target) kept for a revert; None when not kept


def _confirmed(job: dict) -> list[str]:
    """The product assumptions a person has confirmed (job.confirmed.lines)."""
    lines = (job.get("confirmed") or {}).get("lines")
    return [line for line in lines if isinstance(line, str)] if isinstance(lines, list) else []


def _is_temp_name(name: str) -> bool:
    """A temporary file of an atomic write (JobStore ".sf-*.tmp", session saves ".<name>.*.tmp")."""
    return name.startswith(".") and name.endswith(".tmp")


def _remove_below(root: Path, rel: str) -> bool:
    """Remove the file rel (a relative POSIX path) below root, never following a symlink on the way (a symlink as the
    last part is removed itself); whether it is gone."""
    parts = PurePosixPath(rel).parts
    if not parts or PurePosixPath(rel).is_absolute() or any(part in (".", "..") for part in parts):
        return False
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        folder = os.open(root, flags)
    except OSError:
        return False
    try:
        for part in parts[:-1]:
            inner = os.open(part, flags, dir_fd=folder)
            os.close(folder)
            folder = inner
        os.unlink(parts[-1], dir_fd=folder)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    finally:
        os.close(folder)
    return True


def _scan(root: Path, keep: bool = False) -> dict[str, _FileState]:
    """The job folder's files for the write-policy check: relative POSIX path → state.

    Left out: the top-level logs/ (appended by the orchestrator during runs), job.json (_save
    checks it) and temporary files of atomic writes. Symlinks are recorded, never followed;
    FIFOs and devices are never opened. keep: also keep small files' contents so a change can
    be reverted (POLICY_KEEP_BYTES per file, POLICY_KEEP_TOTAL in all).
    """
    states: dict[str, _FileState] = {}
    budget = [POLICY_KEEP_TOTAL if keep else 0]
    pending: list[tuple[Path, str]] = [(root, "")]
    while pending:
        directory, prefix = pending.pop()
        try:
            with os.scandir(directory) as found:
                entries = sorted(found, key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            if (not prefix and entry.name in POLICY_SKIP) or _is_temp_name(entry.name):
                continue
            if len(states) >= POLICY_MAX_FILES:
                states[POLICY_OVERFLOW] = _FileState("other", 0, 0, None, None)
                return states
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISDIR(info.st_mode):
                pending.append((Path(entry.path), f"{prefix}{entry.name}/"))
            else:
                states[prefix + entry.name] = _file_state(Path(entry.path), info, budget)
    return states


def _file_state(path: Path, info: os.stat_result, budget: list[int]) -> _FileState:
    if stat.S_ISLNK(info.st_mode):
        try:
            target = os.fsencode(os.readlink(path))
        except OSError:
            target = b""
        return _FileState("link", len(target), info.st_mtime_ns, hashlib.sha256(target).hexdigest(), target)
    if not stat.S_ISREG(info.st_mode):
        return _FileState("other", info.st_size, info.st_mtime_ns, None, None)
    digest = data = None
    if info.st_size <= POLICY_HASH_BYTES:
        keep = min(POLICY_KEEP_BYTES, budget[0]) if info.st_size <= POLICY_KEEP_BYTES else 0
        try:
            digest, data = _read_digest(path, keep)
        except OSError:
            pass
        if data is not None:
            budget[0] -= len(data)
    return _FileState("file", info.st_size, info.st_mtime_ns, digest, data)


def _read_digest(path: Path, keep: int) -> tuple[str | None, bytes | None]:
    """(sha256 of a regular file's first POLICY_HASH_BYTES, its content if it is at most keep bytes)."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY)
    with open(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            return None, None
        digest, kept, total = hashlib.sha256(), bytearray(), 0
        while total <= POLICY_HASH_BYTES:
            chunk = fh.read(1 << 16)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
            if total <= keep:
                kept += chunk
        if total > POLICY_HASH_BYTES:
            return None, None
        return digest.hexdigest(), bytes(kept) if total <= keep else None


def _state_of(path: Path) -> _FileState | None:
    """One path's state (contents not kept), None if it does not exist."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    return _file_state(path, info, [0])


def _same(a: _FileState | None, b: _FileState | None) -> bool:
    """Same content: by digest when both have one, else by kind, size and mtime."""
    if a is None or b is None:
        return a is b
    if a.kind != b.kind:
        return False
    if a.digest is not None and b.digest is not None:
        return a.digest == b.digest
    return (a.size, a.mtime_ns) == (b.size, b.mtime_ns)


def _changed(before: dict[str, _FileState], after: dict[str, _FileState]) -> list[str]:
    """Paths created, changed or removed between two scans, sorted (a parent before its children)."""
    return sorted(rel for rel in before.keys() | after.keys() if not _same(before.get(rel), after.get(rel)))


def _policy_allows(job_dir: Path, write_paths: list[Path], agent_names: list[str]) -> Callable[[str], bool]:
    """rel → True if a run of one of agent_names may write job_dir/rel: inside write_paths, or one of
    its own session files (sessions/<agent>.json and siblings like <agent>.cli.json)."""
    roots = [Path(os.path.abspath(path)) for path in write_paths]
    base = Path(os.path.abspath(job_dir))

    def allows(rel: str) -> bool:
        path = base / rel
        if any(path == root or root in path.parents for root in roots):
            return True
        parts = PurePosixPath(rel).parts
        return (len(parts) == 2 and parts[0] == "sessions"
                and any(parts[1].startswith(f"{name}.") for name in agent_names))

    return allows


def _revert(root: Path, rel: str, old: _FileState | None) -> bool:
    """Put root/rel back as `old` (None: remove it); False if that is not possible.

    A session file that cannot be restored is removed instead: the next resume starts fresh.
    """
    if rel == POLICY_OVERFLOW:
        return False
    target = root / rel
    try:
        if old is None or (old.data is None and PurePosixPath(rel).parts[0] == "sessions"):
            _remove_path(target)
            return True
        if old.data is None:
            return False
        _prepare_parent(root, rel)
        _remove_path(target)
        if old.kind == "link":
            os.symlink(os.fsdecode(old.data), target)
        else:
            _write_bytes(target, old.data)
        return True
    except OSError:
        return False


def _remove_path(path: Path) -> None:
    """Remove a file, symlink or directory tree (never following a symlink); nothing if absent."""
    try:
        info = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return
    if stat.S_ISDIR(info.st_mode):
        shutil.rmtree(path)
    else:
        os.unlink(path)


def _prepare_parent(root: Path, rel: str) -> None:
    """Make every parent of root/rel a real directory again (a symlink or file in its place is removed)."""
    current = root
    for part in PurePosixPath(rel).parts[:-1]:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            os.mkdir(current)
            continue
        if not stat.S_ISDIR(info.st_mode):
            os.unlink(current)
            os.mkdir(current)


def _write_bytes(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _listing(paths: list[str]) -> str:
    shown = ", ".join(paths[:POLICY_LIST])
    return shown + (f" and {len(paths) - POLICY_LIST} more" if len(paths) > POLICY_LIST else "")


def _revert_note(stuck: list[str]) -> str:
    return f"(could not revert {_listing(stuck)})" if stuck else "(reverted)"


def _stored_session(job: dict, ref: str | None) -> str | None:
    """A session ref as job.json keeps it: a session file inside the job folder becomes relative."""
    if not ref:
        return None
    path = Path(ref)
    if path.is_absolute():
        try:
            return path.relative_to(job["job_dir"]).as_posix()
        except ValueError:
            return ref
    return ref


def _special_paths(repo_path: Path, base: str, branch: str) -> list[tuple[str, str]]:
    """(path, "symlink" | "submodule") for each changed path whose old or new git mode is one."""
    out = gitops.git(repo_path, "diff", "--raw", "-z", "--no-renames", f"{base}...{branch}", "--")
    fields = out.split("\0")
    found = []
    for meta, path in zip(fields[0::2], fields[1::2], strict=False):
        kinds = [SPECIAL_MODES[mode] for mode in meta.lstrip(":").split()[:2] if mode in SPECIAL_MODES]
        if kinds:
            found.append((path, kinds[0]))
    return found


def _pending(job: dict) -> dict[str, str]:
    return {reviewer: "pending" for reviewer in job["reviewers"]}


def _review_detail(n: int, reviews: dict[str, str]) -> str:
    waiting = [r for r, v in reviews.items() if v in ("pending", "running")]
    return f"round {n}: waiting for {', '.join(waiting)}" if waiting else f"round {n}: reviews in"


def _last_review_round(job: dict) -> int:
    return max((int(entry.get("round") or 0) for entry in job.get("history") or []), default=0)


def _non_empty(path: Path) -> bool:
    try:
        return bool(read_small_text(path, 1 << 20).strip())
    except (OSError, UnicodeDecodeError):
        return False


def _first_line_under(path: Path, heading: str) -> str:
    """The first non-empty line below a markdown heading named heading (any level), else ""."""
    try:
        lines = iter(read_small_text(path, 1 << 20).splitlines())
    except (OSError, UnicodeDecodeError):
        return ""
    for line in lines:
        if line.lstrip().startswith("#") and line.strip().lstrip("#").strip().lower() == heading.lower():
            for text in lines:
                if text.lstrip().startswith("#"):
                    return ""
                if text.strip():
                    return text.strip()[:NOTE_CHARS]
    return ""


def _title(job: dict) -> str:
    return next((line.strip() for line in str(job["feature"]).splitlines() if line.strip()), job["id"])[:72]


def _merge_message(job: dict) -> str:
    return f"Merge {job['branch']}: {_title(job)}"


def _handoff_build_md(job: dict) -> str:
    branch = str(job["from_branch"])
    origin = f"in chat {branch[len('chat/'):]}" if branch.startswith("chat/") else f"on branch {branch}"
    return f"STATUS: READY\n\n# Build: {_title(job)}\n\nBuilt interactively {origin}.\n"


def _next_log_path(logs: Path, agent_name: str) -> Path:
    """logs/NN-<agent>.jsonl with the next free run number (for runs outside a job folder)."""
    logs.mkdir(parents=True, exist_ok=True)
    number = len(list(logs.glob("*.jsonl"))) + 1
    while (logs / f"{number:02d}-{agent_name}.jsonl").exists():
        number += 1
    path = logs / f"{number:02d}-{agent_name}.jsonl"
    path.touch()
    return path
