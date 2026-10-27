"""Placement: where a step really runs, what it costs and how long it should take (DESIGN.md section 23).

A route names a logical model. At run start it becomes a placement:

- **builds**: a model entry may list the same logical model on other servers (``builds``); the
  entry's own provider and model are its first build. The first build whose provider answers a
  short probe (settings.reach, REACH_TIMEOUT seconds, the answer cached REACH_TTL seconds) runs.
  A route with one build and no overflow is never probed (nothing to choose).
- **loaded-model awareness**: a provider of kind "tabby" (or with a loaded_model_url) says which
  model it has loaded. Unless it has allow_model_swap, a build naming another model counts as
  unavailable; with swaps on, the run goes ahead and the placement records the swap.
- **overflow**: the model entry's ``overflow`` (else its provider's) runs when no build answers,
  or when no slot of the placed provider frees up within its overflow_after_seconds (take_slot:
  the orchestrator for a job's runs and repo-scout, wait_live for chat turns and the lead).
- **escalation**: a route's ``escalate`` rungs replace the builder's model on its k-th send-back
  (a CHANGES round or a rework): rung k, the last one beyond the list, skipping rungs none of
  whose builds answers.
- **GPU custody** (DESIGN §37, custody.py): a build whose provider needs a request lease (custody
  configured, and lazy_load or allow_model_swap) is usable only once the operator's custody register
  grants one (the placement's claim, a custody.Claims); a denial, an error, a timeout or an unreachable
  register passes the build over, so the run follows the overflow path. A HELD answer (someone else
  holds the card) sends the run at once to the model's other builds (another card that is free takes
  it); only when none of them can take the run now does it wait the HELD answer out, for at most the
  provider's overflow_after_seconds, before the overflow; FULL (the card cannot fit the model now)
  passes the build over at once, to the next build (another card or a smaller build), then the
  overflow. An overflow the register caused says why in its words ("the card cannot fit this model
  now").
- **independence**: a judge (a reviewer or the approver) whose model is of a family the builder ran
  on in that round (the approver: in any round) runs on its ``independent_alternate`` instead
  (judge_choice; under GPU custody, an alternate the register gives no lease for counts as not
  answering); with none, the run goes ahead and says its independence is reduced (or accepted,
  when the routing says ``independent_alternate: false``). A family is a model entry's ``family``
  label, else one read from its model id (default_family).

Every placement other than a route's first build says why (Choice.notes and why, which the callers
put in agent.start, job.runs and a log line): nothing here falls back silently. The reason a run
shows is its placement's (placement_reason): the route's own on the route's model, else the
overflow's, the escalation rung's or the independent alternate's. Prices (USD per
million tokens) price each run (run_cost); estimate() predicts tokens, cost and duration from a
repo's past jobs or a default table; profiles (named sets of per-agent placements, each with a
reason) are read and checked here.
"""

from __future__ import annotations

import dataclasses
import re
import statistics
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import custody
from .config import ModelConfig, ProviderConfig
from .harness.providers import SLICE_SECONDS, provider_semaphore
from .settings import fetch_json, is_local_url, reach

REACH_TTL = 30.0        # seconds a probe's answer is reused
REACH_TIMEOUT = 3.0     # seconds a probe may take
EFFORTS = ("minimal", "low", "medium", "high", "xhigh", "max")
ANTHROPIC_EFFORTS = ("low", "medium", "high", "xhigh", "max")   # Claude's output_config.effort
REASON_KINDS = ("capability", "time", "stage-fit", "cost")
PROFILE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
ROUTE_KEYS = ("model", "driver", "extra_body", "effort", "escalate", "reason",
              "independent_alternate")   # a per-agent placement
REASON_CHARS = 200
# A placement's claim (custody.Claims): (provider, model id) -> (usable, why not). It takes the build's request
# lease when its provider needs one.
Claim = Callable[[ProviderConfig, str], "tuple[bool, str]"]

# Model families (the independence guard): a model entry's own `family` label, else one read from its model id.
FAMILY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,40}$")
VENDOR_ALIASES = {"meta-llama": "meta", "mistralai": "mistral", "deepseek-ai": "deepseek", "x-ai": "xai",
                  "google-deepmind": "google", "zai-org": "z-ai", "thudm": "z-ai", "moonshot": "moonshotai",
                  "alibaba": "qwen", "qwenlm": "qwen", "ibm-granite": "ibm", "xiaomimimo": "xiaomi"}
# a model name (a whole word of the id, or one followed by its version: qwen3, gpt-4o) and the family it names
NAME_FAMILIES = (("claude", "anthropic"), ("gpt", "openai"), ("chatgpt", "openai"), ("codex", "openai"),
                 ("gemini", "google"), ("gemma", "google"), ("qwen", "qwen"), ("qwq", "qwen"),
                 ("deepseek", "deepseek"), ("llama", "meta"), ("codellama", "meta"), ("mistral", "mistral"),
                 ("mixtral", "mistral"), ("codestral", "mistral"), ("devstral", "mistral"), ("magistral", "mistral"),
                 ("grok", "xai"), ("kimi", "moonshotai"), ("glm", "z-ai"), ("mimo", "xiaomi"), ("phi", "microsoft"),
                 ("granite", "ibm"), ("nemotron", "nvidia"))
# a path segment that is a vendor (when no model name says more); any other segment (hf.co, a quantizer's or a
# router's namespace, a user's) names no family
KNOWN_VENDORS = frozenset({family for _, family in NAME_FAMILIES} | set(VENDOR_ALIASES.values())
                          | {"cohere", "amazon", "minimax"})
OPENAI_O_SERIES = frozenset({"o1", "o3", "o4"})
JUDGE_HINT = "independent_alternate is for the judges (reviewer-<lens> and approver)"

