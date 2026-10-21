"""Settings for the GUI and CLI: the config view, validated updates, presets, model probes and agent overrides.

The Factory delegates config_view, update_config, apply_preset, test_model and
the agent endpoints here; the CLI calls the same functions. Every change is
validated as a whole, saved atomically and handed back as a freshly loaded
Config, which the caller swaps in. Changes are applied to the config as last
saved (not to the caller's possibly older copy), one at a time, so concurrent
saves never drop each other's changes.
"""

from __future__ import annotations

import copy
import dataclasses
import http.client
import ipaddress
import json
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable

from . import gitops
from .agentdefs import (AGENT_NAME_RE, MAX_FILE_BYTES, AgentDef, discover_agents, parse_agent_file,
                        parse_frontmatter, read_small_text)
from .config import (AUTH_MODES, CONCURRENCY_SCOPES, DEFAULTS, DELIVERY_MODES, PROJECT_DIR, PROVIDER_KINDS,
                     PROVIDER_TYPES, REPO_NAME_RE, SWEEP_MODES, USER_DIR, Config, ProviderConfig, agent_dirs,
                     dir_exists, expand_path)
from .events import redact
from .harness.providers import (ANTHROPIC_VERSION, USER_AGENT, Completion, ProviderError, make_provider,
                                provider_run_slot)
from .harness.sandbox import SANDBOX_MODES

PRESETS_DIR = PROJECT_DIR / "examples"
BUILTIN_AGENTS_DIR = PROJECT_DIR / "agents"
UPDATABLE_SECTIONS = ("providers", "models", "agents", "pipeline", "limits", "harness", "mcp_servers",
                      "escalate_paths", "repos", "merge", "profiles", "default_profile", "housekeeping")
ENTRY_SECTIONS = ("providers", "models", "agents")  # a preset replaces these entry by entry
PRESET_ENTRY_SECTIONS = (*ENTRY_SECTIONS, "profiles")   # ... and its routing profiles (DESIGN §23)
PRESET_META_KEYS = ("description",)                 # a preset file's own notes, never merged into a config
TIERS = ("opus", "sonnet", "haiku")
MASK = "***"  # what public_view shows for secret-named header and env values
VIEW_ONLY_PROVIDER_KEYS = ("key_present",)

PRESET_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
USER_AGENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
REVIEWER_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
ENTRY_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")  # provider and model keys
MCP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,40}$")  # becomes part of mcp__<server>__<tool>
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_HEADER_RE = re.compile(r"key|token|secret|auth", re.IGNORECASE)
BUILD_KEYS = frozenset({"provider", "model", "max_tokens", "context_tokens"})
MAX_LIST_BYTES = 4 << 20   # a model list or loaded-model answer larger than this is not read

PROBE_TIMEOUT = 30.0
REACH_TIMEOUT = 5.0
PROBE_CALLS = 4  # plain + tool probe, each retried once with more room
ABANDON_SECONDS = 2.0  # how long a timed-out probe call gets to wind down
PLAIN_TOKENS = 16
TOOL_TOKENS = 256
REASONING_ROOM = 2048
MIN_RATE_TOKENS = 16
MIN_RATE_SECONDS = 0.05
TEXT_CHARS = 200
PLAIN_PROMPT = "Reply with OK"
TOOL_PROMPT = "Call the get_time tool now. Do not reply with text."
GET_TIME_TOOL = {"name": "get_time", "description": "Returns the current time.",
                 "input_schema": {"type": "object", "properties": {}}}

_SAVE_LOCK = threading.RLock()   # config saves, one at a time
_AGENT_LOCK = threading.RLock()  # writes to the user agents dir


def _claude(model_id: str, effort: str, fallbacks: bool = True) -> dict:
    extra: dict[str, Any] = {"thinking": {"type": "adaptive", "display": "summarized"},
                             "output_config": {"effort": effort}, "cache_control": {"type": "ephemeral"}}
    if fallbacks:
        extra["fallbacks"] = "default"
    return {"model": model_id, "max_tokens": 64000, "context_tokens": 1000000, "stream": True,
            "allow_history_edits": False, "eager_tools": True, "extra_body": extra}


def _local(model_id: str, context: int, max_tokens: int, extra: dict | None = None) -> dict:
    return {"model": model_id, "max_tokens": max_tokens, "context_tokens": context, "stream": True,
            "extra_body": extra or {}}


TEMPLATE_NOTE = ("Values are starting points, not claims: set context_tokens to the context the server was "
                 "started with (-c for llama.cpp), and run Test (plain reply + tool call) before assigning "
                 "a local model to an agent.")
REVIEWERS = ["reviewer-security", "reviewer-ux", "reviewer-ui", "reviewer-code"]
MODEL_TEMPLATES: list[dict] = [
    {"id": "qwen3-coder", "label": "qwen3-coder (30B-A3B)", "model_hints": ["qwen3-coder", "qwen3-coder:30b"],
     "provider": None, "entry": _local("qwen3-coder", 262144, 16384), "roles": ["builder", "coder"],
     "note": "Non-thinking coder model."},
    {"id": "qwen3.8-27b", "label": "qwen3.8-27b", "model_hints": ["qwen3.8-27b"], "provider": None,
     "entry": _local("qwen3.8-27b", 262144, 16384), "roles": ["builder", "reviewer-code"], "note": ""},
    {"id": "gemma-4-12b", "label": "gemma-4-12b", "model_hints": ["gemma-4-12b"], "provider": None,
     "entry": _local("gemma-4-12b", 131072, 8192), "roles": ["reviewer-ux", "reviewer-ui"], "note": ""},
    {"id": "gemma-4", "label": "gemma-4 (larger)", "model_hints": ["gemma-4"], "provider": None,
     "entry": _local("gemma-4", 131072, 8192), "roles": list(REVIEWERS), "note": ""},
    {"id": "gpt-oss-120b", "label": "gpt-oss-120b", "model_hints": ["gpt-oss-120b"], "provider": None,
     "entry": {**_local("gpt-oss-120b", 131072, 16384, {"chat_template_kwargs": {"reasoning_effort": "high"}}),
               "keep_reasoning": True},
     "roles": ["spec-writer", "reviewer-security", "approver"],
     "note": "extra_body shown is for llama.cpp; vLLM and OpenRouter take {\"reasoning_effort\": \"high\"}."},
    {"id": "qwen3.8-flash-next", "label": "qwen3.8-flash-next (Strata)", "model_hints": ["strata"],
     "provider": "strata", "entry": {**_local("strata", 262144, 16384), "keep_reasoning": False},
     "roles": ["any"], "note": ""},
    {"id": "claude-opus-5-5", "label": "Claude Opus 5.5 (Anthropic API)", "model_hints": ["claude-opus-5-5"],
     "provider": "anthropic", "entry": _claude("claude-opus-5-5", "high"), "roles": ["builder", "approver"],
     "note": "Adaptive thinking (summarized), effort high, prompt caching, server-side refusal fallback."},
    {"id": "deepseek-v4", "label": "DeepSeek V4 Pro / Flash", "model_hints": ["deepseek-v4-pro", "deepseek-v4-flash"],
     "provider": "deepseek",
     "entry": {"model": "deepseek-v4-pro", "max_tokens": 32768, "context_tokens": 1000000, "stream": True,
               "extra_body": {"thinking": {"type": "enabled"}, "reasoning_effort": "high"}},
     "roles": ["any"], "note": ""},
]


