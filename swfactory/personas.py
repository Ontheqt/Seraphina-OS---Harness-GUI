"""Personas: named agents bound to who they are, and Open WebUI profiles (DESIGN.md section 33).

A persona is an agent file in the project or the user agent dir, never a repository's (repository
content is untrusted, so a repository cannot define one), whose frontmatter makes it one:

- ``openwebui: <workspace model id>`` binds it to an Open WebUI workspace model (the owner's "Kate").
  Its chats run through Open WebUI (design A of docs/seraphina/research/openwebui-profiles.md): Open
  WebUI applies the preset's system prompt, settings, skills and filters live on every call, and the
  agent file's body is only a thin note about the harness, so the harness's text does not compete with
  the preset's. At chat start, and once per turn, the binding reads ``GET /api/models``: it records
  ``info.version_id`` (which Kate answered each turn) and the base model's connection, sends the preset's
  default filters as ``filter_ids`` (and its knowledge as ``files`` only with ``knowledge: true``: Open
  WebUI then runs retrieval on every call), sends ``variables`` (local date, time, weekday, timezone,
  language, as the browser would), removes the harness's ``max_tokens`` so the preset's applies, and never
  sends ``tool_ids``, ``chat_id``, ``id`` or ``session_id``.
- ``source: openwebui`` with ``offline: true``: an imported snapshot of one (design B's explicit,
  labelled offline mode), which runs on the harness's own routing. Only an owner's explicit request
  writes one (import_profile with snapshot).
- ``wake: true``: a persona that wakes itself and messages the owner (Wake & Messages, W2, builds it on
  this module). Chats refuse it.

Every persona chat's own hop stays on this machine: its model's provider must have a loopback base_url, and
so must the Open WebUI the importer, the binding and the drift check talk to. Where Open WebUI sends the words
next is its configuration; the binding warns when it does not label the base model's connection local.

The importer reads an Export file (Open WebUI's UI) or ``GET /api/v1/models/export`` (the same shape)
into a read-only profile card: base model, settings as Open WebUI merges them, knowledge, filters,
skills, tools and what of it cannot run through the API, version, and fingerprints. The card keeps the
system prompt's length, hash and placeholders, never its text, and no user or group ids; it lives in the
user config dir (``~/.config/software-factory/profiles/openwebui/``, 0600). The drift check compares the
live preset with the last one seen (``<data_dir>/personas/openwebui/``): ``version_id`` changes on any
edit, the behaviour fingerprints ignore cosmetic ones (name, description, images, tags), and a change
that alters behaviour (or cannot be told apart) opens one system-scope Housekeeping Tracker item.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from . import items as tracker
from .agentdefs import MAX_FILE_BYTES, AgentDef, discover_agents, parse_bool, parse_frontmatter
from .config import USER_DIR, agent_dirs
from .events import utc_now

if TYPE_CHECKING:
    from .config import Config, ModelConfig, ProviderConfig

PERSONA_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,40}$")
RESERVED_NAMES = frozenset({"import", "check"})   # words of the /api/personas routes
OPENWEBUI_KEY_ENV = "OPENWEBUI_API_KEY"            # the owner's key (Wake & Messages §5.5 names it)
OPENWEBUI_KIND = "openwebui"                       # providers.<name>.kind of an Open WebUI provider
DEFAULT_PROVIDER = "openwebui"                     # examples/openwebui.json's provider
HTTP_TIMEOUT = 10.0
MAX_MODEL_ID_CHARS = 200
TURN_VERSIONS_KEPT = 200    # chat.json persona.turns: which version answered each turn, the newest kept
DRIFT_HISTORY_KEPT = 20
# Never sent on a persona chat: tool_ids makes Open WebUI offer (and in legacy mode run) the preset's own
# tools inside the harness's loop; chat_id, id and session_id switch on its saved-chat and socket paths.
FORBIDDEN_KEYS = ("tool_ids", "chat_id", "id", "session_id")
# Settings Open WebUI keeps to itself: never sent upstream (utils/payload.py remove_open_webui_params).
OPENWEBUI_ONLY_PARAMS = ("stream_response", "stream_delta_chunk_size", "function_calling", "reasoning_tags",
                         "compact_token_threshold", "system", "note_id", "tool_approval_mode", "model_controls")
# The meta keys that change what a preset does (the rest, such as description, images, tags, suggestion
# prompts and voice, are cosmetic).
BEHAVIOUR_META = ("toolIds", "skillIds", "filterIds", "defaultFilterIds", "actionIds", "defaultFeatureIds",
                  "capabilities", "builtinTools", "terminalId")
# Placeholders the harness fills itself in an offline snapshot (USER_NAME from the name typed at import);
# on the live path Open WebUI fills all of its own.
HARNESS_VARIABLES = ("CURRENT_DATE", "CURRENT_TIME", "CURRENT_DATETIME", "CURRENT_WEEKDAY", "CURRENT_TIMEZONE",
                     "USER_LANGUAGE", "USER_NAME")
VARIABLE_RE = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


class OpenWebUIError(Exception):
    """Open WebUI could not be asked, or refused (status: the HTTP status, None when it did not answer)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class OpenWebUISetupError(OpenWebUIError):
    """The harness's own setup keeps it from asking Open WebUI at all (no key, a provider that is not on this
    machine): nothing was sent, so the server answers 400, not 502."""


# ---------------------------------------------------------------- personas


@dataclass
class Persona:
    """One persona (the wake design's fields, plus how its chats run).

    kind: "openwebui" (bound live to a workspace model), "snapshot" (an imported offline copy) or "wake".
    soul: the agent file's body; for an Open WebUI binding only a thin note, the soul stays in Open WebUI.
    binding: {type: "openwebui", model, provider, knowledge, filters, timezone, language} or, for a
    snapshot, {type: "snapshot", model, version_id, fingerprint, behaviour_fingerprint, imported_at,
    user_name}. error: why the file cannot be used as a persona (its chats are refused), else None.
    """

    name: str
    description: str
    soul: str
    soul_sha256: str
    privacy: str
    source: dict | None
    path: Path
    kind: str = "openwebui"
    binding: dict | None = None
    error: str | None = None
    agent: AgentDef | None = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict:
        """The JSON view (GET /api/personas): no soul text, only its hash."""
        return {"name": self.name, "description": self.description, "kind": self.kind, "privacy": self.privacy,
                "soul_sha256": self.soul_sha256, "source": copy.deepcopy(self.source),
                "binding": copy.deepcopy(self.binding), "error": self.error, "path": str(self.path),
                "tools": list(self.agent.tools) if self.agent is not None else [],
                "model": self.agent.meta.get("model") if self.agent is not None else None}


def persona_dirs(config: Config | None) -> list[Path]:
    """Where personas are found: the project and user agent dirs, never a repository's."""
    return agent_dirs(config, None)


def discover_personas(config: Config | None) -> dict[str, Persona]:
    """Every persona in the project and user agent dirs (ValueError naming a file that does not parse)."""
    found: dict[str, Persona] = {}
    for name, agent in discover_agents(persona_dirs(config)).items():
        persona = persona_of(agent)
        if persona is not None:
            found[name] = persona
    return found


def find_persona(config: Config | None, name: str) -> Persona:
    """The persona called name (KeyError if there is none)."""
    persona = discover_personas(config).get(name) if isinstance(name, str) else None
    if persona is None:
        raise KeyError(f"unknown persona {name!r}")
    return persona


def wakes(agent: AgentDef) -> bool:
    """Whether the agent file wakes itself (wake: true); a value that is neither true nor false counts as true,
    so a typo never lets a waking persona into a chat."""
    return _flag(agent.meta.get("wake"), False, malformed=True)


def persona_of(agent: AgentDef) -> Persona | None:
    """The persona an agent file defines, or None when it is an ordinary agent."""
    meta = agent.meta
    model_id = str(meta.get("openwebui") or "").strip()
    snapshot = str(meta.get("source") or "").strip() == "openwebui" and _flag(meta.get("offline"), False,
                                                                                   malformed=True)
    wake = wakes(agent)
    if not (wake or model_id or snapshot):
        return None
    problems: list[str] = []
    if not PERSONA_NAME_RE.fullmatch(agent.name) or agent.name in RESERVED_NAMES:
        problems.append(f"a persona's name must match {PERSONA_NAME_RE.pattern} and not be "
                        f"{' or '.join(sorted(RESERVED_NAMES))} (got {agent.name!r})")
    privacy = str(meta.get("privacy") or "private").strip().lower()
    if privacy not in ("clean", "private"):
        problems.append(f"privacy must be clean or private (got {meta.get('privacy')!r})")
        privacy = "private"
    source = None
    if str(meta.get("source") or "").strip() == "openwebui":
        source = {"type": "openwebui", "model": meta.get("source_model") or None,
                  "version_id": meta.get("source_version_id") or None,
                  "fingerprint": meta.get("source_fingerprint") or None, "imported_at": meta.get("imported_at") or None}
    if wake:
        kind, binding = "wake", None
    elif model_id:
        kind = "openwebui"
        if len(model_id) > MAX_MODEL_ID_CHARS:
            problems.append(f"openwebui: the model id is longer than {MAX_MODEL_ID_CHARS} characters")
        zone = str(meta.get("timezone") or "").strip() or None
        if zone is not None and _zone(zone) is None:
            problems.append(f"timezone {zone!r} is not an IANA time zone this system knows")
        binding = {"type": "openwebui", "model": model_id,
                   "provider": str(meta.get("openwebui_provider") or "").strip() or None,
                   "knowledge": _flag(meta.get("knowledge"), False), "filters": _flag(meta.get("filters"), True),
                   "timezone": zone, "language": str(meta.get("language") or "").strip() or None}
    else:
        kind = "snapshot"
        binding = {"type": "snapshot", "model": (source or {}).get("model"),
                   "version_id": (source or {}).get("version_id"),
                   "fingerprint": (source or {}).get("fingerprint"),
                   "behaviour_fingerprint": meta.get("source_behaviour") or None,
                   "imported_at": (source or {}).get("imported_at"), "user_name": meta.get("user_name") or None}
        if not binding["model"]:
            problems.append("an imported snapshot needs source_model (the Open WebUI model it copies)")
    return Persona(name=agent.name, description=agent.description, soul=agent.body,
                   soul_sha256=hashlib.sha256(agent.body.encode("utf-8")).hexdigest(), privacy=privacy,
                   source=source, path=Path(agent.path), kind=kind, binding=binding,
                   error="; ".join(problems) or None, agent=agent)


