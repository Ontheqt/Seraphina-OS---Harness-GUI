"""Mock backend for the routing (placement) area: DESIGN §23 and §15 "Placement" (routing profiles on every start,
GET /api/estimate, the placement fields of agent.start / agent.end and job.runs, cost on jobs, chats and plans, the
independence guard's records), the provider and model placement settings with the server's 400s, the doctor's
placement checks, and GPU custody §37 (GET /api/custody). See tests/gui_mock/__init__.py for the hooks.

The real rules come from swfactory.routing (families, reasons, prices, the estimate, the independence map) and the
real validation messages from swfactory.settings and swfactory.routing, so the mock answers as the server does. The
core mock knows nothing of placement: this module wraps a few of its functions (new_job, new_plan, Run, the config
view and update, the doctor) once, and backfills the seeded jobs from their run logs on every seed."""

from __future__ import annotations

import copy
import json
import re
import sys
import threading
import time
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

from . import store

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from swfactory import custody as C  # noqa: E402  (GPU custody's defaults)
from swfactory import routing as R  # noqa: E402  (the placement rules)
from swfactory import settings as S  # noqa: E402  (the settings' validation messages)

AREA = "routing"
PLACE_KEYS = ("effort", "escalate", "reason", "independent_alternate")
INDEPENDENCE_RANK = {"accepted": 1, "unknown": 2, "reduced": 3}   # orchestrator.INDEPENDENCE_RANK
STAGES = {"spec-writer": "spec", "builder": "build", "approver": "approve", "lead": "lead"}
USAGE_KEYS = ("input_tokens", "output_tokens", "cached_tokens", "calls")
_PENDING = threading.local()    # what a POST asked for (a profile, hand-off models) until the core makes the record
_WRAPPED = "_routing_area_wrapped"

# ---------------------------------------------------------------- the seeded placement settings
PROVIDER_PLACEMENT = {
    "tabby": {"kind": "tabby", "allow_model_swap": True, "swap_back": True, "overflow_after_seconds": 900, "tokens_per_second": 90,
              "custody": {"url": "https://127.0.0.1:7300", "token_file": "/etc/ai-stack/custody.token",
                          "host_header": "register.internal", "ca_file": "/etc/ai-stack/register-ca.pem", "provider": "gpu0"}},
    "strata": {"overflow_after_seconds": 900, "tokens_per_second": 70, "lazy_load": True,
               "custody": {"url": "http://127.0.0.1:7400", "token_credential": "strata-custody-token", "provider": "strata-gpu"}},
}
EXTRA_PROVIDERS = {   # another entry on Tabby's server: it follows tabby's custody (custody_from)
    "tabby-raw": {"type": "openai", "base_url": "http://127.0.0.1:5000/v1", "api_key_env": "TABBY_API_KEY", "auth": "both",
                  "kind": "tabby", "allow_model_swap": True, "timeout": 1800},
}
MODEL_PLACEMENT = {
    "opus": {"price_in": 0.55, "price_out": 2.19, "price_cached_in": 0.07},
    "haiku": {"price_in": 0.14, "price_out": 0.28, "price_cached_in": 0.014},
    "claude": {"price_in": 3, "price_out": 15, "price_cached_in": 0.3},
    "sonnet": {"family": "qwen"},
    "local": {"family": "qwen", "overflow": "haiku",
              "builds": [{"provider": "strata", "model": "strata"},
                         {"provider": "tabby", "model": "qwen3.8-flash-next", "context_tokens": 262144}]},
}
EXTRA_MODELS = {
    "gpt-sol": {"provider": "nanogpt", "model": "openai/gpt-6.1-sol", "max_tokens": 32768, "stream": True,
                "extra_body": {"reasoning_effort": "high"}, "price_in": 2, "price_out": 10},
    "muse-spark": {"provider": "nanogpt", "model": "meta/muse-spark-1.3", "max_tokens": 32768, "stream": True, "extra_body": {},
                   "price_in": 0.8, "price_out": 3.2},
    "gemini-argon": {"provider": "nanogpt", "model": "google/gemini-4-argon", "max_tokens": 32768, "stream": True, "extra_body": {}},
}
AGENT_PLACEMENT = {
    "reviewer-security": {"independent_alternate": False,
                          "reason": {"kind": "stage-fit", "text": "the security lens stays on the builder's model by choice: its findings stay with you"}},
    "reviewer-code": {"independent_alternate": "claude",
                      "reason": {"kind": "stage-fit", "text": "a Qwen reviewer for code the DeepSeek builder wrote: another family catches other mistakes"}},
    "approver": {"effort": "high", "independent_alternate": "claude",
                 "reason": {"kind": "capability", "text": "the merge gate: <b>independent</b> judgment on the whole branch"}},
}
PROFILES = {
    "balanced": {"description": "Every step on its saved model; the builder moves to Claude after a send-back instead of retrying.",
                 "agents": {"spec-writer": {"reason": {"kind": "capability", "text": "the spec sets the bar for every later stage"}},
                            "builder": {"escalate": ["claude"], "reason": {"kind": "cost", "text": "the most tokens, on the cheapest capable model"}}}},
    "local-first": {"description": "Local Qwen builds and reviews security; API models judge, so the other lenses stay independent.",
                    "agents": {"builder": {"model": "local", "escalate": ["gpt-sol"],
                                           "reason": {"kind": "cost", "text": "the most tokens, free on the local Qwen (Strata, then Tabby)"}},
                               "reviewer-security": {"model": "local", "independent_alternate": False,
                                                     "reason": {"kind": "stage-fit", "text": "the security fine-tune; findings stay local"}},
                               "reviewer-code": {"model": "gpt-sol", "independent_alternate": "muse-spark",
                                                 "reason": {"kind": "stage-fit", "text": "OpenAI family, independent of the local builder"}},
                               "reviewer-ux": {"model": "claude", "reason": {"kind": "stage-fit", "text": "Anthropic family for the UX lens"}},
                               "reviewer-ui": {"model": "gemini-argon", "reason": {"kind": "stage-fit", "text": "Google family for the UI lens"}},
                               "approver": {"model": "claude", "independent_alternate": "gpt-sol", "effort": "high",
                                            "reason": {"kind": "capability", "text": "the merge gate on a <b>strong</b> API model"}}}},
    "fastest": {"description": "Every step on an API model, so the four lenses run at once.",
                "agents": {"builder": {"model": "opus", "reason": {"kind": "time", "text": "API slots run in parallel"}},
                           "reviewer-security": {"model": "haiku", "reason": {"kind": "time", "text": "no wait for the one local slot"}},
                           "reviewer-code": {"model": "claude", "reason": {"kind": "time", "text": "no wait for the one local slot"}}}},
}
DEFAULT_PROFILE = "balanced"


# ---------------------------------------------------------------- small helpers
def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _cfg_for(core, routing) -> dict:
    """The config a run resolves against: the saved one, or the preset's merge when routing names one."""
    return core.preset_config(core.STATE["config"], routing) if isinstance(routing, str) else core.STATE["config"]


def _profile(core, cfg: dict, value):
    """server.ApiRequest.profile + routing.profile_name: None (the default), "" (none) or a name; Invalid otherwise."""
    if value is not None and (not isinstance(value, str) or len(value) > 200):
        raise core.Invalid("'profile' must be a profile name, or \"\" for none")
    try:
        return R.profile_name(cfg, value)
    except ValueError as exc:
        raise core.Invalid(str(exc)) from None


def _model_ns(cfg: dict, route: dict) -> SimpleNamespace:
    """A ModelConfig-like view of a snapshot route for routing.price_table / routing.estimate."""
    entry = _dict(_dict(cfg.get("models")).get(route.get("model")))
    prov = _dict(_dict(cfg.get("providers")).get(route.get("provider")))
    pe = _dict(route.get("provider_entry"))
    provider = SimpleNamespace(name=str(route.get("provider") or "?"), base_url=str(prov.get("base_url") or pe.get("base_url") or ""),
                               tokens_per_second=prov.get("tokens_per_second"),
                               max_concurrency=prov.get("max_concurrency", pe.get("max_concurrency")))
    return SimpleNamespace(key=route.get("model"), model=str(route.get("model_id") or route.get("model") or ""), provider=provider,
                           price_in=entry.get("price_in"), price_out=entry.get("price_out"),
                           price_cached_in=entry.get("price_cached_in"), family=entry.get("family"))


def _cost(cfg: dict, route: dict, usage: dict | None) -> tuple:
    return R.run_cost(usage or {}, R.price_table(_model_ns(cfg, route)))


def _decorate(core, cfg: dict, route: dict, overflow: bool = True) -> dict:
    """A resolved route with the real snapshot's placement fields: family, builds (more than one), overflow (a route)."""
    entry = _dict(_dict(cfg.get("models")).get(route.get("model")))
    route["family"] = R.entry_family(entry, route.get("model_id"))
    builds = entry.get("builds")
    if isinstance(builds, list) and builds:   # Factory._model_route: {provider, model_id, provider_entry, limits?}
        listed = [{"provider": b.get("provider"), "model_id": b.get("model"), **{k: b[k] for k in ("max_tokens", "context_tokens") if b.get(k)}}
                  for b in builds if isinstance(b, dict)]
        if not any(b["provider"] == route.get("provider") and b["model_id"] == route.get("model_id") for b in listed):
            listed.insert(0, {"provider": route.get("provider"), "model_id": route.get("model_id")})
        if len(listed) > 1:
            for b in listed:
                b["provider_entry"] = _provider_entry(cfg, b["provider"])
            route["builds"] = listed
    key = entry.get("overflow") or _dict(_dict(cfg.get("providers")).get(route.get("provider"))).get("overflow")
    if overflow and key and key != route.get("model") and isinstance(_dict(cfg.get("models")).get(key), dict):
        route["overflow"] = _route_for(core, cfg, "builder", key, route.get("driver"), overflow=False)
    return route


