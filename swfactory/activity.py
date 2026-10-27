"""What did an agent or persona do since X (DESIGN.md section 36): one time-ordered list from durable files.

``activity(config, agent=..., since=..., until=...)`` merges, oldest first:

- ``receipt``: the change receipts an operator wrote (receipts.py), as the harness's index holds them: those whose
  actor is the agent (or that are attributed to it as the operator), and for an operator the files that could
  not be read; always labelled "reported by the operator"; one entry per receipt id (a second file with the
  same id, and where a moved receipt was before, are named in its evidence.also);
- ``tracker``: every tracker transition the agent made (``by`` is the name, ``persona:<name>`` or
  ``agent:<name>``) in every scope's ``*.log.jsonl``: jobs, follow-ups, chats, plans and the system scope;
- ``audit``: the messages and wakes audit lines (``<data_dir>/wakes/audit-YYYY-MM.jsonl``) that name it as
  persona, agent or by;
- ``run``: the starts and ends of its runs, from the agent.start and agent.end events of the run logs in job,
  plan and scout folders and in chats it ran (``logs/NN-<agent>.jsonl``, the whole name after the number:
  reviewer-code's runs are never code's; ``chats/<id>/logs/NN-turn.jsonl``), and a wake persona's ended wakes
  (``wakes/history-YYYY-MM.jsonl``);
- ``custody``: what the harness asked of the GPU custody register (DESIGN §37) and what it answered, from the
  custody journal (``custody/journal-YYYY-MM.jsonl``): for an operator every line (the register is hers), for
  any agent the lines whose lease purpose names it (``job <repo>/<id> <agent>``, ``wake <persona> <id>``);
- ``drift``: for an operator, the drift checks worth a record (DESIGN §38: a status that changed, an item opened,
  reopened or closed, a role manifest written), from ``drift-sources/<name>/checks-YYYY-MM.jsonl``;
- ``snapraid``: for an operator, the disk protection panel's journal (DESIGN §41: guard reports, health findings
  opened or closed, runs outside Seraphina), from ``snapraid/journal-YYYY-MM.jsonl``: counts, codes and refs only.

Each entry is ``{at, source, kind, agent, summary, evidence: {file, line?, ...}}``: where it was read, so a person
(or the operator after her own memory was compacted) can open the file and see it. Only files are read; nothing is
written. Text is redacted of the run's secrets and cut short; the full record stays in its file. A cut list
continues with ``after=next_after``, an opaque cursor naming the last entry shown (time, source, file, line), so
paging moves on even when more entries than ``limit`` share one second.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator

from . import items as tracker
from .events import redact
from .operator import REPORTED, is_operator
from .receipts import iso_moment

if TYPE_CHECKING:
    from .config import Config
    from .receipts import Receipts

# new sources go last (a cursor names one)
SOURCES = ("receipt", "tracker", "audit", "run", "custody", "drift", "snapraid")
DEFAULT_LIMIT = 500
MAX_LIMIT = 5000
SUMMARY_CHARS = 300
RELATIVE_RE = re.compile(r"^(\d{1,5})\s*(m|min|h|d|w)$", re.IGNORECASE)
AGENT_RE = re.compile(r"^(?:(?:agent|persona):)?[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_UNITS = {"m": 60, "min": 60, "h": 3600, "d": 86400, "w": 7 * 86400}
# the typed fields an audit line's summary names (never free text: the audit holds none)
_AUDIT_FIELDS = ("persona", "wake", "run", "action", "decision", "state", "channel", "message", "verb", "outcome",
                 "what", "why", "by", "via", "version", "chat", "allow", "provider", "local", "model", "reminder",
                 "event", "subject", "source", "hook")


def parse_when(value: Any, *, now: datetime | None = None) -> str | None:
    """A time for since or until as UTC ISO-8601 with Z: an ISO time or date (a date is its midnight UTC; a time
    without a zone is UTC) or a span back from now (30m, 24h, 7d, 2w). None for empty; ValueError otherwise."""
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip()
    now = now or datetime.now(timezone.utc)
    match = RELATIVE_RE.fullmatch(text)
    if match:
        moment = now - timedelta(seconds=int(match.group(1)) * _UNITS[match.group(2).lower()])
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    moment = iso_moment(text)   # the same forms on every supported Python (receipts.ISO_RE)
    if moment is None:
        raise ValueError(f"{text[:40]!r} is not a time: use an ISO time such as 2026-10-22T08:00:00Z, a date, "
                         "or a span such as 24h or 7d")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm_at(value: Any) -> str | None:
    """An entry's own time as UTC ISO-8601 with Z (None when it has none or it does not parse)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return parse_when(value)
    except ValueError:
        return None