def label(persona: Persona) -> str:
    """How the GUI and the transcript name what answers: "Kate through Open WebUI", or for a snapshot
    "Kate, imported snapshot of 2026-10-21, version 7f3c…"."""
    binding = persona.binding or {}
    title = str(binding.get("model") or persona.name).capitalize() if persona.kind == "snapshot" \
        else persona.name.capitalize()
    if persona.kind == "snapshot":
        when = str(binding.get("imported_at") or "")[:10] or "an unknown date"
        version = str(binding.get("version_id") or "unknown")[:12]
        return f"{title}, imported snapshot of {when}, version {version}"
    if persona.kind == "openwebui":
        return f"{title} through Open WebUI"
    return title


# ---------------------------------------------------------------- the model a persona's chats run on


def openwebui_providers(config: Config) -> list[str]:
    """Providers marked kind "openwebui", by name."""
    providers = config.raw.get("providers")
    return sorted(name for name, entry in (providers.items() if isinstance(providers, dict) else [])
                  if isinstance(entry, dict) and entry.get("kind") == OPENWEBUI_KIND)


def binding_provider(config: Config, persona: Persona) -> str:
    """The provider an Open WebUI persona talks to: openwebui_provider, else the one provider of kind openwebui,
    else "openwebui"."""
    named = (persona.binding or {}).get("provider")
    if named:
        return str(named)
    marked = openwebui_providers(config)
    return marked[0] if len(marked) == 1 else DEFAULT_PROVIDER


def default_model_key(config: Config, persona: Persona) -> str | None:
    """The models key a persona's chat runs on when nothing names one: the agent file's model:, else (for an
    Open WebUI binding) the one models entry that runs its workspace model on its provider."""
    agent = persona.agent
    if agent is not None and agent.meta.get("model"):
        return str(agent.meta["model"])
    if persona.kind != "openwebui":
        return None
    provider, model_id = binding_provider(config, persona), (persona.binding or {}).get("model")
    models = config.raw.get("models")
    found = sorted(key for key, entry in (models.items() if isinstance(models, dict) else [])
                   if isinstance(entry, dict) and entry.get("provider") == provider and entry.get("model") == model_id)
    return found[0] if len(found) == 1 else None


def chat_model_key(config: Config, persona: Persona) -> str | None:
    """The models key a persona's chat runs on unless a request or a profile names another: agents.<name>.model
    in the config, else default_model_key."""
    agents = config.raw.get("agents")
    entry = agents.get(persona.name) if isinstance(agents, dict) else None
    if isinstance(entry, dict) and entry.get("model"):
        return str(entry["model"])
    return default_model_key(config, persona)


def check_model(config: Config, persona: Persona, model: ModelConfig) -> None:
    """ValueError unless a persona's chat may run on model: always on this machine (a loopback provider), and
    for an Open WebUI binding on its provider and workspace model."""
    from .settings import is_local_url

    if persona.error:
        raise ValueError(f"persona {persona.name}: {persona.error}")
    if persona.kind == "wake":
        raise ValueError(f"{persona.name} is a persona that wakes itself (wake: true): it talks to you in its own "
                         f"thread, never in a chat")
    models = config.raw.get("models")
    if not isinstance(models, dict) or not isinstance(models.get(model.key), dict):
        raise ValueError(f"persona {persona.name} runs on models.{model.key}, which is not configured: apply the "
                         f"openwebui preset (Settings > Routing > Preset; it adds the openwebui provider and "
                         f"models.kate) or add the models entry")
    where = f"models.{model.key} (provider {model.provider.name}, {model.provider.base_url})"
    if not is_local_url(model.provider.base_url):
        raise ValueError(f"persona {persona.name} would run on {where}, which is not on this machine: a persona's "
                         f"chats carry your own words and stay local, so give it a model on a loopback provider")
    if persona.kind == "openwebui":
        provider, model_id = binding_provider(config, persona), (persona.binding or {}).get("model")
        if model.provider.name != provider or model.model != model_id:
            raise ValueError(f"persona {persona.name} is bound to Open WebUI's {model_id!r} on provider {provider}, "
                             f"but would run on {where} with model {model.model!r}: apply the openwebui preset "
                             f"(models.kate) or add a models entry {{\"provider\": \"{provider}\", \"model\": "
                             f"\"{model_id}\"}} and name it in the persona's model:")


def check_driver(config: Config, persona: Persona, driver: str) -> None:
    """ValueError unless a persona's chat runs on the builtin harness: a CLI driver reaches its own endpoint,
    whatever the models entry says, so a persona's words could leave the machine."""
    try:
        kind = config.driver(driver).type
    except (KeyError, ValueError) as exc:
        raise ValueError(f"persona {persona.name}: driver {driver!r}: {exc.args[0] if exc.args else exc}") from None
    if kind != "builtin":
        raise ValueError(f"persona {persona.name} would run on the {driver} driver ({kind}): a persona's chats run on the "
                         f"builtin harness only, so they stay on the models entry and on this machine")


# ---------------------------------------------------------------- Open WebUI over HTTP


def api_root(base_url: str) -> str:
    """Open WebUI's /api URL from a provider base_url ("http://127.0.0.1:8083/api", "/api/v1" or the bare host)."""
    root = base_url.rstrip("/")
    if root.endswith("/api/v1"):
        return root[:-len("/v1")]
    return root if root.endswith("/api") else root + "/api"


class OpenWebUI:
    """GET requests to an Open WebUI on this machine, with the provider's key (never through the proxy)."""

    def __init__(self, provider: ProviderConfig, timeout: float = HTTP_TIMEOUT) -> None:
        from .settings import is_local_url

        if not is_local_url(provider.base_url):
            raise OpenWebUISetupError(f"provider {provider.name} ({provider.base_url}) is not on this machine: personas "
                                      f"and profiles use an Open WebUI with a loopback base_url only")
        self.provider = provider
        self.timeout = timeout
        self.root = api_root(provider.base_url)

    def key_problem(self) -> str | None:
        """Why there is no key to send, or None."""
        from .settings import missing_key_message

        if not self.provider.api_key_env:
            return f"provider {self.provider.name} has no api_key_env (Open WebUI needs a key such as {OPENWEBUI_KEY_ENV})"
        if not self.provider.api_key():
            return (f"{missing_key_message(self.provider)}: create an API key in Open WebUI (Settings > Account > "
                    f"API keys; an admin turns them on under Admin Settings > General) and store it in the env file "
                    f"(POST /api/secrets)")
        return None

    def get(self, path: str, *, authenticated: bool = True, query: dict | None = None) -> tuple[int, Any]:
        """(status, parsed JSON body or None) of GET <api root><path>; OpenWebUIError when it does not answer."""
        from .settings import _get

        url = self.root + path + (("?" + urllib.parse.urlencode(query)) if query else "")
        status, payload, problem = _get(self.provider, url, self.timeout, authenticated, read=True)
        if status is None:
            raise OpenWebUIError(problem or f"cannot reach {url}")
        return status, payload

    def version(self) -> str | None:
        """Open WebUI's version (GET /api/version, which needs no key), None if it does not say."""
        status, payload = self.get("/version", authenticated=False)
        value = payload.get("version") if isinstance(payload, dict) and status < 400 else None
        return str(value) if isinstance(value, str) and value else None

    def models(self) -> list[dict]:
        """GET /api/models with the key: every model the key's user may read (workspace models have preset true)."""
        problem = self.key_problem()
        if problem:
            raise OpenWebUISetupError(problem)
        status, payload = self.get("/models")
        if status in (401, 403):
            raise OpenWebUIError(f"Open WebUI refused {self.provider.api_key_env} (HTTP {status}): check the key; "
                                 f"API keys must be enabled (ENABLE_API_KEYS) and a non-admin needs the API keys "
                                 f"permission", status)
        if status >= 400:
            raise OpenWebUIError(f"GET {self.root}/models answered HTTP {status}", status)
        data = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(data, list):
            raise OpenWebUIError(f"GET {self.root}/models did not return a model list")
        return [entry for entry in data if isinstance(entry, dict) and isinstance(entry.get("id"), str)]

    def export(self, model_id: str) -> list[dict]:
        """GET /api/v1/models/export?ids=<model_id>: the full records (owner key: write access, and for a
        non-admin the workspace.models_export permission)."""
        problem = self.key_problem()
        if problem:
            raise OpenWebUISetupError(problem)
        status, payload = self.get("/v1/models/export", query={"ids": model_id})
        if status in (401, 403):
            raise OpenWebUIError(f"Open WebUI does not let this key export {model_id!r} (HTTP {status}): exporting needs "
                                 f"write access to the model (the owner's key), and a non-admin the "
                                 f"workspace.models_export permission; or upload the Export file instead", status)
        if status >= 400:
            raise OpenWebUIError(f"GET {self.root}/v1/models/export answered HTTP {status}", status)
        if not isinstance(payload, list):
            raise OpenWebUIError("the export is not a JSON array of model records (or it is larger than 4 MB)")
        return [record for record in payload if isinstance(record, dict)]

    def default_params(self) -> dict | None:
        """The global Model Defaults (GET /api/v1/configs/models, admins only); None when this key may not read them."""
        with contextlib.suppress(OpenWebUIError):
            status, payload = self.get("/v1/configs/models")
            if status < 400 and isinstance(payload, dict):
                params = payload.get("DEFAULT_MODEL_PARAMS")
                return dict(params) if isinstance(params, dict) else {}
        return None