def _provider_entry(cfg: dict, name) -> dict:
    """orchestrator._provider_entry (as the core mock's resolve_route builds it): the provider's settings, no secrets."""
    p = _dict(_dict(cfg.get("providers")).get(name))
    ptype = p.get("type", "openai")
    return {"type": ptype, "base_url": p.get("base_url"), "api_key_env": p.get("api_key_env"), "api_key_file": p.get("api_key_file"),
            "timeout": float(p.get("timeout") or 900), "auth": p.get("auth") or ("x-api-key" if ptype == "anthropic" else "bearer"),
            "max_concurrency": p.get("max_concurrency"), "concurrency_scope": p.get("concurrency_scope") or "call",
            "headers": {k: v for k, v in _dict(p.get("headers")).items() if not re.search(r"key|token|secret|password|auth", k, re.I)}}


def _route_for(core, cfg: dict, agent: str, key: str, driver: str | None = None, overflow: bool = True) -> dict:
    a = core.STATE["agents"].get(agent) or {"name": agent, "model": "opus"}
    route = core.resolve_route(cfg, a, {"model": key, **({"driver": driver} if driver else {})})
    return _decorate(core, cfg, route, overflow)


def placed_snapshot(core, cfg: dict, snapshot: dict, routing, profile: str | None, only: set | None = None) -> dict:
    """orchestrator._routing's placement: per agent, agents.<name>'s placement fields, the profile's placement over them
    and a per-run route over both (routing.merged_placements); its model, rungs and alternate as whole routes, its
    reason normalised, each route's family, builds and overflow. only: re-place just these agents (Change routing)."""
    raw_agents = _dict(cfg.get("agents"))
    profile_routes = R.profile_routes(cfg, profile) if profile else {}
    per_run = routing if isinstance(routing, dict) else {}
    out = {}
    for agent, route in snapshot.items():
        if only is not None and agent not in only:
            out[agent] = route
            continue
        top = {agent: per_run[agent]} if isinstance(per_run.get(agent), dict) else {}
        # routing.merged_placements(profile, per-run), then Factory._resolve_route: agents.<name>'s own placement
        # (effort, escalate, reason, alternate) applies only when no model is named, by the profile or the run
        placement = R.merged_placements({agent: _dict(profile_routes.get(agent))}, top)[agent]
        if not placement.get("model"):
            own = {k: v for k, v in _dict(raw_agents.get(agent)).items() if k in PLACE_KEYS}
            placement = {**own, **placement}
        route = copy.deepcopy(route)
        if placement.get("model") and placement["model"] != route.get("model"):
            route = _route_for(core, cfg, agent, placement["model"], placement.get("driver") or route.get("driver"))
        else:
            route = _decorate(core, cfg, route)
        if placement.get("reason"):
            route["reason"] = R.reason(placement["reason"])
        if placement.get("effort"):   # an effort becomes output_config.effort (Anthropic) or reasoning_effort (OpenAI format)
            ptype = _dict(route.get("provider_entry")).get("type") or "openai"
            route["extra_body"] = core.deep_merge(route.get("extra_body") or {}, R.effort_body(ptype, placement["effort"]))
        rungs = [k for k in placement.get("escalate") or [] if isinstance(_dict(cfg.get("models")).get(k), dict)]
        if rungs:
            route["escalate"] = [_route_for(core, cfg, agent, k, route.get("driver")) for k in rungs]
        alt = placement.get("independent_alternate")
        if alt is False:
            route["independent_alternate"] = False
        elif isinstance(alt, str) and isinstance(_dict(cfg.get("models")).get(alt), dict) and R.is_judge(agent):
            route["independent_alternate"] = _route_for(core, cfg, agent, alt, route.get("driver"))
        out[agent] = route
    return out


def _estimated_judge(route: dict, builder: dict) -> dict:
    """orchestrator._estimated_judge: where the guard puts a judge while the builder runs on its own model."""
    chosen, status = R.judge_choice(route, builder)
    if status == "alternate":
        left = R.shared_model(route, builder)
        return {**chosen, "independence": "alternate", "instead_of": route.get("model"),
                "reason": {"kind": "stage-fit", "text": R.alternate_text(left)}}
    return {**route, "independence": status} if status else route


def estimate(core, repo: str, kind: str, profile, routing) -> dict:
    """Factory.estimate over the mock's config: the real routing.estimate and routing.independence_map."""
    if repo not in [r["name"] for r in core.STATE["config"].get("repos", [])]:
        raise KeyError(f"unknown repo {repo!r}")
    if kind not in ("feature", "plan"):
        raise core.Invalid("kind must be feature or plan")
    cfg = _cfg_for(core, routing)
    name = _profile(core, cfg, profile)
    reviewers = list(_dict(core.STATE["config"].get("pipeline")).get("reviewers") or [])
    snap = core.routing_snapshot(reviewers, routing)
    if kind == "plan":
        lead = core.resolve_route(cfg, core.STATE["agents"].get("lead") or {"name": "lead", "model": "opus"})
        snap = {"lead": lead, **snap}
    snap = placed_snapshot(core, cfg, snap, routing, name)
    builder = snap.get("builder")
    roles = []
    for agent, route in snap.items():
        if builder is not None and R.is_judge(agent):
            route = _estimated_judge(route, {R.route_family(builder): str(builder.get("model"))})
        roles.append(R.Role(agent, STAGES.get(agent, "review"), _model_ns(cfg, route), route))
    history: dict = {}
    with core.LOCK:
        jobs = [j for j in core.STATE["jobs"].values() if j["repo"] == repo]
        plans = [p for p in core.STATE["plans"].values() if p["repo"] == repo]
    for job in jobs:
        totals: dict = {}
        for run in job.get("runs") or []:
            if isinstance(run, dict) and isinstance(run.get("usage"), dict):
                t = totals.setdefault(run["agent"], {k: 0 for k in USAGE_KEYS})
                for k in USAGE_KEYS:
                    t[k] += int(run["usage"].get(k) or 0)
        for agent, usage in totals.items():
            history.setdefault(agent, []).append(usage)
    plan_jobs = [len(p["jobs"]) for p in plans if p.get("jobs")]
    if kind == "plan":
        history["lead"] = [p["usage"] for p in plans if _dict(p.get("usage")).get("calls")]
    result = R.estimate(roles, history, kind, plan_jobs)
    result.update(repo=repo, profile=name, routing=routing if isinstance(routing, str) else None,
                  independence=R.independence_map(snap))
    return result


# ---------------------------------------------------------------- placing a run (Factory._place, simplified)
def _builder_families(job: dict, round_, agent: str) -> dict:
    """{family: model} the builder ran on in round_ (the approver: in every round so far), plus a hand-off's models."""
    out: dict = {}
    for run in job.get("runs") or []:
        if run.get("agent") == "builder" and (agent == "approver" or run.get("round") == round_):
            out.setdefault(str(run.get("family") or R.route_family(run)), str(run.get("model")))
    if job.get("from_branch") and (agent == "approver" or round_ in (None, 1)):
        for m in job.get("handoff_models") or []:
            out.setdefault(str(m.get("family") or R.default_family(m.get("model_id"))), str(m.get("model")))
    return out


def place_job_run(job: dict, agent: str, round_) -> tuple:
    """(the route the run goes to, its labels) as Factory._place records them on agent.start and in job.runs."""
    route = _dict(_dict(job.get("routing")).get(agent))
    labels: dict = {}
    chosen = route
    if agent == "builder" and route.get("escalate") and job.get("sendbacks"):
        rungs = route["escalate"]
        k = R.escalation_start(len(rungs), int(job["sendbacks"]))
        if k is not None:
            chosen = rungs[k]
            labels["escalation"] = {"rung": k + 1, "of": len(rungs), "sendbacks": int(job["sendbacks"]),
                                    "from": route.get("model"), "model": chosen.get("model")}
    if agent == "builder":
        last = next((r for r in reversed(job.get("runs") or []) if r.get("agent") == "builder"), None)
        if last is not None and (last.get("model"), last.get("model_id")) != (chosen.get("model"), chosen.get("model_id")):
            labels["handover"] = True
    if R.is_judge(agent):
        guard = _builder_families(job, round_, agent)
        if job.get("from_branch") and not job.get("handoff_models") and (agent == "approver" or round_ in (None, 1)):
            labels["independence"] = "unknown"
        elif guard:
            picked, status = R.judge_choice(route, guard)
            if status:
                labels["independence"] = status
                labels["shared_model"] = R.shared_model(route, guard)
            if status == "alternate":
                chosen = picked
    builds = chosen.get("builds") or []
    if len(builds) > 1:
        labels["build"] = {"index": 0, "of": len(builds), "provider": builds[0].get("provider"), "model_id": builds[0].get("model_id")}
    why = R.placement_reason(route.get("reason"), labels)
    if why:
        labels["reason"] = why
    labels["family"] = R.route_family(chosen)
    return chosen, labels


def record_run(core, job: dict, run_name: str, agent: str, round_, route: dict, labels: dict, usage: dict, ok: bool) -> dict:
    """Orchestrator._record_run: job.runs, job.cost and the independence badge's records."""
    cost, priced = _cost(_cfg_for(core, None), route, usage)
    entry = {"run": run_name, "agent": agent, "round": round_, "driver": route.get("driver") or "builtin",
             "model": route.get("model"), "provider": route.get("provider"), "model_id": route.get("model_id"),
             **{k: copy.deepcopy(v) for k, v in labels.items()},
             "ok": ok, "usage": {k: int(_dict(usage).get(k) or 0) for k in ("input_tokens", "output_tokens", "cached_tokens")},
             "cost_usd": cost, "priced": priced}
    with core.LOCK:
        R.add_cost(job.setdefault("cost", R.zero_cost()), cost, priced)
        status = labels.get("independence")
        if status in INDEPENDENCE_RANK:
            if status != "unknown":
                shared = {"round": round_, "agent": agent, **_dict(labels.get("shared_model")),
                          **({"accepted": True} if status == "accepted" else {})}
                if shared not in job.setdefault("shared_models", []):
                    job["shared_models"].append(shared)
            if INDEPENDENCE_RANK[status] > INDEPENDENCE_RANK.get(str(job.get("independence")), 0):
                job["independence"] = status
        job.setdefault("runs", []).append(entry)
    return entry