# ---------------------------------------------------------------- the view


def config_view(config: Config) -> dict:
    """config.public_view() plus presets, model templates, the resolved agents and the tier mapping.

    Every section of public_view keeps its shape, so the GUI can post a section back
    as it got it ("agents" stays the routing dict {name or "*": {model, driver,
    extra_body}}). Added: "agent_list" (each agent file with its effective route:
    {name, description, model, driver, source, tier, color, extra_body}),
    "agents_error" (only when an agent file does not parse), "presets" ([{name,
    description, replaces: {providers, models, agents}}], see preset_summaries),
    "preset_names" (the names alone, as "presets" used to be), "model_templates",
    "model_template_note" and "tiers".
    """
    view = config.public_view()
    view["presets"] = preset_summaries()
    view["preset_names"] = list_presets()
    view["model_templates"] = copy.deepcopy(MODEL_TEMPLATES)
    view["model_template_note"] = TEMPLATE_NOTE
    try:
        agents = resolved_agents(config)
    except ValueError as exc:
        agents = {}
        view["agents_error"] = str(exc)
    view["agent_list"] = [_agent_summary(config, agents[name]) for name in sorted(agents)]
    view["tiers"] = tier_view(config)
    return view


def tier_view(config: Config) -> dict:
    """{tier: {model, provider, same_as}} for opus/sonnet/haiku; same_as names a model entry the tier copies.

    A tier that copies an entry follows that entry's changes made through update_config.
    """
    models = _dict_or_empty(config.raw.get("models"))
    tiers: dict[str, dict] = {}
    for tier in TIERS:
        entry = models.get(tier)
        if not isinstance(entry, dict):
            continue
        same = next(iter(_copied_entries(models, tier)), None)
        tiers[tier] = {"model": entry.get("model"), "provider": entry.get("provider"), "same_as": same}
    return tiers


def _copied_entries(models: dict, tier: str) -> list[str]:
    """The non-tier model keys whose entry equals models[tier] (null fields ignored), in config order."""
    entry = models.get(tier)
    if not isinstance(entry, dict):
        return []
    return [key for key, other in models.items()
            if key not in TIERS and isinstance(other, dict) and _pruned(other) == _pruned(entry)]


def route_model(config: Config, agent_name: str, tier: str | None) -> str:
    """The model reference an agent runs on: agents[name].model, then agents["*"].model, then its tier."""
    routes = _dict_or_empty(config.raw.get("agents"))
    for candidate in (agent_name, "*"):
        entry = routes.get(candidate)
        if isinstance(entry, dict) and entry.get("model"):
            return str(entry["model"])
    return tier or "opus"


def _agent_summary(config: Config, agent: AgentDef) -> dict:
    """{name, description, model, driver, source, tier, color, extra_body}, plus the route's effort,
    escalate (rung model keys) and reason ({kind, text}) when it has them (DESIGN §23)."""
    route = _dict_or_empty(config.raw.get("agents")).get(agent.name)
    summary = {"name": agent.name, "description": agent.description,
               "model": route_model(config, agent.name, agent.model), "driver": config.agent_driver(agent.name),
               "source": agent_source(config, agent), "tier": agent.model, "color": agent.color,
               "extra_body": route.get("extra_body") if isinstance(route, dict) else None}
    if isinstance(route, dict):
        from .routing import reason

        for key in ("effort", "escalate"):
            if route.get(key):
                summary[key] = copy.deepcopy(route[key])
        try:
            found = reason(route.get("reason"))
        except ValueError:
            found = None
        if found:
            summary["reason"] = found
    return summary


# ---------------------------------------------------------------- updates


OnSaved = Callable[[Config], None]


def update_config(config: Config, patch: dict, on_saved: OnSaved | None = None,
                  replace: Iterable[tuple[str, str]] = ()) -> Config:
    """Validate patch, deep-merge it into the saved config, save atomically and return the reloaded Config.

    The patch applies to the config last saved at config.path (config itself when no
    file exists yet), never to a stale in-memory copy, and saves run one at a time, so
    two updates at once both land. on_saved(new_config) runs before the save lock is
    released: a caller that swaps the result in from there can never install an older
    config over a newer one.

    Only UPDATABLE_SECTIONS are accepted. Dicts merge, lists replace, null removes a
    key (null on a whole section restores its defaults). "***" for a masked header or
    env value keeps the current value. A tier (opus, sonnet, haiku) that is a copy of
    another model entry follows that entry when the patch changes it and leaves the
    tier as it was (DESIGN §23: switching `local` switches the tiers that copy it).
    replace: (section, key) entries the patch replaces whole instead of merging into (the key is
    dropped from the saved section first, under the same lock).
    Raises ValueError naming the offending field; nothing is saved then.
    """
    if not isinstance(patch, dict):
        raise ValueError("the config patch must be a JSON object")
    refused = sorted(str(key) for key in patch if key not in UPDATABLE_SECTIONS)
    if refused:
        raise ValueError(f"cannot change {', '.join(refused)} here (allowed: {', '.join(UPDATABLE_SECTIONS)})")
    with _SAVE_LOCK:
        current = _last_saved(config)
        base = current.raw
        if replace:
            base = copy.deepcopy(base)
            for section, key in replace:
                if isinstance(base.get(section), dict):
                    base[section].pop(key, None)
        merged = merge_patch(base, _without_view_fields(patch, current.raw))
        _follow_copied_tiers(current.raw, merged)
        return _save(_candidate(current, merged), on_saved)


def merge_patch(base: dict, patch: dict, top: bool = True) -> dict:
    """A deep copy of base with patch applied: dicts merge, other values replace, None removes the key.

    At the top level None restores the section's default instead.
    """
    merged = copy.deepcopy(base)
    for key, value in patch.items():
        if value is None:
            if top and key in DEFAULTS:
                merged[key] = copy.deepcopy(DEFAULTS[key])
            else:
                merged.pop(key, None)
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_patch(merged[key], value, top=False)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _without_view_fields(patch: dict, current: dict) -> dict:
    """patch minus what config_view adds: key_present, and "***" stand-ins for masked values."""
    cleaned = copy.deepcopy(patch)
    for section, field in (("providers", "headers"), ("mcp_servers", "env")):
        entries = cleaned.get(section)
        if not isinstance(entries, dict):
            continue
        for name, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            if section == "providers":
                for key in VIEW_ONLY_PROVIDER_KEYS:
                    entry.pop(key, None)
            values = entry.get(field)
            if isinstance(values, dict):
                existing = _dict_or_empty(_dict_or_empty(_dict_or_empty(current.get(section)).get(name)).get(field))
                for key in [k for k, v in values.items() if v == MASK and k in existing]:
                    del values[key]
    return cleaned