def fetch_openwebui_export(base_url: str, key: str, model_id: str, timeout: float = HTTP_TIMEOUT) -> list:
    """GET /api/v1/models/export?ids=<model_id> at base_url with key (the wake design's importer entry point)."""
    from .config import ProviderConfig

    @dataclass
    class _Keyed(ProviderConfig):
        key: str = field(default="", repr=False)

        def api_key(self) -> str | None:
            return self.key or None

    provider = _Keyed(name="openwebui-export", type="openai", base_url=base_url, api_key_env=OPENWEBUI_KEY_ENV, key=key)
    return OpenWebUI(provider, timeout).export(model_id)


def client(config: Config, persona: Persona | None = None, provider: str | None = None) -> OpenWebUI:
    """An OpenWebUI client for provider (default: the persona's binding provider). KeyError for a provider that is
    not configured (apply the openwebui preset), OpenWebUIError for one that is not on this machine."""
    name = provider or (binding_provider(config, persona) if persona is not None else None)
    if name is None:
        marked = openwebui_providers(config)
        name = marked[0] if len(marked) == 1 else DEFAULT_PROVIDER
    try:
        cfg = config.provider(name)
    except KeyError:
        raise KeyError(f"no provider {name!r}: apply the openwebui preset (Settings > Routing > Preset) or add an "
                       f"Open WebUI provider") from None
    return OpenWebUI(cfg)


def listed(entries: list[dict], model_id: str) -> dict | None:
    """The GET /api/models entry of model_id, or None."""
    return next((entry for entry in entries if entry.get("id") == model_id), None)


# ---------------------------------------------------------------- variables, the live binding and each turn


def local_zone() -> str:
    """This machine's IANA time zone: $TZ, else /etc/localtime's target, else /etc/timezone, else UTC."""
    candidates = [os.environ.get("TZ", "").lstrip(":")]
    with contextlib.suppress(OSError):
        target = os.readlink("/etc/localtime")
        if "zoneinfo/" in target:
            candidates.append(target.split("zoneinfo/", 1)[1])
    with contextlib.suppress(OSError, UnicodeDecodeError):
        candidates.append(Path("/etc/timezone").read_text(encoding="utf-8").strip())
    for name in candidates:
        if name and _zone(name) is not None:
            return name
    return "UTC"


def local_language() -> str:
    """A BCP 47 tag from $LC_ALL, $LC_MESSAGES or $LANG ("en_GB.UTF-8" is "en-GB"), else "en-US"."""
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(name, "").split(".", 1)[0].split("@", 1)[0]
        if value and value not in ("C", "POSIX") and re.fullmatch(r"[A-Za-z]{2,3}(_[A-Za-z]{2})?", value):
            return value.replace("_", "-")
    return "en-US"


def prompt_variables(zone: str | None = None, language: str | None = None, now: datetime | None = None) -> dict:
    """The browser's prompt variables as Open WebUI takes them in a request's variables object: the owner's
    local date (YYYY-MM-DD), time (HH:MM:SS, 24 h), date and time, weekday, IANA time zone and language."""
    zone = zone or local_zone()
    tz = _zone(zone) or timezone.utc
    local = (now or datetime.now(timezone.utc)).astimezone(tz)
    date, clock = local.strftime("%Y-%m-%d"), local.strftime("%H:%M:%S")
    return {"{{CURRENT_DATE}}": date, "{{CURRENT_TIME}}": clock, "{{CURRENT_DATETIME}}": f"{date} {clock}",
            "{{CURRENT_WEEKDAY}}": WEEKDAYS[local.weekday()], "{{CURRENT_TIMEZONE}}": zone,
            "{{USER_LANGUAGE}}": language or local_language()}


def fill_variables(text: str, variables: dict, user_name: str | None = None) -> str:
    """text with the placeholders the harness knows replaced (an offline snapshot's prompt; Open WebUI fills
    them itself on the live path); others are left as they are."""
    values = {key.strip("{} "): value for key, value in variables.items()}
    if user_name:
        values["USER_NAME"] = user_name

    def one(match: re.Match[str]) -> str:
        return str(values[match.group(1)]) if match.group(1) in values else match.group(0)

    return VARIABLE_RE.sub(one, text)


def start_binding(config: Config, persona: Persona, model: ModelConfig, *,
                  open_item: Callable[..., dict] | None = None) -> dict:
    """The record a persona chat keeps in chat.json["persona"], read at chat start; ValueError when the chat
    cannot start (the model is not the persona's, Open WebUI does not answer or does not list it).

    Open WebUI binding: {type, persona, label, model, provider, name, version_id, base_model_id, base_model:
    {id, listed, owned_by, connection_type} | None, warnings: [text], knowledge: [{type, id}], filter_ids,
    not_through_api: [part], open_webui_version, live_fingerprint, drift, read_at, turns: []}. Snapshot:
    {type, persona, label, model, version_id, fingerprint, imported_at, stale, drift, turns: []}. warnings
    says what the harness cannot vouch for beyond its own hop (the base model's connection, see
    base_model_warnings); the chat still starts.
    """
    check_model(config, persona, model)
    if persona.kind == "snapshot":
        binding = persona.binding or {}
        record = {"type": "snapshot", "persona": persona.name, "label": label(persona), "model": binding.get("model"),
                  "version_id": binding.get("version_id"), "fingerprint": binding.get("fingerprint"),
                  "imported_at": binding.get("imported_at"), "stale": None, "drift": None, "turns": []}
        record.update(_snapshot_check(config, persona, record))
        return record
    try:
        api = client(config, persona)
        entries = api.models()
        version = _quiet(api.version)
    except (KeyError, OpenWebUIError) as exc:
        raise ValueError(f"persona {persona.name}: {exc.args[0] if exc.args else exc}") from None
    model_id = (persona.binding or {}).get("model")
    entry = listed(entries, model_id)
    if entry is None:
        workspace = sorted(e["id"] for e in entries if e.get("preset"))
        raise ValueError(f"persona {persona.name}: Open WebUI does not list {model_id!r} for this key (workspace models "
                         f"it lists: {', '.join(workspace) or 'none'})")
    record = _record(persona, api.provider.name, entry, version, entries)
    record["turns"] = []
    record["drift"] = _drift_summary(check_drift(config, model_id, entry.get("info") or {},
                                                 name=str(entry.get("name") or model_id), provider=api.provider.name,
                                                 open_item=open_item, api=api))
    return record


def turn_body(persona: Persona, record: dict, now: datetime | None = None) -> dict:
    """The extra_body of one persona chat turn, merged over the route's: for an Open WebUI binding the
    variables, files (the preset's knowledge, when the persona file turns it on), filter_ids (its default
    filters), max_tokens removed so the preset's applies, and every FORBIDDEN_KEYS entry removed (None, so
    providers.merge_extra drops it whatever the model entry says). stream is left to the models entry: Open
    WebUI's Stream Chat Response overrides it either way, and the provider reads a whole JSON answer to a
    stream request and a stream answering a request without one. A snapshot sends nothing extra."""
    if record.get("type") != "openwebui":
        return {}
    binding = persona.binding or {}
    body: dict[str, Any] = {key: None for key in FORBIDDEN_KEYS}
    body["max_tokens"] = None
    body["variables"] = prompt_variables(binding.get("timezone"), binding.get("language"), now)
    if record.get("knowledge"):
        body["files"] = [dict(entry) for entry in record["knowledge"]]
    if record.get("filter_ids"):
        body["filter_ids"] = list(record["filter_ids"])
    return body