def _chat_placement(core, chat: dict) -> tuple:
    cfg = core.STATE["config"]
    route = core.resolve_route(cfg, core.STATE["agents"].get(chat.get("agent") or "coder") or {"name": "coder", "model": "opus"},
                               {"model": chat.get("model")} if chat.get("model") else None)
    route = _decorate(core, cfg, route)
    labels = {"family": route["family"]}
    if chat.get("reason"):
        labels["reason"] = chat["reason"]
    sm = _dict(chat.get("session_model"))
    if sm and (sm.get("model"), sm.get("model_id")) != (route.get("model"), route.get("model_id")):
        labels["handover"] = True
    return route, labels


def _lead_placement(core, plan: dict) -> tuple:
    routing = plan.get("routing")
    cfg = _cfg_for(core, routing)
    override = routing.get("lead") if isinstance(routing, dict) else None
    route = _decorate(core, cfg, core.resolve_route(cfg, core.STATE["agents"].get("lead") or {"name": "lead", "model": "opus"}, override))
    reason = _dict(_dict(cfg.get("agents")).get("lead")).get("reason")
    labels = {"family": route["family"], **({"reason": R.reason(reason)} if reason else {})}
    return route, labels


# ---------------------------------------------------------------- wrapping the core (once per core module)
def _install(core) -> None:
    if getattr(core, _WRAPPED, False):
        return
    setattr(core, _WRAPPED, True)
    core.CONFIG_SECTIONS = tuple(core.CONFIG_SECTIONS) + tuple(s for s in ("profiles", "default_profile") if s not in core.CONFIG_SECTIONS)

    orig_view = core.config_view

    def config_view():
        view = orig_view()
        cfg = core.STATE["config"]
        view["model_families"] = {str(k): R.entry_family(e) for k, e in _dict(cfg.get("models")).items() if isinstance(e, dict)}
        routes = _dict(cfg.get("agents"))
        for a in view.get("agent_list") or []:
            route = routes.get(a.get("name"))
            if not isinstance(route, dict):
                continue
            for key in ("effort", "escalate", "independent_alternate"):
                if route.get(key) or (key == "independent_alternate" and route.get(key) is False):
                    a[key] = copy.deepcopy(route[key])
            try:
                found = R.reason(route.get("reason"))
            except ValueError:
                found = None
            if found:
                a["reason"] = found
        return view
    core.config_view = config_view

    orig_validate = core.validate_config

    def validate_config(cfg: dict) -> None:
        orig_validate(cfg)
        try:
            check_settings(core, cfg)
        except ValueError as exc:
            raise core.Invalid(str(exc)) from None
    core.validate_config = validate_config

    orig_update = core.update_config

    def update_config(patch):
        if not isinstance(patch, dict) or not any(k in patch for k in ("profiles", "default_profile")):
            return orig_update(patch)
        rest = {k: v for k, v in patch.items() if k not in ("profiles", "default_profile")}
        with core.LOCK:
            before = core.STATE["config"]
            staged = copy.deepcopy(before)
            if "profiles" in patch:
                value = patch["profiles"]
                if value is None:
                    staged.pop("profiles", None)
                elif isinstance(value, dict) and isinstance(staged.get("profiles"), dict):
                    staged["profiles"] = core.deep_merge(staged["profiles"], value)
                else:
                    staged["profiles"] = copy.deepcopy(value)
            if "default_profile" in patch:
                if patch["default_profile"] is None:
                    staged.pop("default_profile", None)
                else:
                    staged["default_profile"] = patch["default_profile"]
            core.STATE["config"] = staged
            try:
                return orig_update(rest)
            except BaseException:
                core.STATE["config"] = before
                raise
    core.update_config = update_config

    orig_new_job = core.new_job

    def new_job(*args, **kwargs):
        pending = getattr(_PENDING, "job", None)
        _PENDING.job = None
        routing = kwargs.get("routing", args[3] if len(args) > 3 else None)
        job = orig_new_job(*args, **kwargs)
        try:
            cfg = _cfg_for(core, routing)
            name = R.profile_name(cfg, pending.get("profile") if pending else None)
            per_run = routing
            if pending and pending.get("placements") and isinstance(routing, dict):
                per_run = {a: {**_dict(routing.get(a)), **pending["placements"].get(a, {})} for a in set(routing) | set(pending["placements"])}
            job["routing"] = placed_snapshot(core, cfg, job["routing"], per_run, name)
            job.update(profile=name, cost=R.zero_cost(), runs=[], sendbacks=0)
            if pending and pending.get("handoff_models") is not None:
                job["handoff_models"] = copy.deepcopy(pending["handoff_models"])
            with core.LOCK:
                core.files_of(job)["job.json"] = json.dumps(core.public_job(job), indent=2)
        except Exception as exc:   # never break the core's own job
            sys.stderr.write(f"[routing mock] new_job: {exc!r}\n")
        return job
    core.new_job = new_job

    orig_new_plan = core.new_plan

    def new_plan(*args, **kwargs):
        pending = getattr(_PENDING, "plan", None)
        _PENDING.plan = None
        routing = kwargs.get("routing", args[2] if len(args) > 2 else None)
        plan = orig_new_plan(*args, **kwargs)
        try:
            plan.update(profile=R.profile_name(_cfg_for(core, routing), pending.get("profile") if pending else None), cost=R.zero_cost())
        except Exception as exc:
            sys.stderr.write(f"[routing mock] new_plan: {exc!r}\n")
        return plan
    core.new_plan = new_plan

    orig_publish = core.BUS.publish

    def publish(type_, data=None, repo=None, job=None, agent=None, ts=None):
        if type_ in ("chat.updated", "chat.created") and isinstance(data, dict) and isinstance(data.get("chat"), dict):
            try:
                with core.LOCK:
                    chat = core.STATE["chats"].get(data["chat"].get("id"))
                    if chat is not None and "cost" not in chat:
                        pending = getattr(_PENDING, "chat", None)
                        _PENDING.chat = None
                        _chat_fields(core, chat, pending.get("profile") if pending else None)
                    if chat is not None:
                        data = dict(data, chat=copy.deepcopy(chat))
            except Exception as exc:
                sys.stderr.write(f"[routing mock] chat fields: {exc!r}\n")
        return orig_publish(type_, data, repo=repo, job=job, agent=agent, ts=ts)
    core.BUS.publish = publish

    orig_emit = core.Run.emit
    orig_end = core.Run.end

    def emit(self, type_, data):
        try:
            if type_ == "agent.start":
                data = dict(data)
                script = getattr(self, "_routing_script", None)
                if script is not None:
                    self._routing_placed = (script["route"], dict(script["labels"]))
                elif not self.instant:
                    if self.job is not None:
                        if self.agent == "builder" and isinstance(self.round, int) and self.round > 1:
                            self.job["sendbacks"] = max(int(self.job.get("sendbacks") or 0), self.round - 1)
                        self._routing_placed = place_job_run(self.job, self.agent, self.round)
                    elif self.chat is not None:
                        self._routing_placed = _chat_placement(core, self.chat)
                    elif self.plan is not None:
                        self._routing_placed = _lead_placement(core, self.plan)
                placed = getattr(self, "_routing_placed", None)
                if placed is not None and isinstance(data.get("grid"), dict):
                    placed[1].setdefault("grid", copy.deepcopy(data["grid"]))   # Factory._run's labels carry it to job.runs
                if placed is not None:
                    route, labels = placed
                    data.update(model=route.get("model"), model_id=route.get("model_id"), provider=route.get("provider"),
                                driver=route.get("driver") or data.get("driver"), **copy.deepcopy(labels))
            elif type_ == "agent.end" and getattr(self, "_routing_placed", None) is not None:
                data = dict(data)
                cost, priced = _cost(_cfg_for(core, None), self._routing_placed[0], data.get("usage"))
                data.update(cost_usd=cost, priced=priced)
        except Exception as exc:
            sys.stderr.write(f"[routing mock] emit {type_}: {exc!r}\n")
        return orig_emit(self, type_, data)
    core.Run.emit = emit

    def end(self, ok=True, error=None):
        orig_end(self, ok, error)
        placed = getattr(self, "_routing_placed", None)
        if placed is None:
            return
        route, labels = placed
        try:
            if self.job is not None:
                record_run(core, self.job, self.run, self.agent, self.round, route, labels, self.usage, ok)
                if not self.instant:
                    core.save_job(self.job)
            elif self.chat is not None:
                cost, priced = _cost(core.STATE["config"], route, self.usage)
                with core.LOCK:
                    R.add_cost(self.chat.setdefault("cost", R.zero_cost()), cost, priced)
                    model = {"model": route.get("model"), "provider": route.get("provider"), "model_id": route.get("model_id"), "family": labels.get("family")}
                    if model not in self.chat.setdefault("models", []):
                        self.chat["models"].append(model)
                    sm = {"driver": route.get("driver"), "model": route.get("model"), "provider": route.get("provider"), "model_id": route.get("model_id")}
                    if self.chat.get("session_model") and self.chat["session_model"] != sm:
                        self.chat.setdefault("earlier_sessions", []).append({"file": f"session-{len(self.chat['earlier_sessions']) + 1}.json",
                                                                             **self.chat["session_model"], "handover_text": None})
                    self.chat["session_model"] = sm
            elif self.plan is not None:
                cost, priced = _cost(_cfg_for(core, self.plan.get("routing")), route, self.usage)
                with core.LOCK:
                    R.add_cost(self.plan.setdefault("cost", R.zero_cost()), cost, priced)
        except Exception as exc:
            sys.stderr.write(f"[routing mock] end: {exc!r}\n")
    core.Run.end = end

    orig_preset_config = core.preset_config

    def preset_config(cfg, name):
        """settings.preset_config: a preset's routing profiles replace same-named ones whole (the core deep-merges)."""
        merged = orig_preset_config(cfg, name)
        profiles = _dict(core.load_preset(name).get("profiles"))
        if profiles:
            merged["profiles"] = {**_dict(cfg.get("profiles")), **copy.deepcopy(profiles)}
        return merged
    core.preset_config = preset_config

    orig_doctor = core.doctor_checks

    def doctor_checks():
        return orig_doctor() + doctor_rows(core)
    core.doctor_checks = doctor_checks