def _follow_copied_tiers(before: dict, after: dict) -> None:
    """In after["models"], re-copy each tier that copied exactly one changed entry and was left as it was."""
    old = _dict_or_empty(before.get("models"))
    new = after.get("models")
    if not isinstance(new, dict):
        return
    for tier in TIERS:
        if not isinstance(old.get(tier), dict) or _pruned(new.get(tier)) != _pruned(old[tier]):
            continue  # no tier before, or the patch changed or removed it: the patch decides
        changed = [key for key in _copied_entries(old, tier)
                   if isinstance(new.get(key), dict) and _pruned(new[key]) != _pruned(old[key])]
        if len(changed) == 1:
            new[tier] = copy.deepcopy(new[changed[0]])


def _last_saved(config: Config) -> Config:
    """The config as saved at config.path now (config itself while no file exists); ValueError if malformed.

    Read once, like Config.load, but a missing file gives the caller's config, never
    bare defaults (which would save over the token and repos).
    """
    try:
        text = config.path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return config
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{config.path}: invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{config.path}: the top level must be a JSON object")
    return Config(config.path, data)


def _candidate(previous: Config, merged: dict) -> Config:
    """A validated, unsaved Config holding merged at previous.path (new repo paths normalised)."""
    merged["repos"] = _checked_repos(merged.get("repos"), _list_or_empty(previous.raw.get("repos")),
                                     previous.path.parent)
    candidate = Config(previous.path)
    candidate.raw = merged
    candidate.data_dir = expand_path(merged.get("data_dir") or DEFAULTS["data_dir"], candidate.path.parent)
    validate_settings(candidate)
    return candidate


def _save(candidate: Config, on_saved: OnSaved | None = None) -> Config:
    """Save candidate and return it freshly loaded, after on_saved(loaded); the caller holds _SAVE_LOCK."""
    candidate.save()
    saved = Config.load(candidate.path)
    if on_saved is not None:
        on_saved(saved)
    return saved


# ---------------------------------------------------------------- presets


def list_presets() -> list[str]:
    """Names of the presets in examples/ (examples/<name>.json), sorted."""
    return sorted(path.stem for path in PRESETS_DIR.glob("*.json") if PRESET_NAME_RE.fullmatch(path.stem))


def preset_summaries() -> list[dict]:
    """[{name, description, replaces, merges, default_profile}] for each preset, sorted by name.

    description: its "description" string ("" if it has none). replaces: {providers, models,
    agents}: the entry names it replaces whole, plus profiles when it brings routing profiles.
    merges: {section: [keys]} for any other section it deep-merges (an add-on preset such as rea
    only adds mcp_servers). default_profile: the profile it makes the default (None when it sets
    none). A preset file that does not load is listed with its error as the description and
    nothing replaced or merged.
    """
    summaries = []
    for name in list_presets():
        try:
            preset = load_preset(name)
        except ValueError as exc:
            summaries.append({"name": name, "description": f"(cannot load: {exc})",
                              "replaces": {section: [] for section in ENTRY_SECTIONS}, "merges": {},
                              "default_profile": None})
            continue
        description = preset.get("description")
        replaces = {section: sorted(_dict_or_empty(preset.get(section))) for section in ENTRY_SECTIONS}
        if isinstance(preset.get("profiles"), dict):
            replaces["profiles"] = sorted(preset["profiles"])
        default = preset.get("default_profile")
        summaries.append({
            "name": name, "description": description if isinstance(description, str) else "",
            "replaces": replaces,
            "merges": {section: sorted(value) if isinstance(value, dict) else []
                       for section, value in preset.items()
                       if section not in PRESET_ENTRY_SECTIONS and section not in PRESET_META_KEYS
                       and section != "default_profile"},
            "default_profile": default if isinstance(default, str) else None,
        })
    return summaries


def load_preset(name: str) -> dict:
    """The parsed examples/<name>.json (ValueError if the name is unknown or the file malformed)."""
    if not isinstance(name, str) or name not in list_presets():
        raise ValueError(f"unknown preset {name!r} (available: {', '.join(list_presets())})")
    path = PRESETS_DIR / f"{name}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path}: a preset must be a JSON object")
    return data


def preset_config(config: Config, name: str) -> Config:
    """config with preset `name` merged in, validated but not saved.

    The preset's providers, models (tiers included), agents and profiles replace same-named
    entries whole; any other section it sets is deep-merged; the server token is kept. A preset
    that routes agents also sets default_profile: its own, or none.
    """
    preset = load_preset(name)
    merged = copy.deepcopy(config.raw)
    if isinstance(preset.get("agents"), dict):
        # A routing preset routes every agent; a default profile left from an earlier preset would
        # route them again on top of it, so default_profile becomes the preset's own (or none).
        merged["default_profile"] = preset.get("default_profile") or None
    for section, value in preset.items():
        if section in PRESET_META_KEYS:
            continue
        if section in PRESET_ENTRY_SECTIONS and isinstance(value, dict):
            entries = _dict_or_empty(merged.get(section))
            entries.update(copy.deepcopy(value))
            merged[section] = entries
        elif isinstance(value, dict) and isinstance(merged.get(section), dict):
            merged[section] = merge_patch(merged[section], value, top=False)
        else:
            merged[section] = copy.deepcopy(value)
    token = _dict_or_empty(config.raw.get("server")).get("token")
    if isinstance(merged.get("server"), dict):
        merged["server"]["token"] = token
    return _candidate(config, merged)


def apply_preset(config: Config, name: str, on_saved: OnSaved | None = None) -> Config:
    """Merge examples/<name>.json into the saved config (see preset_config), save it, return the reloaded Config.

    Like update_config, it starts from the config last saved at config.path and calls
    on_saved(new_config) while still holding the save lock.
    """
    with _SAVE_LOCK:
        return _save(preset_config(_last_saved(config), name), on_saved)


# ---------------------------------------------------------------- validation


def validate_settings(config: Config) -> None:
    """Raise ValueError naming the first invalid setting (stricter than Config.validate, which runs last)."""
    raw = config.raw
    providers = _section(raw, "providers")
    for name, entry in providers.items():
        _check_provider(str(name), entry)
    models = _section(raw, "models")
    live_providers = {name for name, entry in providers.items() if isinstance(entry, dict)}
    for key, entry in models.items():
        _check_model(str(key), entry, live_providers)
    live_models = {key for key, entry in models.items() if isinstance(entry, dict)}
    drivers = {name for name, entry in _section(raw, "drivers").items() if isinstance(entry, dict)}
    for key, entry in models.items():
        if isinstance(entry, dict) and entry.get("overflow") is not None:
            _check_overflow(f"models.{key}", entry["overflow"], live_models, key)
    for name, entry in providers.items():
        if isinstance(entry, dict) and entry.get("overflow") is not None:
            _check_overflow(f"providers.{name}", entry["overflow"], live_models)
    for name, entry in _section(raw, "agents").items():
        _check_route(str(name), entry, live_models, drivers)
    from .routing import check_profiles   # routing imports this module

    check_profiles(raw, live_models, drivers, AGENT_NAME_RE)
    _check_efforts(config)
    _check_pipeline(_section(raw, "pipeline"), config)
    _check_limits(_section(raw, "limits"))
    _check_harness(_section(raw, "harness"))
    _check_merge(_section(raw, "merge"))
    _check_housekeeping(_section(raw, "housekeeping"))
    for name, entry in _section(raw, "mcp_servers").items():
        _check_mcp_server(str(name), entry)
    if raw.get("escalate_paths") is not None:
        _require(raw["escalate_paths"], "escalate_paths", _is_str_list, "a list of glob strings")
    config.validate()