def _names(config: Config, agent: str) -> tuple[str, set[str], set[str]]:
    """(bare name, the tracker by values, the lower-case names an audit line may carry) for agent."""
    bare = agent.split(":", 1)[1] if agent.startswith(("agent:", "persona:")) else agent
    by = {bare, f"persona:{bare}", f"agent:{bare}"}
    lowered = {bare.lower(), f"persona:{bare}".lower(), f"agent:{bare}".lower()}
    with contextlib.suppress(Exception):
        from .personas import discover_personas

        persona = discover_personas(config).get(bare)
        if persona is not None and persona.instance_name:
            lowered.add(persona.instance_name.lower())
    with contextlib.suppress(Exception):
        wakes = config.raw.get("wakes") if isinstance(config.raw.get("wakes"), dict) else {}
        cfg = (wakes.get("personas") or {}).get(bare) or {}
        if isinstance(cfg, dict) and isinstance(cfg.get("instance_name"), str):
            lowered.add(cfg["instance_name"].lower())
    return bare, by, lowered


def _lines(path: Path, needle: str | None = None) -> Iterator[tuple[int, dict]]:
    """(line number, object) of each JSON line of path that parses (only lines holding needle, when given)."""
    try:
        handle = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return
    with handle:
        for number, raw in enumerate(handle, 1):
            if needle is not None and needle not in raw:
                continue
            with contextlib.suppress(ValueError):
                line = json.loads(raw)
                if isinstance(line, dict):
                    yield number, line


def _modified_since(path: Path, since: str | None) -> bool:
    """Whether an append-only file may hold a line at or after since (its mtime is not older)."""
    if since is None:
        return True
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except OSError:
        return False
    return mtime >= since


def _within(at: str | None, since: str | None, until: str | None) -> bool:
    return at is not None and (since is None or at >= since) and (until is None or at <= until)


def _clip(text: Any) -> str:
    return tracker.clip(text, SUMMARY_CHARS)


# ---------------------------------------------------------------- the sources


def _one_per_receipt(files: dict) -> list[tuple[dict, list[dict]]]:
    """[(entry, the other files of the same receipt)]: a receipt is listed once, from the file it is in now (or,
    when every copy went away, the one that went last); a second file with its id (duplicate_of) and the place it
    was moved from (gone, the same id) are named beside it. Files that cannot be read are listed as they are."""
    groups: dict[str, list[dict]] = {}
    out: list[tuple[dict, list[dict]]] = []
    for rel in sorted(files):
        entry = files[rel]
        if entry.get("status") == "read" and entry.get("id"):
            groups.setdefault(str(entry["id"]), []).append(entry)
        else:
            out.append((entry, []))
    for entries in groups.values():
        live = [e for e in entries if not e.get("gone") and not e.get("duplicate_of")]
        primary = live[0] if live else max(entries, key=lambda e: (str(e.get("gone_at") or ""), str(e.get("file"))))
        out.append((primary, [e for e in entries if e is not primary]))
    return out