def _chat_fields(core, chat: dict, profile) -> None:
    """chats.new_chat's placement fields: profile, reason, cost, session_model, models (every model a turn ran on)."""
    cfg = core.STATE["config"]
    name = R.profile_name(cfg, profile)
    agent = chat.get("agent") or "coder"
    placement = R.merged_placements({agent: {**{k: v for k, v in _dict(_dict(cfg.get("agents")).get(agent)).items() if k in PLACE_KEYS},
                                             **_dict(R.profile_routes(cfg, name).get(agent))}},
                                    {agent: {"model": chat["model"]}} if chat.get("model") else {})[agent]
    chat.update(profile=name, reason=R.reason(placement.get("reason")) if placement.get("reason") else None, cost=R.zero_cost(),
                session_model=None, models=[], earlier_sessions=[])


# ---------------------------------------------------------------- settings validation (the real messages)
def check_settings(core, cfg: dict) -> None:
    """settings.validate_settings for the placement fields only (the core mock checks the rest its own way)."""
    providers = _dict(cfg.get("providers"))
    for name, entry in providers.items():
        if not isinstance(entry, dict):
            continue
        label = f"providers.{name}"
        S._optional(entry, label, "kind", lambda v: v in S.PROVIDER_KINDS, "one of " + ", ".join(S.PROVIDER_KINDS) + " or null")
        S._optional(entry, label, "loaded_model_url", S._is_http_url, "an http(s) URL or null")
        for flag in ("allow_model_swap", "swap_back", "lazy_load"):
            S._optional(entry, label, flag, lambda v: isinstance(v, bool), "true or false")
        S.parse_custody(entry.get("custody"), f"{label}.custody")
        S._optional(entry, label, "overflow", lambda v: isinstance(v, str) and v.strip(), "a model key or null")
        for key in ("overflow_after_seconds", "tokens_per_second"):
            S._optional(entry, label, key, S._is_positive_number, "a positive number or null")
    S._check_custody_servers(providers)
    models = _dict(cfg.get("models"))
    live_providers = {n for n, e in providers.items() if isinstance(e, dict)}
    for key, entry in models.items():
        if not isinstance(entry, dict):
            continue
        label = f"models.{key}"
        S._optional(entry, label, "family", lambda v: isinstance(v, str) and S.FAMILY_LABEL_RE.fullmatch(v.strip().lower()),
                    "a family label such as \"openai\" (a-z, 0-9, '.', '_' and '-') or null")
        for price in ("price_in", "price_out", "price_cached_in"):
            S._optional(entry, label, price, S._is_non_negative_number, "a non-negative number (USD per million tokens)")
        if (entry.get("price_in") is None) != (entry.get("price_out") is None):
            raise ValueError(f"{label}: set price_in and price_out together (a missing price means unknown)")
        if entry.get("price_cached_in") is not None and entry.get("price_in") is None:
            raise ValueError(f"{label}.price_cached_in needs price_in and price_out")
        builds = entry.get("builds")
        if builds is not None:
            S._require(builds, f"{label}.builds", lambda v: isinstance(v, list), "a list of {provider, model} objects")
            for index, build in enumerate(builds):
                where = f"{label}.builds[{index}]"
                S._require(build, where, lambda v: isinstance(v, dict) and not set(v) - S.BUILD_KEYS,
                           "an object like {\"provider\": ..., \"model\": ..., \"context_tokens\": ...}")
                if build.get("provider") not in live_providers:
                    raise ValueError(f"{where}.provider {build.get('provider')!r} is not a configured provider")
                S._require(build.get("model"), f"{where}.model", lambda v: isinstance(v, str) and v.strip(), "a model id")
                S._optional(build, where, "max_tokens", S._is_positive_int, "a positive integer")
                S._optional(build, where, "context_tokens", S._is_positive_int, "a positive integer")
                limit = build.get("max_tokens") or entry.get("max_tokens") or 32768
                if build.get("context_tokens") is not None and build["context_tokens"] <= limit:
                    raise ValueError(f"{where}.context_tokens must be larger than max_tokens ({limit})")
    live_models = {k for k, e in models.items() if isinstance(e, dict)}
    for key, entry in models.items():
        if isinstance(entry, dict) and entry.get("overflow") is not None:
            S._check_overflow(f"models.{key}", entry["overflow"], live_models, key)
    for name, entry in providers.items():
        if isinstance(entry, dict) and entry.get("overflow") is not None:
            S._check_overflow(f"providers.{name}", entry["overflow"], live_models)
    drivers = {n for n, e in _dict(cfg.get("drivers")).items() if isinstance(e, dict)}
    for name, entry in _dict(cfg.get("agents")).items():
        if not isinstance(entry, dict):
            continue
        placed = [k for k in PLACE_KEYS if k in entry]
        if name == "*" and placed:
            raise ValueError(f"agents.*: {', '.join(placed)} apply to one agent's placement; set them on "
                             f"agents.<name> or in a profile (agents.* takes model, driver and extra_body)")
        R.check_placement({k: entry[k] for k in placed}, f"agents.{name}", live_models, agent=str(name))
    R.check_profiles(cfg, live_models, drivers, S.AGENT_NAME_RE)
    checks = []   # settings._check_efforts and _check_alternates over agents.<name> and every profile placement
    for name, entry in _dict(cfg.get("agents")).items():
        if name != "*" and isinstance(entry, dict):
            checks.append((f"agents.{name}", str(name), entry))
    for pname, prof in _dict(cfg.get("profiles")).items():
        for name, entry in _dict(_dict(prof).get("agents")).items():
            if isinstance(entry, dict):
                checks.append((f"profiles.{pname}.agents.{name}", str(name), entry))
    for label, agent, entry in checks:
        key = entry.get("model") or core.route_model(agent, (core.STATE["agents"].get(agent) or {}).get("model"))
        own = models.get(str(key))
        if entry.get("effort") and isinstance(own, dict):
            ptype = _dict(providers.get(own.get("provider"))).get("type")
            problem = R.effort_problem(ptype, entry["effort"])
            if problem:
                raise ValueError(f"{label} on model {key}: {problem}")
        alt = entry.get("independent_alternate")
        if alt and isinstance(models.get(str(alt)), dict):
            family = R.entry_family(own) if isinstance(own, dict) else R.entry_family({}, key)
            if R.entry_family(models[str(alt)]) == family:
                raise ValueError(f"{label}.independent_alternate {alt!r} is of the {family} family, like the "
                                 f"judge's own model {key!r}: name a model of another family")


# ---------------------------------------------------------------- doctor and custody
def doctor_rows(core) -> list:
    """The doctor's placement checks (DESIGN §23, §37, §38), as their real names and details read."""
    providers = _dict(core.STATE["config"].get("providers"))
    rows = []
    if "tabby" in providers:
        rows += [
            {"name": "provider tabby models", "ok": True, "level": "info",
             "detail": "qwen3.8-flash-next listed by http://127.0.0.1:5000/v1/models (3 models)"},
            {"name": "provider tabby loaded model", "ok": True, "level": "info",
             "detail": "qwen3.8-27b-exl3 loaded; runs on qwen3.8-flash-next make it load them (allow_model_swap)"},
        ]
    if "nanogpt" in providers:
        rows.append({"name": "provider nanogpt models", "ok": False, "level": "warn",
                     "detail": "https://nano-gpt.com/api/v1/models does not list google/gemini-4-argon (412 models listed); "
                               "correct the id in Settings → Models"})
    rows += [
        {"name": "local servers", "ok": True, "level": "info", "detail": "up: strata, tabby (qwen3.8-27b-exl3 loaded); down: ollama, vllm; to look for a model server on another port, press \"Look for model servers\" on the setup page's Server details step (Setup, then Server details), or run factory doctor --look-around"},  # doctor.LOOK_AROUND_ADVICE
        {"name": "placement", "ok": True, "level": "info",
         "detail": "1 extra builds, 1 overflows, 0 escalation ladders, 2 independent alternates, "
                   f"{len(_dict(core.STATE['config'].get('profiles')))} profiles"
                   + (f" (default {core.STATE['config'].get('default_profile')})" if core.STATE["config"].get("default_profile") else "")
                   + "; references resolve"},
    ]
    if _dict(providers.get("tabby")).get("custody"):
        rows.append({"name": "custody tabby", "ok": True, "level": "info",
                     "detail": "the register answers (generation 42; gpu0/qwen3.8-flash-next); runs on tabby take a request lease"})
    strata = _dict(providers.get("strata"))
    strata_custody = _dict(strata.get("custody"))
    if strata_custody and not (strata.get("lazy_load") or strata.get("allow_model_swap")):
        rows.append({"name": "custody strata", "ok": False, "level": "warn",
                     "detail": "http://127.0.0.1:7400 is configured, but neither lazy_load nor allow_model_swap is set on strata: "
                               "no call there takes a lease"})
    # doctor._custody_check in the server (in_server: never the shell-only "custody <p> credential" warning, which
    # only `factory doctor` in a shell gives): the credential is not loaded
    elif strata_custody.get("token_credential"):
        from swfactory.plainhttp import credential_problem
        rows.append({"name": "custody strata", "ok": False, "level": "error",
                     "detail": f"{credential_problem(strata_custody['token_credential'])}: every run on strata that needs a lease "
                               f"is unavailable and follows its overflow"})
    rows.append({"name": "drift source checker", "ok": False, "level": "warn",
                 "detail": "last check: cannot_read (unknown schemaVersion 3; this source reads 1, 2), observed 2026-10-22T07:58:10Z"})
    return rows