def _check_provider(name: str, entry: Any) -> None:
    label = f"providers.{name}"
    _check_key(name, "providers")
    if entry is None:
        return
    _require(entry, label, lambda v: isinstance(v, dict), "an object")
    _require(entry.get("type"), f"{label}.type", lambda v: v in PROVIDER_TYPES, "one of " + ", ".join(PROVIDER_TYPES))
    _require(entry.get("base_url"), f"{label}.base_url", _is_http_url, "an http(s) URL such as https://api.example.com/v1")
    _optional(entry, label, "api_key_env", lambda v: isinstance(v, str) and (v == "" or ENV_NAME_RE.fullmatch(v)),
              "an environment variable name such as DEEPSEEK_API_KEY, or null")
    _optional(entry, label, "api_key_file", lambda v: isinstance(v, str), "a file path or null")
    _optional(entry, label, "auth", lambda v: v == "" or v in AUTH_MODES, "one of " + ", ".join(AUTH_MODES))
    _optional(entry, label, "concurrency_scope", lambda v: v in CONCURRENCY_SCOPES,
              "one of " + ", ".join(CONCURRENCY_SCOPES))
    _optional(entry, label, "max_concurrency", _is_positive_int, "a positive integer or null")
    _optional(entry, label, "timeout", _is_positive_number, "a positive number of seconds")
    _optional(entry, label, "headers", _is_str_dict, "an object of header names to strings")
    _optional(entry, label, "kind", lambda v: v in PROVIDER_KINDS, "one of " + ", ".join(PROVIDER_KINDS) + " or null")
    _optional(entry, label, "loaded_model_url", _is_http_url, "an http(s) URL or null")
    for flag in ("allow_model_swap", "swap_back"):
        _optional(entry, label, flag, lambda v: isinstance(v, bool), "true or false")
    _optional(entry, label, "overflow", lambda v: isinstance(v, str) and v.strip(), "a model key or null")
    for key in ("overflow_after_seconds", "tokens_per_second"):
        _optional(entry, label, key, _is_positive_number, "a positive number or null")


def _check_model(key: str, entry: Any, providers: set) -> None:
    label = f"models.{key}"
    _check_key(key, "models")
    if entry is None:
        return
    _require(entry, label, lambda v: isinstance(v, dict), "an object")
    provider = entry.get("provider")
    if not isinstance(provider, str) or provider not in providers:
        raise ValueError(f"{label}.provider {provider!r} is not a configured provider")
    _require(entry.get("model"), f"{label}.model", lambda v: isinstance(v, str) and v.strip(), "a model id")
    _optional(entry, label, "max_tokens", _is_positive_int, "a positive integer")
    _optional(entry, label, "context_tokens", _is_positive_int, "a positive integer or null")
    for flag in ("stream", "keep_reasoning", "allow_history_edits", "eager_tools"):
        _optional(entry, label, flag, lambda v: isinstance(v, bool), "true or false")
    _optional(entry, label, "extra_body", lambda v: isinstance(v, dict), "an object")
    for price in ("price_in", "price_out", "price_cached_in"):
        _optional(entry, label, price, _is_non_negative_number, "a non-negative number (USD per million tokens)")
    if (entry.get("price_in") is None) != (entry.get("price_out") is None):
        raise ValueError(f"{label}: set price_in and price_out together (a missing price means unknown)")
    if entry.get("price_cached_in") is not None and entry.get("price_in") is None:
        raise ValueError(f"{label}.price_cached_in needs price_in and price_out")
    builds = entry.get("builds")
    if builds is not None:
        _require(builds, f"{label}.builds", lambda v: isinstance(v, list), "a list of {provider, model} objects")
        for index, build in enumerate(builds):
            where = f"{label}.builds[{index}]"
            _require(build, where, lambda v: isinstance(v, dict) and not set(v) - BUILD_KEYS,
                     "an object like {\"provider\": ..., \"model\": ..., \"context_tokens\": ...}")
            if build.get("provider") not in providers:
                raise ValueError(f"{where}.provider {build.get('provider')!r} is not a configured provider")
            _require(build.get("model"), f"{where}.model", lambda v: isinstance(v, str) and v.strip(), "a model id")
            _optional(build, where, "max_tokens", _is_positive_int, "a positive integer")
            _optional(build, where, "context_tokens", _is_positive_int, "a positive integer")
            limit = build.get("max_tokens") or entry.get("max_tokens") or 32768
            if build.get("context_tokens") is not None and build["context_tokens"] <= limit:
                raise ValueError(f"{where}.context_tokens must be larger than max_tokens ({limit})")


def _check_overflow(label: str, key: Any, models: set, own: str | None = None) -> None:
    if not isinstance(key, str) or key not in models:
        raise ValueError(f"{label}.overflow {key!r} is not a key of models")
    if key == own:
        raise ValueError(f"{label}.overflow cannot be the model itself")


def _check_route(name: str, entry: Any, models: set, drivers: set) -> None:
    label = f"agents.{name}"
    if name != "*" and not AGENT_NAME_RE.fullmatch(name):
        raise ValueError(f"agents: invalid agent name {name!r}")
    if entry is None:
        return
    _require(entry, label, lambda v: isinstance(v, dict), 'an object like {"model": ..., "driver": ...}')
    model = entry.get("model")
    if model not in (None, "") and (not isinstance(model, str) or model not in models):
        raise ValueError(f"{label}.model {model!r} is not a key of models")
    driver = entry.get("driver")
    if driver not in (None, "") and (not isinstance(driver, str) or driver not in drivers):
        raise ValueError(f"{label}.driver {driver!r} is not a configured driver")
    _optional(entry, label, "extra_body", lambda v: isinstance(v, dict), "an object")
    placed = [key for key in ("effort", "escalate", "reason") if key in entry]
    if name == "*" and placed:
        raise ValueError(f"agents.*: {', '.join(placed)} apply to one agent's placement; set them on "
                         f"agents.<name> or in a profile (agents.* takes model, driver and extra_body)")
    from .routing import check_placement   # routing imports this module

    check_placement({key: entry[key] for key in placed}, label, models)


def _check_efforts(config: Config) -> None:
    """Every effort on agents.<name> or in a profile placement is one its model's provider type takes
    (routing.effort_problem: Claude's output_config.effort has no "minimal"). The model is the
    placement's own, else the agent's route (agents.<name>, agents.*, its file's tier); one that does
    not resolve is left to the other checks."""
    from .routing import effort_problem   # routing imports this module

    raw = config.raw
    checks: list[tuple[str, str, Any, Any]] = []
    for name, entry in _section(raw, "agents").items():
        if name != "*" and isinstance(entry, dict) and entry.get("effort"):
            checks.append((f"agents.{name}", str(name), entry.get("model"), entry["effort"]))
    for profile_key, profile in _section(raw, "profiles").items():
        placements = profile.get("agents") if isinstance(profile, dict) else None
        for name, entry in (placements if isinstance(placements, dict) else {}).items():
            if isinstance(entry, dict) and entry.get("effort"):
                checks.append((f"profiles.{profile_key}.agents.{name}", str(name), entry.get("model"), entry["effort"]))
    tiers: dict[str, str] | None = None
    for label, agent, key, effort in checks:
        if not key:
            if tiers is None:
                try:
                    tiers = {name: found.model for name, found in resolved_agents(config).items()}
                except ValueError:
                    tiers = {}
            key = route_model(config, agent, tiers.get(agent))
        try:
            provider_type = config.model(str(key)).provider.type
        except (KeyError, ValueError):
            continue
        problem = effort_problem(provider_type, effort)
        if problem:
            raise ValueError(f"{label} on model {key}: {problem}")