def persona_turn(config: Config, persona: Persona, record: dict, *, turn: int,
                 open_item: Callable[..., dict] | None = None, now: datetime | None = None
                 ) -> tuple[dict, dict, list[tuple[str, str]], dict]:
    """One turn of a persona chat: (extra_body, the record's update for chat.json, log notes [(level, text)],
    the persona.turn event's data). The live binding is read again (GET /api/models), so each turn records
    the version that answered; when that read fails the turn keeps the binding it had and its version is
    recorded as unknown (the chat call itself then says what is wrong)."""
    notes: list[tuple[str, str]] = []
    update: dict[str, Any] = {}
    if persona.kind == "snapshot":
        update = _snapshot_check(config, persona, record)
        if update.get("stale"):
            notes.append(("warn", f"{record.get('label')}: the live preset has changed since this snapshot "
                                  f"({update['stale']}); re-import it to follow Open WebUI"))
        version = record.get("version_id")
    else:
        model_id = (persona.binding or {}).get("model")
        version = None
        try:
            api = client(config, persona)
            entries = api.models()
            entry = listed(entries, model_id)
            if entry is None:
                raise OpenWebUIError(f"Open WebUI no longer lists {model_id!r} for this key")
        except (KeyError, OpenWebUIError) as exc:
            notes.append(("warn", f"{label(persona)}: could not read the live binding ({exc.args[0] if exc.args else exc}); "
                                  f"this turn uses the one read before, and its version is unknown"))
        else:
            fresh = _record(persona, api.provider.name, entry, record.get("open_webui_version"), entries)
            update = {key: fresh[key] for key in ("name", "version_id", "base_model_id", "base_model", "warnings",
                                                  "knowledge", "filter_ids", "not_through_api", "live_fingerprint",
                                                  "read_at")}
            if turn == 1 or fresh["warnings"] != (record.get("warnings") or []):
                notes.extend(("warn", f"{label(persona)}: {warning}") for warning in fresh["warnings"])
            version = fresh["version_id"]
            if version != record.get("version_id"):
                notes.append(("info", f"{label(persona)} changed in Open WebUI since the last turn (version "
                                      f"{_short(record.get('version_id'))} → {_short(version)})"))
                update["drift"] = _drift_summary(check_drift(config, model_id, entry.get("info") or {},
                                                             name=str(entry.get("name") or model_id),
                                                             provider=api.provider.name, open_item=open_item, api=api))
    merged = {**record, **update}
    turns = list(record.get("turns") or [])
    turns.append({"turn": turn, "version_id": version, "at": utc_now()})
    update["turns"] = turns[-TURN_VERSIONS_KEPT:]
    event = {"persona": persona.name, "type": record.get("type"), "model": record.get("model"), "version_id": version,
             "previous_version_id": record.get("version_id"), "turn": turn, "label": record.get("label")}
    if update.get("drift"):
        event["drift"] = update["drift"]
    return turn_body(persona, merged), update, notes, event


def prompt_context(persona: Persona, record: dict) -> dict:
    """What the builtin loop needs to build a persona's thin system prompt (RunContext.persona)."""
    binding = persona.binding or {}
    context = {"type": record.get("type"), "name": persona.name, "label": record.get("label") or label(persona)}
    if persona.kind == "snapshot":
        context["variables"] = prompt_variables(None, None)
        context["user_name"] = binding.get("user_name")
    return context


def _record(persona: Persona, provider: str, entry: dict, version: str | None, entries: list[dict]) -> dict:
    info = entry.get("info") if isinstance(entry.get("info"), dict) else {}
    meta = info.get("meta") if isinstance(info.get("meta"), dict) else {}
    binding = persona.binding or {}
    knowledge_on, filters_on = binding.get("knowledge", False), binding.get("filters", True)
    knowledge = _knowledge_refs(meta.get("knowledge")) if knowledge_on else []
    filters = _str_list(meta.get("defaultFilterIds")) if filters_on else []
    base = base_model_view(entries, entry)
    name = entry.get("name") or info.get("name")
    return {"type": "openwebui", "persona": persona.name, "label": label(persona), "model": binding.get("model"),
            "provider": provider, "name": name, "version_id": info.get("version_id"),
            "base_model_id": info.get("base_model_id"), "base_model": base,
            "warnings": base_model_warnings(base, str(name or binding.get("model") or persona.name)),
            "knowledge": knowledge, "filter_ids": filters,
            "not_through_api": [part["part"] for part in parts(meta, knowledge_on, filters_on) if not part["through_api"]],
            "open_webui_version": version, "live_fingerprint": live_fingerprint(info), "read_at": utc_now()}


def base_model_view(entries: list[dict], entry: dict) -> dict | None:
    """{id, listed, owned_by, connection_type} of a workspace model's base model as GET /api/models shows it
    (looked up as Open WebUI does, also without a ":tag"), None when the preset names none. connection_type is
    the base model's, else the one Open WebUI copied onto the preset's own entry."""
    info = entry.get("info") if isinstance(entry.get("info"), dict) else {}
    base_id = info.get("base_model_id")
    if not isinstance(base_id, str) or not base_id:
        return None
    base = listed(entries, base_id) or listed(entries, base_id.split(":", 1)[0])
    return {"id": base_id, "listed": base is not None, "owned_by": (base or entry).get("owned_by"),
            "connection_type": (base or {}).get("connection_type") or entry.get("connection_type")}


def base_model_warnings(base: dict | None, title: str) -> list[str]:
    """What the harness cannot vouch for about where a persona's words go after Open WebUI: it keeps its own hop
    on this machine (a loopback Open WebUI), but Open WebUI sends the words on to the preset's base model (and,
    by its own settings, to its task and embedding models). A base model the key does not see, or one on a
    connection Open WebUI does not label local, is a warning (Open WebUI labels an OpenAI-type connection
    external unless an admin sets it to local, so a local TabbyAPI can carry that label too)."""
    if base is None:
        return []
    if not base.get("listed"):
        return [f"Open WebUI does not list {title}'s base model {base['id']!r} for this key: it may refuse the call, "
                f"or (with ENABLE_CUSTOM_MODEL_FALLBACK) answer on another model the harness cannot see"]
    kind = base.get("connection_type")
    if kind == "local":
        return []
    owner = f", owned by {base['owned_by']}" if base.get("owned_by") else ""
    return [f"Open WebUI labels the connection of {title}'s base model {base['id']!r} {kind or 'with no type'}{owner}: "
            f"the harness keeps your words on this machine only as far as Open WebUI, which sends them on to that "
            f"connection. If it runs on this machine, set its connection type to Local in Open WebUI's connection "
            f"settings; if it does not, your words leave the machine there"]


def _snapshot_check(config: Config, persona: Persona, record: dict) -> dict:
    """{stale, drift} for a snapshot: compared with the live preset when Open WebUI answers; nothing when it
    does not (that is what the offline mode is for)."""
    model_id = (persona.binding or {}).get("model")
    try:
        api = client(config, persona)
        entry = listed(api.models(), model_id)
    except (KeyError, OpenWebUIError):
        return {}
    if entry is None:
        return {"stale": f"Open WebUI no longer lists {model_id!r}"}
    live = (entry.get("info") or {}).get("version_id")
    if live and live != record.get("version_id"):
        return {"stale": f"version {_short(record.get('version_id'))} → {_short(live)}"}
    return {"stale": None}


# ---------------------------------------------------------------- the importer and the profile card


@dataclass
class ImportedProfile:
    """One workspace model read from an export (the wake design's fields first, then the profile card's)."""

    model_id: str
    name: str
    description: str
    system: str
    variables: list[str]
    unknown_variables: list[str]
    unavailable: dict[str, list[str]]       # what an imported copy cannot have: tools, knowledge, filters, ...
    version_id: str | None
    fingerprint: str                        # sha256 of the canonical JSON of the system prompt and params
    base_model_id: str | None = None
    updated_at: Any = None
    params: dict = field(default_factory=dict)        # the preset's own params, system left out
    meta: dict = field(default_factory=dict)          # its behaviour meta (BEHAVIOUR_META), as given
    knowledge: list[dict] = field(default_factory=list)   # [{type, id, name}]
    effective: dict = field(default_factory=dict)     # the settings Open WebUI sends upstream (effective_params)
    stream: bool | None = None                        # Stream Chat Response, when set
    function_calling: str | None = None
    model_controls: dict | None = None
    defaults_known: bool = False                      # DEFAULT_MODEL_PARAMS were read (an admin's key)
    behaviour_fingerprint: str = ""
    live_fingerprint: str = ""
    notes: list[str] = field(default_factory=list)