def custody_view(core) -> dict:
    """GET /api/custody (Factory.custody): a register with a held lease and a denial, a follower entry, a fenced one."""
    st = store(core, AREA)
    cfg = core.STATE["config"]
    providers = _dict(cfg.get("providers"))
    until = core.iso(time.time() + 1500)
    resident = [{"provider": "gpu0", "model": "qwen3.8-flash-next", "vram_mib": 21840, "since": core.ago(7300)}]
    gpu0 = {"url": "https://127.0.0.1:7300", "generation": 42, "epoch": "reg-2026-10-20", "fenced": None,
            "residency": {"generation": 42, "epoch": "reg-2026-10-20", "observed_at": core.ago(st.get("read_ago", 95)),
                          "read_at": core.ago(st.get("read_ago", 95) - 1), "resident": resident},
            "leases": [{"lease_id": "L-7f3a9c", "provider": "tabby", "model": "qwen3.8-flash-next", "register_provider": "gpu0",
                        "register_model": "qwen3.8-flash-next", "purpose": "job my-app/011-cache-the-pricing-page builder",
                        "holder": "harness-4711", "until": until, "generation": 42, "epoch": "reg-2026-10-20", "state": "held", "error": None, "users": 1,
                        "resident_before": [["gpu0", "qwen3.8-flash-next"]], "resident_after": None},
                       {"lease_id": "L-51c0d2", "provider": "tabby", "model": "qwen3.8-27b-exl3", "register_provider": "gpu0",
                        "register_model": "qwen3.8-27b-exl3", "purpose": "chat c-1a2b3c4d5e <img src=x onerror=alert('purpose')>",
                        "holder": "harness-4711", "until": core.ago(600), "generation": 40, "epoch": "reg-2026-10-19", "state": "lost",
                        "error": "the register restarted: it no longer knows this lease", "users": 0, "resident_before": [], "resident_after": []}],
            "history": [{"at": core.ago(1500), "op": "release", "outcome": "released", "provider": "tabby", "model": "qwen3.8-27b-exl3",
                         "lease_id": "L-41aa07"},
                        {"at": core.ago(1400), "op": "release", "outcome": "lost", "provider": "gpu0", "model": "qwen3.8-27b-exl3",
                         "lease_id": "L-51c0d2", "purpose": "chat c-1a2b3c4d5e",
                         "reason": "not sent: the register restarted (epoch reg-2026-10-19 became reg-2026-10-20) (the run ended)"},
                        {"at": core.ago(1200), "op": "claim", "outcome": "denied", "reason": "LEASED", "provider": "tabby",
                         "model": "qwen3.8-27b-exl3", "purpose": "wake kate W-2210 <b>check-in</b>",
                         "words": "another lease covers it", "generation": 42},
                        {"at": core.ago(900), "op": "claim", "outcome": "denied", "reason": "FULL", "provider": "gpu0",
                         "model": "qwen3.8-27b-exl3", "purpose": "job my-app/011-cache-the-pricing-page builder",
                         "words": "the card cannot fit this model now", "generation": 42},
                        {"at": core.ago(700), "op": "wait", "outcome": "waiting", "reason": "HELD", "provider": "gpu0",
                         "model": "qwen3.8-flash-next", "purpose": "chat c-1a2b3c4d5e", "seconds": 900},
                        {"at": core.ago(640), "op": "wait", "outcome": "granted", "reason": "HELD", "provider": "gpu0",
                         "model": "qwen3.8-flash-next", "purpose": "chat c-1a2b3c4d5e", "seconds": 60.2},
                        {"at": core.ago(300), "op": "claim", "outcome": "granted", "provider": "tabby", "model": "qwen3.8-flash-next",
                         "purpose": "job my-app/011-cache-the-pricing-page builder", "lease_id": "L-7f3a9c", "generation": 42}]}
    strata = {"url": "http://127.0.0.1:7400", "generation": 3, "epoch": None, "fenced": {"seen": 17, "answered": 3, "since": core.ago(5400)},
              "residency": None, "leases": [], "history": [{"at": core.ago(5400), "op": "residency", "outcome": "stale",
                                                           "reason": "generation 3 is older than 17"}]}
    rows = []
    for name in sorted(providers):
        entry = providers[name]
        if not isinstance(entry, dict):
            continue
        own = _dict(entry.get("custody"))
        follows = None
        if not own and name == "tabby-raw" and _dict(providers.get("tabby")).get("custody"):
            own, follows = _dict(providers["tabby"]["custody"]), "tabby"
        if not own:
            continue
        settings = {"url": own.get("url"), "timeout": float(own.get("timeout", C.DEFAULT_TIMEOUT)),
                    **{k: own[k] for k in ("token_file", "token_env", "token_credential", "host_header", "ca_file", "provider", "models")
                       if own.get(k)}}
        needs = bool(entry.get("lazy_load") or entry.get("allow_model_swap"))
        register = strata if own.get("url") == "http://127.0.0.1:7400" else gpu0
        rows.append({"provider": name, "lazy_load": bool(entry.get("lazy_load")), "allow_model_swap": bool(entry.get("allow_model_swap")),
                     "needs_lease": needs, "custody_from": follows, "held_wait_seconds": entry.get("overflow_after_seconds") or None,
                     "settings": settings, "register": copy.deepcopy(register)})
    return {"providers": rows, "reason_codes": dict(C.REFUSAL_WORDS)}


# ---------------------------------------------------------------- seeding
def _patch_config(core) -> None:
    cfg = core.STATE["config"]
    providers = cfg.setdefault("providers", {})
    for name, extra in PROVIDER_PLACEMENT.items():
        if isinstance(providers.get(name), dict):
            providers[name].update(copy.deepcopy(extra))
    for name, entry in EXTRA_PROVIDERS.items():
        providers.setdefault(name, copy.deepcopy(entry))
    models = cfg.setdefault("models", {})
    for key, extra in MODEL_PLACEMENT.items():
        if isinstance(models.get(key), dict):
            models[key].update(copy.deepcopy(extra))
    for key, entry in EXTRA_MODELS.items():
        models.setdefault(key, copy.deepcopy(entry))
    agents = cfg.setdefault("agents", {})
    for name, extra in AGENT_PLACEMENT.items():
        agents[name] = {**_dict(agents.get(name)), **copy.deepcopy(extra)}
    cfg["profiles"] = copy.deepcopy(PROFILES)
    cfg["default_profile"] = DEFAULT_PROFILE


def _log_events(core, job: dict) -> list:
    """[(rel, [events])] of the job's run logs, in run order."""
    files = core.files_of(job)
    out = []
    for rel in sorted(k for k in files if re.match(r"^logs/\d+-[\w.-]+\.jsonl$", k)):
        out.append((rel, [json.loads(x) for x in files[rel].splitlines() if x.strip()]))
    return out


def backfill(core, job: dict, profile) -> None:
    """A seeded job as the placement engine would have left it: its snapshot placed (profile), and each finished run
    in job.runs with its placement labels and cost; the agent.start / agent.end lines of its logs say the same."""
    cfg = core.STATE["config"]
    job["routing"] = placed_snapshot(core, cfg, job["routing"], None, profile)
    job.update(profile=profile, cost=R.zero_cost(), runs=[], sendbacks=0)
    for key in ("independence", "shared_models"):
        job.pop(key, None)
    files = core.files_of(job)
    for rel, events in _log_events(core, job):
        start = next((e for e in events if e["type"] == "agent.start"), None)
        if start is None:
            continue
        d = start["data"]
        agent, round_ = d.get("agent") or start.get("agent"), d.get("round")
        if agent == "builder" and isinstance(round_, int) and round_ > 1:
            job["sendbacks"] = round_ - 1
        if agent not in _dict(job.get("routing")):
            continue
        route, labels = place_job_run(job, agent, round_)
        d.update(model=route.get("model"), model_id=route.get("model_id"), provider=route.get("provider"), **copy.deepcopy(labels))
        end = next((e for e in reversed(events) if e["type"] == "agent.end"), None)
        if end is not None:
            usage = _dict(end["data"].get("usage"))
            entry = record_run(core, job, rel[5:-6], agent, round_, route, labels, usage, end["data"].get("ok") is not False)
            end["data"].update(cost_usd=entry["cost_usd"], priced=entry["priced"])
        files[rel] = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)
    files["job.json"] = json.dumps(core.public_job(job), indent=2)


def _scripted_run(core, job: dict, agent: str, round_, route: dict, labels: dict, *, inp: int, out: int, text: str,
                  ok: bool = True, error: str | None = None, tool=None) -> None:
    r = core.Run(agent, core.log_name(job, agent), job=job, round_=round_, instant=True)
    r._routing_script = {"route": route, "labels": labels}
    r.start()
    r.reasoning(text)
    if tool:
        r.tool(*tool)
    r.call(inp, out)
    r.end(ok, error)