def _check_pipeline(section: dict, config: Config) -> None:
    reviewers = section.get("reviewers")
    if reviewers is not None:
        if not isinstance(reviewers, list) or not reviewers:
            raise ValueError("pipeline.reviewers must be a non-empty list of names like 'security'")
        for reviewer in reviewers:
            if not isinstance(reviewer, str) or not REVIEWER_RE.fullmatch(reviewer):
                raise ValueError(f"pipeline.reviewers: invalid name {reviewer!r} (use a-z, 0-9 and '-')")
        if len(set(reviewers)) != len(reviewers):
            raise ValueError("pipeline.reviewers lists a reviewer twice")
        agents = resolved_agents(config)
        for reviewer in reviewers:
            if f"reviewer-{reviewer}" not in agents:
                raise ValueError(f"pipeline.reviewers: there is no agent reviewer-{reviewer} "
                                 f"(add {user_agents_dir(config) / f'reviewer-{reviewer}.md'})")
    _optional(section, "pipeline", "max_resumes", lambda v: _is_int(v) and 0 <= v <= 10, "an integer from 0 to 10")
    _optional(section, "pipeline", "max_parallel_jobs", lambda v: _is_int(v) and 1 <= v <= 16,
              "an integer from 1 to 16")
    _optional(section, "pipeline", "auto_merge", lambda v: isinstance(v, bool), "true or false")
    _optional(section, "pipeline", "max_auto_merge_lines", lambda v: _is_int(v) and v >= 0,
              "a non-negative integer")


def _check_limits(section: dict) -> None:
    for key in ("max_turns", "tool_output_chars", "max_context_chars"):
        _optional(section, "limits", key, _is_positive_int, "a positive integer")
    for key in ("max_wall_seconds", "bash_timeout"):
        _optional(section, "limits", key, _is_positive_number, "a positive number of seconds")


def _check_harness(section: dict) -> None:
    _optional(section, "harness", "sandbox", lambda v: v in SANDBOX_MODES, "one of " + ", ".join(SANDBOX_MODES))
    _optional(section, "harness", "sandbox_network", lambda v: isinstance(v, bool), "true or false")
    for key in ("extra_read_roots", "skills_dirs"):
        _optional(section, "harness", key, _is_str_list, "a list of paths")


def _check_merge(section: dict) -> None:
    _optional(section, "merge", "push", lambda v: isinstance(v, bool), "true or false")
    _optional(section, "merge", "remote", lambda v: isinstance(v, str) and v.strip() and not v.startswith("-")
              and not any(ch.isspace() for ch in v), "a remote name such as origin")
    _optional(section, "merge", "delivery", lambda v: v in DELIVERY_MODES, "one of " + ", ".join(DELIVERY_MODES))
    _optional(section, "merge", "output_dir", lambda v: isinstance(v, str) and v.strip(), "a directory path or null")


def _check_housekeeping(section: dict) -> None:
    for flag in ("enabled", "cli_hooks", "mirror_cli_todos"):
        _optional(section, "housekeeping", flag, lambda v: isinstance(v, bool), "true or false")
    for key, low, high in (("reinject_every_tool_turns", 1, 50), ("reinject_every_tool_turns_api", 1, 50),
                           ("reinject_max_chars", 200, 20000), ("stop_guard_attempts", 0, 8), ("stale_rounds", 1, 50),
                           ("followup_due_days", 1, 3650), ("max_proposals_per_run", 0, 100),
                           ("max_proposals_per_scope", 10, 5000)):
        _optional(section, "housekeeping", key, lambda v, lo=low, hi=high: _is_int(v) and lo <= v <= hi,
                  f"an integer from {low} to {high}")
    _optional(section, "housekeeping", "sweep", lambda v: v in SWEEP_MODES, "one of " + ", ".join(SWEEP_MODES))
    _optional(section, "housekeeping", "sweeper_model", lambda v: isinstance(v, str) and v.strip(),
              "a model key or null")


def _check_mcp_server(name: str, entry: Any) -> None:
    label = f"mcp_servers.{name}"
    if not MCP_NAME_RE.fullmatch(name):
        raise ValueError(f"mcp_servers: invalid server name {name!r} (use letters, digits, '_' and '-')")
    if entry is None:
        return
    _require(entry, label, lambda v: isinstance(v, dict), "an object")
    _require(entry.get("command"), f"{label}.command", lambda v: isinstance(v, str) and v.strip(), "a command")
    _optional(entry, label, "args", _is_str_list, "a list of strings")
    _optional(entry, label, "env", _is_str_dict, "an object of variable names to strings")
    _optional(entry, label, "cwd", lambda v: isinstance(v, str), "a directory or null")
    _optional(entry, label, "tools", _is_str_list, "a list of tool names or null")
    _optional(entry, label, "write_args", _is_str_list_dict,
              'an object of tool names to lists of argument names, like {"export": ["path"]}')
    for key in ("timeout", "startup_timeout"):
        _optional(entry, label, key, _is_positive_number, "a positive number of seconds")


def _checked_repos(entries: Any, previous: list, config_dir: Path) -> list:
    """Repo entries validated like Config.add_repo; new or changed ones must be git top-levels (path resolved)."""
    if entries is None:
        return []
    if not isinstance(entries, list):
        raise ValueError("repos must be a list")
    names: set[str] = set()
    paths: dict[Path, str] = {}
    checked = []
    for index, entry in enumerate(entries):
        label = f"repos[{index}]"
        _require(entry, label, lambda v: isinstance(v, dict), 'an object like {"name": ..., "path": ...}')
        name = entry.get("name")
        if not isinstance(name, str) or not REPO_NAME_RE.fullmatch(name):
            raise ValueError(f"{label}.name {name!r} is invalid: use [a-z0-9][a-z0-9._-]*")
        label = f"repos.{name}"
        if name in names:
            raise ValueError(f"{label}: a repo named {name!r} is listed twice")
        names.add(name)
        _require(entry.get("path"), f"{label}.path", lambda v: isinstance(v, str) and v.strip(), "a directory path")
        _optional(entry, label, "base", lambda v: isinstance(v, str) and v.strip() and not v.startswith("-")
                  and not any(ch.isspace() for ch in v), "a branch name or null")
        _optional(entry, label, "setup", lambda v: isinstance(v, str), "a shell command or null")
        _optional(entry, label, "escalate_paths", _is_str_list, "a list of glob strings")
        entry = copy.deepcopy(entry)
        if entry not in previous:
            top = _git_toplevel(expand_path(entry["path"], config_dir), label)
            entry["path"] = str(top)
            if entry.get("base"):
                try:
                    gitops.detect_base(top, entry["base"])
                except gitops.GitError as exc:
                    raise ValueError(f"{label}.base: branch {entry['base']!r} does not exist in {top}") from exc
        resolved = expand_path(entry["path"], config_dir).resolve()
        if resolved in paths:
            raise ValueError(f"{label}.path {resolved} is already configured as repo {paths[resolved]!r}")
        paths[resolved] = name
        checked.append(entry)
    return checked