def parse_openwebui_export(records: Any, model_id: str | None = None, *,
                           defaults: dict | None = None) -> ImportedProfile:
    """The workspace model model_id in an Export file or GET /api/v1/models/export answer (a JSON array of
    model records; a single record object is taken too); with model_id None the file must hold exactly one.
    defaults: Open WebUI's DEFAULT_MODEL_PARAMS when known (an admin's key). ValueError naming what is missing."""
    if isinstance(records, dict):
        records = [records]
    if not isinstance(records, list) or not all(isinstance(record, dict) for record in records):
        raise ValueError("an Open WebUI export is a JSON array of model records")
    ids = [str(record.get("id")) for record in records if isinstance(record.get("id"), str)]
    if model_id is None:
        if len(records) != 1:
            raise ValueError(f"the export holds {len(records)} models ({', '.join(ids) or 'none with an id'}): "
                             f"say which one (model_id)")
        record = records[0]
    else:
        record = next((r for r in records if r.get("id") == model_id), None)
        if record is None:
            raise ValueError(f"the export has no model {model_id!r} (it has: {', '.join(ids) or 'none'})")
    if not isinstance(record.get("id"), str) or not record["id"].strip():
        raise ValueError("the model record has no id")
    params = record.get("params") if isinstance(record.get("params"), dict) else {}
    meta = record.get("meta") if isinstance(record.get("meta"), dict) else {}
    if record.get("params") is not None and not isinstance(record.get("params"), dict):
        raise ValueError("the model record's params is not an object")
    system = params.get("system") if isinstance(params.get("system"), str) else ""
    names = sorted(set(VARIABLE_RE.findall(system)))
    known = [name for name in names if name in HARNESS_VARIABLES]
    unknown = [name for name in names if name not in HARNESS_VARIABLES]
    knowledge = _knowledge(meta.get("knowledge"))
    function_calling = params.get("function_calling") if isinstance(params.get("function_calling"), str) else None
    notes = []
    if function_calling == "default":
        notes.append("function_calling 'default' is the value from before Open WebUI 0.10: it runs as native now "
                     "(only 'legacy' selects the old prompt-based tool mode)")
    controls = params.get("model_controls") if isinstance(params.get("model_controls"), dict) else None
    if controls:
        notes.append(f"model controls ({', '.join(sorted(controls))}): the settings include each control's default "
                     f"option, as Open WebUI applies it to an API caller that picks none (the harness picks none)")
    stops = params.get("stop") if isinstance(params.get("stop"), list) else [params.get("stop")]
    if any(isinstance(stop, str) and "\\" in stop for stop in stops):
        notes.append("stop sequences with a backslash (such as \\n) reach the model as written, not as a newline: "
                     "Open WebUI 0.12.3 does not unescape a preset's stop for an API caller")
    if unknown:
        notes.append(f"placeholders an offline copy cannot fill: {', '.join('{{' + n + '}}' for n in unknown)} "
                     f"(Open WebUI fills them on the live path)")
    own = {key: value for key, value in params.items() if key != "system"}
    profile = ImportedProfile(
        model_id=record["id"], name=str(record.get("name") or record["id"]),
        description=str(meta.get("description") or ""), system=system, variables=known, unknown_variables=unknown,
        unavailable=_unavailable(meta, knowledge), version_id=_str_or_none(record.get("version_id")),
        fingerprint=sha256_of({"system": system, "params": own}),
        base_model_id=_str_or_none(record.get("base_model_id")), updated_at=record.get("updated_at"),
        params=own, meta={key: copy.deepcopy(meta[key]) for key in BEHAVIOUR_META if key in meta},
        knowledge=knowledge, effective=effective_params(params, defaults),
        stream=params.get("stream_response") if isinstance(params.get("stream_response"), bool) else None,
        function_calling=function_calling, model_controls=copy.deepcopy(controls), defaults_known=defaults is not None,
        behaviour_fingerprint=behaviour_fingerprint(record, defaults), live_fingerprint=live_fingerprint(record),
        notes=notes)
    return profile


def merge_model_params(defaults: dict | None, params: dict | None) -> dict:
    """The preset's params over the global defaults; custom_params merged key by key (utils/misc.py)."""
    base, own = dict(defaults or {}), dict(params or {})
    merged = {**base, **own}
    if isinstance(base.get("custom_params"), dict) or isinstance(own.get("custom_params"), dict):
        merged["custom_params"] = {**_dict(base.get("custom_params")), **_dict(own.get("custom_params"))}
    return merged


def effective_params(params: dict | None, defaults: dict | None = None) -> dict:
    """The settings Open WebUI 0.12.3 sends upstream for a preset when an API caller picks no model control and
    sends none of them itself (a request's own top-level fields win, fill-only): DEFAULT_MODEL_PARAMS, the
    preset's params over them (custom_params key by key, main.py), each model control's default option
    deep-merged into custom_params (utils/payload.py apply_model_controls), Open WebUI-only keys left out, and
    each custom_params value parsed as JSON when it is a JSON string and deep-merged in as a top-level field
    (apply_params_to_form_data). stop is kept as stored: on this path the preset's params reach the request
    before the router's unescaping pass, which only fills keys still missing, so a stored "\\n" stays a
    backslash and an n."""
    merged = merge_model_params(defaults, params)
    custom = _dict(merged.pop("custom_params", None))
    for control in _dict(merged.get("model_controls")).values():
        control = _dict(control)
        choice = control.get("default")
        options = _dict(control.get("options"))
        if isinstance(choice, str) and isinstance(options.get(choice), dict):
            custom = _deep(custom, _dict(options[choice].get("params")))
    out = {key: copy.deepcopy(value) for key, value in merged.items()
           if key not in OPENWEBUI_ONLY_PARAMS and value is not None}
    for key, value in custom.items():
        value = _json_value(value)
        out[key] = _deep(out.get(key), value)
    return out


def parts(meta: dict, knowledge_on: bool | None = None, filters_on: bool = True) -> list[dict]:
    """What of a preset reaches a harness chat through Open WebUI's API (design A), part by part: [{part, ids,
    through_api, note}]. The prompt, settings and skills apply live; knowledge and default filters when the
    binding sends them (knowledge_on None: a profile card's view, before any persona file decides; knowledge
    is off unless the persona file says knowledge: true); tools, actions, features, built-in tools and the
    terminal only in Open WebUI's own chats."""
    meta = meta if isinstance(meta, dict) else {}
    tools = _str_list(meta.get("toolIds"))
    rows = [
        {"part": "system prompt", "ids": [], "through_api": True,
         "note": "Open WebUI puts it first on every call; the harness's short note comes after it"},
        {"part": "settings", "ids": [], "through_api": True,
         "note": "applied fill-only; the harness leaves out its own max_tokens so the preset's applies"},
    ]
    knowledge = [entry["id"] for entry in _knowledge_refs(meta.get("knowledge"))]
    if knowledge:
        cost = "retrieval then runs on every model call, with Open WebUI's task and embedding models"
        rows.append({"part": "knowledge", "ids": knowledge, "through_api": knowledge_on is not False,
                     "note": f"sent as files on every call (knowledge: true); {cost}" if knowledge_on
                     else f"off unless the persona file says knowledge: true; {cost}" if knowledge_on is None
                     else f"off: the persona file does not say knowledge: true ({cost})"})
    if _str_list(meta.get("filterIds")):
        rows.append({"part": "filters", "ids": _str_list(meta.get("filterIds")), "through_api": True,
                     "note": "always-on filters run; what an outlet filter changes is not returned"})
    if _str_list(meta.get("defaultFilterIds")):
        rows.append({"part": "default filters", "ids": _str_list(meta.get("defaultFilterIds")),
                     "through_api": filters_on,
                     "note": "sent as filter_ids" if filters_on else "the persona file turns them off (filters: false)"})
    if _str_list(meta.get("skillIds")):
        rows.append({"part": "skills", "ids": _str_list(meta.get("skillIds")), "through_api": True,
                     "note": "Open WebUI adds each skill in full to the system message"})
    if tools:
        mcp = [tool for tool in tools if tool.startswith("server:mcp:")]
        note = "Open WebUI runs a preset's tools only in its own chats; the harness never sends tool_ids"
        if mcp:
            note += f"; {', '.join(mcp)} can be connected to the harness as its own MCP server instead"
        rows.append({"part": "tools", "ids": tools, "through_api": False, "note": note})
    for key, name in (("actionIds", "actions"), ("defaultFeatureIds", "features")):
        if _str_list(meta.get(key)):
            rows.append({"part": name, "ids": _str_list(meta.get(key)), "through_api": False,
                         "note": "only in Open WebUI's web UI (they need its browser session)"})
    if meta.get("builtinTools"):
        rows.append({"part": "built-in tools", "ids": sorted(_dict(meta.get("builtinTools"))), "through_api": False,
                     "note": "only in Open WebUI's web UI (they need its browser session)"})
    if meta.get("terminalId"):
        rows.append({"part": "terminal", "ids": [str(meta["terminalId"])], "through_api": False,
                     "note": "only in Open WebUI's web UI"})
    return rows


def profile_card(profile: ImportedProfile, *, source: str, open_webui_version: str | None = None,
                 imported_at: str | None = None) -> dict:
    """The read-only profile card: what the preset is and what reaches the harness. It holds the system
    prompt's length, hash and placeholders but not its text, and no user or group ids."""
    meta = profile.meta
    tools = [{"id": tool, "kind": "mcp" if tool.startswith("server:mcp:") else "tool", "through_api": False,
              "harness": ("connect the same MCP server to the harness (mcp_servers)" if tool.startswith("server:mcp:")
                          else "cannot run outside Open WebUI")}
             for tool in _str_list(meta.get("toolIds"))]
    return {
        "model_id": profile.model_id, "name": profile.name, "description": profile.description,
        "source": source, "imported_at": imported_at or utc_now(), "open_webui_version": open_webui_version,
        "version_id": profile.version_id, "updated_at": profile.updated_at, "base_model_id": profile.base_model_id,
        "system": {"chars": len(profile.system), "sha256": hashlib.sha256(profile.system.encode("utf-8")).hexdigest(),
                   "variables": list(profile.variables), "unknown_variables": list(profile.unknown_variables)},
        "settings": copy.deepcopy(profile.effective), "defaults_known": profile.defaults_known,
        "stream": profile.stream, "function_calling": profile.function_calling,
        "model_controls": sorted(profile.model_controls) if profile.model_controls else [],
        "knowledge": [dict(entry) for entry in profile.knowledge],
        "filters": {"always": _str_list(meta.get("filterIds")), "default_on": _str_list(meta.get("defaultFilterIds"))},
        "skills": _str_list(meta.get("skillIds")), "tools": tools, "actions": _str_list(meta.get("actionIds")),
        "features": _str_list(meta.get("defaultFeatureIds")), "builtin_tools": sorted(_dict(meta.get("builtinTools"))),
        "terminal": meta.get("terminalId") or None, "capabilities": copy.deepcopy(_dict(meta.get("capabilities"))),
        "parts": parts({**meta, "knowledge": profile.knowledge}),
        "unavailable": copy.deepcopy(profile.unavailable), "notes": list(profile.notes),
        "fingerprint": profile.fingerprint, "behaviour_fingerprint": profile.behaviour_fingerprint,
        "live_fingerprint": profile.live_fingerprint,
    }