def _showcase(core) -> None:
    """Three jobs that show every placement record: my-app/011 (local-first: a second build with a swap and a custody
    lease, an escalation with a hand-over, an independent alternate, an overflow that reduced a judge's independence,
    an unpriced model), my-app/010 (a chat hand-off, accepted) and docs-site/004 (a hand-off without models: unknown)."""
    cfg = core.STATE["config"]
    allp = {"security": "PASS", "ux": "PASS", "ui": "PASS", "code": "PASS"}
    core.seed_clock(5400)
    j = core.new_job("my-app", "Cache the pricing page for 5 minutes and purge it when a plan price changes", created_ago=5600,
                     id="011-cache-the-pricing-page")
    j["routing"] = placed_snapshot(core, cfg, core.routing_snapshot(j["reviewers"], None), None, "local-first")
    j.update(profile="local-first", cost=R.zero_cost(), runs=[], sendbacks=0, _seed=True, base_commit="7d1e2f3a")
    rt = j["routing"]
    spec = rt["spec-writer"]
    _scripted_run(core, j, "spec-writer", None, spec, {"family": spec["family"], "reason": spec.get("reason") or {"kind": "capability", "text": "the spec sets the bar"}},
                  inp=152000, out=8100, text="Read the pricing view and the cache settings first.")
    b = rt["builder"]
    tabby = {**b, "provider": "tabby", "model_id": "qwen3.8-flash-next"}
    custody = {"lease_id": "L-7f3a9c", "provider": "tabby", "model": "qwen3.8-flash-next", "until": core.ago(-1500), "generation": 42,
               "resident_before": [["gpu0", "qwen3.8-27b-exl3"]]}   # custody.Claims.info: the register's (provider, model) pairs
    refused = [{"provider": "strata", "model": "strata", "code": "FULL", "words": C.refusal_words("FULL", "strata")}]   # custody.Claims.info
    _scripted_run(core, j, "builder", 1, tabby, {"build": {"index": 1, "of": 2, "provider": "tabby", "model_id": "qwen3.8-flash-next"},
                                                  "swap": {"provider": "tabby", "from": "qwen3.8-27b-exl3", "to": "qwen3.8-flash-next"},
                                                  "custody": custody, "custody_refused": refused, "family": "qwen", "reason": b.get("reason")},
                  inp=1210000, out=38000, text="Strata's card cannot fit the model now: the run goes to the Tabby build.",
                  tool=("Bash", {"command": "pytest tests/test_pricing.py"}, "4 passed"))
    verdicts = {1: {"security": "PASS", "ux": "CHANGES", "ui": "PASS", "code": "PASS"}, 2: dict(allp)}
    for rv in j["reviewers"]:
        route = rt["reviewer-" + rv]
        labels = {"family": route["family"], **({"reason": route["reason"]} if route.get("reason") else {})}
        if rv == "security":
            labels.update(independence="accepted", shared_model={"model": "local", "model_id": "strata", "family": "qwen", "builder_model": "local"})
        _scripted_run(core, j, "reviewer-" + rv, 1, route, labels, inp=246000, out=5800, text=f"Review lens {rv}: {verdicts[1][rv]}.")
    j["sendbacks"] = 1
    rung = b["escalate"][0]
    _scripted_run(core, j, "builder", 2, rung, {"escalation": {"rung": 1, "of": 1, "sendbacks": 1, "from": "local", "model": rung["model"]},
                                                 "handover": True, "family": rung["family"],
                                                 "reason": {"kind": "capability", "text": "escalation rung 1 of 1 after 1 send-back(s)"}},
                  inp=640000, out=21000, text="A fresh session on GPT-6.1 Sol with the hand-over: spec, build.md, the UX review.")
    for rv in j["reviewers"]:
        route = rt["reviewer-" + rv]
        labels = {"family": route["family"], **({"reason": route["reason"]} if route.get("reason") else {})}
        run_route = route
        if rv == "code":
            alt = route["independent_alternate"]
            shared = {"model": "gpt-sol", "model_id": "openai/gpt-6.1-sol", "family": "openai", "builder_model": "gpt-sol"}
            run_route = alt
            labels = {"family": alt["family"], "independence": "alternate", "shared_model": shared,
                      "reason": {"kind": "stage-fit", "text": R.alternate_text(shared)}}
        if rv == "ui":
            over = _route_for(core, cfg, "reviewer-ui", "gpt-sol")
            run_route = over
            labels = {"family": "openai", "overflow": {"from": "gemini-argon", "to": "gpt-sol", "why": "no slot of nanogpt came free within 900s"},
                      "reason": {"kind": "time", "text": "overflow from gemini-argon: no slot of nanogpt came free within 900s"},
                      "independence": "reduced",
                      "shared_model": {"model": "gpt-sol", "model_id": "openai/gpt-6.1-sol", "family": "openai", "builder_model": "gpt-sol"}}
        _scripted_run(core, j, "reviewer-" + rv, 2, run_route, labels, inp=251000, out=6100, text=f"Review lens {rv}, round 2: PASS.")
    ap = rt["approver"]
    _scripted_run(core, j, "approver", 2, ap, {"family": ap["family"], **({"reason": ap["reason"]} if ap.get("reason") else {})},
                  inp=150000, out=3200, text="ESCALATE: the cache purge touches billing; a person should decide.")
    core.job_log(j, "info", "independence reduced: reviewer-ui ran on gpt-sol (openai/gpt-6.1-sol), the openai family the builder "
                            "ran on in round 2: its own model gemini-argon overflowed to gpt-sol, and it has no independent_alternate")
    j.update(stage="needs-human", round=2, resumes=1, reviews=dict(allp), history=[{"round": 1, "reviews": verdicts[1]}, {"round": 2, "reviews": dict(allp)}],
             decision="ESCALATE", reason="approver escalated: the cache purge touches billing")
    j["updated_at"] = core.ago(2400)
    # 010: a chat hand-off whose chat ran on the local Qwen; the security lens shares that family by the routing's choice
    h = core.new_job("my-app", "Search filters for the reports list", created_ago=20000, id="010-search-filters-from-chat",
                     from_branch="chat/c-77aa11bb22")
    h["routing"] = placed_snapshot(core, cfg, h["routing"], None, "balanced")
    h.update(profile="balanced", cost=R.zero_cost(), runs=[], sendbacks=0, _seed=True,
             handoff_models=[{"model": "local", "provider": "strata", "model_id": "strata", "family": "qwen"}], base_commit="2b3c4d5e")
    core.seed_clock(19000)
    for agent in ["spec-writer", *("reviewer-" + r for r in h["reviewers"]), "approver"]:
        route, labels = place_job_run(h, agent, None if agent == "spec-writer" else 1)
        if agent == "reviewer-ux":   # strata/qwen, like the chat's model: the routing has no alternate for it
            labels.update(independence="reduced", shared_model={"model": "sonnet", "model_id": "strata", "family": "qwen", "builder_model": "local"})
        _scripted_run(core, h, agent, None if agent == "spec-writer" else 1, route, labels, inp=120000, out=4000, text="Checked the branch the chat wrote.")
    h.update(stage="merged", round=1, reviews=dict(allp), history=[{"round": 1, "reviews": dict(allp)}], decision="APPROVE",
             merged_commit="b4c5d6e7f8091a2b3c4d")
    h["updated_at"] = core.ago(15000)
    # docs-site/004: a hand-off from a chat whose models were not recorded: its round-1 judges cannot be compared
    u = core.new_job("docs-site", "Redirect the old doc links", created_ago=30000, id="004-redirect-old-doc-links",
                     from_branch="chat/c-0011223344")
    u["routing"] = placed_snapshot(core, cfg, u["routing"], None, None)
    u.update(profile=None, cost=R.zero_cost(), runs=[], sendbacks=0, _seed=True, handoff_models=[], base_commit="9a8b7c6d")
    core.seed_clock(29000)
    for agent in ["spec-writer", *("reviewer-" + r for r in u["reviewers"]), "approver"]:
        route, labels = place_job_run(u, agent, None if agent == "spec-writer" else 1)
        _scripted_run(core, u, agent, None if agent == "spec-writer" else 1, route, labels, inp=90000, out=3000, text="Reviewed the redirects.")
    u.update(stage="merged", round=1, reviews=dict(allp), history=[{"round": 1, "reviews": dict(allp)}], decision="APPROVE",
             merged_commit="c1d2e3f4a5b6c7d8e9f0")
    u["updated_at"] = core.ago(26000)
    core.SEED_CLOCK["t"] = None
    for job in (j, h, u):
        core.STATE["diffs"][(job["repo"], job["id"])] = {"stat": core.NORMAL_STAT, "diff": core.NORMAL_DIFF, "truncated": False}
        core.files_of(job)["spec.md"] = core.spec_md(job["feature"])
        core.files_of(job)["job.json"] = json.dumps(core.public_job(job), indent=2)