def _git_toplevel(path: Path, label: str) -> Path:
    resolved = path.resolve()
    if not resolved.is_dir():
        raise ValueError(f"{label}.path {resolved} is not a directory")
    try:
        top = Path(gitops.git(resolved, "rev-parse", "--show-toplevel", timeout=30).strip()).resolve()
    except gitops.GitError as exc:
        raise ValueError(f"{label}.path {resolved} is not a git repository") from exc
    if top != resolved:
        raise ValueError(f"{label}.path {resolved} is not the top level of its git repository ({top})")
    return resolved


def _require(value: Any, label: str, predicate: Callable[[Any], Any], expected: str) -> None:
    if not predicate(value):
        raise ValueError(f"{label} must be {expected} (got {_short(value)})")


def _optional(entry: dict, label: str, key: str, predicate: Callable[[Any], Any], expected: str) -> None:
    value = entry.get(key)
    if value is not None and not predicate(value):
        raise ValueError(f"{label}.{key} must be {expected} (got {_short(value)})")


def _check_key(name: str, section: str) -> None:
    if not ENTRY_KEY_RE.fullmatch(name):
        raise ValueError(f"{section}: invalid name {name!r} (use letters, digits, '.', '_' and '-')")


def _section(raw: dict, name: str) -> dict:
    value = raw.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    return value


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_positive_int(value: Any) -> bool:
    return _is_int(value) and value > 0


def _is_positive_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _is_non_negative_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _is_str_list_dict(value: Any) -> bool:
    return isinstance(value, dict) and all(_is_str_list(item) for item in value.values())


def _is_str_dict(value: Any) -> bool:
    return isinstance(value, dict) and all(isinstance(v, str) for v in value.values())


def _is_http_url(value: Any) -> bool:
    if not isinstance(value, str) or any(ch.isspace() for ch in value):
        return False
    try:
        parts = urllib.parse.urlsplit(value)
        return parts.scheme in ("http", "https") and bool(parts.hostname)
    except ValueError:
        return False


def _short(value: Any) -> str:
    text = json.dumps(value) if isinstance(value, (dict, list, str, int, float, bool, type(None))) else repr(value)
    return text if len(text) <= 80 else text[:77] + "..."