def profiles_dir() -> Path:
    """Where profile cards live: ~/.config/software-factory/profiles/openwebui (also when $FACTORY_CONFIG moves
    the config, like the user agent dir)."""
    return Path(os.path.expanduser(USER_DIR)) / "profiles" / "openwebui"


def profile_path(model_id: str) -> Path:
    return profiles_dir() / f"{slug(model_id)}.json"


def save_profile(card: dict) -> Path:
    """Write a profile card (0600 in a 0700 dir), atomically."""
    from .chats import write_json

    path = profile_path(card["model_id"])
    _private_dir(path.parent)
    write_json(path, card)
    os.chmod(path, 0o600)
    return path


def load_profile(model_id: str) -> dict | None:
    """The saved profile card of model_id, or None."""
    from .chats import read_json

    with contextlib.suppress(OSError, ValueError):
        card = read_json(profile_path(model_id))
        if card.get("model_id") == model_id:
            return card
    return None


def import_profile(config: Config, *, records: Any = None, model_id: str | None = None,
                   provider: str | None = None, snapshot: dict | None = None,
                   on_saved: Callable[[Any], None] | None = None) -> dict:
    """Import one workspace model: from records (an uploaded Export file) or, with records None, from GET
    /api/v1/models/export on provider (the owner's key). Saves its profile card and returns {card, path,
    unavailable, unknown_variables, fingerprint, snapshot}. snapshot (only when the owner asks for the offline
    mode): {name, model?: the models key it runs on, user_name?}: also writes that persona file and its models
    entry (see write_snapshot). ValueError, KeyError or OpenWebUIError when it cannot be done."""
    version = defaults = None
    source = "export file"
    if records is None:
        if not model_id:
            raise ValueError("say which workspace model to import (model_id)")
        api = client(config, provider=provider)
        records = api.export(model_id)
        version, defaults, source = _quiet(api.version), api.default_params(), "api"
    profile = parse_openwebui_export(records, model_id, defaults=defaults)
    card = profile_card(profile, source=source, open_webui_version=version)
    path = save_profile(card)
    result = {"card": card, "path": str(path), "unavailable": card["unavailable"],
              "unknown_variables": card["system"]["unknown_variables"], "fingerprint": card["fingerprint"],
              "snapshot": None}
    if snapshot:
        result["snapshot"] = write_snapshot(config, profile, snapshot, on_saved=on_saved)
    return result


def persona_file_text(profile: ImportedProfile, name: str, *, model: str | None = None, wake: bool = False,
                      user_name: str | None = None) -> str:
    """The persona file of an imported profile, validated as settings.save_agent will (ValueError, nothing
    written): its soul is params.system, its frontmatter records the source (model, version_id, fingerprints,
    imported_at). With wake, the soul of a persona that wakes itself (Wake & Messages); otherwise an offline
    snapshot, a chat persona on the models entry model."""
    if not PERSONA_NAME_RE.fullmatch(name or "") or name in RESERVED_NAMES:
        raise ValueError(f"a persona's name must match {PERSONA_NAME_RE.pattern} (got {name!r})")
    if not profile.system.strip():
        raise ValueError(f"{profile.model_id!r} has no system prompt to copy")
    if not wake and not model:
        raise ValueError("an offline snapshot needs the models key it runs on")
    when = utc_now()
    description = (f"{profile.name}: Open WebUI's {profile.model_id!r}, imported snapshot of {when[:10]}, version "
                   f"{_short(profile.version_id)}" + ("" if wake else "; runs on the harness's own routing, "
                                                                      "not through Open WebUI"))
    lines = ["---", f"name: {name}", f"description: {_value(description)}", "tools: none"]
    if not wake:
        lines.append(f"model: {model}")
    lines += ["privacy: private", "source: openwebui", f"source_model: {_value(profile.model_id)}",
              f"source_version_id: {_value(profile.version_id or '')}",
              f"source_fingerprint: {profile.fingerprint}", f"source_behaviour: {profile.behaviour_fingerprint}",
              f"imported_at: {when}"]
    lines.append("wake: true" if wake else "offline: true")
    if user_name:
        lines.append(f"user_name: {_value(user_name)}")
    lines.append("---")
    text = "\n".join(lines) + "\n" + profile.system.strip() + "\n"
    if len(text.encode("utf-8")) > MAX_FILE_BYTES:
        raise ValueError(f"the persona file would be larger than {MAX_FILE_BYTES} bytes (the system prompt is "
                         f"{len(profile.system.encode('utf-8'))} bytes)")
    meta, body = parse_frontmatter(text, f"agents/{name}.md")
    if meta.get("name") != name or not body:
        raise ValueError(f"agents/{name}.md: the persona file does not parse back (name {meta.get('name')!r})")
    return text


def write_persona_file(config: Config, profile: ImportedProfile, name: str, *, model: str | None = None,
                       wake: bool = False, user_name: str | None = None) -> Path:
    """Write the persona file of an imported profile (persona_file_text) to the user agent dir (0600, privacy:
    private)."""
    return _save_persona_text(config, name, persona_file_text(profile, name, model=model, wake=wake,
                                                              user_name=user_name))


def _save_persona_text(config: Config, name: str, text: str) -> Path:
    from . import settings

    detail = settings.save_agent(config, name, text)
    path = Path(detail["path"])
    os.chmod(path, 0o600)
    return path


def write_snapshot(config: Config, profile: ImportedProfile, request: dict,
                   on_saved: Callable[[Any], None] | None = None) -> dict:
    """The explicit offline mode: models.<name> (the harness model the preset's base model maps to, with the
    preset's settings in extra_body) saved to the config, and the snapshot persona file <name>. request:
    {name, model?: the models key of the base model (asked for when the base model maps to none or several),
    user_name?}. Returns {persona, path, model, candidates}.

    name must be new both as an agent and as a models key, unless it is this model's own earlier snapshot (a
    snapshot persona of the same source_model whose model: is name): a re-import then replaces both. The
    persona file is built and validated first, the models entry is replaced whole (never merged with what was
    there), and when the persona file cannot be written the models entry is put back as it was."""
    from . import settings

    if not isinstance(request, dict):
        raise ValueError("snapshot must be an object like {\"name\": \"kate-offline\"}")
    name = request.get("name")
    if not isinstance(name, str) or not PERSONA_NAME_RE.fullmatch(name) or name in RESERVED_NAMES:
        raise ValueError(f"snapshot.name must match {PERSONA_NAME_RE.pattern} (got {name!r})")
    own = _is_snapshot_of(config, name, profile.model_id)
    if find_agent(config, name) is not None and not own:
        raise ValueError(f"an agent called {name!r} already exists: choose another name for the snapshot")
    models = _dict(config.raw.get("models"))
    if name in models and not (own and str((find_agent(config, name).meta or {}).get("model") or "") == name):
        entry = _dict(models.get(name))
        raise ValueError(f"models.{name} already exists (it runs {entry.get('provider') or '?'}/"
                         f"{entry.get('model') or '?'}, and agents routed to it would move onto the snapshot): choose "
                         f"another name for the snapshot")
    candidates = map_base_model(config, profile.base_model_id)
    base = request.get("model")
    if base is None:
        if len(candidates) != 1:
            raise ValueError(f"the base model {profile.base_model_id!r} maps to "
                             f"{'none' if not candidates else ', '.join(candidates)} of the harness's local models: "
                             f"say which one the snapshot runs on (snapshot.model)")
        base = candidates[0]
    entry = snapshot_model_entry(config, str(base), profile)
    text = persona_file_text(profile, name, model=name, user_name=request.get("user_name") or None)
    previous = copy.deepcopy(models.get(name)) if name in models else None
    saved = settings.update_config(config, {"models": {name: entry}}, on_saved=on_saved, replace=[("models", name)])
    try:
        path = _save_persona_text(saved, name, text)
    except BaseException:
        with contextlib.suppress(Exception):
            settings.update_config(saved, {"models": {name: previous}}, on_saved=on_saved, replace=[("models", name)])
        raise
    return {"persona": name, "path": str(path), "model": name, "base": base, "candidates": candidates}


def map_base_model(config: Config, base_model_id: str | None) -> list[str]:
    """The models keys on a local provider whose model id is the preset's base model, as Open WebUI names it
    or without its connection prefix ("<prefix>.<model>")."""
    from .settings import is_local_url

    if not base_model_id:
        return []
    wanted = {base_model_id, base_model_id.split(".", 1)[1] if "." in base_model_id else base_model_id}
    found = []
    models = config.raw.get("models")
    for key, entry in (models.items() if isinstance(models, dict) else []):
        if not isinstance(entry, dict) or entry.get("model") not in wanted:
            continue
        with contextlib.suppress(KeyError, ValueError):
            provider = config.provider(str(entry.get("provider")))
            if is_local_url(provider.base_url) and provider.kind != OPENWEBUI_KIND:
                found.append(str(key))
    return sorted(found)