def seed(core, fresh: bool) -> None:
    _install(core)
    st = store(core, AREA)
    st.clear()
    if fresh:
        return
    _patch_config(core)
    profiles = {"004-render-user-notes": None}
    with core.LOCK:
        jobs = [j for j in core.STATE["jobs"].values()]
    for job in jobs:
        backfill(core, job, profiles.get(job["id"], DEFAULT_PROFILE) if job["repo"] == "my-app" else None)
    _showcase(core)
    lead_usage = {"P001-csv-export-for-reports": {"input_tokens": 412000, "output_tokens": 15200, "cached_tokens": 130000, "calls": 9},
                  "P002-audit-log-v2": {"input_tokens": 186000, "output_tokens": 7400, "cached_tokens": 52000, "calls": 4}}
    for plan in core.STATE["plans"].values():
        if plan["id"] in lead_usage and not _dict(plan.get("usage")).get("calls"):
            plan["usage"] = dict(lead_usage[plan["id"]])
        route, labels = _lead_placement(core, plan)
        cost, priced = _cost(_cfg_for(core, plan.get("routing")), route, plan.get("usage"))
        plan.update(profile=DEFAULT_PROFILE if plan["repo"] == "my-app" else None,
                    cost={"usd": cost or 0.0, "priced": priced, "unpriced_runs": 0 if priced else 1})
        core.plan_files(plan)["plan.json"] = json.dumps(core.public_job(plan), indent=2)
    for chat in core.STATE["chats"].values():
        _chat_fields(core, chat, None)
        route, labels = _chat_placement(core, chat)
        cost, priced = _cost(core.STATE["config"], route, chat.get("usage"))
        chat["cost"] = {"usd": cost or 0.0, "priced": priced, "unpriced_runs": 0 if priced else 1}
        chat["session_model"] = {"driver": route.get("driver"), "model": route.get("model"), "provider": route.get("provider"), "model_id": route.get("model_id")}
        chat["models"] = [{"model": route.get("model"), "provider": route.get("provider"), "model_id": route.get("model_id"), "family": labels["family"]}]
    first = core.STATE["chats"].get("c-1a2b3c4d5e")
    if first is not None:   # one earlier session: a turn moved the chat to another model (a hand-over, §19)
        first["earlier_sessions"] = [{"file": "session-1.json", "driver": "builtin", "model": "local", "provider": "strata", "model_id": "strata",
                                      "handover_text": "The conversation so far, handed over to deepseek-v4-pro."}]
        first["models"].insert(0, {"model": "local", "provider": "strata", "model_id": "strata", "family": "qwen"})


# ---------------------------------------------------------------- routes
def _query(handler) -> dict:
    """The query with blank values kept (server.py parses it so: "profile=" means no profile)."""
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query, keep_blank_values=True)
    return {k: v[0] for k, v in q.items()}


def api(core, handler, method, parts, query, body):
    _PENDING.__dict__.clear()
    p = parts
    b = body if isinstance(body, dict) else {}
    if method == "GET" and p == ["estimate"]:
        q = _query(handler)
        repo = q.get("repo")
        if not repo:
            raise core.Invalid("missing query parameter 'repo'")
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", repo):
            raise core.Invalid("invalid repo name")
        routing = q.get("routing") or None
        if routing is not None and not re.fullmatch(r"[a-z0-9][a-z0-9-]*", routing):
            raise core.Invalid("'routing' must be a preset name here")
        return handler.send_json(estimate(core, repo, q.get("kind") or "feature", q.get("profile"), routing))
    if method == "GET" and p == ["custody"]:
        if _query(handler).get("refresh") in ("1", "true"):
            store(core, AREA)["read_ago"] = 2
        return handler.send_json(custody_view(core))
    if method == "POST" and p == ["jobs"]:
        _profile(core, _cfg_for(core, b.get("routing") if isinstance(b.get("routing"), str) else None), b.get("profile"))
        _PENDING.job = {"profile": b.get("profile"), "placements": _take_placements(core, b)}
        return NotImplemented
    if method == "POST" and p == ["plans"]:
        _profile(core, _cfg_for(core, b.get("routing") if isinstance(b.get("routing"), str) else None), b.get("profile"))
        _PENDING.plan = {"profile": b.get("profile")}
        _take_placements(core, b)
        return NotImplemented
    if method == "POST" and p == ["chats"]:
        _profile(core, core.STATE["config"], b.get("profile"))
        _PENDING.chat = {"profile": b.get("profile")}
        return NotImplemented
    if method == "POST" and len(p) == 3 and p[0] == "chats" and p[2] == "handoff":
        with core.LOCK:
            chat = core.STATE["chats"].get(p[1])
        if chat is not None:
            _profile(core, core.STATE["config"], b.get("profile"))
            _PENDING.job = {"profile": b.get("profile"), "handoff_models": list(chat.get("models") or [])}
        return NotImplemented
    if method == "POST" and len(p) == 4 and p[0] == "jobs" and p[3] == "routing":
        return change_routing(core, handler, p[1], p[2], b)
    return NotImplemented


def _take_placements(core, body: dict) -> dict:
    """A per-run route's placement fields (effort, escalate, reason, independent_alternate; the real server takes them,
    routing.ROUTE_KEYS), taken out of body["routing"] in place so the core mock's own check sees model, driver and
    extra_body only; checked like the real route (routing.check_placement)."""
    routing = body.get("routing") if isinstance(body, dict) else None
    taken: dict = {}
    if not isinstance(routing, dict):
        return taken
    for agent, entry in routing.items():
        if isinstance(entry, dict):
            placed = {k: entry.pop(k) for k in PLACE_KEYS if k in entry}
            if placed:
                try:
                    taken[agent] = R.check_placement(placed, f"routing for {agent}", agent=str(agent))
                except ValueError as exc:
                    raise core.Invalid(str(exc)) from None
    return taken


def change_routing(core, handler, repo: str, jid: str, body: dict):
    """POST /api/jobs/<repo>/<id>/routing {routing, profile?} (Factory.set_routing): a dict re-routes the agents it
    names, a preset or null every agent; the job keeps its profile unless one is named ("" for none)."""
    with core.LOCK:
        job = core.STATE["jobs"].get((repo, jid))
    if job is None:
        raise KeyError(f"{repo}/{jid}")
    if "routing" not in body:
        raise core.Invalid("'routing' is required: a preset name, an object of per-agent routes, or null for the saved config")
    placements = _take_placements(core, body)
    routing = None if body["routing"] is None else core.body_routing(body, required=True)
    if job["stage"] not in ("needs-human", "failed"):
        return handler.error(409, f"cannot change the routing of {repo}/{jid} while it is {job['stage']}")
    cfg = _cfg_for(core, routing)
    named = body.get("profile") is not None   # naming a profile ("" for none) re-routes every agent (set_routing)
    profile = _profile(core, cfg, body.get("profile")) if named else job.get("profile")
    if routing is None or isinstance(routing, str):
        snapshot = placed_snapshot(core, cfg, core.routing_snapshot(job["reviewers"], routing), routing, profile)
    else:
        snapshot = core.routing_snapshot(job["reviewers"], routing, keep=None if named else job.get("routing") or {})
        per_run = {a: {**_dict(routing.get(a)), **placements.get(a, {})} for a in set(routing) | set(placements)}
        snapshot = placed_snapshot(core, cfg, snapshot, per_run, profile, only=None if named else set(per_run))
    with core.LOCK:
        job["routing"] = snapshot
        job["profile"] = profile
    core.save_job(job)
    return handler.send_json({"job": core.public_job(job)})


def mock(core, handler, method, name, body):
    """POST /__mock/routing/run {repo, job, agent, round?, labels?, model?, input_tokens?, output_tokens?, error?}: a run
    of a job, live: agent.start with its placement labels, agent.end with its cost (and error: a failed run), then
    job.updated with job.runs and job.cost (the GUI's live cost). Without labels the run is placed as the factory
    would (place_job_run).
    POST /__mock/routing/held {repo, job, agent}: a live run that waits out the GPU custody register's HELD answer
    (agent.waiting {provider, overflow_after_seconds, custody: "HELD"}, orchestrator._placement's on_wait); POST
    /__mock/routing/held-end ends it."""
    b = body if isinstance(body, dict) else {}
    if name in ("held", "held-end") and method == "POST":
        st = store(core, AREA)
        if name == "held-end":
            r, stage = st.pop("held_run", None), st.pop("held_stage", None)
            if r is not None:
                r.text("The hold ended: lease granted.")
                r.end(True, None)
                with core.LOCK:
                    r.job["stage"] = stage
                core.save_job(r.job)
            return handler.send_json({"ended": r is not None})
        with core.LOCK:
            job = core.STATE["jobs"].get((str(b.get("repo")), str(b.get("job"))))
        if job is None:
            raise KeyError(f"{b.get('repo')}/{b.get('job')}")
        agent = str(b.get("agent") or "builder")
        st["held_stage"] = job["stage"]   # a run waits only in a job that is running: review while it holds
        with core.LOCK:
            job["stage"] = "review"
        core.save_job(job)
        r = core.Run(agent, core.log_name(job, agent), job=job, round_=b.get("round"), instant=False)
        r.start()
        r.emit("agent.waiting", {"provider": "tabby", "overflow_after_seconds": 900, "custody": "HELD"})
        st["held_run"] = r
        return handler.send_json({"run": r.run})
    if name != "run" or method != "POST":
        return NotImplemented
    with core.LOCK:
        job = core.STATE["jobs"].get((str(b.get("repo")), str(b.get("job"))))
    if job is None:
        raise KeyError(f"{b.get('repo')}/{b.get('job')}")
    agent = str(b.get("agent") or "reviewer-code")
    round_ = b.get("round")
    route, labels = place_job_run(job, agent, round_)
    if b.get("model"):
        route = _route_for(core, core.STATE["config"], agent, str(b["model"]))
        labels["family"] = route["family"]
    if isinstance(b.get("labels"), dict):
        labels.update(copy.deepcopy(b["labels"]))
    r = core.Run(agent, core.log_name(job, agent), job=job, round_=round_, instant=False)
    r._routing_script = {"route": route, "labels": labels}
    r.start()
    r.text(str(b.get("text") or "Placed as the guard decided."))
    r.call(int(b.get("input_tokens") or 200000), int(b.get("output_tokens") or 5000))
    r.end(not b.get("error"), str(b["error"]) if b.get("error") else None)
    return handler.send_json({"job": core.public_job(job), "run": r.run})