def _receipt_entries(receipts: Receipts | None, bare: str, lowered: set[str], operator: bool,
                     since: str | None, until: str | None) -> list[dict]:
    if receipts is None:
        return []
    index = receipts.index()
    directory = index.get("receipts_dir") or ""

    def where(entry: dict) -> str:
        return os.path.join(directory, str(entry.get("file"))) if directory else str(entry.get("file"))

    out = []
    for entry, others in _one_per_receipt(index["files"]):
        read = entry.get("status") == "read"
        # hers when its actor names her: the one-operator fallback (operator without operator_named) decides who
        # owns another agent's receipt items, never that she made that change
        mine = read and ((entry.get("operator") == bare and entry.get("operator_named"))
                         or str(entry.get("actor") or "").lower() in lowered)
        if not (mine or (operator and not read)):
            continue
        at = entry.get("time") if read else entry.get("mtime")
        if not _within(at, since, until):
            continue
        view = receipts.view(entry, full=False)
        evidence = {"file": where(entry), "sha256": entry.get("sha256"), "first_read_at": entry.get("first_read_at")}
        if entry.get("gone"):
            evidence["gone_at"] = entry.get("gone_at")   # the file went away; the harness's index still has it
        if others:   # the same receipt in other files: a duplicate id, or where it was before it was moved
            evidence["also"] = [{"file": where(e), "sha256": e.get("sha256"),
                                 "why": "the same id again" if not e.get("gone") else
                                 "was here (gone)" if e.get("sha256") == entry.get("sha256") else
                                 "an earlier version was here (gone)"} for e in others]
        if read:
            out.append({"at": at, "source": "receipt", "kind": "change", "agent": bare,
                        "summary": _clip(f"{entry.get('id')}: {entry.get('intent')}"), "label": REPORTED,
                        "receipt": entry.get("id"), "undo": view.get("undo"), "outcome": view.get("outcome"),
                        "not_verified": view.get("not_verified"), "verified": view.get("verified"),
                        "items": view.get("items") or [], "warnings": view.get("warnings") or [],
                        **({"reported_outcome": view["reported_outcome"]} if view.get("reported_outcome") else {}),
                        "evidence": {**evidence, "receipt": entry.get("id")}})
        else:
            out.append({"at": at, "source": "receipt", "kind": "cannot read", "agent": bare,
                        "summary": _clip(f"{entry.get('file')}: cannot read: {entry.get('problem')}"),
                        "label": REPORTED, "evidence": evidence})
    return out


def _tracker_files(data_dir: Path) -> Iterator[Path]:
    yield data_dir / tracker.SYSTEM_DIR / f"{tracker.SYSTEM_NAME}.log.jsonl"
    jobs = data_dir / "jobs"
    if jobs.is_dir():
        yield from sorted(jobs.glob("*/followups.log.jsonl"))
        yield from sorted(jobs.glob("*/*/items.log.jsonl"))
    for pattern in ("chats/*/items.log.jsonl", "plans/*/*/items.log.jsonl"):
        yield from sorted(data_dir.glob(pattern))


def _tracker_entries(data_dir: Path, bare: str, by: set[str], since: str | None, until: str | None) -> list[dict]:
    out = []
    for path in _tracker_files(data_dir):
        if not path.is_file() or not _modified_since(path, since):
            continue
        for number, line in _lines(path, '"by"'):
            if line.get("by") not in by:
                continue
            at = _norm_at(line.get("at"))
            if not _within(at, since, until):
                continue
            ref = str(line.get("ref") or "")
            frm, to = line.get("from"), line.get("to")
            what = f"{ref}: {frm or 'new'} → {to}" if frm != to else f"{ref}: {to} (touched)"
            note = f" ({line.get('note')})" if line.get("note") else ""
            entry = {"at": at, "source": "tracker", "kind": "item", "agent": bare, "summary": _clip(what + note),
                     "ref": ref, "from": frm, "to": to, "evidence": {"file": str(path), "line": number}}
            if line.get("evidence"):
                entry["item_evidence"] = line["evidence"]
            out.append(entry)
    return out


def _month_files(root: Path, prefix: str, since: str | None) -> list[Path]:
    if not root.is_dir():
        return []
    files = sorted(root.glob(f"{prefix}-*.jsonl"))
    if since is None:
        return files
    month = since[:7]
    return [path for path in files if path.stem[len(prefix) + 1:] >= month]


def _audit_entries(data_dir: Path, bare: str, by: set[str], lowered: set[str], since: str | None,
                   until: str | None) -> list[dict]:
    out = []
    for path in _month_files(data_dir / "wakes", "audit", since):
        for number, line in _lines(path):
            named = line.get("persona") == bare or str(line.get("agent") or "").lower() in lowered \
                or line.get("by") in by
            if not named:
                continue
            at = _norm_at(line.get("at"))
            if not _within(at, since, until):
                continue
            kind = str(line.get("kind") or "audit")
            fields = [f"{key} {line[key]}" for key in _AUDIT_FIELDS
                      if key in line and isinstance(line[key], (str, int, float, bool)) and line[key] != ""]
            out.append({"at": at, "source": "audit", "kind": kind, "agent": bare,
                        "summary": _clip(f"{kind}" + (f": {', '.join(fields)}" if fields else "")),
                        "evidence": {"file": str(path), "line": number}})
    return out