def snapshot_model_entry(config: Config, base: str, profile: ImportedProfile) -> dict:
    """models.<snapshot>: the base entry with the preset's settings (effective_params) as extra_body, its
    max_tokens and its Stream Chat Response."""
    from .settings import is_local_url

    models = config.raw.get("models")
    entry = models.get(base) if isinstance(models, dict) else None
    if not isinstance(entry, dict):
        raise ValueError(f"snapshot.model {base!r} is not a key of models")
    provider = config.provider(str(entry.get("provider")))
    if not is_local_url(provider.base_url) or provider.kind == OPENWEBUI_KIND:
        raise ValueError(f"snapshot.model {base!r} runs on {provider.name} ({provider.base_url}): an offline snapshot "
                         f"runs on a local model of the harness, not through Open WebUI or an API")
    settings_ = dict(profile.effective)
    max_tokens = settings_.pop("max_tokens", None)
    settings_.pop("stream", None)
    out = copy.deepcopy(entry)
    out["extra_body"] = {**_dict(entry.get("extra_body")), **settings_}
    if isinstance(max_tokens, int) and not isinstance(max_tokens, bool) and max_tokens > 0:
        context = out.get("context_tokens")
        if not isinstance(context, int) or max_tokens < context:
            out["max_tokens"] = max_tokens
    if isinstance(profile.stream, bool):
        out["stream"] = profile.stream
    for key in ("builds", "overflow"):
        out.pop(key, None)   # a snapshot runs where its base runs now, never overflowing elsewhere
    return out


def find_agent(config: Config, name: str) -> AgentDef | None:
    with contextlib.suppress(ValueError):
        return discover_agents(persona_dirs(config)).get(name)
    return None


def _is_snapshot_of(config: Config, name: str, model_id: str) -> bool:
    agent = find_agent(config, name)
    persona = persona_of(agent) if agent is not None else None
    return persona is not None and persona.kind == "snapshot" and (persona.binding or {}).get("model") == model_id


# ---------------------------------------------------------------- fingerprints and drift