# ---------------------------------------------------------------- contract checks (tests/test_gui_static.py)
def contract(test) -> None:
    """The mock's placement answers have the real shapes and messages."""
    status, _, raw = test.request("GET", "/api/estimate?repo=my-app&kind=plan")
    test.assertEqual(status, 200, raw)
    est = json.loads(raw)
    probe = R.estimate([R.Role("builder", "build", _model_ns({}, {"model": "x", "model_id": "x", "provider": "p"}), {"model": "x"})], {})
    test.assertEqual(set(est) - {"repo", "profile", "routing", "independence"}, set(probe), "estimate keys (routing.estimate)")
    test.assertEqual(est["profile"], DEFAULT_PROFILE, "absent: the default profile")
    test.assertEqual({r["agent"] for r in est["roles"]} >= {"lead", "builder", "approver"}, True)
    for row in est["roles"]:
        test.assertLessEqual(set(probe["roles"][0]) - {"reason"}, set(row), row)
    test.assertTrue(all(set(r) == {"builder", "alternate", "reduced", "accepted"} for r in est["independence"]), est["independence"])
    status, _, raw = test.request("GET", "/api/estimate?repo=my-app&profile=")
    test.assertEqual(json.loads(raw)["profile"], None, "profile= means none")
    status, _, raw = test.request("GET", "/api/estimate?repo=my-app&profile=local-first")
    rows = {r["agent"]: r for r in json.loads(raw)["roles"]}
    test.assertEqual((rows["builder"]["model"], rows["builder"]["local"]), ("local", True), "the profile places the builder")
    test.assertFalse(json.loads(raw)["total"]["priced"], "gemini-argon has no prices: the total leaves it out")
    status, _, raw = test.request("GET", "/api/estimate?repo=my-app&profile=nope")
    test.assertEqual(status, 400)
    try:
        R.profile_name({"profiles": PROFILES}, "nope")
    except ValueError as exc:
        test.assertEqual(json.loads(raw)["error"], str(exc), "the real unknown-profile message")
    status, _, raw = test.request("POST", "/api/jobs", {"repo": "docs-site", "feature": "Profile none", "profile": ""})
    test.assertEqual(status, 201, raw)
    job = json.loads(raw)["job"]
    test.assertIsNone(job["profile"])
    test.assertEqual(set(job["cost"]), set(R.zero_cost()))
    test.assertEqual((job["runs"], job["sendbacks"]), ([], 0))
    status, _, raw = test.request("POST", "/api/jobs", {"repo": "docs-site", "feature": "Profile bad", "profile": "nope"})
    test.assertEqual(status, 400, raw)
    _, _, raw = test.request("GET", "/api/jobs/my-app/011-cache-the-pricing-page")
    shown = json.loads(raw)["job"]
    test.assertEqual((shown["independence"], shown["profile"], shown["cost"]["priced"]), ("reduced", "local-first", False))
    for run in shown["runs"]:
        test.assertLessEqual({"run", "agent", "round", "driver", "model", "provider", "model_id", "family", "ok", "usage", "cost_usd", "priced"}, set(run), run)
    test.assertIn({"round": 1, "agent": "reviewer-security", "model": "local", "model_id": "strata", "family": "qwen",
                   "builder_model": "local", "accepted": True}, shown["shared_models"])
    status, _, raw = test.request("POST", "/api/config", {"agents": {"approver": {"model": "claude", "effort": "minimal"}}})
    test.assertEqual((status, json.loads(raw)["error"]), (400, "agents.approver on model claude: " + R.effort_problem("anthropic", "minimal")))
    status, _, raw = test.request("POST", "/api/config", {"agents": {"reviewer-code": {"independent_alternate": "sonnet"}}})
    test.assertEqual(status, 400)
    test.assertIn("is of the qwen family, like the judge's own model 'local': name a model of another family", json.loads(raw)["error"])
    status, _, raw = test.request("POST", "/api/config", {"default_profile": "nope"})
    test.assertEqual((status, json.loads(raw)["error"]), (400, "default_profile 'nope' is not a key of profiles"))
    status, _, raw = test.request("GET", "/api/config")
    view = json.loads(raw)
    test.assertEqual(view["model_families"]["local"], "qwen")
    test.assertEqual(view["model_families"]["claude"], R.default_family("claude-sonnet-4.5"))
    approver = next(a for a in view["agent_list"] if a["name"] == "approver")
    test.assertEqual((approver["effort"], approver["independent_alternate"], approver["reason"]["kind"]), ("high", "claude", "capability"))
    status, _, raw = test.request("GET", "/api/custody")
    rows = {r["provider"]: r for r in json.loads(raw)["providers"]}
    test.assertTrue(rows["tabby"]["needs_lease"] and rows["tabby"]["register"]["leases"])
    test.assertEqual(rows["tabby-raw"]["custody_from"], "tabby")
    test.assertIsNone(rows["strata"]["register"]["residency"])
    test.assertIsNotNone(rows["strata"]["register"]["fenced"])
    test.assertEqual(rows["tabby"]["settings"]["timeout"], C.DEFAULT_TIMEOUT, "custody.CustodySettings.to_dict")
    import inspect   # the history's ops are the ones custody.py notes (CustodyClient._note, and a placement's HELD wait)
    noted = set(re.findall(r'\._note\("([a-z_]+)"', inspect.getsource(C)))
    test.assertIn("wait", noted)
    test.assertEqual(json.loads(raw)["reason_codes"], C.REFUSAL_WORDS)
    test.assertEqual(rows["tabby"]["held_wait_seconds"], 900, "a HELD answer waits up to the provider's overflow_after_seconds")
    test.assertIsNone(rows["tabby-raw"]["held_wait_seconds"], "no overflow_after_seconds: a HELD answer does not wait")
    test.assertEqual(rows["strata"]["settings"].get("token_credential"), "strata-custody-token")
    # GET /api/doctor is run_checks(factory=…): in_server, so the shell-only "<check> credential" warnings never reach it
    from swfactory import doctor as D
    test.assertIsNone(D._shell_credential("strata-custody-token", in_server=True))
    status, _, raw = test.request("GET", "/api/doctor")
    test.assertEqual(status, 200, raw)
    body = json.loads(raw)
    names = [r["name"] for r in (body.get("checks", body) if isinstance(body, dict) else body)]
    test.assertIn("custody strata", names)
    test.assertEqual([n for n in names if n.endswith(" credential")], [], "a shell-only doctor row in the server's doctor")
    for row in rows.values():
        for line in row["register"]["history"]:
            test.assertIn(line["op"], noted, line)
    for lease in rows["tabby"]["register"]["leases"]:   # custody.Lease.view: residency as the register's [provider, model] pairs
        test.assertEqual(set(lease), {"lease_id", "provider", "model", "register_provider", "register_model", "purpose", "holder",
                                      "until", "generation", "epoch", "state", "error", "users", "resident_before", "resident_after"})
        for pairs in (lease["resident_before"], lease["resident_after"]):
            test.assertTrue(pairs is None or all(isinstance(x, list) and len(x) == 2 for x in pairs), pairs)
    builder = next(r for r in shown["runs"] if r["run"] == "02-builder")
    test.assertEqual(set(builder["custody"]), {"lease_id", "provider", "model", "until", "generation", "resident_before"}, "custody.Claims.info")
    test.assertEqual([set(x) for x in builder["custody_refused"]], [{"provider", "model", "code", "words"}], "custody.Claims.info")
    # Change routing (Factory.set_routing, Factory._resolve_route; tests/test_gui_routing_bodies.py runs the same
    # bodies on the real Factory): null places every step from the saved config, agents.<name>'s placement included;
    # a per-run route that names a model leaves agents.<name>'s placement out; naming a profile re-routes every step.
    status, _, raw = test.request("POST", "/__mock/stopped-job", {"repo": "my-app", "feature": "Routing contract", "stage": "needs-human"})
    test.assertEqual(status, 201, raw)
    path = "/api/jobs/my-app/" + json.loads(raw)["job"]["id"] + "/routing"
    status, _, raw = test.request("POST", path, {"routing": None, "profile": ""})
    test.assertEqual(status, 200, raw)
    snap = json.loads(raw)["job"]["routing"]
    test.assertEqual((snap["approver"]["reason"]["kind"], snap["approver"]["independent_alternate"]["model"]), ("capability", "claude"),
                     "Saved settings (null) keeps agents.approver's reason and alternate")
    status, _, raw = test.request("POST", path, {"routing": {"approver": {"model": "haiku", "driver": "builtin"}}})
    snap = json.loads(raw)["job"]["routing"]
    test.assertEqual(snap["approver"]["model"], "haiku")
    test.assertNotIn("reason", snap["approver"], "an explicit model: agents.approver's reason does not apply (own = {})")
    test.assertNotIn("independent_alternate", snap["approver"])
    status, _, raw = test.request("POST", path, {"routing": {"approver": {"model": "opus", "driver": "builtin", "reason": {"kind": "capability", "text": "kept"},
                                                                          "independent_alternate": "claude"}}})
    snap = json.loads(raw)["job"]["routing"]
    test.assertEqual((snap["approver"]["reason"]["text"], snap["approver"]["independent_alternate"]["model"]), ("kept", "claude"), "given with the route: kept")
    status, _, raw = test.request("POST", path, {"routing": {"builder": {"driver": "qwen"}}, "profile": "fastest"})
    job = json.loads(raw)["job"]
    test.assertEqual((job["profile"], job["routing"]["reviewer-security"]["model"], job["routing"]["builder"]["driver"]), ("fastest", "haiku", "qwen"),
                     "a named profile re-routes the steps the route leaves out too (keep=None)")
    status, _, raw = test.request("POST", path, {"routing": None, "profile": "local-first"})
    job = json.loads(raw)["job"]
    test.assertEqual((job["profile"], job["routing"]["builder"]["model"], job["routing"]["approver"]["model"]), ("local-first", "local", "claude"),
                     "only a profile: the saved settings with it")