def _dict_or_empty(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _list_or_empty(value: Any) -> list:
    return value if isinstance(value, list) else []


def _pruned(value: Any) -> Any:
    """value without None entries at any depth (saved configs mark removed defaults with null)."""
    if isinstance(value, dict):
        return {key: _pruned(item) for key, item in value.items() if item is not None}
    return value


# ---------------------------------------------------------------- model probes


@dataclasses.dataclass
class _Call:
    completion: Completion | None = None
    error: str | None = None
    latency: float = 0.0     # seconds from sending the request to the full reply
    generating: float = 0.0  # seconds from the first streamed token to the full reply (non-streamed: latency)


def test_model(config: Config, key: str, cancel: threading.Event | None = None,
               timeout: float = PROBE_TIMEOUT) -> dict:
    """Probe model `key` with a plain reply, then one call of a trivial get_time tool.

    Returns {ok, latency_ms, text, error, tool_call, tokens_per_second}: ok means the
    plain probe answered; error explains the first failure ("tool probe: ..." when only
    the tool probe failed). Each call may wait up to `timeout` seconds for a free
    provider slot and then gets `timeout` seconds to reply (probe_budget is the most
    the whole probe can take); never raises.
    """
    result: dict[str, Any] = {"ok": False, "latency_ms": None, "text": "", "error": None, "tool_call": False,
                              "tokens_per_second": None}
    try:
        _probe(config, key, cancel, timeout, result)
    except Exception as exc:  # a probe reports every failure in its result
        result["error"] = f"{type(exc).__name__}: {exc}"
    try:
        secrets = config.secret_values()
    except Exception:  # an unreadable key file must not turn a report into a crash
        secrets = []
    return redact(result, secrets)


def probe_budget(timeout: float = PROBE_TIMEOUT) -> float:
    """The most seconds test_model(..., timeout=timeout) can take: reach check, then each call's slot wait and reply."""
    return min(REACH_TIMEOUT, timeout) + PROBE_CALLS * (2 * timeout + ABANDON_SECONDS)


def _probe(config: Config, key: str, cancel: threading.Event | None, timeout: float, result: dict) -> None:
    if not isinstance(_dict_or_empty(config.raw.get("models")).get(key), dict):
        result["error"] = f"unknown model {key!r}"
        return
    model = config.model(key)
    provider_cfg = dataclasses.replace(model.provider, timeout=min(model.provider.timeout, timeout))
    model = dataclasses.replace(model, provider=provider_cfg)
    if provider_cfg.api_key_env and not provider_cfg.api_key():
        result["error"] = missing_key_message(provider_cfg)
        return
    status, problem = reach(provider_cfg, min(REACH_TIMEOUT, timeout))
    if status is None:
        result["error"] = problem
        return
    provider = make_provider(provider_cfg)
    room = min(model.max_tokens, REASONING_ROOM)
    thinks = _thinks(model)
    calls: list[_Call] = []
    plain = _call_with_room(provider, model, PLAIN_PROMPT, [], room if thinks else min(PLAIN_TOKENS, room), room,
                            cancel, timeout, calls)
    if plain.error is not None or plain.completion is None:
        result["error"] = plain.error
        return
    result.update(ok=True, latency_ms=round(plain.latency * 1000), text=plain.completion.text.strip()[:TEXT_CHARS])
    tool = _call_with_room(provider, model, TOOL_PROMPT, [GET_TIME_TOOL], room if thinks else min(TOOL_TOKENS, room),
                           room, cancel, timeout, calls)
    if tool.error is not None or tool.completion is None:
        result["error"] = f"tool probe: {tool.error}"
    else:
        result["tool_call"] = any(call.name == GET_TIME_TOOL["name"] for call in tool.completion.tool_calls)
    result["tokens_per_second"] = _rate(calls)


def _thinks(model: Any) -> bool:
    """Whether a model reasons before answering (and so needs room in max_tokens)."""
    if model.provider.type == "anthropic":
        return True
    extra = model.extra_body or {}
    thinking = extra.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type") not in (None, "disabled"):
        return True
    kwargs = extra.get("chat_template_kwargs")
    template_reasoning = isinstance(kwargs, dict) and bool(kwargs.get("reasoning_effort") or kwargs.get("enable_thinking"))
    return bool(extra.get("reasoning_effort") or extra.get("reasoning") or template_reasoning)


def _call_with_room(provider: Any, model: Any, prompt: str, tools: list[dict], budget: int, room: int,
                    cancel: threading.Event | None, timeout: float, calls: list[_Call]) -> _Call:
    """One probe call; a reply cut off before any text or tool call is retried once with `room` tokens."""
    messages = [provider.user_message(prompt)]
    call = _timed_call(provider, dataclasses.replace(model, max_tokens=budget), messages, tools, cancel, timeout)
    if call.completion is not None:
        calls.append(call)
        cut_short = call.completion.stop == "length" and not call.completion.text and not call.completion.tool_calls
        if cut_short and budget < room:
            call = _timed_call(provider, dataclasses.replace(model, max_tokens=room), messages, tools, cancel, timeout)
            if call.completion is not None:
                calls.append(call)
    return call


def _timed_call(provider: Any, model: Any, messages: list[dict], tools: list[dict],
                cancel: threading.Event | None, timeout: float) -> _Call:
    """provider.complete in a worker thread; abandoned when cancel is set, when no provider slot frees up
    within timeout seconds, or when the reply takes more than timeout seconds once the slot is held."""
    stop = threading.Event()
    call = _Call()
    marks: dict[str, float] = {}

    def on_delta(_kind: str, _chunk: str) -> None:
        marks.setdefault("first", time.monotonic())

    def work() -> None:
        slot_cfg = dataclasses.replace(model.provider, concurrency_scope="run")
        try:
            with provider_run_slot(slot_cfg, stop):
                marks["start"] = time.monotonic()
                completion = provider.complete(model, "", messages, tools, on_delta=on_delta, cancel=stop)
                end = time.monotonic()
            call.completion = completion
            call.latency = end - marks["start"]
            call.generating = end - (marks.get("first", marks["start"]) if _streams(model) else marks["start"])
        except ProviderError as exc:
            call.error = str(exc)
        except Exception as exc:  # reported, never raised into the caller
            call.error = f"{type(exc).__name__}: {exc}"

    worker = threading.Thread(target=work, name=f"probe-{model.key}", daemon=True)
    begun = time.monotonic()
    worker.start()
    while True:
        worker.join(0.05)
        if not worker.is_alive():
            return call
        if cancel is not None and cancel.is_set():
            reason = "cancelled"
            break
        started = marks.get("start")  # set by the worker once it holds the provider slot
        if time.monotonic() >= (begun if started is None else started) + timeout:
            reason = _timeout_message(model.provider, timeout, waited_for_slot=started is None)
            break
    stop.set()
    worker.join(ABANDON_SECONDS)
    return _Call(error=reason)


def _streams(model: Any) -> bool:
    """Whether the provider streams this call (extra_body "stream" overrides model.stream)."""
    extra = model.extra_body or {}
    return bool(extra["stream"]) if "stream" in extra else bool(model.stream)


def _timeout_message(provider: ProviderConfig, timeout: float, waited_for_slot: bool) -> str:
    if waited_for_slot:
        return (f"no free slot on provider {provider.name} within {timeout:g}s (max_concurrency "
                f"{provider.max_concurrency}): another agent is using it; try again when it is idle")
    return f"no reply within {timeout:g}s"


def _rate(calls: list[_Call]) -> float | None:
    tokens = sum(int(call.completion.usage.get("output_tokens") or 0) for call in calls if call.completion)
    seconds = sum(call.generating for call in calls)
    if tokens < MIN_RATE_TOKENS or seconds < MIN_RATE_SECONDS:
        return None
    return round(tokens / seconds, 1)


def missing_key_message(provider: ProviderConfig) -> str:
    """Why provider has no key: its env var is unset (and its key file does not define it)."""
    message = f"{provider.api_key_env} is not set"
    if provider.api_key_file:
        message += f" and {provider.api_key_file} does not define it"
    return message


def models_url(provider: ProviderConfig) -> str:
    """The provider's model list: <base_url>/models (OpenAI) or <base_url>/v1/models (Anthropic)."""
    suffix = "/v1/models" if provider.type == "anthropic" else "/models"
    return provider.base_url.rstrip("/") + suffix


def is_local_url(url: str) -> bool:
    """True for localhost and loopback addresses."""
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def reach(provider: ProviderConfig, timeout: float = REACH_TIMEOUT, authenticated: bool = True) -> tuple[int | None, str]:
    """GET the provider's model list: (HTTP status, "") if it answered, else (None, why not).

    authenticated=False sends no key and no secret-looking headers (to see whether the
    server enforces its key). Loopback servers are contacted without the proxy.
    """
    status, _, problem = _get(provider, models_url(provider), timeout, authenticated, read=False)
    return status, problem


def fetch_json(provider: ProviderConfig, url: str, timeout: float = REACH_TIMEOUT) -> tuple[int | None, Any, str]:
    """GET url with the provider's auth: (HTTP status, the parsed JSON body or None, "") if it answered,
    else (None, None, why not). A body that is not JSON (or over MAX_LIST_BYTES) parses as None."""
    return _get(provider, url, timeout, True, read=True)


def list_models(provider: ProviderConfig, timeout: float = REACH_TIMEOUT) -> tuple[int | None, list[str] | None, str]:
    """The provider's model list (GET models_url): (HTTP status, the model ids or None when the answer is
    not a list of models, ""), or (None, None, why not). OpenAI-format {"data": [{"id"}]} (also what
    Anthropic's /v1/models returns), or a bare list of such objects or of ids."""
    status, payload, problem = fetch_json(provider, models_url(provider), timeout)
    if status is None or status >= 400:
        return status, None, problem
    items = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return status, None, ""
    ids = [item.get("id") if isinstance(item, dict) else item for item in items]
    return status, [str(model_id) for model_id in ids if isinstance(model_id, str) and model_id], ""


def _get(provider: ProviderConfig, url: str, timeout: float, authenticated: bool,
         read: bool) -> tuple[int | None, Any, str]:
    if not _is_http_url(url):
        return None, None, f"cannot reach {url}: base_url must be an http(s) URL"
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if provider.type == "anthropic":
        headers["anthropic-version"] = ANTHROPIC_VERSION
    key = provider.api_key() if authenticated else None
    if key:
        if provider.auth in ("bearer", "both"):
            headers["Authorization"] = f"Bearer {key}"
        if provider.auth in ("x-api-key", "both"):
            headers["x-api-key"] = key
    headers.update({name: value for name, value in provider.headers.items()
                    if authenticated or not _SECRET_HEADER_RE.search(name)})
    local = is_local_url(url)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({})) if local else urllib.request.build_opener()
    try:
        with opener.open(urllib.request.Request(url, headers=headers, method="GET"), timeout=timeout) as response:
            return int(response.status), _json_body(response) if read else None, ""
    except urllib.error.HTTPError as err:
        err.close()
        return int(err.code), None, ""
    except (OSError, http.client.HTTPException, ValueError) as err:
        reason = err.reason if isinstance(err, urllib.error.URLError) else err
        hint = " (start your local server)" if local else ""
        return None, None, f"cannot reach {url}: {reason or type(reason).__name__}{hint}"


def _json_body(response: Any) -> Any:
    raw = response.read(MAX_LIST_BYTES + 1)
    if len(raw) > MAX_LIST_BYTES:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


# ---------------------------------------------------------------- agent overrides


def user_agents_dir(config: Config) -> Path:
    """Where agent overrides live: ~/.config/software-factory/agents (DESIGN §7, §22).

    The same dir config.agent_dirs gives the Factory and the chats, wherever the config
    file is ($FACTORY_CONFIG does not move it). `config` is kept for the callers.
    """
    return Path(os.path.expanduser(USER_DIR)) / "agents"