def _run_log_files(data_dir: Path, bare: str) -> Iterator[tuple[Path, dict]]:
    """Run logs of the agent: job, scout and plan folders name the agent in the file (NN-<agent>.jsonl, the whole
    name: reviewer-code's runs are never code's); a chat's turns (NN-turn.jsonl) count when the chat's agent is it."""
    stem = re.compile(r"\d+-" + re.escape(bare))
    jobs = data_dir / "jobs"
    if jobs.is_dir():
        for path in sorted(jobs.glob(f"*/*/logs/*-{bare}.jsonl")):
            if stem.fullmatch(path.stem):
                folder = path.parent.parent
                yield path, {"repo": folder.parent.name, "job": folder.name}
    plans = data_dir / "plans"
    if plans.is_dir():
        for path in sorted(plans.glob(f"*/*/logs/*-{bare}.jsonl")):
            if stem.fullmatch(path.stem):
                folder = path.parent.parent
                yield path, {"repo": folder.parent.name, "plan": folder.name}
    chats = data_dir / "chats"
    if chats.is_dir():
        for chat_file in sorted(chats.glob("*/chat.json")):
            try:
                chat = json.loads(chat_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(chat, dict) or chat.get("agent") != bare:
                continue
            for path in sorted((chat_file.parent / "logs").glob("*-turn.jsonl")):
                yield path, {"chat": chat_file.parent.name}


def _run_entries(data_dir: Path, bare: str, since: str | None, until: str | None) -> list[dict]:
    out = []
    for path, where in _run_log_files(data_dir, bare):
        if not _modified_since(path, since):
            continue
        for number, line in _lines(path, '"agent.'):
            kind = line.get("type")
            if kind not in ("agent.start", "agent.end"):
                continue
            data = line.get("data") if isinstance(line.get("data"), dict) else {}
            if line.get("agent") not in (bare, None) or (line.get("agent") is None
                                                          and data.get("agent") not in (bare, None)):
                continue   # a line another agent wrote
            at = _norm_at(line.get("ts"))
            if not _within(at, since, until):
                continue
            place = "/".join(str(where[key]) for key in ("repo", "job", "plan", "chat") if where.get(key))
            if kind == "agent.start":
                summary = f"run {path.stem} started in {place} on {data.get('model') or data.get('model_id') or '?'}"
                entry_kind = "run started"
            else:
                state = "ok" if data.get("ok") else f"failed: {data.get('error')}" if data.get("error") else "failed"
                summary = f"run {path.stem} ended in {place}: {state}"
                entry_kind = "run ended"
            out.append({"at": at, "source": "run", "kind": entry_kind, "agent": bare, "summary": _clip(summary),
                        "run": path.stem, **where, "evidence": {"file": str(path), "line": number}})
    for path in _month_files(data_dir / "wakes", "history", since):
        for number, line in _lines(path, '"persona"'):
            if line.get("persona") != bare:
                continue
            at = _norm_at(line.get("ended_at"))
            if not _within(at, since, until):
                continue
            actions = ", ".join(str(a) for a in line.get("actions") or [] if isinstance(a, str))
            summary = f"wake {line.get('id')} ({line.get('source')}) {line.get('state')}" + \
                (f": {actions}" if actions else "")
            out.append({"at": at, "source": "run", "kind": "wake ended", "agent": bare, "summary": _clip(summary),
                        "wake": line.get("id"), "run": line.get("run"),
                        "evidence": {"file": str(path), "line": number}})
    return out


def _purpose_names(purpose: Any, bare: str) -> bool:
    """Whether a custody lease's purpose names the agent: the agent of a job's run (``job <repo>/<id> <agent>``)
    or the persona of a wake turn (``wake <persona> <wake id>``)."""
    words = str(purpose or "").split()
    if len(words) == 3 and words[0] == "job":
        return words[2] == bare
    return len(words) >= 2 and words[0] == "wake" and words[1] == bare


def _custody_summary(line: dict) -> tuple[str, str]:
    """(kind, summary) of one custody journal line, from its typed fields."""
    op, outcome = str(line.get("op") or "?"), str(line.get("outcome") or "?")
    what = f"{line.get('model')} on {line.get('provider')}" if line.get("model") else ""
    purpose = f" for {line.get('purpose')}" if line.get("purpose") else ""
    reason = f": {line.get('reason')}" if line.get("reason") else ""
    words = f" ({line.get('words')})" if line.get("words") else ""
    if outcome == "requested":   # a request journaled before it went out, with no answer after it (DESIGN §40)
        target = what or f"lease {line.get('lease_id')}"
        return f"{op} outcome unknown", (f"{op} requested with no answer recorded: {target}{purpose} (the harness "
                                         f"stopped while it was out, or it is out now); check the register")
    if op == "claim":
        kind = {"granted": "lease granted", "denied": "lease denied", "unknown": "lease outcome unknown",
                "stale": "lease stale"}.get(outcome, f"lease {outcome}")
        tail = (f" ({line.get('lease_id')}, generation {line.get('generation')}, until {line.get('until')})"
                if outcome == "granted" else reason + words)
        return kind, f"{kind}: {what}{purpose}{tail}"
    if op == "wait":   # a placement waiting out a HELD answer (someone else holds the card)
        seconds = line.get("seconds")
        kind = {"waiting": "lease wait", "granted": "lease wait ended", "gave_up": "lease wait gave up",
                "cancelled": "lease wait cancelled", "ended": "lease wait stopped"}.get(outcome,
                                                                                       f"lease wait {outcome}")
        summary = {"waiting": f"waits up to {seconds}s for {what}{purpose}: the card is held by someone else",
                   "granted": f"the hold on {what} ended after {seconds}s: lease granted{purpose}",
                   "gave_up": f"gave up waiting for {what}{purpose} after {seconds}s: still held",
                   "cancelled": f"stopped waiting for {what}{purpose}: the run was cancelled",
                   "ended": f"stopped waiting for {what}{purpose}{reason}"}.get(outcome, f"wait {outcome}{reason}")
        return kind, summary
    if op == "release":
        after = line.get("resident_after")
        resident = (", ".join(str(pair[1]) for pair in after if isinstance(pair, list) and len(pair) > 1)
                    or "nothing") if isinstance(after, list) else None
        return f"lease {outcome}", (f"lease {line.get('lease_id')} {outcome}: {what}{reason}"
                                    + (f"; resident afterwards: {resident}" if resident is not None
                                       else "; residency could not be read afterwards"))
    if op == "renew":   # held: the register could not be reached, the lease stays held until its until
        kind = "renewal failed" if outcome == "held" else f"renewal {outcome}"
        return kind, f"lease {line.get('lease_id')} not renewed ({outcome}){reason}"
    if op == "epoch":
        return "register restarted", f"the register restarted{reason}"
    if op == "call":
        return "call outcome unknown", f"a call under lease {line.get('lease_id')} was cut off{reason}"
    return f"{op} {outcome}", f"{op} {outcome}{reason}"


def _custody_entries(data_dir: Path, bare: str, operator: bool, since: str | None, until: str | None) -> list[dict]:
    """The custody journal's lines as entries. A lease request is journaled before it is sent (outcome
    "requested"); the next line of the same register and op is its answer (a client's calls go one at a time), so
    a request is shown only when no answer follows it: its outcome is unknown. A quiet answer (a HELD asked again
    while a HELD wait runs; the wait's own lines are entries) answers its request and is not shown."""
    out = []
    pending: dict[tuple[str, str], tuple[Path, int, dict]] = {}

    def add(path: Path, number: int, line: dict) -> None:
        if not (operator or _purpose_names(line.get("purpose"), bare)):
            return
        at = _norm_at(line.get("at"))
        if not _within(at, since, until):
            return
        kind, summary = _custody_summary(line)
        entry = {"at": at, "source": "custody", "kind": kind, "agent": bare, "summary": _clip(summary),
                 "evidence": {"file": str(path), "line": number}}
        for key in ("op", "outcome", "provider", "model", "purpose", "lease_id", "generation"):
            if line.get(key) is not None:
                entry[key] = line[key]
        out.append(entry)

    for path in _month_files(data_dir / "custody", "journal", since):
        for number, line in _lines(path):
            key = (str(line.get("register") or ""), str(line.get("op") or ""))
            if key[1] in ("claim", "release"):
                earlier = pending.pop(key, None)
                if earlier is not None and line.get("outcome") == "requested":
                    add(*earlier)   # a second request with none answered in between: the first had no answer
                if line.get("outcome") == "requested":
                    pending[key] = (path, number, line)
                    continue
                if line.get("quiet"):   # answers its request, and is not an entry (a HELD asked again in a wait)
                    continue
            add(path, number, line)
    for earlier in pending.values():
        add(*earlier)
    out.sort(key=lambda entry: entry["at"] or "")
    return out


def _drift_entries(data_dir: Path, bare: str, since: str | None, until: str | None) -> list[dict]:
    """Every drift source's recorded checks (for an operator: the checks are her toolkit's, DESIGN §38)."""
    out = []
    root = data_dir / "drift-sources"
    folders = sorted(path for path in root.iterdir() if path.is_dir()) if root.is_dir() else []
    for folder in folders:
        for path in _month_files(folder, "checks", since):
            for number, line in _lines(path):
                at = _norm_at(line.get("at"))
                if not _within(at, since, until):
                    continue
                name, status = str(line.get("source") or folder.name), str(line.get("status") or "?")
                refs = {key: [str(ref) for ref in line.get(key) or [] if isinstance(ref, str)]
                        for key in ("opened", "reopened", "closed")}
                counts = ", ".join(f"{key} {len(found)}" for key, found in refs.items() if found)
                detail = f": {line.get('detail')}" if line.get("detail") else ""
                summary = f"{name}: {status}" + (f" (was {line.get('previous')})"
                                                 if line.get("previous") not in (None, status) else "") + detail
                if counts:
                    summary += f"; {counts}"
                out.append({"at": at, "source": "drift", "kind": "drift check", "agent": bare,
                            "summary": _clip(summary), "drift_source": name, "status": status,
                            "observed_at": line.get("observed_at"),
                            "items": [ref for found in refs.values() for ref in found],
                            "evidence": {"file": str(path), "line": number}})
    return out


def _snapraid_entries(data_dir: Path, bare: str, since: str | None, until: str | None) -> list[dict]:
    """The disk protection panel's journal (for an operator: disk protection is part of her server, DESIGN §41)."""
    out = []
    folder = data_dir / "snapraid"
    for path in _month_files(folder, "journal", since) if folder.is_dir() else []:
        for number, line in _lines(path):
            at = _norm_at(line.get("at"))
            if not _within(at, since, until):
                continue
            refs = [str(ref) for key in ("opened", "closed") for ref in line.get(key) or [] if isinstance(ref, str)]
            out.append({"at": at, "source": "snapraid", "kind": str(line.get("kind") or "panel"), "agent": bare,
                        "summary": _clip(str(line.get("summary") or "")), "items": refs,
                        "evidence": {"file": str(path), "line": number}})
    return out


# ---------------------------------------------------------------- the query


def _sort_key(entry: dict) -> tuple[str, int, str, int]:
    """An entry's place in the list: its time, its source, its file and line (unique: one line is one entry)."""
    evidence = entry.get("evidence") or {}
    return (str(entry["at"]), SOURCES.index(entry["source"]) if entry["source"] in SOURCES else len(SOURCES),
            str(evidence.get("file")), int(evidence.get("line") or 0))


def cursor(entry: dict) -> str:
    """The opaque continuation after this entry (next_after): its place in the list, base64url JSON."""
    return base64.urlsafe_b64encode(json.dumps(list(_sort_key(entry))).encode("utf-8")).decode("ascii").rstrip("=")


def read_cursor(value: Any) -> tuple[str, int, str, int]:
    """The place an after cursor names; ValueError when it is not one this query returned."""
    text = str(value or "").strip()
    try:
        key = json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)).decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError):
        key = None
    if not (isinstance(key, list) and len(key) == 4 and isinstance(key[0], str) and isinstance(key[2], str)
            and all(isinstance(key[i], int) and not isinstance(key[i], bool) for i in (1, 3))
            and _norm_at(key[0]) == key[0]):
        raise ValueError("after must be the next_after value an activity query returned")
    return key[0], key[1], key[2], key[3]