# Estimates (estimate()): rough per-job defaults, replaced by the median of a repo's past jobs once
# it has MIN_HISTORY of them. Tokens of one agent over a whole job, every round together.
MIN_HISTORY = 3
DEFAULT_TOKENS: dict[str, dict[str, int]] = {
    "spec-writer": {"input_tokens": 150_000, "output_tokens": 8_000, "cached_tokens": 90_000},
    "builder": {"input_tokens": 1_500_000, "output_tokens": 40_000, "cached_tokens": 1_100_000},
    "reviewer": {"input_tokens": 250_000, "output_tokens": 6_000, "cached_tokens": 150_000},
    "approver": {"input_tokens": 150_000, "output_tokens": 3_000, "cached_tokens": 80_000},
    "lead": {"input_tokens": 400_000, "output_tokens": 15_000, "cached_tokens": 250_000},
}
DEFAULT_TOKENS_PER_SECOND = {"local": 60.0, "api": 80.0}   # generation speed when a provider names none
PREFILL_RATIO = 20.0    # uncached prompt tokens are read this many times faster than tokens are written
DEFAULT_PLAN_JOBS = 3   # jobs a plan is assumed to have without history
TOKEN_KEYS = ("input_tokens", "output_tokens", "cached_tokens")


# ---------------------------------------------------------------- prices and cost


def price_table(model: ModelConfig) -> dict | None:
    """{"in", "out", "cached_in"} in USD per million tokens, or None when unknown.

    A model entry with price_in and price_out is priced (price_cached_in defaults to price_in, an
    upper bound); without them it is unknown, not free, unless its provider is local
    (settings.is_local_url), which defaults to free.
    """
    if model.price_in is not None and model.price_out is not None:
        cached = model.price_cached_in if model.price_cached_in is not None else model.price_in
        return {"in": float(model.price_in), "out": float(model.price_out), "cached_in": float(cached)}
    if is_local_url(model.provider.base_url):
        return {"in": 0.0, "out": 0.0, "cached_in": 0.0}
    return None


def run_cost(usage: dict | None, prices: dict | None) -> tuple[float | None, bool]:
    """(cost in USD, priced) of one run's usage; (None, False) when the prices are unknown.

    input_tokens includes cached_tokens (DESIGN §13): cached tokens are charged at cached_in, the
    rest at in, output at out.
    """
    if prices is None:
        return None, False
    usage = usage or {}
    total_in = max(0, int(usage.get("input_tokens") or 0))
    cached = min(total_in, max(0, int(usage.get("cached_tokens") or 0)))
    output = max(0, int(usage.get("output_tokens") or 0))
    usd = ((total_in - cached) * prices["in"] + cached * prices["cached_in"] + output * prices["out"]) / 1e6
    return round(usd, 6), True


def add_cost(total: dict | None, usd: float | None, priced: bool) -> dict:
    """A running total {usd, priced, unpriced_runs} with one more run added (total changed in place too).

    priced stays true while every run so far was priced; usd sums the priced runs only.
    """
    total = total if isinstance(total, dict) else {}
    total.setdefault("usd", 0.0)
    total.setdefault("priced", True)
    total.setdefault("unpriced_runs", 0)
    if priced and usd is not None:
        total["usd"] = round(float(total["usd"]) + usd, 6)
    else:
        total["priced"] = False
        total["unpriced_runs"] = int(total["unpriced_runs"]) + 1
    return total


def zero_cost() -> dict:
    return {"usd": 0.0, "priced": True, "unpriced_runs": 0}


# ---------------------------------------------------------------- reachability


def loaded_model_url(provider: ProviderConfig) -> str | None:
    """Where a server says which model it has loaded: loaded_model_url, else for kind "tabby"
    <base_url>/model (TabbyAPI's GET /v1/model), else None (the server is not model-aware)."""
    if getattr(provider, "loaded_model_url", None):
        return str(provider.loaded_model_url)
    if getattr(provider, "kind", None) == "tabby":
        return provider.base_url.rstrip("/") + "/model"
    return None