def agent_search_dirs(config: Config, repo_path: str | os.PathLike | None = None) -> list[Path]:
    """The existing agent dirs in override order (later wins), exactly as the Factory searches them
    (config.agent_dirs): shipped agents/, user overrides, <repo>/.factory/agents."""
    return agent_dirs(config, Path(repo_path) if repo_path is not None else None)


def resolved_agents(config: Config, repo_path: str | os.PathLike | None = None) -> dict[str, AgentDef]:
    """Every agent after overrides (ValueError naming the file if one does not parse)."""
    return discover_agents(agent_search_dirs(config, repo_path))


def agent_source(config: Config, agent: AgentDef) -> str:
    """"builtin" (shipped agents/), "user" (the overrides dir) or "repo" (a repo's .factory/agents)."""
    parent = Path(agent.path).parent.resolve()
    if parent == BUILTIN_AGENTS_DIR.resolve():
        return "builtin"
    if parent == user_agents_dir(config).resolve():
        return "user"
    return "repo"


def agent_detail(config: Config, name: str, repo_path: str | os.PathLike | None = None) -> dict:
    """{name, description, tools, model, color, skills, mcp, body, text, source, path} (KeyError if unknown)."""
    agent = resolved_agents(config, repo_path).get(name)
    if agent is None:
        raise KeyError(f"unknown agent {name!r}")
    return _detail(config, agent)


def save_agent(config: Config, name: str, text: str) -> dict:
    """Validate a whole agent file and write it atomically as the user override of `name`; returns its detail.

    Agents are known by their frontmatter name, not their file name (DESIGN §22: any
    markdown file in the overrides dir), so an existing override is rewritten in place,
    whatever its file is called; a new one is <name>.md. The frontmatter must parse and
    set name (equal to `name`) and description, and the body (the system prompt) must
    not be empty. ValueError, with nothing written, if the text is invalid or the write
    would leave the dir with two files defining one agent.
    """
    _check_writable_name(name)
    if not isinstance(text, str):
        raise ValueError("the agent text must be a string")
    if len(text.encode("utf-8")) > MAX_FILE_BYTES:
        raise ValueError(f"the agent file is larger than {MAX_FILE_BYTES} bytes")
    label = f"agents/{name}.md"
    meta, body = parse_frontmatter(text, label)
    if meta.get("name") != name:
        raise ValueError(f"{label}: the frontmatter name must be {name!r} (got {meta.get('name')!r})")
    if not meta.get("description"):
        raise ValueError(f"{label}: the frontmatter needs a description")
    if not body:
        raise ValueError(f"{label}: the body (the agent's system prompt) is empty")
    with _AGENT_LOCK:
        path = _override_target(_overrides_dir(config), name)
        _atomic_write(path, text if text.endswith("\n") else text + "\n")
        agent = parse_agent_file(path)
    return _detail(config, agent)


def new_agent(config: Config, name: str, from_: str = "reviewer-code") -> dict:
    """Create the user agent `name` as a copy of agent from_ with the name replaced; returns its detail.

    Copying one review lens to another also renames the review file it writes
    (review-code.md → review-<lens>.md). ValueError if name exists or from_ is unknown.
    """
    _check_writable_name(name)
    with _AGENT_LOCK:
        agents = resolved_agents(config)
        if name in agents:
            raise ValueError(f"agent {name!r} already exists ({agent_source(config, agents[name])}); edit it instead")
        source = agents.get(from_)
        if source is None:
            raise ValueError(f"unknown agent {from_!r} to copy from")
        text = read_small_text(source.path, MAX_FILE_BYTES)
        return save_agent(config, name, _renamed(text, from_, name))


def reset_agent(config: Config, name: str) -> dict | None:
    """Delete the user override of `name`, whatever its file is called (every such file, if several define it).

    KeyError if there is none. Returns the agent now in effect, or None if the agent
    existed only as the override.
    """
    _check_writable_name(name)
    with _AGENT_LOCK:
        files = _files_defining(_overrides_dir(config), name)
        if not files:
            raise KeyError(f"agent {name!r} has no user override")
        for path in files:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
    agent = resolved_agents(config).get(name)
    return _detail(config, agent) if agent is not None else None


def user_override_files(config: Config, name: str) -> list[Path]:
    """The files in the user agents dir that define agent `name`, sorted.

    A file defines the agent its frontmatter names (else its file stem, as
    discover_agents reads it). A file that does not parse counts by its stem, so a
    broken <name>.md is an override of `name` (reset removes it, save replaces it).
    """
    return _files_defining(user_agents_dir(config), name)


def _files_defining(directory: Path, name: str) -> list[Path]:
    if not dir_exists(directory):
        return []
    found = []
    for path in sorted(directory.glob("*.md")):
        if path.name.startswith(".") or path.name.lower() == "readme.md" or not path.is_file():
            continue  # discover_agents skips these too
        try:
            defined = parse_agent_file(path).name
        except ValueError:
            defined = path.stem
        if defined == name:
            found.append(path)
    return found


def _override_target(directory: Path, name: str) -> Path:
    """The file to write `name`'s override to: its one existing override, else <name>.md if that name is free."""
    files = _files_defining(directory, name)
    if len(files) > 1:
        raise ValueError(f"agent {name!r} is defined by {len(files)} files in {directory} "
                         f"({', '.join(path.name for path in files)}): reset it or delete all but one")
    if files:
        return files[0]
    path = directory / f"{name}.md"
    if os.path.lexists(path):
        raise ValueError(f"{path} exists but does not define agent {name!r} (it defines another agent or is "
                         f"not a regular file): rename it first")
    return path


def _detail(config: Config, agent: AgentDef) -> dict:
    return {"name": agent.name, "description": agent.description, "tools": list(agent.tools),
            "model": agent.model, "color": agent.color, "skills": list(agent.skills), "mcp": list(agent.mcp),
            "body": agent.body, "text": read_small_text(agent.path, MAX_FILE_BYTES),
            "source": agent_source(config, agent), "path": str(agent.path)}


def _check_writable_name(name: Any) -> None:
    if not isinstance(name, str) or not USER_AGENT_NAME_RE.fullmatch(name):
        raise ValueError(f"invalid agent name {name!r}: use a-z, 0-9 and '-' (max 41 chars, not starting with '-')")


def _overrides_dir(config: Config) -> Path:
    directory = user_agents_dir(config)
    if directory.resolve() == BUILTIN_AGENTS_DIR.resolve():
        raise ValueError(f"refusing to write agent overrides into the shipped {BUILTIN_AGENTS_DIR}")
    return directory


def _renamed(text: str, old: str, new: str) -> str:
    """Agent file text with the frontmatter name set to new (and review-<old>.md renamed for lenses)."""
    head, delimiter, body = text.partition("\n---")
    head, count = re.subn(r"(?m)^name[ \t]*:.*$", f"name: {new}", head, count=1)
    if not count:
        head = head.replace("---", f"---\nname: {new}", 1)
    prefix = "reviewer-"
    if old.startswith(prefix) and new.startswith(prefix):
        body = body.replace(f"review-{old[len(prefix):]}.md", f"review-{new[len(prefix):]}.md")
    return head + delimiter + body


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