def activity(config: Config, *, agent: str, since: Any = None, until: Any = None, limit: int = DEFAULT_LIMIT,
             after: Any = None, receipts: Receipts | None = None, secrets: Callable[[], list[str]] = list,
             now: datetime | None = None) -> dict:
    """{agent, operator, since, until, entries, count, truncated, next_after, sources, errors}: everything the agent
    did in [since, until], oldest first (at most limit entries). A cut list continues with after=next_after: the
    entries after the last one shown, even when many share one second. ValueError for a bad name, time or cursor.
    receipts: the operator's receipts (already scanned by the caller), or None."""
    agent = str(agent or "").strip()
    if not AGENT_RE.fullmatch(agent):
        raise ValueError("agent must be an agent or persona name such as builder, persona:<name> or agent:<name>")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be from 1 to {MAX_LIMIT}")
    since_at = parse_when(since, now=now)
    until_at = parse_when(until, now=now)
    if since_at and until_at and since_at > until_at:
        raise ValueError("since is after until")
    position = read_cursor(after) if after not in (None, "") else None
    read_from = max(since_at or "", position[0]) if position else since_at
    bare, by, lowered = _names(config, agent)
    operator = is_operator(config, bare)
    data_dir = Path(config.data_dir)
    entries: list[dict] = []
    errors: list[str] = []
    for name, gather in (
            ("receipt", lambda: _receipt_entries(receipts, bare, lowered, operator, read_from, until_at)),
            ("tracker", lambda: _tracker_entries(data_dir, bare, by, read_from, until_at)),
            ("audit", lambda: _audit_entries(data_dir, bare, by, lowered, read_from, until_at)),
            ("run", lambda: _run_entries(data_dir, bare, read_from, until_at)),
            ("custody", lambda: _custody_entries(data_dir, bare, operator, read_from, until_at)),
            ("drift", lambda: _drift_entries(data_dir, bare, read_from, until_at) if operator else []),
            ("snapraid", lambda: _snapraid_entries(data_dir, bare, read_from, until_at) if operator else [])):
        try:
            entries += gather()
        except (OSError, ValueError) as exc:   # one unreadable source never hides the others
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
    entries.sort(key=_sort_key)
    if position is not None:
        entries = [entry for entry in entries if _sort_key(entry) > position]
    counts = {name: sum(1 for e in entries if e["source"] == name) for name in SOURCES}
    truncated = len(entries) > limit
    shown = redact(entries[:limit], secrets())
    return {"agent": bare, "operator": operator, "since": since_at, "until": until_at, "entries": shown,
            "count": len(entries), "truncated": truncated,
            "next_after": cursor(entries[limit - 1]) if truncated else None, "sources": counts, "errors": errors}


def activity_lines(result: dict) -> list[str]:
    """The activity for a terminal: one line per entry (time, source, summary), a receipt's not_verified first."""
    lines = []
    for entry in result.get("entries") or []:
        lines.append(f"{entry['at']}  {entry['source']:<7}  {entry['summary']}")
        if entry["source"] == "receipt" and entry.get("kind") == "change":
            for text in entry.get("not_verified") or []:
                lines.append(f"{'':22}  NOT verified: {text}")
            for text in entry.get("verified") or []:
                lines.append(f"{'':22}  verified (reported): {text}")
            if not entry.get("verified"):
                lines.append(f"{'':22}  verified: nothing")
            if entry.get("reported_outcome"):
                lines.append(f"{'':22}  the change: {entry['reported_outcome'].get('words')}")
        evidence = entry.get("evidence") or {}
        where = f"{evidence.get('file')}" + (f":{evidence['line']}" if evidence.get("line") else "")
        lines.append(f"{'':22}  evidence: {where}")
    if result.get("truncated"):
        lines.append(f"… {result['count'] - len(result.get('entries') or [])} more: continue with --after "
                     f"{result.get('next_after')}")
    return lines