def model_id_of(payload: Any) -> str | None:
    """The model id in a loaded-model answer: TabbyAPI's model card {"id": ...}, or {"model_name"},
    {"model"}, {"name"}, or the first entry of an OpenAI-style {"data": [...]} list."""
    if isinstance(payload, dict):
        for key in ("id", "model_name", "model", "name"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        data = payload.get("data")
        if isinstance(data, list) and data:
            return model_id_of(data[0])
    return None


def probe_up(provider: ProviderConfig, timeout: float) -> tuple[bool, str]:
    """Whether provider answers its model list: any HTTP status below 500 counts (a 401 is a server
    that answers; the run then reports the key problem), a 5xx or no answer does not."""
    status, problem = reach(provider, timeout)
    if status is None:
        return False, problem
    if status >= 500:
        return False, f"{provider.base_url} answered HTTP {status}"
    return True, ""


def probe_loaded(provider: ProviderConfig, timeout: float) -> tuple[str | None, str]:
    """(the model provider has loaded, "") or (None, why it cannot be read)."""
    url = loaded_model_url(provider)
    if url is None:
        return None, "the provider does not report a loaded model"
    status, payload, problem = fetch_json(provider, url, timeout)
    if status is None:
        return None, problem
    if status >= 400:
        return None, f"{url} answered HTTP {status}"
    found = model_id_of(payload)
    return (found, "") if found else (None, f"{url} names no model")


class Reachability:
    """Cached probes: does a provider answer, and which model has it loaded (process-wide by default).

    probe(provider, timeout) -> (up, why) and loaded(provider, timeout) -> (model or None, why) can be
    replaced (tests). Answers are reused for ttl seconds; forget() drops one provider's answers (after
    a swap, the loaded model changed).
    """

    def __init__(self, probe: Callable[[ProviderConfig, float], tuple[bool, str]] | None = None,
                 loaded: Callable[[ProviderConfig, float], tuple[str | None, str]] | None = None,
                 ttl: float = REACH_TTL, timeout: float = REACH_TIMEOUT,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.probe = probe or probe_up
        self.loaded = loaded or probe_loaded
        self.ttl = ttl
        self.timeout = timeout
        self.clock = clock
        self._cache: dict[tuple, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def up(self, provider: ProviderConfig) -> tuple[bool, str]:
        return self._cached(("up", provider.name, provider.base_url.rstrip("/")),
                            lambda: self.probe(provider, self.timeout))

    def loaded_model(self, provider: ProviderConfig) -> tuple[str | None, str]:
        return self._cached(("loaded", provider.name, loaded_model_url(provider)),
                            lambda: self.loaded(provider, self.timeout))

    def forget(self, provider_name: str) -> None:
        with self._lock:
            for key in [key for key in self._cache if key[1] == provider_name]:
                del self._cache[key]

    def _cached(self, key: tuple, compute: Callable[[], Any]) -> Any:
        now = self.clock()
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None and 0 <= now - hit[0] < self.ttl:
                return hit[1]
        try:
            value = compute()
        except Exception as exc:  # a probe that breaks counts as no answer, never as a failed run
            value = (False, f"probe failed: {type(exc).__name__}: {exc}") if key[0] == "up" \
                else (None, f"probe failed: {type(exc).__name__}: {exc}")
        with self._lock:
            self._cache[key] = (self.clock(), value)
        return value


REACHABILITY = Reachability()   # shared by the Factory, chats and plans unless they are given another


# ---------------------------------------------------------------- choosing a build


@dataclass
class Build:
    """The logical model on one server: its provider and the model id there (max_tokens and
    context_tokens: the build's own limits, None for the model entry's)."""

    provider: ProviderConfig
    model_id: str
    max_tokens: int | None = None
    context_tokens: int | None = None

    def where(self) -> str:
        return f"{self.provider.name}/{self.model_id}"


@dataclass
class Choice:
    """Which build runs: index into the base builds, or into the overflow builds when overflowed."""

    index: int = 0
    overflowed: bool = False
    swap: dict | None = None
    notes: list[str] = field(default_factory=list)
    why: str = ""                 # why the run overflowed


def model_aware(provider: ProviderConfig) -> bool:
    return loaded_model_url(provider) is not None


def needs_probe(builds: list[Build], has_overflow: bool) -> bool:
    """Whether there is anything to choose: several builds, an overflow, a model-aware server, or a server
    under GPU custody (its lease decides whether the build is usable)."""
    return len(builds) > 1 or has_overflow or any(model_aware(build.provider) or custody.needs_lease(build.provider)
                                                  for build in builds)


def choose_build(builds: list[Build], reach_: Reachability,
                 claim: Claim | None = None) -> tuple[int | None, dict | None, list[str]]:
    """(the first usable build's index or None, the swap it needs or None, why earlier builds were passed over).

    Usable: its provider answers and, on a model-aware server, it has the build's model loaded, or
    allow_model_swap lets the request load it (the swap: {provider, from, to}); with claim, a build whose
    provider needs a request lease also needs the custody register to grant one (DESIGN §37). Without claim
    (a probe that only looks, such as the escalation rungs' check) no lease is asked for.

    HELD while another card is free goes to the free card at once (the operator's rule, §37): a custody.Claims
    is asked with wait=False, so a build whose register answers HELD is put off and the later builds are asked
    first; only when none of them can take the run now are the put-off builds waited out, in order
    (Claims.wait_for: the placement's one wait). LEASED, FULL and every other refusal pass the build over as
    before."""
    notes: list[str] = []
    claims = claim if isinstance(claim, custody.Claims) else None   # puts a HELD answer off (wait=False)
    held: list[tuple[int, dict | None, int]] = []   # builds whose HELD answer was put off: (index, swap, its note)
    for index, build in enumerate(builds):
        up, why = reach_.up(build.provider)
        if not up:
            notes.append(f"build {build.where()} is unavailable: {why}")
            continue
        swap = None
        if model_aware(build.provider):
            loaded, why = reach_.loaded_model(build.provider)
            if loaded != build.model_id:
                if not build.provider.allow_model_swap:
                    notes.append(f"build {build.where()} is unavailable: {build.provider.name} has {loaded} loaded "
                                 f"and allow_model_swap is off" if loaded else
                                 f"build {build.where()} is unavailable: cannot tell which model "
                                 f"{build.provider.name} has loaded ({why}) and allow_model_swap is off")
                    continue
                swap = {"provider": build.provider.name, "from": loaded, "to": build.model_id}
        if claim is not None:
            granted, why = claims(build.provider, build.model_id, wait=False) if claims is not None \
                else claim(build.provider, build.model_id)
            if not granted:
                notes.append(f"build {build.where()} is unavailable: GPU custody: {why}")
                if claims is not None and claims.put_off_held(build.provider, build.model_id):
                    held.append((index, swap, len(notes) - 1))
                continue
        return index, swap, notes
    # no other build can take the run now: wait out the HELD answers put off, in the builds' order
    for index, swap, note in held if claims is not None else []:
        build = builds[index]
        granted, why = claims.wait_for(build.provider, build.model_id)
        if granted:
            del notes[note]   # it waited, and got the card: as if it had been waited for at once
            return index, swap, notes
        notes[note] = f"build {build.where()} is unavailable: GPU custody: {why}"
    return None, None, notes


def choose(builds: list[Build], overflow: list[Build] | None, reach_: Reachability, overflow_key: str = "",
           claim: Claim | None = None) -> Choice:
    """The build a run uses: the first usable of builds; else the overflow's first usable build; else
    builds[0] anyway (the run then fails on it and says why; no silent fallback elsewhere: on a server under
    custody without a lease its calls are refused, custody.lease_problem)."""
    if not needs_probe(builds, overflow is not None):
        return Choice()
    index, swap, notes = choose_build(builds, reach_, claim)
    if index is not None:
        return Choice(index=index, swap=swap, notes=notes)
    why = f"{none_answered(builds, claim)} (" + "; ".join(notes) + ")"
    if overflow:
        found, swap, more = choose_build(overflow, reach_, claim)
        if found is not None:
            return Choice(index=found, overflowed=True, swap=swap, notes=notes + more, why=why)
        notes = notes + more + [f"the overflow {overflow_key or 'model'} is unavailable too"]
    return Choice(notes=notes + [f"running on {builds[0].where()} anyway"])


def none_answered(builds: list[Build], claim: Claim | None) -> str:
    """Why none of builds runs, in the words an overflow's why starts with: the custody register's when it
    refused every build for the same reason (FULL: "the card cannot fit this model now"), else "no build
    answered"."""
    found = claim.common_refusal([(b.provider.name, b.model_id) for b in builds]) \
        if isinstance(claim, custody.Claims) else None
    return found or "no build answered"


def escalation_start(rungs: int, sendbacks: int) -> int | None:
    """The rung (0-based) for the builder's sendbacks-th send-back: rung sendbacks-1, the last one beyond
    the list; None before any send-back or without rungs."""
    if rungs < 1 or sendbacks < 1:
        return None
    return min(sendbacks, rungs) - 1


# ---------------------------------------------------------------- provider slots


def take_slot(provider: ProviderConfig, seconds: float, cancel: threading.Event,
              on_wait: Callable[[], None] | None = None) -> Callable[[], None] | None:
    """Wait at most seconds for one of provider's slots (max_concurrency). Returns a release function,
    or None when no slot came in time or cancel was set (a slot that came free after the cancel is
    given back at once, so a cancelled caller never holds one).

    With concurrency_scope "run" the slot stays held until release(): the runner, on this same thread,
    re-enters it (ProviderSlots is re-entrant per thread) and never waits again. With "call" the slot
    is given back at once, so the run's calls queue as before; release() then does nothing.
    on_wait() is called once if the slot is busy.
    """
    slots = provider_semaphore(provider.name, provider.max_concurrency)
    if slots is None:
        return lambda: None
    if not slots.acquire(blocking=False):
        if on_wait is not None:
            on_wait()
        deadline = time.monotonic() + max(0.0, float(seconds))
        while True:
            remaining = deadline - time.monotonic()
            if cancel.is_set() or remaining <= 0:
                return None
            if slots.acquire(timeout=min(SLICE_SECONDS, remaining)):
                break
    if cancel.is_set():   # cancelled while (or just before) the slot came free: give it back, never keep it
        slots.release()
        return None
    if provider.concurrency_scope != "run":
        slots.release()
        return lambda: None
    held = [True]

    def release() -> None:
        if held:
            held.clear()
            slots.release()
    return release


# ---------------------------------------------------------------- placements on the live config


def config_builds(config: Any, model: ModelConfig) -> list[Build]:
    """model's builds in config: the entry's own provider and model first, then its builds (the
    entry's own server listed again is left out). A literal model id has one build."""
    first = Build(model.provider, model.model)
    entry = _dict(_dict(config.raw.get("models")).get(model.key))
    builds = [first]
    for item in entry.get("builds") or []:
        if not isinstance(item, dict):
            continue
        provider, model_id = str(item.get("provider") or ""), str(item.get("model") or "")
        if (provider, model_id) == (first.provider.name, first.model_id):
            continue
        builds.append(Build(config.provider(provider), model_id, _int_or_none(item.get("max_tokens")),
                            _int_or_none(item.get("context_tokens"))))
    return builds


def overflow_key(config: Any, model: ModelConfig) -> str | None:
    """The model key a model entry overflows to: its own overflow, else its provider's, never itself."""
    entry = _dict(_dict(config.raw.get("models")).get(model.key))
    key = entry.get("overflow") or getattr(model.provider, "overflow", None)
    return str(key) if key and key != model.key else None


def on_build(model: ModelConfig, build: Build) -> ModelConfig:
    """model served by build: its provider and model id, and the build's own limits where it has them."""
    return dataclasses.replace(model, provider=build.provider, model=build.model_id,
                               max_tokens=build.max_tokens or model.max_tokens,
                               context_tokens=build.context_tokens or model.context_tokens)


def place_live(config: Any, model: ModelConfig, reach_: Reachability | None = None,
               cli: bool = False, claim: Claim | None = None) -> tuple[ModelConfig, dict, list[str]]:
    """(the model a chat turn or a lead run runs on, placement info for agent.start, notes): model on
    the first of its builds that answers, else its overflow (config.model of the overflow key) on that
    model's first usable build. A CLI driver reaches its own endpoint, so cli=True keeps model as it is
    (its request lease, when its provider needs one, is the caller's: chats.place_run). claim: the
    placement's custody.Claims (DESIGN §37).
    """
    if cli:
        return model, {}, []
    reach_ = reach_ or REACHABILITY
    builds = config_builds(config, model)
    key = overflow_key(config, model)
    overflow_model = config.model(key) if key else None
    overflow = config_builds(config, overflow_model) if overflow_model is not None else None
    choice = choose(builds, overflow, reach_, key or "", claim)
    if choice.overflowed and overflow_model is not None and overflow is not None:
        placed = on_build(overflow_model, overflow[choice.index])
        info = {"overflow": {"from": model.key, "to": overflow_model.key, "why": choice.why}}
    else:
        placed = on_build(model, builds[choice.index])
        info = {}
    if len(builds) > 1 and not choice.overflowed:
        info["build"] = {"index": choice.index, "of": len(builds), "provider": placed.provider.name,
                         "model_id": placed.model}
    if choice.swap:
        info["swap"] = choice.swap
    return placed, info, choice.notes


def wait_live(config: Any, model: ModelConfig, placed: ModelConfig, info: dict, cancel: threading.Event,
              on_wait: Callable[[], None] | None = None, reach_: Reachability | None = None,
              cli: bool = False, claim: Claim | None = None
              ) -> tuple[ModelConfig, dict, list[str], Callable[[], None]] | None:
    """The slot wait of a chat turn or lead run that place_live put on one of model's own builds
    (placed, info), as the orchestrator does it for a job's run: when model has an overflow
    and placed's provider max_concurrency and overflow_after_seconds, wait at most that long for one
    of its slots (take_slot; on_wait() once when it is busy). Past that the run goes to the
    overflow's first usable build; when none answers, it keeps waiting for the slot.

    Returns (the model to run on, its placement info, notes, the slot's release: call it when the run
    ends), or None when cancel was set while waiting. A CLI run, an overflowed placement or a model
    without an overflow does not wait here (its calls queue on the slots as before)."""
    key = overflow_key(config, model)
    provider = placed.provider
    if cli or "overflow" in info or key is None or not provider.overflow_after_seconds or not provider.max_concurrency:
        return placed, info, [], lambda: None
    seconds = float(provider.overflow_after_seconds)
    release = take_slot(provider, seconds, cancel, on_wait)
    if release is not None:
        return placed, info, [], release
    if cancel.is_set():
        return None
    if isinstance(claim, custody.Claims):   # the run waited its time: the overflow's HELD answers do not wait
        claim.stop_waiting(f"waited {seconds:g}s for a slot of {provider.name}")
    overflow_model = config.model(key)
    builds = config_builds(config, overflow_model)
    found, swap, notes = choose_build(builds, reach_ or REACHABILITY, claim)
    if found is None:   # nowhere to go: keep waiting for the slot, as without an overflow
        release = take_slot(provider, float("inf"), cancel)
        if release is None:
            return None
        return placed, info, notes + [f"the overflow {key} is unavailable too; waited for {provider.name}"], release
    moved = {name: value for name, value in info.items() if name not in ("build", "swap")}
    moved["overflow"] = {"from": model.key, "to": key, "why": f"no slot of {provider.name} came free within {seconds:g}s"}
    if swap:
        moved["swap"] = swap
    return on_build(overflow_model, builds[found]), moved, notes, lambda: None


# ---------------------------------------------------------------- reasons, efforts, profiles


def reason(value: Any, label: str = "reason") -> dict | None:
    """A placement's reason as {kind, text}: from {"kind", "text"} or "kind: text"; kind is one of
    capability, time, stage-fit, cost. None for none; ValueError for anything else."""
    if value is None:
        return None
    if isinstance(value, str):
        kind, colon, text = value.partition(":")
        value = {"kind": kind.strip(), "text": text.strip()} if colon else {"kind": value.strip(), "text": ""}
    if not isinstance(value, dict) or set(value) - {"kind", "text"}:
        raise ValueError(f'{label} must be {{"kind": ..., "text": ...}} or "kind: text"')
    kind, text = value.get("kind"), value.get("text") or ""
    if kind not in REASON_KINDS:
        raise ValueError(f"{label}.kind must be one of {', '.join(REASON_KINDS)} (got {kind!r})")
    if not isinstance(text, str) or len(text) > REASON_CHARS:
        raise ValueError(f"{label}.text must be a string of at most {REASON_CHARS} characters")
    return {"kind": kind, "text": text.strip()}


def placement_reason(route_reason: dict | None, info: dict) -> dict | None:
    """Why a run goes where its placement put it: the route's own reason while it runs on the route's
    model (on any of its builds); an overflow's, an escalation rung's or an independent alternate's
    own reason otherwise (the route's reason was given for another model)."""
    overflow = info.get("overflow")
    if isinstance(overflow, dict):
        why = str(overflow.get("why") or "").split(" (", 1)[0]
        return {"kind": "time", "text": f"overflow from {overflow.get('from')}: {why}"[:REASON_CHARS]}
    escalation = info.get("escalation")
    if isinstance(escalation, dict):
        return {"kind": "capability", "text": f"escalation rung {escalation.get('rung')} of {escalation.get('of')} "
                                              f"after {escalation.get('sendbacks')} send-back(s)"}
    shared = info.get("shared_model")
    if info.get("independence") == "alternate" and isinstance(shared, dict):
        return {"kind": "stage-fit", "text": alternate_text(shared)}
    return route_reason


def alternate_text(shared: dict) -> str:
    """The reason text of a judge run moved to its independent alternate."""
    return (f"independent alternate: the builder ran on the {shared.get('family')} family "
            f"({shared.get('builder_model')}) this round, like {shared.get('model')}")[:REASON_CHARS]


# ---------------------------------------------------------------- independence (the judges and the builder)


def is_judge(agent: str) -> bool:
    """A judge of the build: a reviewer (reviewer-<lens>) or the approver."""
    return agent == "approver" or agent.startswith("reviewer-")


def default_family(model_id: Any) -> str:
    """The family a model id names when its entry gives none: the first model name among the words of the whole
    id, its path segments included (claude is anthropic, gpt openai, gemini and gemma google, qwen, deepseek,
    llama meta, mistral, grok xai, kimi moonshotai, glm z-ai, mimo xiaomi, phi microsoft, granite ibm; o1, o3
    and o4 openai), matched as a whole word or with its version (qwen3, gpt-4o; not gptq); else a known vendor
    among its path segments (meta/muse-spark is meta; a few spellings folded: meta-llama is meta); else the
    first word of its last segment. A hub, quantizer, router or user namespace (hf.co, unsloth, openrouter)
    names no family, so hf.co/<user>/Qwen3-...-GGUF is qwen; an id that names no family (strata) is its own,
    so label such entries."""
    text = str(model_id or "").strip().lower()
    segments = [segment.strip() for segment in text.split("/") if segment.strip()]
    words = [word for segment in segments for word in re.split(r"[\s_:@-]+", segment) if word]
    for word in words:
        if word in OPENAI_O_SERIES:
            return "openai"
        for name, family in NAME_FAMILIES:
            if word == name or (word.startswith(name) and re.match(r"[0-9.]", word[len(name):])):
                return family
    for segment in segments[:-1]:
        if segment in VENDOR_ALIASES:
            return VENDOR_ALIASES[segment]
        if segment in KNOWN_VENDORS:
            return segment
    last = [word for word in re.split(r"[\s_:@-]+", segments[-1]) if word] if segments else []
    first = re.match(r"[a-z]+", last[0]) if last else None
    return first.group(0) if first else (last[0] if last else "unknown")


def entry_family(entry: Any, model_id: Any = None) -> str:
    """A model entry's family: its own family label (lowercased), else default_family of its model id."""
    entry = entry if isinstance(entry, dict) else {}
    label = entry.get("family")
    if isinstance(label, str) and label.strip():
        return label.strip().lower()
    return default_family(model_id if model_id is not None else entry.get("model"))


def model_family(model: ModelConfig) -> str:
    """The family of the model a ModelConfig runs: its family label, else default_family of its model id."""
    label = getattr(model, "family", None)
    return label.strip().lower() if isinstance(label, str) and label.strip() else default_family(model.model)


def route_family(route: Any) -> str:
    """The family of a snapshot route (or a job.runs entry): its family, else default_family of its model id
    (a job.json from before families). A CLI route's family is its model entry's: the CLI's own model
    mapping is not looked into, so label the entry with the family the CLI really runs."""
    route = route if isinstance(route, dict) else {}
    label = route.get("family")
    if isinstance(label, str) and label.strip():
        return label.strip().lower()
    return default_family(route.get("model_id") or route.get("model"))


def judge_choice(route: dict, builder: dict[str, str]) -> tuple[dict, str | None]:
    """(the route a judge runs on, its independence) for a round whose builder ran on builder's families
    ({family: the builder's model key}): the judge's own route and None when its family is not one of
    them; its independent_alternate and "alternate" when the alternate's family is not one either; else
    its own route and shared_status(route): "reduced" (the run goes ahead, and says so), or "accepted"
    when the routing accepts the shared family (independent_alternate: false)."""
    if route_family(route) not in builder:
        return route, None
    alternate = route.get("independent_alternate")
    if isinstance(alternate, dict) and route_family(alternate) not in builder:
        return alternate, "alternate"
    return route, shared_status(route)


def shared_status(route: dict) -> str:
    """The independence of a judge run on a family the builder ran on: "accepted" when the judge's route
    says independent_alternate: false (the owner chose it, say a local security fine-tune whose findings
    stay local), else "reduced"."""
    return "accepted" if route.get("independent_alternate") is False else "reduced"


def shared_model(route: dict, builder: dict[str, str], model_id: str | None = None) -> dict:
    """What a judge shares with the builder: {model, model_id, family, builder_model} of route (the judge's
    model of the builder's family)."""
    family = route_family(route)
    return {"model": route.get("model"), "model_id": model_id or route.get("model_id"), "family": family,
            "builder_model": builder.get(family)}


def builder_placements(route: dict) -> list[tuple[str, dict]]:
    """Every route a builder snapshot route can run on, labelled: the route, its overflow, each escalation
    rung and its overflow (a CLI route never overflows); each model key once, its labels joined
    ("overflow, rung 2")."""
    found: list[tuple[str, dict]] = []

    def add(label: str, part: Any) -> None:
        if not isinstance(part, dict):
            return
        for index, (known, placed) in enumerate(found):
            if placed.get("model") == part.get("model"):
                found[index] = (f"{known}, {label}", placed)
                return
        found.append((label, part))

    def with_overflow(label: str, part: Any) -> None:
        add(label, part)
        if isinstance(part, dict) and "cli_model" not in part:
            add(f"{label} overflow" if label != "route" else "overflow", part.get("overflow"))

    with_overflow("route", route)
    for index, rung in enumerate(route.get("escalate") or [], start=1):
        with_overflow(f"rung {index}", rung)
    return found


def independence_map(snapshot: dict) -> list[dict]:
    """What the guard does with the judges of a routing snapshot for each model the builder can run on
    (builder_placements): [{builder: {via, model, family}, alternate: {judge: the alternate's model key},
    reduced: {judge: its model key, shared with the builder's family}, accepted: {judge: its model key,
    shared by the routing's choice}}]. A reviewer is compared with that model's family; the approver, which
    judges the whole branch once, with every placement up to that one (the code it approves may have been
    written on any of them). Judges that stay independent are not listed. A judge's own overflow is decided
    at run time, so it is not in the map."""
    builder = snapshot.get("builder")
    if not isinstance(builder, dict):
        return []
    judges = {agent: route for agent, route in snapshot.items() if is_judge(str(agent)) and isinstance(route, dict)}
    rows = []
    so_far: dict[str, str] = {}
    for via, placed in builder_placements(builder):
        family = route_family(placed)
        families = {family: str(placed.get("model"))}
        so_far.setdefault(family, str(placed.get("model")))
        found: dict[str, dict] = {"alternate": {}, "reduced": {}, "accepted": {}}
        for agent in sorted(judges):
            chosen, status = judge_choice(judges[agent], dict(so_far) if agent == "approver" else families)
            if status is not None:
                found[status][agent] = chosen.get("model")
        rows.append({"builder": {"via": via, "model": placed.get("model"), "family": family}, **found})
    return rows


def effort_problem(provider_type: str | None, effort: Any) -> str | None:
    """Why provider_type cannot take effort, or None: one of EFFORTS, and on Anthropic-format providers
    one of ANTHROPIC_EFFORTS (Claude's output_config.effort has no "minimal")."""
    if effort not in EFFORTS:
        return f"effort must be one of {', '.join(EFFORTS)} (got {effort!r})"
    if provider_type == "anthropic" and effort not in ANTHROPIC_EFFORTS:
        return (f"effort {effort!r} is not taken by Anthropic-format providers (Claude's output_config.effort "
                f"is one of {', '.join(ANTHROPIC_EFFORTS)})")
    return None


def effort_body(provider_type: str, effort: str | None) -> dict:
    """The extra_body that sets a reasoning effort: Anthropic output_config.effort, OpenAI-format
    reasoning_effort (DeepSeek, OpenAI, NanoGPT's GPT models). {} for none; ValueError for an effort
    the provider type does not take (effort_problem)."""
    if not effort:
        return {}
    problem = effort_problem(provider_type, effort)
    if problem:
        raise ValueError(problem)
    if provider_type == "anthropic":
        return {"output_config": {"effort": effort}}
    return {"reasoning_effort": effort}


def check_placement(entry: Any, label: str, models: set[str] | None = None, drivers: set[str] | None = None,
                    allowed: tuple[str, ...] | None = ROUTE_KEYS, agent: str | None = None) -> dict:
    """entry as a per-agent placement {model?, driver?, extra_body?, effort?, escalate?, reason?,
    independent_alternate?}, with its reason normalised; ValueError naming label for a key outside allowed
    (None: any) or a bad value (model, the escalate rungs and the independent alternate must be keys of
    models, driver a configured driver, when those sets are given; independent_alternate may instead be
    false, which accepts the builder's family; with agent, only a judge may name either)."""
    if not isinstance(entry, dict):
        raise ValueError(f"{label} must be an object like {{\"model\": ...}}")
    unknown = sorted(str(key) for key in set(entry) - set(allowed)) if allowed else []
    if unknown:
        raise ValueError(f"{label}: unknown keys {', '.join(unknown)} (allowed: {', '.join(allowed)})")
    checked = dict(entry)
    for key in ("model", "driver"):
        value = entry.get(key)
        if value is not None and (not isinstance(value, str) or (value == "" and key == "model")):
            raise ValueError(f"{label}.{key} must be a string")
    if entry.get("model") and models is not None and entry["model"] not in models:
        raise ValueError(f"{label}.model {entry['model']!r} is not a key of models")
    if entry.get("driver") and drivers is not None and entry["driver"] not in drivers:
        raise ValueError(f"{label}.driver {entry['driver']!r} is not a configured driver")
    if entry.get("extra_body") is not None and not isinstance(entry["extra_body"], dict):
        raise ValueError(f"{label}.extra_body must be an object")
    if entry.get("effort") is not None and entry["effort"] not in EFFORTS:
        raise ValueError(f"{label}.effort must be one of {', '.join(EFFORTS)} (got {entry['effort']!r})")
    rungs = entry.get("escalate")
    if rungs is not None:
        if not isinstance(rungs, list) or not all(isinstance(rung, str) and rung for rung in rungs):
            raise ValueError(f"{label}.escalate must be a list of model keys")
        if models is not None:
            for rung in rungs:
                if rung not in models:
                    raise ValueError(f"{label}.escalate: {rung!r} is not a key of models")
    alternate = entry.get("independent_alternate")
    if alternate is not None:
        if alternate is not False and (not isinstance(alternate, str) or not alternate):
            raise ValueError(f"{label}.independent_alternate must be a model key, or false to accept the "
                             f"builder's family")
        if agent is not None and not is_judge(agent):
            raise ValueError(f"{label}: {JUDGE_HINT}")
        if models is not None and alternate is not False and alternate not in models:
            raise ValueError(f"{label}.independent_alternate {alternate!r} is not a key of models")
    if "reason" in entry:
        checked["reason"] = reason(entry["reason"], f"{label}.reason")
    return checked


def check_profiles(raw: dict, models: set[str] | None = None, drivers: set[str] | None = None,
                   agent_name_re: re.Pattern[str] | None = None) -> None:
    """profiles and default_profile as DESIGN §23 has them; ValueError naming the first bad field."""
    profiles = raw.get("profiles")
    if profiles is None:
        profiles = {}
    if not isinstance(profiles, dict):
        raise ValueError("profiles must be an object of profile names to {description, agents}")
    for name, profile in profiles.items():
        label = f"profiles.{name}"
        if not isinstance(name, str) or not PROFILE_NAME_RE.fullmatch(name):
            raise ValueError(f"profiles: invalid name {name!r} (use a-z, 0-9 and '-')")
        if profile is None:
            continue
        if not isinstance(profile, dict) or set(profile) - {"description", "agents"}:
            raise ValueError(f'{label} must be an object like {{"description": ..., "agents": {{...}}}}')
        if profile.get("description") is not None and not isinstance(profile["description"], str):
            raise ValueError(f"{label}.description must be a string")
        agents = profile.get("agents") or {}
        if not isinstance(agents, dict):
            raise ValueError(f"{label}.agents must be an object of agent names to placements")
        for agent, entry in agents.items():
            if agent_name_re is not None and not agent_name_re.fullmatch(str(agent)):
                raise ValueError(f"{label}.agents: invalid agent name {agent!r}")
            check_placement(entry, f"{label}.agents.{agent}", models, drivers, agent=str(agent))
    default = raw.get("default_profile")
    if default is not None and (not isinstance(default, str) or not isinstance(profiles.get(default), dict)):
        raise ValueError(f"default_profile {default!r} is not a key of profiles")


def profile_name(raw: dict, profile: str | None) -> str | None:
    """The profile a run uses: profile itself, the config's default_profile when profile is None, no
    profile for "" (the routing as saved). ValueError for an unknown name."""
    profiles = _dict(raw.get("profiles"))
    if profile is None:
        default = raw.get("default_profile")
        if not default:
            return None
        profile = default
    elif profile == "":
        return None
    if not isinstance(profile, str) or not isinstance(profiles.get(profile), dict):
        known = sorted(name for name, entry in profiles.items() if isinstance(entry, dict))
        raise ValueError(f"unknown profile {profile!r} (available: {', '.join(known) or 'none'})")
    return profile


def profile_routes(raw: dict, name: str | None) -> dict[str, dict]:
    """The profile's per-agent placements (reasons normalised), {} without a profile."""
    if name is None:
        return {}
    agents = _dict(_dict(_dict(raw.get("profiles")).get(name)).get("agents"))
    return {str(agent): check_placement(entry, f"profiles.{name}.agents.{agent}", agent=str(agent))
            for agent, entry in agents.items() if entry is not None}


def merged_placements(base: dict[str, dict], top: dict[str, dict]) -> dict[str, dict]:
    """Per agent, top's keys over base's (a per-run route over a profile's placement). A top entry that
    names another model than base's drops base's reason and independent alternate unless it gives its
    own: they were given for base's model (its family, for the alternate)."""
    merged = {agent: dict(entry) for agent, entry in base.items()}
    for agent, entry in top.items():
        under = dict(merged.get(agent, {}))
        if entry.get("model") and entry["model"] != under.get("model"):
            for key in ("reason", "independent_alternate"):
                if key not in entry:
                    under.pop(key, None)
        merged[agent] = {**under, **entry}
    return merged


# ---------------------------------------------------------------- estimates


@dataclass
class Role:
    """One agent of an estimate: its stage (spec, build, review, approve or lead), the model it runs
    on (first build; prices and speed come from it) and its route (model key, reason)."""

    agent: str
    stage: str
    model: ModelConfig
    route: dict


def default_tokens(agent: str) -> dict[str, int]:
    """DEFAULT_TOKENS for agent: every reviewer-<r> as "reviewer", an agent the table lacks as "approver"."""
    if agent.startswith("reviewer-"):
        return DEFAULT_TOKENS["reviewer"]
    return DEFAULT_TOKENS.get(agent, DEFAULT_TOKENS["approver"])


def median_usage(samples: list[dict]) -> dict:
    return {key: int(statistics.median(int(sample.get(key) or 0) for sample in samples)) for key in TOKEN_KEYS}


def role_seconds(tokens: dict, model: ModelConfig) -> tuple[float, float]:
    """(seconds, tokens per second used): output at the provider's tokens_per_second (default by
    local or API), uncached input PREFILL_RATIO times faster."""
    local = is_local_url(model.provider.base_url)
    speed = getattr(model.provider, "tokens_per_second", None) or DEFAULT_TOKENS_PER_SECOND["local" if local else "api"]
    uncached = max(0, int(tokens["input_tokens"]) - int(tokens["cached_tokens"]))
    return int(tokens["output_tokens"]) / speed + uncached / (speed * PREFILL_RATIO), float(speed)


def parallel_seconds(roles: list[tuple[Role, float]]) -> float:
    """How long roles that start together take: on one provider they share its max_concurrency slots
    (one slot: one after another), different providers work at the same time."""
    by_provider: dict[str, list[tuple[Role, float]]] = {}
    for role, seconds in roles:
        by_provider.setdefault(role.model.provider.name, []).append((role, seconds))
    longest = 0.0
    for items in by_provider.values():
        times = [seconds for _, seconds in items]
        slots = items[0][0].model.provider.max_concurrency
        spent = max(times) if not slots else max(max(times), sum(times) / slots)
        longest = max(longest, spent)
    return longest


def estimate(roles: list[Role], history: dict[str, list[dict]], kind: str = "feature",
             plan_jobs: list[int] | None = None) -> dict:
    """Expected tokens, cost and duration of one job (kind "feature") or one plan (kind "plan": the
    lead, then its jobs one after another).

    history: {agent: [usage of that agent in one past job of the repo]}; an agent with at least
    MIN_HISTORY of them is estimated by their median, else by DEFAULT_TOKENS. plan_jobs: the job
    counts of the repo's past plans (their median once there are MIN_HISTORY, else
    DEFAULT_PLAN_JOBS). Stages run one after another; the reviewers of a round start together and
    share each provider's slots (parallel_seconds). Escalation rungs and overflows are not in it. A
    role whose route carries independence ("alternate", "reduced" or "accepted") and instead_of (the
    judge's own model key, for an alternate) passes them on to its row.
    """
    rows: list[dict] = []
    timed: list[tuple[Role, float]] = []
    from_history = 0
    for role in roles:
        samples = [usage for usage in history.get(role.agent, []) if isinstance(usage, dict)]
        if len(samples) >= MIN_HISTORY:
            tokens, basis = median_usage(samples), f"median of {len(samples)} past jobs"
            from_history += 1
        else:
            tokens = dict(default_tokens(role.agent))
            basis = "default table" + (f" ({len(samples)} past jobs; {MIN_HISTORY} needed)" if samples else "")
        cost, priced = run_cost(tokens, price_table(role.model))
        seconds, speed = role_seconds(tokens, role.model)
        timed.append((role, seconds))
        rows.append({"agent": role.agent, "stage": role.stage, "model": role.route.get("model"),
                     "provider": role.model.provider.name, "model_id": role.model.model,
                     "local": is_local_url(role.model.provider.base_url), "tokens": tokens, "basis": basis,
                     "cost_usd": cost, "priced": priced, "seconds": round(seconds), "tokens_per_second": speed,
                     **({"reason": role.route["reason"]} if role.route.get("reason") else {}),
                     **{key: role.route[key] for key in ("independence", "instead_of") if role.route.get(key)}})
    stages: list[dict] = []
    for stage in ("lead", "spec", "build", "review", "approve"):
        members = [(role, seconds) for role, seconds in timed if role.stage == stage]
        if members:
            spent = parallel_seconds(members) if stage == "review" else sum(seconds for _, seconds in members)
            stages.append({"stage": stage, "seconds": round(spent), "agents": [role.agent for role, _ in members]})
    job_rows = [row for row in rows if row["stage"] != "lead"]
    job_seconds = sum(stage["seconds"] for stage in stages if stage["stage"] != "lead")
    jobs = 1
    notes = ["the builder's escalation rungs, overflows and extra review rounds are not priced in"]
    if any(role.route.get("independence") == "alternate" for role in roles):
        notes.append("a judge on the builder's own model family is priced on its independent alternate, where the "
                     "independence guard puts it while the builder is on its own model")
    if kind == "plan":
        counts = [int(count) for count in plan_jobs or [] if isinstance(count, int) and count > 0]
        jobs = int(statistics.median(counts)) if len(counts) >= MIN_HISTORY else DEFAULT_PLAN_JOBS
        notes.append(f"a plan of {jobs} jobs" + (f" (median of {len(counts)} past plans)" if len(counts) >= MIN_HISTORY
                                                  else " (default)") + ", run one after another")
    lead_rows = [row for row in rows if row["stage"] == "lead"]
    lead_seconds = sum(stage["seconds"] for stage in stages if stage["stage"] == "lead")
    priced = all(row["priced"] for row in rows)
    usd = sum(row["cost_usd"] or 0.0 for row in lead_rows) + jobs * sum(row["cost_usd"] or 0.0 for row in job_rows)
    tokens = {key: sum(row["tokens"][key] for row in lead_rows) + jobs * sum(row["tokens"][key] for row in job_rows)
              for key in TOKEN_KEYS}
    if not priced:
        missing = sorted({row["model"] for row in rows if not row["priced"]})
        notes.append(f"no prices for {', '.join(map(str, missing))}: the cost leaves them out")
    basis = "history" if from_history == len(roles) and roles else "defaults" if not from_history else "mixed"
    return {"kind": kind, "basis": basis, "roles": rows, "stages": stages, "jobs": jobs,
            "total": {"cost_usd": round(usd, 4), "priced": priced, "seconds": round(lead_seconds + jobs * job_seconds),
                      "tokens": tokens},
            "notes": notes}


# ---------------------------------------------------------------- helpers


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _int_or_none(value: Any) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None