def canonical_json(value: Any) -> str:
    """Sorted keys, no spaces: the text the fingerprints hash."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_of(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def behaviour_fingerprint(record: dict, defaults: dict | None = None, attached_versions: dict | None = None) -> str:
    """sha256 over what changes how a preset behaves: the base model, the system prompt, the settings as Open
    WebUI merges them (effective_params, with defaults when known), Stream Chat Response, function calling,
    model controls, the behaviour meta (tools, skills, filters, default filters, actions, features,
    capabilities, built-in tools, terminal), the (type, id) of each knowledge entry, the global defaults and,
    when given, the version_id of each attached tool, function and skill. Name, description, images, tags and
    other cosmetic fields are left out."""
    params = record.get("params") if isinstance(record.get("params"), dict) else {}
    meta = record.get("meta") if isinstance(record.get("meta"), dict) else {}
    return sha256_of({
        "base_model_id": record.get("base_model_id"),
        "system": params.get("system") if isinstance(params.get("system"), str) else "",
        "effective": effective_params(params, defaults), "stream": params.get("stream_response"),
        "function_calling": params.get("function_calling"), "model_controls": params.get("model_controls"),
        "meta": {key: meta.get(key) for key in BEHAVIOUR_META},
        "knowledge": [[entry["type"], entry["id"]] for entry in _knowledge_refs(meta.get("knowledge"))],
        "defaults": defaults, "attached_versions": attached_versions,
    })


def live_fingerprint(info: dict) -> str:
    """sha256 over the behaviour a read-only key can see in GET /api/models' info: the base model, the model
    controls as the listing shows them (public_model_controls), the behaviour meta and the knowledge refs. It
    misses the system prompt and the settings, which /api/models never shows.

    Only two listings compare: the listing also carries the admin's Model Defaults metadata (merged into every
    listed model's meta, never into the export), so the same value computed from an export record (the
    profile card's) can differ from the listing's with nothing edited."""
    info = info if isinstance(info, dict) else {}
    params = info.get("params") if isinstance(info.get("params"), dict) else {}
    meta = info.get("meta") if isinstance(info.get("meta"), dict) else {}
    return sha256_of({
        "base_model_id": info.get("base_model_id"), "model_controls": public_model_controls(params.get("model_controls")),
        "meta": {key: meta.get(key) for key in BEHAVIOUR_META},
        "knowledge": [[entry["type"], entry["id"]] for entry in _knowledge_refs(meta.get("knowledge"))],
    })


def public_model_controls(controls: Any) -> dict | None:
    """Model controls as GET /api/models shows them (Open WebUI 0.12.3 utils/models.py public_model_params): each
    control's label, description, default and display, and only the label of each option. None when there are
    none. The same reduction of an already reduced listing changes nothing."""
    if not isinstance(controls, dict) or not controls:
        return None
    shown = {}
    for key, control in controls.items():
        control = _dict(control)
        options = _dict(control.get("options"))
        shown[key] = {**{name: control[name] for name in ("label", "description", "default", "display") if name in control},
                      "options": {name: {"label": _dict(option).get("label")} for name, option in options.items()}}
    return shown


def drift_dir(config: Config) -> Path:
    return Path(config.data_dir) / "personas" / "openwebui"


def drift_state(config: Config, model_id: str) -> dict | None:
    """The last observation of model_id: {model, provider, version_id, live_fingerprint, behaviour_fingerprint,
    defaults_known, seen_at, changed_at, kind, item, history: [{at, from, to, kind, item}]}, or None."""
    from .chats import read_json

    with contextlib.suppress(OSError, ValueError):
        state = read_json(drift_dir(config) / f"{slug(model_id)}.json")
        if state.get("model") == model_id:
            return state
    return None


def check_drift(config: Config, model_id: str, info: dict, *, name: str | None = None, provider: str | None = None,
                record: dict | None = None, defaults: dict | None = None, api: OpenWebUI | None = None,
                open_item: Callable[..., dict] | None = None, write: bool = True) -> dict:
    """Compare the live preset (info from GET /api/models; record, its export, when known) with the last one
    seen (the drift state, else the saved profile card) and record what is seen now.

    kind: "new" (first sight: the baseline), "none", "cosmetic" (version_id changed, the behaviour
    fingerprints did not), "behaviour" (the live fingerprint, or both behaviour fingerprints, changed) or
    "unknown" (version_id changed, and the prompt and settings cannot be compared: this key cannot export
    them). The live fingerprint is compared only with an earlier listing's (the drift state): a profile card's
    comes from the export, which lacks what the listing adds (the admin's Model Defaults metadata), so against
    a card only version_id and the behaviour fingerprint count. When the version changes and api is given, the
    export is tried once to tell cosmetic from behaviour. "behaviour" and "unknown" open one system-scope
    tracker item per model (open_item: Factory.system_item; without it the system store is written directly),
    kept while it is open. write False only compares (the doctor). Returns {model, version_id,
    previous_version_id, kind, changed, item, seen_at, changed_at}."""
    now = utc_now()
    live = {"version_id": info.get("version_id"), "live_fingerprint": live_fingerprint(info)}
    previous = drift_state(config, model_id)
    card = load_profile(model_id)
    baseline = previous or ({"version_id": card.get("version_id"), "live_fingerprint": None,
                             "behaviour_fingerprint": card.get("behaviour_fingerprint"),
                             "defaults_known": card.get("defaults_known", False), "item": None, "history": [],
                             "changed_at": None} if card else None)
    # A listing's live fingerprint, to compare with this one (None: the baseline is a card, or a state from
    # before the listing was recorded).
    listed_before = (baseline or {}).get("live_fingerprint")
    live_changed = listed_before is not None and listed_before != live["live_fingerprint"]
    changed = baseline is not None and (baseline.get("version_id") != live["version_id"] or live_changed)
    behaviour, defaults_known = None, defaults is not None
    # The export (an owner's key) tells cosmetic edits from behaviour: read it when the preset changed, and when
    # there is no behaviour baseline yet (first sight), so the next change can be told apart.
    if record is None and api is not None and (changed or not (baseline or {}).get("behaviour_fingerprint")):
        with contextlib.suppress(OpenWebUIError, ValueError):
            records = api.export(model_id)
            record = next((r for r in records if r.get("id") == model_id), None)
            defaults = api.default_params()
            defaults_known = defaults is not None
    comparable = None   # this preset's behaviour fingerprint computed as the baseline's was (with or without defaults)
    if record is not None:
        behaviour = behaviour_fingerprint(record, defaults)
        if bool((baseline or {}).get("defaults_known")) == defaults_known:
            comparable = behaviour
        elif not (baseline or {}).get("defaults_known"):
            comparable = behaviour_fingerprint(record, None)
    if baseline is None:
        kind = "new"
    elif not changed:
        kind = "none"
    elif live_changed:
        kind = "behaviour"
    elif comparable is not None and baseline.get("behaviour_fingerprint"):
        kind = "cosmetic" if comparable == baseline.get("behaviour_fingerprint") else "behaviour"
    else:
        kind = "unknown"
    item = (baseline or {}).get("item")
    if kind in ("behaviour", "unknown") and write:
        item = _open_drift_item(config, model_id, name or model_id, provider, baseline or {}, live, kind, open_item,
                                same_version=baseline.get("version_id") == live["version_id"])
    result = {"model": model_id, "version_id": live["version_id"],
              "previous_version_id": (baseline or {}).get("version_id"), "kind": kind, "changed": changed,
              "item": item, "seen_at": now,
              "changed_at": now if changed else (baseline or {}).get("changed_at")}
    if write:
        history = list((baseline or {}).get("history") or [])
        if changed:
            history.append({"at": now, "from": (baseline or {}).get("version_id"), "to": live["version_id"],
                            "kind": kind, "item": item})
        state = {"model": model_id, "provider": provider, "version_id": live["version_id"],
                 "live_fingerprint": live["live_fingerprint"],
                 "behaviour_fingerprint": behaviour if behaviour is not None else (
                     None if changed else (baseline or {}).get("behaviour_fingerprint")),
                 "defaults_known": defaults_known if behaviour is not None else bool((baseline or {}).get("defaults_known")),
                 "seen_at": now, "changed_at": result["changed_at"], "kind": kind, "item": item,
                 "history": history[-DRIFT_HISTORY_KEPT:]}
        from .chats import write_json

        _private_dir(drift_dir(config))
        path = drift_dir(config) / f"{slug(model_id)}.json"
        write_json(path, state)
        os.chmod(path, 0o600)
    return result


def _open_drift_item(config: Config, model_id: str, title: str, provider: str | None, baseline: dict, live: dict,
                     kind: str, open_item: Callable[..., dict] | None, same_version: bool = False) -> str | None:
    words = {"behaviour": "what it does changed (its tools, filters, knowledge, base model or settings)",
             "unknown": "this key cannot read the prompt and settings, so it may be only a cosmetic edit"}[kind]
    version = (f"version {_short(live['version_id'])} is unchanged, but Open WebUI now lists its tools, filters, "
               f"knowledge, capabilities or base model differently (the admin's Model Defaults, for example)"
               if same_version else
               f"version {_short(baseline.get('version_id'))} → {_short(live['version_id'])}; {words}")
    fields = {"title": f"{title} changed in Open WebUI",
              "detail": (f"Open WebUI's workspace model {model_id!r}{f' on provider {provider}' if provider else ''} "
                         f"changed: {version}. Look at it in Open WebUI, import its profile card again, and re-import "
                         f"any offline snapshot of it."),
              "kind": "commitment",
              "source": {"type": "drift", "model": model_id, "provider": provider, "from": baseline.get("version_id"),
                         "to": live["version_id"], "change": kind}}
    dedupe = f"openwebui:{model_id}"[:200]
    try:
        if open_item is not None:
            return open_item(fields, dedupe=dedupe).get("ref")
        store = tracker.system_store(config.data_dir)
        with store.transaction(secrets=config.secret_values()) as ledger:
            item, _ = tracker.open_once(ledger, fields, dedupe)
        return store.scope.item_ref(item["id"])
    except (OSError, ValueError, tracker.ItemsBusy):
        return baseline.get("item")


def _drift_summary(result: dict) -> dict:
    return {key: result.get(key) for key in ("kind", "changed", "previous_version_id", "item", "changed_at")}


# ---------------------------------------------------------------- the views the API serves


def persona_view(config: Config, persona: Persona) -> dict:
    """GET /api/personas row: the persona plus its label, model key, profile card presence and drift state."""
    row = persona.to_dict()
    row["label"] = label(persona)
    row["model"] = chat_model_key(config, persona) if persona.kind != "wake" else None
    model_id = (persona.binding or {}).get("model")
    row["card"] = load_profile(model_id) is not None if model_id else False
    state = drift_state(config, model_id) if model_id else None
    row["drift"] = {key: state.get(key) for key in ("kind", "version_id", "seen_at", "changed_at", "item")} \
        if state else None
    if persona.kind == "openwebui":
        row["provider"] = binding_provider(config, persona)
    return row


def live_card(config: Config, persona: Persona) -> dict:
    """What a read key sees now (GET /api/models), for the profile card page: {listed, name, preset, version_id,
    base_model_id, base_model: {id, listed, owned_by, connection_type} | None, warnings, parts,
    live_fingerprint, open_webui_version}, {listed: false, open_webui_version} or {error}."""
    model_id = (persona.binding or {}).get("model")
    try:
        api = client(config, persona)
        entries = api.models()
        version = _quiet(api.version)
    except (KeyError, OpenWebUIError) as exc:
        return {"error": str(exc.args[0] if exc.args else exc)}
    entry = listed(entries, model_id)
    if entry is None:
        return {"listed": False, "open_webui_version": version}
    info = entry.get("info") if isinstance(entry.get("info"), dict) else {}
    base = base_model_view(entries, entry)
    binding = persona.binding or {}
    return {"listed": True, "name": entry.get("name"), "preset": bool(entry.get("preset")),
            "version_id": info.get("version_id"), "base_model_id": info.get("base_model_id"), "base_model": base,
            "warnings": base_model_warnings(base, str(entry.get("name") or model_id)),
            "parts": parts(info.get("meta") or {}, binding.get("knowledge", False), binding.get("filters", True))
            if persona.kind == "openwebui" else parts(info.get("meta") or {}),
            "live_fingerprint": live_fingerprint(info), "open_webui_version": version}


def workspace_models(config: Config, provider: str | None = None) -> dict:
    """GET /api/openwebui/models: {provider, open_webui_version, models: [{id, name, preset, version_id,
    base_model_id, personas: [names bound to it], card}]} (workspace models first)."""
    api = client(config, provider=provider)
    entries = api.models()
    bound: dict[str, list[str]] = {}
    with contextlib.suppress(ValueError):
        for persona in discover_personas(config).values():
            model_id = (persona.binding or {}).get("model")
            if model_id:
                bound.setdefault(str(model_id), []).append(persona.name)
    rows = []
    for entry in entries:
        info = entry.get("info") if isinstance(entry.get("info"), dict) else {}
        rows.append({"id": entry["id"], "name": entry.get("name") or entry["id"], "preset": bool(entry.get("preset")),
                     "version_id": info.get("version_id"), "base_model_id": info.get("base_model_id"),
                     "personas": sorted(bound.get(entry["id"], [])), "card": load_profile(entry["id"]) is not None})
    rows.sort(key=lambda row: (not row["preset"], row["id"]))
    return {"provider": api.provider.name, "open_webui_version": _quiet(api.version), "models": rows}


# ---------------------------------------------------------------- helpers


def slug(model_id: str) -> str:
    """A file name for a model id: the id when it is plain, else its safe characters plus a short hash."""
    text = str(model_id)
    safe = _SLUG_RE.sub("_", text).strip("._")[:60] or "model"
    return safe if safe == text else f"{safe}-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:8]}"


def _flag(value: Any, default: bool, malformed: bool | None = None) -> bool:
    """A frontmatter flag; a value that is neither true nor false is malformed (default unless given)."""
    try:
        return parse_bool(value if isinstance(value, str) else None, default)
    except ValueError:
        return default if malformed is None else malformed


def _zone(name: str) -> Any:
    try:
        from zoneinfo import ZoneInfo
    except ImportError:   # pragma: no cover - Python 3.9+ has it
        return None
    try:
        return ZoneInfo(name)
    except (ValueError, OSError, KeyError):
        return None
    except Exception:   # noqa: BLE001 - ZoneInfoNotFoundError and friends
        return None


def _quiet(call: Callable[[], Any]) -> Any:
    with contextlib.suppress(OpenWebUIError):
        return call()
    return None


def _knowledge(value: Any) -> list[dict]:
    """[{type, id, name}] of meta.knowledge (legacy entries with collection_name(s) count as collections)."""
    found = []
    for entry in value if isinstance(value, list) else []:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            continue
        kind = entry.get("type") if isinstance(entry.get("type"), str) else "collection"
        found.append({"type": kind, "id": entry["id"], "name": str(entry.get("name") or entry["id"])})
    return found


def _knowledge_refs(value: Any) -> list[dict]:
    return [{"type": entry["type"], "id": entry["id"]} for entry in _knowledge(value)]


def _unavailable(meta: dict, knowledge: list[dict]) -> dict[str, list[str]]:
    """What an imported copy (a snapshot, a wake soul) cannot have: it runs without Open WebUI."""
    return {"tools": _str_list(meta.get("toolIds")), "knowledge": [entry["name"] for entry in knowledge],
            "filters": sorted(set(_str_list(meta.get("filterIds")) + _str_list(meta.get("defaultFilterIds")))),
            "skills": _str_list(meta.get("skillIds")), "actions": _str_list(meta.get("actionIds")),
            "features": _str_list(meta.get("defaultFeatureIds"))}


def _str_list(value: Any) -> list[str]:
    return [str(item) for item in value if isinstance(item, str) and item] if isinstance(value, list) else []


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _json_value(value: Any) -> Any:
    if isinstance(value, str):
        with contextlib.suppress(ValueError):
            return json.loads(value)
    return value


def _deep(base: Any, value: Any) -> Any:
    if isinstance(base, dict) and isinstance(value, dict):
        merged = dict(base)
        for key, item in value.items():
            merged[key] = _deep(merged.get(key), item)
        return merged
    return copy.deepcopy(value)


def _short(version: Any) -> str:
    return str(version)[:12] if version else "unknown"


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def _value(text: str) -> str:
    """One frontmatter value on one line, quoted when the parser would otherwise cut or unquote it."""
    line = _one_line(text)
    return f'"{line}"' if " #" in line or line[:1] in ("'", '"') else line


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)
