"""The browsing preset check (BROWSER_AND_CODEX.md §2.10 rule 7, Kate's K13: no writer and no transmitter).

Before every browsing turn of an Open WebUI persona, and in the doctor's ``browser preset`` line, the harness reads
the preset's export (``GET /api/v1/models/export``), the global function list (``GET /api/v1/functions/``) and the
Pipelines list (``GET /api/v1/pipelines/list``) and refuses to browse when any of these is present: a tool; a filter
(on the preset, global or always-on); an action (on the preset or global); a skill; knowledge; memory; a built-in
tool or feature switched on; a terminal; a custom ``tools`` parameter; a base connection that is not on this machine;
a connected Pipelines server. Its reason names what it found. Where a list cannot be read with the key the harness
has, the turn is refused unless the operator recorded that she checked it herself (``browser.preset_checked_by_hand``).
A browsing agent that is not an Open WebUI persona may not reach Open WebUI at all (``swfactory/chats.py``).

The harness changes nothing in Open WebUI: every request here is a GET.
"""

from __future__ import annotations

from typing import Any

LISTS = ("export", "functions", "pipelines")


def _list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if item not in (None, "", False)]
    if isinstance(value, str) and value:
        return [value]
    return []


def _on(value: Any) -> list[str]:
    """The names switched on in a {name: bool} map, or the entries of a list."""
    if isinstance(value, dict):
        return sorted(str(key) for key, on in value.items() if on)
    return _list(value)


def export_findings(record: dict) -> list[str]:
    """What a preset's export record carries that §2.10 rule 7 refuses, in words that name each part."""
    meta = record.get("meta") if isinstance(record.get("meta"), dict) else {}
    params = record.get("params") if isinstance(record.get("params"), dict) else {}
    found: list[str] = []
    for key, words in (("toolIds", "a tool"), ("filterIds", "a filter on the preset"),
                       ("defaultFilterIds", "an always-on filter on the preset"), ("actionIds", "an action"),
                       ("skillIds", "a skill")):
        ids = _list(meta.get(key))
        if ids:
            found.append(f"{words} ({', '.join(ids)})")
    knowledge = meta.get("knowledge")
    if isinstance(knowledge, list) and knowledge:
        names = [str(item.get("name") or item.get("id") or "?") if isinstance(item, dict) else str(item)
                 for item in knowledge]
        found.append(f"knowledge ({', '.join(names)})")
    capabilities = meta.get("capabilities") if isinstance(meta.get("capabilities"), dict) else {}
    features = _list(meta.get("defaultFeatureIds"))
    builtin = _on(meta.get("builtinTools"))
    if capabilities.get("memory") or "memory" in features or "memory" in builtin or params.get("memory"):
        found.append("memory switched on")
    others = [name for name in builtin if name != "memory"]
    if others:
        found.append(f"a built-in tool switched on ({', '.join(others)})")
    features = [name for name in features if name != "memory"]
    if features:
        found.append(f"a built-in feature switched on ({', '.join(features)})")
    if meta.get("terminalId"):
        found.append(f"a terminal ({meta['terminalId']})")
    custom = params.get("custom_params") if isinstance(params.get("custom_params"), dict) else {}
    if "tools" in custom or "tools" in params or "tool_ids" in custom:
        found.append("a custom tools parameter")
    return found


def function_findings(functions: list) -> list[str]:
    """Global filters and actions in Open WebUI's function list (GET /api/v1/functions/): Open WebUI attaches an
    active global one to every model, the browsing preset included, so the preset carries it (K13)."""
    found = []
    for entry in functions:
        if not isinstance(entry, dict) or entry.get("type") not in ("filter", "action") \
                or entry.get("is_active") is False:
            continue
        if entry.get("is_global"):
            found.append(f"a global {entry['type']} ({entry.get('id') or entry.get('name') or '?'})")
    return found


def pipeline_findings(pipelines: Any) -> list[str]:
    """A connected Pipelines server runs its filters on every request: any entry is refused."""
    data = pipelines.get("data") if isinstance(pipelines, dict) else pipelines
    if isinstance(data, list) and data:
        return [f"a Pipelines server ({len(data)} connected)"]
    return []


def connection_findings(base: dict | None, local_ids: set[str] | None) -> list[str]:
    """A base connection that is not on this machine (DESIGN §33's base model rule)."""
    from ..personas import base_model_warnings

    if base is None:
        return ["no base model the harness can see"]
    return ["a base connection that is not on this machine"] if base_model_warnings(base, "the preset",
                                                                                     local_ids) else []


def check(api: Any, model_id: str, *, local_ids: set[str] | None = None, checked_by_hand: dict | None = None,
          entries: list[dict] | None = None) -> dict:
    """{ok, findings, unread, notes} for the preset model_id through an OpenWebUI client: ok only when nothing was
    found and every list was read (or the operator checked the unread one herself)."""
    from ..personas import OpenWebUIError, base_model_view, listed

    findings: list[str] = []
    unread: list[str] = []
    notes: list[str] = []
    attested = checked_by_hand if isinstance(checked_by_hand, dict) else {}
    try:
        records = api.export(model_id)
        record = next((r for r in records if r.get("id") == model_id), None)
        if record is None:
            raise OpenWebUIError(f"the export has no {model_id!r}")
        findings += export_findings(record)
        params = record.get("params") if isinstance(record.get("params"), dict) else {}
        if params.get("function_calling") == "legacy":
            notes.append("function calling is legacy on the preset: the harness's tools pass through unchanged "
                         "(read in source, not run)")
    except (OpenWebUIError, KeyError) as exc:
        unread.append(f"export ({exc.args[0] if exc.args else exc})")
    for name, path, reader in (("functions", "/v1/functions/", function_findings),
                               ("pipelines", "/v1/pipelines/list", pipeline_findings)):
        try:
            status, payload = api.get(path)
        except OpenWebUIError as exc:
            status, payload = None, str(exc)
        if status is not None and status < 400 and payload is not None:
            findings += reader(payload if isinstance(payload, (list, dict)) else [])
        elif status == 404 and name == "pipelines":
            continue   # no Pipelines support at all: nothing runs from there
        else:
            unread.append(f"{name} (HTTP {status})" if status is not None else name)
    try:
        listing = entries if entries is not None else api.models()
        entry = listed(listing, model_id)
        findings += connection_findings(base_model_view(listing, entry) if entry else None, local_ids)
    except (OpenWebUIError, KeyError) as exc:
        unread.append(f"models ({exc.args[0] if exc.args else exc})")
    missing = [item for item in unread if not attested.get(item.split(" ", 1)[0])]
    by_hand = [item.split(" ", 1)[0] for item in unread if attested.get(item.split(" ", 1)[0])]
    if by_hand:
        notes.append(f"checked by hand by the operator: {', '.join(by_hand)}")
    return {"ok": not findings and not missing, "findings": findings, "unread": missing, "notes": notes}


def reason(result: dict, model_id: str) -> str:
    """The refusal's words."""
    parts = []
    if result["findings"]:
        parts.append("the browsing preset " + repr(model_id) + " carries " + "; ".join(result["findings"]))
    if result["unread"]:
        parts.append("could not read " + ", ".join(result["unread"]) + " (record that you checked it yourself in "
                     "browser.preset_checked_by_hand, or use a key that may read it)")
    return "not browsing: " + "; and ".join(parts) + " (BROWSER_AND_CODEX.md §2.10 rule 7: no writer, no transmitter)"
