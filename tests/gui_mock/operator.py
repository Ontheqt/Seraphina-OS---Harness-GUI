"""Mock backend for the Operator area: DESIGN §36 "GUI interface notes (operator persona)" (the mark, her change
receipts, the activity query) and the operator page's status panels for GPU custody §37, drift sources §38 and
error capture §39 (their GUI notes at the end of §39). See tests/gui_mock/__init__.py for the hooks.

Real code makes the answers wherever it can, so the mock serves what the server would:

- **Receipts.** Each receipt file's bytes go through receipts.parse_receipt (the cannot-read reasons are its own:
  "nested more than 64 levels deep", "not JSON: NaN is not JSON …", an unknown schema) and the index is written to
  a temporary data directory exactly as receipts.Receipts keeps it (duplicates marked by Receipts._mark_duplicates);
  GET /api/operator/receipts and /api/operator/receipts/<id> are answered by a real receipts.Receipts reading that
  index (rows, get, summary, view: the label, not_verified before verified, the outcome words).
- **Activity.** The durable files the activity query reads (the system tracker log and a job's items log, the
  wakes audit, run logs, a persona chat's turn log, the custody journal, two drift sources' checks) are written to
  the same directory and read by activity.py's own source readers; the list is sorted, cut and continued with its
  own cursor (activity.cursor, activity.read_cursor). Only "is this name the operator" comes from this module's
  state (the real one discovers persona files).
- **The mark.** GET /api/operator from this module's marks with operator.marked and operator.rule_problem; POST
  /api/config agents.<name>.role and operator.{several, receipts_dir} with the settings' messages (a second
  operator, a factory agent, a receipts directory that overlaps the harness's files).

GET /api/custody and GET /api/drift-sources answer here only while the routing and tracker areas (earlier in
gui_mock.AREAS) do not; this module's rows for them are also at GET /__mock/operator/custody and
/__mock/operator/drift-sources, which tests/gui/operator.mjs routes the page's requests to, so its checks hold
whichever area answers.

Test hooks (/__mock/operator/<name>):
    POST scenario {name}   normal (reset) | none | rule-problem | not-operator | unreadable | writable | past-limit |
                           scan-fails (a failed receipt scan: scan.problem, and activity's "receipt scan: …" error) |
                           many (230 older receipts: more than the page's first 200) | no-folder (marked, no
                           operator.receipts_dir: the index's problem is receipts.Receipts' "… is not set")
    POST new-receipt       a receipt the next scan finds (POST /api/operator/receipts/scan reads it: new 1)
    POST errors            publish the failed calls again (agent.error, persona.window), as seed() does
    POST state {...}       fields laid over GET /api/operator's answer
    GET  custody, GET drift-sources   the rows above
"""

from __future__ import annotations

import atexit
import copy
import hashlib
import json
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from . import store

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from swfactory import activity as A  # noqa: E402  (the activity query's own readers and cursor)
from swfactory import custody as C  # noqa: E402  (GPU custody's reason codes in words)
from swfactory import operator as OP  # noqa: E402  (the mark and the one-operator rule)
from swfactory import receipts as R  # noqa: E402  (the receipt parser and the index reader)
from swfactory.personas import PERSONA_NAME_RE  # noqa: E402

AREA = "operator"
REPORTED = OP.REPORTED                       # "reported by the operator"
RECEIPTS_DIR = "/home/operator/receipts"
DATA_DIR = "/srv/ai-work/factory"            # the core mock's data_dir (its default_config)
CONFIG_DIR = "/home/me/.config/software-factory"
# The personas this mock knows (tests/gui_mock/personas.py seeds the same names) and their kinds: only an Open
# WebUI-bound persona or one that wakes itself can be the operator (operator.OPERATOR_KINDS).
PERSONA_KINDS = {"kate": "openwebui", "kate-browser": "openwebui", "kate-offline": "snapshot", "seraphina": "wake"}
RECEIPT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,95}")   # server._PARAM_RULES["receipt"]
JOB = ("my-app", "011-cache-the-pricing-page")
CHAT = "c-7e1f2a3b4c"
WRITABLE_WARNING = ("the harness's own user may write this file or its folder: it may not be the operator's (its "
                    "tracker items are marked untrusted)")
# The tracker area's seeded system items for the two claims her receipts report as not verified (tracker.py
# _seed_system: S-4 "restore drill not done", S-5 "nginx reload not verified"), and the drift items (S-1, S-2).
CLAIM_ITEMS = {"restore drill": "system:S-4", "nginx reload": "system:S-5"}
# A receipt whose own outcome is unknown opens "Outcome unknown: check before retrying (receipt <id>)" (DESIGN §40):
# the tracker area seeds it as S-9.
OUTCOME_ITEMS = {"r-20261025-013": "system:S-9"}
# The receipts the owner and the operator attached to the import of the workspace model kate (DESIGN §33 "Changes
# before import"): one of each outcome, one whose file went away, and one the index no longer holds (missing).
ATTACHED = (("r-20261025-013", "operator"), ("r-20261025-014", "owner"), ("r-20261022-002", "owner"),
            ("r-20261024-008", "owner"), ("r-20261025-011", "operator"))
ATTACHED_MISSING = "r-20261019-000"
_DIRS: list[str] = []
_PUBLISHED: list[bool] = []


def _cleanup(keep: str | None = None) -> None:
    """Remove the seeds' directories, except keep (the one the store points at now)."""
    for d in [d for d in _DIRS if d != keep]:
        _DIRS.remove(d)
        shutil.rmtree(d, ignore_errors=True)


atexit.register(_cleanup)


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_ms(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _st(core) -> dict:
    return store(core, AREA)


# ---------------------------------------------------------------- receipts


def _doc(rid: str, at: float, intent: str, undo: str, verified: list, not_verified: list, rollback: str, *,
         schema: str = "kate-change-receipt/1", actor: str = "kate", jobs=(), before=None, after=None, extra=None,
         time_text: str | None = None) -> dict:
    """A receipt as the operator writes it (all twelve fields, DESIGN §36 "Receipts")."""
    doc = {"schema": schema, "id": rid, "time": time_text or _iso(at), "actor": actor, "intent": intent, "undo_class": undo,
           "jobs": list(jobs), "before": before if before is not None else {}, "after": after if after is not None else {},
           "rollback": rollback, "verified": verified, "not_verified": not_verified}
    doc.update(extra or {})
    return doc


def _bytes(doc: dict) -> bytes:
    return json.dumps(doc, indent=1, ensure_ascii=False).encode("utf-8")


def _entry(rel: str, data: bytes | None, *, mtime: float, read: float, why: str | None = None,
           writable: bool = False, inode: int = 0) -> dict:
    """One file of the index as Receipts._entry keeps it (its parse by receipts.parse_receipt)."""
    entry = {"file": rel, "stamp": [inode, len(data or b""), int(mtime * 1e9), int(mtime * 1e9)], "bytes": len(data or b""),
             "mtime": _iso(mtime), "read_at": _iso(read), "first_read_at": _iso(read), "label": REPORTED}
    if writable:
        entry["harness_writable"] = True
    if data is None:
        entry.update(status="cannot read", problem=why, sha256=None)
        return entry
    entry["sha256"] = hashlib.sha256(data).hexdigest()
    entry["first_sha256"] = entry["sha256"]
    parsed = R.parse_receipt(data)
    if parsed["status"] == "read":
        warnings = parsed["warnings"]
        parsed["before"] = R._kept_object(parsed["before"], "before", warnings)
        parsed["after"] = R._kept_object(parsed["after"], "after", warnings)
        if writable:
            warnings.append(WRITABLE_WARNING)
    entry.update(parsed)
    return entry


def _large_tls(now: float, which: str) -> dict:
    """A large before/after (about 9 KB): the nginx TLS config and the certificate chain, with markup in it."""
    certs = [{"subject": f"CN=nas.home.arpa, OU=node-{i} <b>bold</b>" if i == 3 else f"CN=nas.home.arpa, OU=node-{i}",
              "issuer": "CN=Home Lab Intermediate CA" if which == "after" else "CN=Old Home Lab CA",
              "not_after": _iso(now + (90 if which == "after" else 4) * 86400 + i * 3600),
              "sha256": hashlib.sha256(f"{which}-{i}".encode()).hexdigest()} for i in range(18)]
    conf = "\n".join(f"    ssl_certificate /etc/nginx/tls/{'fullchain' if which == 'after' else 'cert'}.pem;  # line {n}"
                     for n in range(40))
    return {"nginx": {"conf": "server {\n    listen 443 ssl;\n" + conf + "\n    <script>alert('conf')</script>\n}",
                      "includes": [f"/etc/nginx/conf.d/site-{n}.conf" for n in range(24)], "reloaded": which == "after"},
            "certs": certs, "chain_ok": which == "after"}


def _huge(now: float) -> dict:
    """A before larger than the index keeps (16 KB of canonical JSON): the index keeps its keys and a note."""
    return {f"package-{n:03d}": {"version": f"575.{n}.0", "files": [f"/usr/lib/nvidia/lib-{n}-{k}.so" for k in range(6)]}
            for n in range(80)}


def _receipt_files(now: float) -> tuple[dict, dict]:
    """(files of the index, the bytes of each read file by id for the first version of a changed one)."""
    H = 3600.0
    D = 86400.0
    files: dict[str, dict] = {}

    def add(rel, doc, *, ago, read_ago=None, writable=False, raw=None, why=None, inode=0):
        data = raw if raw is not None else (_bytes(doc) if doc is not None else None)
        files[rel] = _entry(rel, data, mtime=now - ago, read=now - (read_ago if read_ago is not None else max(ago - 60, 0)),
                            why=why, writable=writable, inode=inode or (len(files) + 101))
        return files[rel]

    # partly verified, from a place the harness's user may write (its item S-4 is private untrusted)
    add("2026-10/r-20261021-001.json", _doc(
        "r-20261021-001", now - 4 * D, "Rotate the backup encryption key <b>now</b> before the weekly archive",
        "undo-restore", ["new key decrypts last night's archive"], ["restore drill not done"],
        "Restore the old key from the vault (vault kv get backup/old-key) and point restic at it again.",
        schema="operator-receipt/1", before={"restic": {"key_id": "k-2025-03", "repo": "/mnt/backup/restic"}},
        after={"restic": {"key_id": "k-2026-10", "repo": "/mnt/backup/restic"}}), ago=4 * D, writable=True)
    # nothing verified, with a large before and after
    add("2026-10/r-20261022-002.json", _doc(
        "r-20261022-002", now - 3 * D, "Change the TLS certificate chain on nginx <img src=x onerror=alert('intent')>",
        "undo-instant", [], ["nginx reload not verified"],
        "Put fullchain.pem.bak back in /etc/nginx/tls and reload nginx (nginx -s reload).",
        before=_large_tls(now, "before"), after=_large_tls(now, "after")), ago=3 * D)
    # verified, with a time written without a zone (read as UTC, a warning); a second file has the same id
    six = _doc("r-20261022-006", now - 2.5 * D, "Reload nginx and check the new chain from outside", "undo-instant",
               ["nginx reloaded and answers 200", "the chain validates from outside (openssl s_client)"], [],
               "Reload nginx with the old chain: the receipt r-20261022-002 rollback.",
               time_text=_iso(now - 2.5 * D).rstrip("Z"))
    add("2026-10/r-20261022-006.json", six, ago=2.5 * D)
    add("copies/r-20261022-006.json", six, ago=2.4 * D)
    # nothing verified, changed after it was first read, no rollback written (a warning)
    first = _doc("r-20261022-004", now - 2.8 * D, "Rotate the backup key again after the vault migration", "undo-restore",
                 [], ["Restore drill: not yet done."], "Restore the previous key from the vault.")
    changed = dict(first, intent="Rotate the backup key again after the vault migration (and the offsite copy)", rollback="")
    entry = add("2026-10/r-20261022-004.json", changed, ago=2.0 * D, read_ago=0.5 * D)
    entry["first_read_at"] = _iso(now - 2.7 * D)
    entry["first_sha256"] = hashlib.sha256(_bytes(first)).hexdigest()
    entry["changed_at"] = _iso(now - 0.5 * D)
    entry["warnings"].append(f"changed after it was first read on {entry['first_read_at']} (sha256 "
                             f"{entry['first_sha256'][:12]}… → {entry['sha256'][:12]}…)")
    # the reboot fallback, a before too large to keep
    add("2026-10/r-20261023-007.json", _doc(
        "r-20261023-007", now - 2 * D, "Pin the GPU driver to 575.x and reboot the server", "undo-reboot",
        ["nvidia-smi lists both GPUs after the reboot"], [],
        "If the server does not come back, GRUB boots the previous kernel and driver once by itself.",
        jobs=["3f2a9c1e-7b4d-4e8a-9c0f-1a2b3c4d5e6f", "not-a-uuid"], before=_huge(now),
        after={"driver": "575.64.03", "kernel": "6.8.0-48"}), ago=2 * D)
    # cannot be undone; its file went away (what she reported stays reported)
    gone = add("2026-10/r-20261024-008.json", _doc(
        "r-20261024-008", now - 1 * D, "Remove the old docker images and their volumes", "cannot-undo",
        ["df shows 40 GB free on /var/lib/docker"], [], "None: the images are gone; they can be pulled again."), ago=1 * D)
    gone.update(gone=True, gone_at=_iso(now - 3 * H))
    # an undo class this harness does not know, a field it does not read; moved from inbox/ (the old file gone)
    ten = _doc("r-20261025-010", now - 40 * 60, "Raise the Open WebUI upload limit <script>alert('x')</script>",
               "undo-by-hand", ["uploads of 50 MB work"], [], "Set UPLOAD_LIMIT back to 25 MB and restart open-webui.",
               extra={"ticket": "HOME-42"})
    old = add("inbox/r-20261025-010.json", ten, ago=50 * 60)
    old.update(gone=True, gone_at=_iso(now - 30 * 60))
    add("2026-10/r-20261025-010.json", ten, ago=35 * 60, read_ago=30 * 60)
    # verified by her, newest; operator-receipt/1 with its own outcome (succeeded) and the response it got (DESIGN §40)
    add("2026-10/r-20261025-011.json", _doc(
        "r-20261025-011", now - 2 * H, "Restart the Open WebUI container after the image update", "undo-instant",
        ["open-webui answers 200 on /health", "kate's workspace model loads"], [],
        "docker compose up -d with the previous image tag (open-webui:v0.6.30).", schema="operator-receipt/1",
        extra={"outcome": "succeeded", "response": {"status": 200, "ids": ["container 3f9a0c2e"],
                                                     "body_sha256": hashlib.sha256(b"restarted").hexdigest()}}), ago=2 * H)
    # the outcome she reports unknown (a timeout after sending): never verified, an "Outcome unknown" item (S-9)
    add("2026-10/r-20261025-013.json", _doc(
        "r-20261025-013", now - 3 * H, "Push the new Open WebUI settings to the backup host <i>now</i>", "undo-instant",
        ["the backup host answered ping"], ["the settings arrived on the backup host"],
        "Copy settings.bak back on the backup host.", schema="operator-receipt/1",
        extra={"outcome": "unknown", "response": {"status": 504, "ids": [],
                                                  "body_sha256": hashlib.sha256(b"<html>Gateway Timeout</html>").hexdigest()}}),
        ago=3 * H)
    # the outcome she reports failed (the other side refused it): never green
    add("2026-10/r-20261025-014.json", _doc(
        "r-20261025-014", now - 5 * H, "Raise the model server's context length to 64k", "undo-instant",
        ["the old settings are still active"], [], "Nothing to undo: the change was refused.", schema="operator-receipt/1",
        extra={"outcome": "failed", "response": {"status": 409, "ids": ["conflict-7 <b>busy</b>"], "body_sha256": None}}),
        ago=5 * H)
    # cannot read: each reason as parse_receipt (or the scan's wrapper) words it
    add("2026-10/deep.json", None, ago=1.5 * D, raw=b"[" * 70 + b"]" * 70)
    add("2026-10/nan.json", None, ago=1.2 * D,
        raw=b'{"schema": "kate-change-receipt/1", "id": "r-nan", "before": {"vram": NaN}}')
    add("2026-10/locked.json", None, ago=20 * H, why="could not be read (PermissionError)")
    add("2026-10/next-schema.json", {"schema": "kate-change-receipt/2", "id": "r-20261024-009", "note": "<b>new</b> fields"},
        ago=10 * H)
    return files


def _items(doc: dict) -> None:
    """Which tracker items each receipt's not-verified checks are (Receipts._apply_items: one item per claim), and
    the "Outcome unknown: check before retrying" item of a receipt that reports its outcome unknown."""
    for entry in doc["files"].values():
        if entry.get("status") != "read":
            continue
        entry["operator"] = "kate"   # attributed: exactly one operator, and her name is the actor
        entry["items"] = [] if entry.get("duplicate_of") else \
            list(dict.fromkeys(CLAIM_ITEMS[R.claim_key(text)] for text in entry.get("not_verified") or []
                               if R.claim_key(text) in CLAIM_ITEMS))
        if not entry.get("duplicate_of") and entry.get("id") in OUTCOME_ITEMS:
            entry["items"].append(OUTCOME_ITEMS[entry["id"]])


def _index(now: float) -> dict:
    doc = {"version": R.INDEX_VERSION, "receipts_dir": RECEIPTS_DIR, "scanned_at": _iso(now - 1800),
           "files": _receipt_files(now), "handled": {}, "past_limit": 0, "writable": False, "problem": None,
           "refused": [{"file": "latest.json", "why": "a symbolic link: refused (it could point outside the directory)"},
                       {"file": "archive/2024/q1/jan/old/", "why": f"deeper than {R.MAX_DEPTH} levels: not read"},
                       # her 0700 root folder: named, left as it is (never widened)
                       {"file": "change-receipts/root-only/", "why": f"{R.UNREADABLE}; owner uid 0, mode 0700",
                        "unreadable": True}]}
    R.Receipts._mark_duplicates(doc)
    _items(doc)
    return doc


def _reader(core) -> R.Receipts:
    st = _st(core)
    shim = SimpleNamespace(raw={}, path=Path(CONFIG_DIR) / "config.json", data_dir=st["dir"])
    return R.Receipts(lambda: shim, Path(st["dir"]))


def _seed_attached(reader: R.Receipts, now: float) -> None:
    """The changes before kate's import, attached by id with the real Receipts.attach (one call each, so the order
    and the attacher are as listed), then one row whose receipt the index no longer holds (written as the file
    keeps it: the index lost it after it was attached)."""
    for rid, by in ATTACHED:
        reader.attach("kate", attach=[rid], by=by)
    doc = reader._attachments()
    doc["models"]["kate"]["receipts"].append({"id": ATTACHED_MISSING, "attached_at": _iso(now - 6 * 86400), "by": "owner"})
    reader._save(doc, reader.root / R.BEFORE_IMPORT_FILE)


def import_record(core, model_id: str, imported_at, version_id) -> dict:
    """Factory._import_record over this area's receipts index: the baseline sentence and the receipts attached as the
    changes before the import (the Personas area's card page and the Messages area's wake persona page show it)."""
    from swfactory.personas import IMPORT_BASELINE

    reader = _reader(core)
    rows = []
    for row in reader.attached(model_id):
        try:
            receipt = reader.get(row["id"])["receipt"]
        except KeyError:
            rows.append({**row, "missing": True, "label": REPORTED, "note": "the receipts index no longer holds this receipt"})
            continue
        rows.append({**row, **{key: receipt.get(key) for key in ("time", "intent", "not_verified", "verified", "outcome",
                                                                  "reported_outcome", "file", "gone")}, "label": REPORTED})
    return {"model": model_id, "imported_at": imported_at, "version_id": version_id, "diff": None,
            "baseline": IMPORT_BASELINE, "changes_before_import": rows}


def before_import(core, persona: str, model_id: str, imported_at, version_id, body) -> dict:
    """POST /api/personas/<persona>/before-import (server.api_persona_before_import, Factory.persona_before_import):
    the request's checks, then Receipts.attach with its own refusals (400), persona.updated {what: before_import}."""
    body = body if isinstance(body, dict) else {}
    lists = {}
    for key in ("attach", "detach"):
        value = body.get(key, [])
        if not isinstance(value, list) or len(value) > 200 or not all(isinstance(x, str) for x in value):
            raise core.Invalid(f"'{key}' must be a list of receipt ids")
        lists[key] = value
    by = body.get("by", "owner")
    if by not in ("owner", "operator"):
        raise core.Invalid("'by' must be owner or operator")
    try:
        _reader(core).attach(model_id, attach=lists["attach"], detach=lists["detach"], by=by)
    except ValueError as exc:
        raise core.Invalid(str(exc)) from None
    core.BUS.publish("persona.updated", {"persona": persona, "what": "before_import"})
    return {"import": import_record(core, model_id, imported_at, version_id)}


def _save_index(core, doc: dict) -> None:
    path = Path(_st(core)["dir"]) / R.INDEX_DIR / R.INDEX_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def _no_folder(core, doc: dict) -> None:
    """A scan with no operator.receipts_dir, by Receipts._scan_into itself (its problem words, refused emptied)."""
    _reader(core)._scan_into(doc, {"receipts_dir": None}, time.time())


def _scan(core, *, rescan: bool) -> dict:
    """What Receipts.scan answers: the summary (skipped, unless rescan) or a scan that read what changed."""
    st = _st(core)
    reader = _reader(core)
    if st.get("scan_fails"):
        return {**reader.summary(), "skipped": False,
                "problem": "the scan failed (ItemsBusy): what was read before is shown"}
    if not rescan:
        return {**reader.summary(), "skipped": True}
    doc = reader.index()
    now = time.time()
    doc["scanned_at"] = _iso(now)
    stats = {"new": 0, "changed": 0, "gone": 0}
    items: list[str] = []
    pending = st.pop("pending", None)
    if pending and not doc.get("problem"):
        rel, entry = pending
        doc["files"][rel] = entry
        R.Receipts._mark_duplicates(doc)
        _items(doc)
        stats["new"] = 1
        items = list(entry.get("items") or [])
    _save_index(core, doc)
    summary = {**reader._summary(doc), **stats, "items": items, "skipped": False}
    if stats["new"]:
        core.BUS.publish("operator.receipts", {key: summary[key] for key in ("read", "cannot_read", "refused", "new", "changed", "gone")}
                         | {"items": items})
    return summary


# ---------------------------------------------------------------- the durable files the activity query reads


def _lines(path: Path, lines: list[dict], when: str = "at") -> None:
    """Append JSON lines to path; a monthly file (…-YYYY-MM.jsonl) is chosen by each line's own month."""
    for line in lines:
        target = path
        if path.name.endswith("-MONTH.jsonl"):
            target = path.with_name(path.name.replace("MONTH", str(line[when])[:7]))
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n")


def _write_sources(root: Path, now: float) -> None:
    H = 3600.0
    D = 86400.0
    at = lambda ago: _iso(now - ago)  # noqa: E731
    repo, job = JOB
    # tracker: her transitions in the system scope, the builder's in a job's items log (by is the name)
    _lines(root / "housekeeping" / "system.log.jsonl", [
        {"at": at(3.9 * D), "ref": "system:S-4", "by": "factory", "from": None, "to": "proposed",
         "note": "Reported as not verified in receipt r-20261021-001"},
        {"at": at(1.9 * D), "ref": "system:S-7", "by": "persona:kate", "from": None, "to": "open",
         "note": "Check in with you about the sleep log on Friday <b>soon</b>"},
        {"at": at(1.1 * D), "ref": "system:S-7", "by": "persona:kate", "from": "open", "to": "doing", "note": ""},
        {"at": at(5 * H), "ref": "system:S-7", "by": "persona:kate", "from": "doing", "to": "done",
         "note": "asked in the evening check-in", "evidence": [{"kind": "wake", "ref": "W-2214", "check": "pending"}]},
    ])
    _lines(root / "jobs" / repo / job / "items.log.jsonl", [
        {"at": at(1.5 * D), "ref": f"job:{repo}/{job}/A-1", "by": "builder", "from": "open", "to": "doing", "note": ""},
        {"at": at(1.4 * D), "ref": f"job:{repo}/{job}/A-1", "by": "builder", "from": "doing", "to": "done",
         "note": "cache headers set", "evidence": [{"kind": "commit", "ref": "9f2c4e1", "check": "pending"}]},
    ])
    # audit: the messages and wakes audit (typed fields only)
    _lines(root / "wakes" / "audit-MONTH.jsonl", [
        {"at": at(1.8 * D), "kind": "message", "persona": "kate", "agent": "persona:kate", "channel": "ntfy",
         "message": "M-000041", "verb": "sent", "instance": "Seraphina OS Harness", "prev": None},
        {"at": at(20 * H), "kind": "chat.checkin", "persona": "kate", "agent": "persona:kate", "chat": CHAT,
         "decision": "allowed", "instance": "Seraphina OS Harness", "prev": None},
        {"at": at(3 * H), "kind": "message", "persona": "kate", "agent": "persona:kate", "channel": "telegram",
         "message": "M-000057", "verb": "acknowledged", "by": "human", "instance": "Seraphina OS Harness", "prev": None},
    ])
    # run: her persona chat's turns, the builder's job run
    chat = root / "chats" / CHAT
    chat.mkdir(parents=True, exist_ok=True)
    (chat / "chat.json").write_text(json.dumps({"id": CHAT, "agent": "kate", "mode": "none"}), encoding="utf-8")
    _lines(chat / "logs" / "01-turn.jsonl", [
        {"ts": at(20 * H + 30), "type": "agent.start", "agent": "kate", "data": {"model": "kate", "model_id": "kate", "chat": CHAT}},
        {"ts": at(20 * H - 20), "type": "agent.end", "agent": "kate", "data": {"ok": True, "chat": CHAT}},
    ], when="ts")
    _lines(chat / "logs" / "02-turn.jsonl", [
        {"ts": at(4 * H + 15), "type": "agent.start", "agent": "kate", "data": {"model": "kate", "model_id": "kate", "chat": CHAT}},
        {"ts": at(4 * H), "type": "agent.end", "agent": "kate",
         "data": {"ok": False, "error": "model call failed: HTTP 502 <html>Bad gateway</html>", "chat": CHAT}},
    ], when="ts")
    logs = root / "jobs" / repo / job / "logs"
    _lines(logs / "02-builder.jsonl", [
        {"ts": at(1.6 * D), "type": "agent.start", "agent": "builder", "data": {"model": "local", "model_id": "qwen3.8-flash-next"}},
        {"ts": at(1.4 * D - 60), "type": "agent.end", "agent": "builder", "data": {"ok": True}},
    ], when="ts")
    # custody: what the harness asked the operator's register and what it answered (every line is hers)
    purpose_job = f"job {repo}/{job} builder"
    reg = "https://127.0.0.1:7300"
    _lines(root / "custody" / "journal-MONTH.jsonl", [
        {"at": at(1.6 * D), "register": reg, "op": "claim", "outcome": "granted", "provider": "gpu0", "model": "qwen3.8-flash-next",
         "purpose": purpose_job, "lease_id": "L-7f3a9c", "generation": 41, "until": at(1.6 * D - 1800)},
        {"at": at(1.4 * D), "register": reg, "op": "release", "outcome": "released", "provider": "gpu0", "model": "qwen3.8-flash-next",
         "purpose": purpose_job, "lease_id": "L-7f3a9c", "resident_after": [["gpu0", "qwen3.8-flash-next"], ["gpu0", "embed-small"]]},
        {"at": at(22 * H), "register": reg, "op": "claim", "outcome": "denied", "reason": "LEASED", "provider": "gpu0",
         "model": "qwen3.8-27b-exl3", "purpose": "wake seraphina W-2210 <b>check-in</b>", "holder": "harness-4711", "generation": 41},
        {"at": at(21 * H), "register": reg, "op": "claim", "outcome": "unknown", "provider": "gpu0", "model": "qwen3.8-27b-exl3",
         "purpose": f"chat {CHAT}", "reason": "the connection was lost before the answer came", "generation": 41},
        {"at": at(9 * H), "register": reg, "op": "epoch", "outcome": "restarted", "reason": "epoch reg-2026-10-19 became reg-2026-10-20"},
        {"at": at(8 * H), "register": reg, "op": "release", "outcome": "unreleased", "provider": "gpu0", "model": "qwen3.8-27b-exl3",
         "purpose": "lead my-app/P003-billing", "lease_id": "L-51c0d2", "reason": "the run ended",
         "resident_after": None},
        # a lease of the register's earlier epoch is not given back to its new instance: nothing is sent
        {"at": at(7.5 * H), "register": reg, "op": "release", "outcome": "lost", "provider": "gpu0", "model": "qwen3.8-27b-exl3",
         "purpose": f"chat {CHAT}", "lease_id": "L-41aa07",
         "reason": "not sent: the register restarted (epoch reg-2026-10-19 became reg-2026-10-20) (the run ended)",
         "resident_after": [["gpu0", "qwen3.8-flash-next"]]},
        # FULL: the request journaled first, then the answer (CustodyClient._intend, _note); the card cannot fit it
        {"at": at(6 * H), "register": reg, "op": "claim", "outcome": "requested", "provider": "gpu0", "model": "qwen3.8-27b-exl3",
         "purpose": "plan my-app/P004 lead", "holder": "harness-4711"},
        {"at": at(6 * H - 1), "register": reg, "op": "claim", "outcome": "denied", "reason": "FULL", "provider": "gpu0",
         "model": "qwen3.8-27b-exl3", "purpose": "plan my-app/P004 lead", "words": "the card cannot fit this model now",
         "generation": 42},
        # HELD with no other card free: a placement waits out someone else's hold, once, and gives up after the provider's
        # overflow_after_seconds
        {"at": at(5 * H), "register": reg, "op": "claim", "outcome": "requested", "provider": "gpu0", "model": "qwen3.8-flash-next",
         "purpose": f"chat {CHAT}", "holder": "harness-4711"},
        {"at": at(5 * H - 1), "register": reg, "op": "claim", "outcome": "denied", "reason": "HELD", "provider": "gpu0",
         "model": "qwen3.8-flash-next", "purpose": f"chat {CHAT}", "words": "the card is held by someone else", "generation": 42},
        {"at": at(5 * H - 2), "register": reg, "op": "wait", "outcome": "waiting", "reason": "HELD", "provider": "gpu0",
         "model": "qwen3.8-flash-next", "purpose": f"chat {CHAT}", "seconds": 120},
        {"at": at(5 * H - 7), "register": reg, "op": "claim", "outcome": "requested", "provider": "gpu0", "model": "qwen3.8-flash-next",
         "purpose": f"chat {CHAT}", "holder": "harness-4711"},
        {"at": at(5 * H - 8), "register": reg, "op": "claim", "outcome": "denied", "reason": "HELD", "provider": "gpu0",
         "model": "qwen3.8-flash-next", "purpose": f"chat {CHAT}", "generation": 42, "quiet": True},
        {"at": at(5 * H - 122), "register": reg, "op": "wait", "outcome": "gave_up", "reason": "HELD", "provider": "gpu0",
         "model": "qwen3.8-flash-next", "purpose": f"chat {CHAT}", "seconds": 120},
        # a request journaled with no answer after it: the harness stopped while it was out (outcome unknown, §40)
        {"at": at(2 * H), "register": reg, "op": "claim", "outcome": "requested", "provider": "gpu0", "model": "qwen3.8-27b-exl3",
         "purpose": "wake kate W-2230", "holder": "harness-4711"},
    ])
    # snapraid: the disk protection panel's journal (DESIGN §41: counts, codes and refs only; the operator's only)
    _lines(root / "snapraid" / "journal-MONTH.jsonl", [
        {"at": at(1.2 * D), "kind": "guard", "summary": "guard report: hold (deleted_over_threshold); 0 file(s) would be lost",
         "guard": "G-0007", "reasons": ["deleted_over_threshold"], "verdict": "hold"},
        {"at": at(6.5 * H), "kind": "health", "summary": "health check: 1 finding(s) opened, 0 closed", "job": "J-0012",
         "opened": ["system:S-1"]},
    ])
    # drift: two sources' recorded checks (the operator's only)
    _lines(root / "drift-sources" / "checker" / "checks-MONTH.jsonl", [
        {"at": at(1.0 * D), "source": "checker", "status": "ok", "http": 200, "observed_at": at(1.0 * D + 20), "previous": "never",
         "opened": ["system:S-1", "system:S-2"], "reopened": [], "closed": [], "kept": 0, "profiles": 2},
        {"at": at(1.4 * H), "source": "checker", "status": "ok", "http": 200, "observed_at": at(1.4 * H + 15), "previous": "ok",
         "opened": [], "reopened": [], "closed": ["system:S-2"], "kept": 1},
    ])
    _lines(root / "drift-sources" / "inventory" / "checks-MONTH.jsonl", [
        {"at": at(10 * 60), "source": "inventory", "status": "cannot_read", "http": 200, "previous": "ok",
         "detail": "unknown schemaVersion '3' (this source may send 1, 2): cannot read, which is not \"no drift\"",
         "opened": [], "reopened": [], "closed": [], "kept": 0},
    ])


def operators(core) -> list[str]:
    """operator.operators: the marked names when the rule holds and each is a persona that can be one."""
    raw = _raw(core)
    if OP.rule_problem(raw):
        return []
    return [name for name in OP.marked(raw) if PERSONA_KINDS.get(name) in OP.OPERATOR_KINDS]


def _raw(core) -> dict:
    st = _st(core)
    return {"agents": {name: {"role": OP.ROLE} for name in st["marks"]},
            "operator": {"several": list(st["several"]), "receipts_dir": st["receipts_dir"]}}


def activity(core, agent: str, since=None, until=None, limit: int = A.DEFAULT_LIMIT, after=None) -> dict:
    """activity.activity over the mock's data directory (its own readers, sort key and cursor); operator from here."""
    st = _st(core)
    agent = str(agent or "").strip()
    if not A.AGENT_RE.fullmatch(agent):
        raise ValueError("agent must be an agent or persona name such as builder, persona:<name> or agent:<name>")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= A.MAX_LIMIT:
        raise ValueError(f"limit must be from 1 to {A.MAX_LIMIT}")
    since_at, until_at = A.parse_when(since), A.parse_when(until)
    if since_at and until_at and since_at > until_at:
        raise ValueError("since is after until")
    position = A.read_cursor(after) if after not in (None, "") else None
    read_from = max(since_at or "", position[0]) if position else since_at
    shim = SimpleNamespace(raw={}, data_dir=st["dir"])
    bare, by, lowered = A._names(shim, agent)
    operator = bare in operators(core)
    data_dir = Path(st["dir"])
    reader = _reader(core)
    entries: list[dict] = []
    errors: list[str] = []
    for name, gather in (
            ("receipt", lambda: A._receipt_entries(reader, bare, lowered, operator, read_from, until_at)),
            ("tracker", lambda: A._tracker_entries(data_dir, bare, by, read_from, until_at)),
            ("audit", lambda: A._audit_entries(data_dir, bare, by, lowered, read_from, until_at)),
            ("run", lambda: A._run_entries(data_dir, bare, read_from, until_at)),
            ("custody", lambda: A._custody_entries(data_dir, bare, operator, read_from, until_at)),
            ("drift", lambda: A._drift_entries(data_dir, bare, read_from, until_at) if operator else []),
            ("snapraid", lambda: A._snapraid_entries(data_dir, bare, read_from, until_at) if operator else [])):
        try:
            entries += gather()
        except (OSError, ValueError) as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
    entries.sort(key=A._sort_key)
    if position is not None:
        entries = [entry for entry in entries if A._sort_key(entry) > position]
    counts = {name: sum(1 for e in entries if e["source"] == name) for name in A.SOURCES}
    truncated = len(entries) > limit
    result = {"agent": bare, "operator": operator, "since": since_at, "until": until_at, "entries": entries[:limit],
              "count": len(entries), "truncated": truncated,
              "next_after": A.cursor(entries[limit - 1]) if truncated else None, "sources": counts, "errors": errors}
    if st.get("scan_fails"):   # Factory.activity: a receipt scan that failed is an error, never a failed query
        result["errors"] = [*result["errors"], "receipt scan: ItemsBusy: the scan failed; what was read before is used"]
    return result


def shown_paths(core, result: dict) -> dict:
    """The answer with evidence paths under the configured data_dir (the files live in a temporary directory;
    next_after, an opaque cursor, still names them as read)."""
    tmp = _st(core)["dir"]
    base = str(core.STATE["config"].get("data_dir") or DATA_DIR)

    def fix(value):
        return base + value[len(tmp):] if isinstance(value, str) and value.startswith(tmp) else value
    out = copy.deepcopy(result)
    for entry in out["entries"]:
        evidence = entry.get("evidence") or {}
        evidence["file"] = fix(evidence.get("file"))
        for also in evidence.get("also") or []:
            also["file"] = fix(also.get("file"))
    return out


# ---------------------------------------------------------------- GPU custody and drift sources (their status)


def custody_rows(core) -> dict:
    """GET /api/custody (Factory.custody): a register with a residency read, held, unknown and lost leases (the only
    states CustodyClient.leases can hold: release drops a lease before it asks the register, so a given-back or
    not-given-back one is in the history only) and a history of every kind of answer; a provider entry that follows it (custody_from); a register whose
    generation went back (fenced, residency never read); a provider under custody that needs no lease."""
    now = time.time()
    at = lambda ago: _iso_ms(now - ago)  # noqa: E731
    gpu0 = {
        "url": "https://127.0.0.1:7300", "generation": 42, "epoch": "reg-2026-10-20", "fenced": None,
        "residency": {"generation": 42, "epoch": "reg-2026-10-20", "observed_at": _iso(now - 96), "read_at": at(95),
                      "resident": [{"provider": "gpu0", "model": "qwen3.8-flash-next", "vram_mib": 21840, "since": _iso(now - 7300)},
                                   {"provider": "gpu0", "model": "embed-small", "vram_mib": 910, "since": None}]},
        "leases": [
            {"lease_id": "L-8c21d0", "provider": "tabby", "model": "qwen3.8-flash-next", "register_provider": "gpu0",
             "register_model": "qwen3.8-flash-next", "purpose": f"job {JOB[0]}/{JOB[1]} builder", "holder": "harness-4711",
             "until": at(-1500), "generation": 42, "epoch": "reg-2026-10-20", "state": "held", "error": None, "users": 2,
             "resident_before": [["gpu0", "qwen3.8-flash-next"]], "resident_after": None},
            {"lease_id": "L-93ab10", "provider": "tabby", "model": "qwen3.8-27b-exl3", "register_provider": "gpu0",
             "register_model": "qwen3.8-27b-exl3", "purpose": "wake seraphina W-2216 <b>check-in</b>", "holder": "harness-4711",
             "until": at(-600), "generation": 42, "epoch": "reg-2026-10-20", "state": "unknown",
             "error": "renew: the connection was lost before the answer was whole", "users": 1,
             "resident_before": [["gpu0", "qwen3.8-flash-next"]], "resident_after": None},
            {"lease_id": "L-41aa07", "provider": "tabby", "model": "qwen3.8-27b-exl3", "register_provider": "gpu0",
             "register_model": "qwen3.8-27b-exl3", "purpose": f"chat {CHAT}", "holder": "harness-4711", "until": at(400),
             "generation": 40, "epoch": "reg-2026-10-19", "state": "lost", "error": "the register restarted (epoch reg-2026-10-19 became reg-2026-10-20)",
             "users": 0, "resident_before": [["gpu0", "qwen3.8-27b-exl3"]], "resident_after": None},
        ],
        "history": [   # oldest first (CustodyClient.history is a deque appended to)
            {"at": at(5400), "op": "claim", "provider": "gpu0", "model": "qwen3.8-27b-exl3", "purpose": f"chat {CHAT}",
             "outcome": "granted", "lease_id": "L-41aa07", "generation": 40, "until": _iso(now - 5400 + 1800)},
            {"at": at(4000), "op": "epoch", "outcome": "restarted", "reason": "epoch reg-2026-10-19 became reg-2026-10-20"},
            {"at": at(3000), "op": "claim", "provider": "gpu0", "model": "qwen3.8-27b-exl3", "purpose": "wake seraphina W-2210",
             "outcome": "denied", "reason": "LEASED", "holder": "operator-shell", "generation": 41},
            {"at": at(2400), "op": "claim", "provider": "gpu0", "model": "qwen3.8-27b-exl3", "purpose": "model test local",
             "outcome": "stale", "reason": "generation 39 is older than 41, the last one seen"},
            {"at": at(1900), "op": "claim", "provider": "gpu0", "model": "qwen3.8-27b-exl3", "purpose": "wake seraphina W-2216",
             "outcome": "unknown", "reason": "the connection was lost before the answer came", "generation": 41},
            {"at": at(1800), "op": "claim", "provider": "gpu0", "model": "qwen3.8-27b-exl3", "purpose": "wake seraphina W-2216",
             "outcome": "granted", "lease_id": "L-93ab10", "generation": 42, "until": _iso(now + 600)},
            {"at": at(1700), "op": "claim", "provider": "gpu0", "model": "qwen3.8-flash-next", "purpose": f"job {JOB[0]}/{JOB[1]} builder",
             "outcome": "granted", "lease_id": "L-8c21d0", "generation": 42, "until": _iso(now + 1500)},
            {"at": at(1500), "op": "claim", "provider": "gpu0", "model": "qwen3.8-27b-exl3", "purpose": "plan my-app/P004 lead",
             "outcome": "denied", "reason": "FULL", "words": "the card cannot fit this model now", "generation": 42},
            {"at": at(1400), "op": "wait", "provider": "gpu0", "model": "qwen3.8-flash-next", "purpose": "chat c-1a2b3c4d5e",
             "outcome": "waiting", "reason": "HELD", "seconds": 120},
            {"at": at(1280), "op": "wait", "provider": "gpu0", "model": "qwen3.8-flash-next", "purpose": "chat c-1a2b3c4d5e",
             "outcome": "gave_up", "reason": "HELD", "seconds": 120},
            {"at": at(1200), "op": "release", "lease_id": "L-51c0d2", "provider": "gpu0", "model": "qwen3.8-27b-exl3",
             "purpose": "lead my-app/P003-billing", "outcome": "unreleased", "reason": "the run ended"},
            {"at": at(1100), "op": "release", "lease_id": "L-41aa07", "provider": "gpu0", "model": "qwen3.8-27b-exl3",
             "purpose": f"chat {CHAT}", "outcome": "lost",
             "reason": "not sent: the register restarted (epoch reg-2026-10-19 became reg-2026-10-20) (the run ended)",
             "resident_after": [["gpu0", "qwen3.8-flash-next"]]},
            {"at": at(900), "op": "renew", "lease_id": "L-93ab10", "purpose": "wake seraphina W-2216", "outcome": "unknown",
             "reason": "OUTCOME_UNKNOWN"},
            {"at": at(890), "op": "residency", "outcome": "unread", "reason": "the register did not answer in time (TIMEOUT)"},
            {"at": at(600), "op": "call", "lease_id": "L-8c21d0", "purpose": f"job {JOB[0]}/{JOB[1]} builder", "outcome": "unknown",
             "reason": "the stream ended before the model finished", "resident": [["gpu0", "qwen3.8-flash-next"]]},
            {"at": at(95), "op": "release", "lease_id": "L-77e301", "provider": "gpu0", "model": "embed-small",
             "purpose": "model test embed", "outcome": "released", "resident_after": [["gpu0", "embed-small"], ["gpu0", "qwen3.8-flash-next"]]},
        ]}
    strata = {"url": "http://127.0.0.1:7400", "generation": 17, "epoch": None,
              "fenced": {"seen": 17, "answered": 3, "since": at(5400)}, "residency": None, "leases": [],
              "history": [{"at": at(5400), "op": "residency", "outcome": "stale", "reason": "generation 3 is older than 17, the last one seen"},
                          {"at": at(5400), "op": "residency", "outcome": "unread", "reason": "generation 3 is older than 17, the last one seen"}]}
    ollama = {"url": "http://127.0.0.1:7500", "generation": 5, "epoch": None, "fenced": None,
              "residency": {"generation": 5, "epoch": None, "observed_at": _iso(now - 400), "read_at": at(399), "resident": []},
              "leases": [], "history": []}
    gpu0_settings = {"url": "https://127.0.0.1:7300", "timeout": 5.0, "token_credential": "custody-token",
                     "host_header": "register.internal", "ca_file": "/etc/ai-stack/register-ca.pem", "provider": "gpu0"}
    return {"providers": [
        {"provider": "ollama", "lazy_load": False, "allow_model_swap": False, "needs_lease": False, "custody_from": None,
         "held_wait_seconds": None, "settings": {"url": "http://127.0.0.1:7500", "timeout": 5.0, "provider": "ollama-gpu"},
         "register": ollama},
        {"provider": "strata", "lazy_load": True, "allow_model_swap": False, "needs_lease": True, "custody_from": None,
         "held_wait_seconds": None,
         "settings": {"url": "http://127.0.0.1:7400", "timeout": 5.0, "token_env": "STRATA_CUSTODY_TOKEN", "provider": "strata-gpu"},
         "register": strata},
        {"provider": "tabby", "lazy_load": False, "allow_model_swap": True, "needs_lease": True, "custody_from": None,
         "held_wait_seconds": 120.0, "settings": dict(gpu0_settings, models={"qwen3.8-flash-next": "qwen3.8-flash-next"}),
         "register": gpu0},
        {"provider": "tabby-raw", "lazy_load": False, "allow_model_swap": True, "needs_lease": True, "custody_from": "tabby",
         "held_wait_seconds": 120.0, "settings": gpu0_settings, "register": copy.deepcopy(gpu0)},
    ], "reason_codes": dict(C.REFUSAL_WORDS)}


def drift_rows(core) -> list[dict]:
    """GET /api/drift-sources rows (DriftSources.views): every status, findings present with their items, a manifest."""
    now = time.time()
    at = lambda ago: _iso(now - ago)  # noqa: E731

    def row(name, url, **state):
        base = {"name": name, "url": url, "host_header": None, "ca_file": None, "token_from": "file",
                "schema_versions": ["1", "2"], "interval": 3600.0, "timeout": 10.0, "status": "never", "detail": None,
                "checked_at": None, "observed_at": None, "valid_until": None, "schema_version": None, "next_at": None,
                "failures": None, "http": None, "last_result": None, "present": [], "items": [], "manifest": False}
        base.update(state)
        return base
    return [
        row("checker", "https://127.0.0.1:8443/drift", host_header="checker.local", ca_file="/etc/checker/ca.pem", status="ok",
            checked_at=at(4900), observed_at=at(4915), valid_until=at(-1800), schema_version="1", next_at=at(-1300), failures=0,
            http=200, manifest=True, present=["image:open-webui", "port:8080 <b>open</b>"],
            last_result={"opened": ["system:S-1"], "reopened": [], "closed": ["system:S-2"], "kept": ["system:S-1"]},
            items=["system:S-1", "system:S-2"]),
        row("edge-proxy", "http://127.0.0.1:9400/drift", status="ok", checked_at=at(700), observed_at=at(710),
            valid_until=at(-2900), schema_version="2", next_at=at(-2900), failures=0, http=200,
            last_result={"opened": [], "reopened": [], "closed": [], "kept": []}),
        # only profiles, never a findings key (DriftSources._act leaves findings and last_result alone): it says
        # nothing about drift, which GET /api/drift-sources shows only as last_result null
        row("role-inventory", "http://127.0.0.1:9150/inventory", status="ok", checked_at=at(900), observed_at=at(905),
            valid_until=at(-2700), schema_version="2", next_at=at(-2700), failures=0, http=200, manifest=True),
        row("inventory", "http://127.0.0.1:9100/inventory", token_from="env", status="cannot_read",
            detail="unknown schemaVersion '3' (this source may send 1, 2): cannot read, which is not \"no drift\"",
            checked_at=at(600), http=200, failures=2, next_at=at(-6600)),
        row("gpu-watch", "http://127.0.0.1:9200/drift", status="busy", detail="check in progress (429)", checked_at=at(40),
            http=429, failures=1, next_at=at(-30)),
        row("nas-check", "http://192.168.1.20:9000/drift", status="unreachable",
            detail="cannot connect to 192.168.1.20:9000 (connection refused)", checked_at=at(300), failures=3, next_at=at(-180)),
        row("cert-watch", "http://127.0.0.1:9600/drift", status="stale",
            detail=f"the observation from {at(9000)} was valid for 3600s: it is too old to act on", checked_at=at(200),
            observed_at=at(9000), valid_until=at(5400), schema_version="1", http=200, failures=1, next_at=at(-3400)),
        row("disk-audit", "http://127.0.0.1:9700/drift", status="error", checked_at=at(1200), http=200, failures=1,
            observed_at=at(1210), valid_until=at(-2400), schema_version="1", next_at=at(-60),
            detail="the observation was read, but the tracker or the manifest could not be written: ItemsBusy: the system scope "
                   "is busy (retry in a second)"),
        row("backup-checker", "http://127.0.0.1:9300/drift"),
    ]


# ---------------------------------------------------------------- failed calls on the event stream (section 39)

ERROR_JOB = ("my-app", "003-fix-the-flaky-checkout-test")   # a failed job of the core seed


def publish_errors(core) -> None:
    """agent.error events as the loop emits them (capture.error_record, capture.tool_error_record: the real
    records), and a persona turn that did not finish (persona.window): what the Errors tab lists."""
    from swfactory import capture

    now = time.time()
    repo, job = ERROR_JOB
    first = capture.error_record(
        provider="tabby", model_id="qwen3.8-flash-next", url="http://127.0.0.1:5000/v1/chat/completions?stream=1", attempt=1,
        will_retry=True, status=500, http_status=200, message="the answer carried an error: CUDA out of memory",
        body=json.dumps({"error": {"message": "CUDA out of memory <b>while</b> loading", "code": 500, "request_id": "req_8f2a91"}}),
        headers={"x-request-id": "tabby-7781", "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"},
        started=now - 190.4, failed=now - 188.1)
    second = capture.error_record(
        provider="tabby", model_id="qwen3.8-flash-next", url="http://127.0.0.1:5000/v1/chat/completions", attempt=2,
        will_retry=False, status=503, http_status=503, message="HTTP 503 from tabby",
        body="<html><body><h1>503 Service Unavailable</h1><script>alert('body')</script></body></html>\n" + "padding " * 2100,
        headers={"cf-ray": "8d1f2a3b4c5d6e7f-AMS"}, started=now - 186.0, failed=now - 181.6)
    cut = capture.error_record(
        provider="openwebui", model_id="kate", url="http://127.0.0.1:3000/api/chat/completions", attempt=1, will_retry=False,
        status=None, message="the connection was lost before the answer was whole", connection=True,
        started=now - 4 * 3600 - 15, failed=now - 4 * 3600)
    tool = capture.tool_error_record(
        name="Bash", call_id="call_9", output="exit status 2\nrm: cannot remove '/var/lib/x': Permission denied "
                                              "<img src=x onerror=alert('tool')>", started=now - 420.2, failed=now - 419.9)
    core.BUS.publish("agent.error", dict(first, run="02-builder"), repo=repo, job=job, agent="builder")
    core.BUS.publish("agent.error", dict(second, run="02-builder"), repo=repo, job=job, agent="builder")
    core.BUS.publish("agent.error", dict(tool, run="03-reviewer-code"), repo=repo, job=job, agent="reviewer-code")
    core.BUS.publish("agent.error", dict(cut, chat=CHAT, run="02-turn"), agent="kate")
    core.BUS.publish("persona.window", {"persona": "kate", "model": "kate", "provider": "openwebui", "chat": CHAT,
                                        "started_at": cut["started_at"], "ended_at": cut["failed_at"], "ok": False}, agent="kate")


# ---------------------------------------------------------------- seed, routes, hooks


def seed(core, fresh: bool) -> None:
    # Built aside and swapped in at once: the page's own requests go on while a test resets the scenario, and a
    # half-cleared store would answer them 404 (a KeyError) or read a removed directory.
    new = {"dir": tempfile.mkdtemp(prefix="sf-mock-operator-"), "marks": [] if fresh else ["kate"], "several": [],
           "receipts_dir": None if fresh else RECEIPTS_DIR, "override": {}, "scan_fails": False, "fresh": fresh}
    _DIRS.append(new["dir"])
    now = time.time()
    index = {"version": R.INDEX_VERSION, "receipts_dir": None, "scanned_at": None, "files": {}, "handled": {},
             "refused": [], "problem": None} if fresh else _index(now)
    path = Path(new["dir"]) / R.INDEX_DIR / R.INDEX_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    if not fresh:
        _write_sources(Path(new["dir"]), now)
        shim = SimpleNamespace(raw={}, path=Path(CONFIG_DIR) / "config.json", data_dir=new["dir"])
        _seed_attached(R.Receipts(lambda: shim, Path(new["dir"])), now)
    st = _st(core)
    with core.LOCK:
        for key in [k for k in st if k not in new]:
            del st[key]
        st.update(new)
    _cleanup(keep=new["dir"])   # the previous seeds' directories (STATE["area"] starts empty on every seed)
    if fresh:
        return
    if not _PUBLISHED:   # once per mock process (a scenario reset reseeds; POST /__mock/operator/errors publishes again)
        _PUBLISHED.append(True)
        publish_errors(core)


def state(core) -> dict:
    """GET /api/operator (Factory.operator_state)."""
    st = _st(core)
    raw = _raw(core)
    summary = _reader(core).summary()
    out = {"operators": operators(core), "marked": OP.marked(raw), "several": list(st["several"]),
           "problem": OP.rule_problem(raw), "receipts_dir": st["receipts_dir"], "label": REPORTED,
           "scan": {key: summary[key] for key in ("scanned_at", "read", "cannot_read", "refused", "past_limit",
                                                  "unreadable", "writable", "gone", "problem")}}
    out.update(copy.deepcopy(st["override"]))
    return out


def _flag(core, value):
    if value is None or value.strip().lower() in ("", "0", "false", "no"):
        return False
    if value.strip().lower() in ("1", "true", "yes"):
        return True
    raise core.Invalid(f"invalid flag {value!r} (use 1 or 0)")


def _times(core, query) -> tuple:
    since, until = query.get("since") or None, query.get("until") or None
    for value in (since, until):
        if value is not None and len(value) > 64:
            raise core.Invalid("since and until are times such as 2026-10-22T08:00:00Z or spans such as 24h")
    return since, until


def _config(core, handler, body: dict):
    """POST /api/config: agents.<name>.role and the operator section, with settings' rules and messages
    (settings._check_route's role checks, settings._check_operator); everything else goes on to the core."""
    agents = body.get("agents") if isinstance(body.get("agents"), dict) else {}
    roles = {name: entry["role"] for name, entry in agents.items() if isinstance(entry, dict) and "role" in entry}
    section = body.get("operator")
    if not roles and section is None:
        return NotImplemented
    st = _st(core)
    marks, several, directory = set(st["marks"]), list(st["several"]), st["receipts_dir"]
    for name, role in roles.items():
        label = f"agents.{name}"
        if role is not None:
            if name == "*":
                raise core.Invalid("agents.*.role: the operator is one persona; set it on agents.<name>")
            if role not in OP.ROLES:
                raise core.Invalid(f'{label}.role must be "operator" or null')
            if not PERSONA_NAME_RE.fullmatch(name):
                raise core.Invalid(f"{label}.role: only a persona can be the operator, and {name!r} is not a persona name")
            marks.add(name)
        else:
            marks.discard(name)
    if section is not None:
        if not isinstance(section, dict):
            raise core.Invalid("operator must be an object")
        unknown = sorted(set(section) - {"several", "receipts_dir", "max_receipt_bytes", "max_receipts", "scan_seconds"})
        if unknown:
            raise core.Invalid(f"operator.{unknown[0]} is not a setting (use max_receipt_bytes, max_receipts, receipts_dir, "
                               "scan_seconds, several)")
        if "several" in section:
            value = section["several"]
            if value is not None and not (isinstance(value, list) and all(isinstance(x, str) and PERSONA_NAME_RE.fullmatch(x)
                                                                         for x in value)):
                raise core.Invalid("operator.several must be a list of persona names (the operators, when the owner wants "
                                   "more than one)")
            several = list(value or [])
        if "receipts_dir" in section:
            value = section["receipts_dir"]
            if value is not None and not (isinstance(value, str) and value.strip() and "\x00" not in value):
                raise core.Invalid("operator.receipts_dir must be a directory path or null")
            directory = value
    raw = {"agents": {name: {"role": OP.ROLE} for name in marks}, "operator": {"several": several}}
    problem = OP.rule_problem(raw)
    if problem:
        raise core.Invalid(problem)
    factory = {a for a in core.STATE.get("agents", {})}
    for name in sorted(marks):
        if name not in PERSONA_KINDS and name in factory:
            raise core.Invalid(f"agents.{name}.role: {name} is a factory agent, not a persona: only a persona can be the operator")
    if isinstance(directory, str) and directory.strip() and directory != st["receipts_dir"]:
        overlap = _overlap(core, directory)
        if overlap:
            raise core.Invalid(f"operator.receipts_dir: {overlap}")
    st["marks"], st["several"] = sorted(marks), several
    if directory != st["receipts_dir"]:
        st["receipts_dir"] = directory
        reader = _reader(core)
        doc = reader.index()
        doc["receipts_dir"] = directory
        if not directory:
            _no_folder(core, doc)
        else:
            doc["problem"] = None if directory == RECEIPTS_DIR else f"the receipts directory {directory} does not exist"
        _save_index(core, doc)
    if "agents" in body:   # Factory.update_config publishes no event: the page reads GET /api/operator again
        for name in roles:
            rest = {k: v for k, v in agents[name].items() if k != "role"}
            if rest:
                agents[name] = rest
            else:
                del agents[name]
        if not agents:
            del body["agents"]
    body.pop("operator", None)
    if not body:
        return handler.send_json(core.config_view())
    return NotImplemented


def _overlap(core, directory: str) -> str | None:
    """receipts.receipts_problem's words for a directory that is, holds or lies inside the harness's own files."""
    target = Path(directory.replace("~", "/home/me", 1) if directory.startswith("~") else directory)
    places = [("the harness's data directory", Path(core.STATE["config"].get("data_dir") or DATA_DIR)),
              ("the config directory", Path(CONFIG_DIR))]
    places += [(f"repository {r['name']}", Path(r["path"])) for r in core.STATE["config"].get("repos", []) if r.get("path")]
    for label, place in places:
        if target == place or place in target.parents or target in place.parents:
            return (f"{directory} overlaps {label} ({place}), where the harness and its agents write: name a directory of the "
                    "operator's own, apart from the harness's files")
    return None


def api(core, handler, method, parts, query, body):
    p = parts
    b = body if isinstance(body, dict) else {}
    if method == "GET" and p == ["operator"]:
        return handler.send_json(state(core))
    if method == "GET" and p == ["operator", "receipts"]:
        raw = query.get("limit")
        if raw and not re.fullmatch(r"\d+", raw):
            raise core.Invalid("limit must be a non-negative integer")
        limit = int(raw) if raw else 200
        if not 1 <= limit <= 1000:
            raise core.Invalid("limit must be from 1 to 1000")
        since, until = _times(core, query)
        rescan = _flag(core, query.get("rescan"))
        try:
            since_at, until_at = A.parse_when(since), A.parse_when(until)
        except ValueError as exc:
            raise core.Invalid(str(exc)) from None
        scan = _scan(core, rescan=rescan)
        reader = _reader(core)
        index = reader.index()
        return handler.send_json({"receipts": reader.rows(limit=limit, since=since_at, until=until_at),
                                  "refused": index.get("refused") or [], "unreadable": R.unreadable(index), "scan": scan})
    if method == "POST" and p == ["operator", "receipts", "scan"]:
        return handler.send_json({"scan": _scan(core, rescan=True)})
    if method == "GET" and len(p) == 3 and p[:2] == ["operator", "receipts"]:
        if len(p[2]) > 256 or not RECEIPT_ID_RE.fullmatch(p[2]):
            raise core.Invalid("invalid receipt id")
        return handler.send_json(_reader(core).get(p[2]))
    if method == "GET" and p == ["activity"]:
        agent = query.get("agent")
        if agent is None:
            raise core.Invalid("missing query parameter 'agent'")
        if len(agent) > 80:
            raise core.Invalid("invalid agent name")
        raw = query.get("limit")
        if raw and not re.fullmatch(r"\d+", raw):
            raise core.Invalid("limit must be a non-negative integer")
        since, until = _times(core, query)
        after = query.get("after") or None
        if after is not None and len(after) > 4096:
            raise core.Invalid("after must be the next_after value an activity query returned")
        try:
            result = activity(core, agent, since=since, until=until, limit=int(raw) if raw else 500, after=after)
        except ValueError as exc:
            raise core.Invalid(str(exc)) from None
        return handler.send_json(shown_paths(core, result))
    fresh = _st(core).get("fresh")   # a fresh install has no custody register and no drift source configured
    if method == "GET" and p == ["custody"]:
        return handler.send_json({"providers": []} if fresh else custody_rows(core))
    if method == "GET" and p == ["drift-sources"]:
        return handler.send_json({"sources": [] if fresh else drift_rows(core)})
    if method == "POST" and p == ["config"]:
        return _config(core, handler, b)
    return NotImplemented


SCENARIOS = ("normal", "none", "rule-problem", "not-operator", "unreadable", "writable", "past-limit", "scan-fails", "many",
             "no-folder")


def mock(core, handler, method, name, body):
    st = _st(core)
    b = body if isinstance(body, dict) else {}
    if method == "POST" and name == "state":
        st["override"].update(b)
        return handler.send_json(state(core))
    if method == "POST" and name == "scenario":
        which = b.get("name")
        if which not in SCENARIOS:
            raise core.Invalid("name must be one of " + ", ".join(SCENARIOS))
        reader = _reader(core)
        doc = reader.index()
        if which == "normal":
            seed(core, st.get("fresh", False))
            return handler.send_json(state(core))
        if which == "none":
            st["marks"] = []
        elif which == "rule-problem":
            st["marks"], st["several"] = ["kate", "seraphina"], []
        elif which == "not-operator":
            st["marks"] = ["kate-offline"]
        elif which == "unreadable":
            doc["problem"] = f"the harness may not read {RECEIPTS_DIR}: give its user read access"
        elif which == "writable":
            doc["writable"] = True
        elif which == "past-limit":
            doc["past_limit"] = 3
            doc["refused"] = [*doc.get("refused", []), *({"file": f"archive/2025-0{n}.json",
                                                          "why": "past operator.max_receipts (5000): an older file, not read"}
                                                         for n in (1, 2, 3))]
        elif which == "scan-fails":
            st["scan_fails"] = True
        elif which == "many":
            now = time.time()
            for n in range(230):
                at = now - 6 * 86400 - n * 3600
                rid = f"r-bulk-{n:03d}"
                rel = f"2026-09/{rid}.json"
                doc["files"][rel] = _entry(rel, _bytes(_doc(rid, at, f"Rotate log file {n}", "undo-instant", ["logrotate ran"], [],
                                                             "Restore the rotated file from /var/log/old.")),
                                           mtime=at, read=at + 60, inode=5000 + n)
            _items(doc)
        elif which == "no-folder":
            st["receipts_dir"] = None
            _no_folder(core, doc)
        _save_index(core, doc)
        return handler.send_json(state(core))
    if method == "POST" and name == "new-receipt":
        now = time.time()
        rel = "2026-10/r-20261025-012.json"
        doc = _doc("r-20261025-012", now - 60, "Turn on the nightly restore drill", "undo-instant",
                   ["the drill's timer is listed"], ["restore drill not done"], "systemctl disable --now restore-drill.timer")
        st["pending"] = (rel, _entry(rel, _bytes(doc), mtime=now - 60, read=now))
        return handler.send_json({"pending": rel})
    if method == "POST" and name == "errors":
        publish_errors(core)
        return handler.send_json({"published": 5})
    if method == "GET" and name == "custody":
        return handler.send_json(custody_rows(core))
    if method == "GET" and name == "drift-sources":
        return handler.send_json({"sources": drift_rows(core)})
    return NotImplemented


# ---------------------------------------------------------------- contract (tests/test_gui_static.py)


def contract(test) -> None:
    """The operator's answers have the server's shapes: GET /api/operator (Factory.operator_state), the receipts as
    receipts.Receipts serves them (every outcome, cannot-read reasons, gone, changed, duplicate, writable, refused),
    the detail, a scan, the activity query against activity.activity itself, the custody and drift rows against
    custody.py and driftsources.py, and the settings' 400s for the mark and the receipts directory."""
    from swfactory import custody as C
    from swfactory import driftsources as DS

    def get(path):
        status, _, raw = test.request("GET", path)
        return status, json.loads(raw)

    test.request("POST", "/__mock/operator/scenario", {"name": "normal"})
    status, st = get("/api/operator")
    test.assertEqual(status, 200)
    test.assertEqual(set(st), {"operators", "marked", "several", "problem", "receipts_dir", "label", "scan"})
    test.assertEqual(set(st["scan"]), {"scanned_at", "read", "cannot_read", "refused", "past_limit", "unreadable", "writable",
                                       "gone", "problem"})
    test.assertEqual(st["label"], OP.REPORTED)
    test.assertEqual(st["operators"], ["kate"])

    status, listing = get("/api/operator/receipts")
    test.assertEqual(status, 200)
    test.assertEqual(set(listing), {"receipts", "refused", "unreadable", "scan"})
    test.assertEqual(listing["unreadable"], ["change-receipts/root-only/"], "named, left as it is")
    keep = {"file", "status", "problem", "schema", "id", "time", "time_as_written", "actor", "operator", "intent", "undo", "jobs",
            "rollback", "not_verified", "verified", "outcome", "reported_outcome", "warnings", "label", "items", "sha256",
            "first_sha256", "bytes", "mtime", "read_at", "first_read_at", "changed_at", "gone", "gone_at", "duplicate_of",
            "harness_writable", "unreadable"}
    rows = listing["receipts"]
    states, problems, reports = set(), [], set()
    for row in rows:
        test.assertLessEqual(set(row), keep, row.get("file"))
        test.assertIn("actor", row, "every receipt carries its actor (null: not stated)")
        test.assertEqual(row["label"], OP.REPORTED)
        test.assertNotIn("before", row)
        keys = list(row)
        test.assertLess(keys.index("not_verified"), keys.index("verified"), "not_verified comes first")
        if row["status"] == "read":
            reported = (row.get("reported_outcome") or {}).get("state")
            test.assertEqual(row["outcome"], R.outcome(row["verified"], row["not_verified"], reported))
            if row.get("reported_outcome"):   # operator-receipt/1's own outcome and response, as receipts.reported_outcome reads them
                test.assertEqual(set(row["reported_outcome"]), {"state", "words", "response"})
                reports.add(reported)
            test.assertIn(row["undo"]["words"], [*R.UNDO_WORDS.values(), "reversibility unknown"])
            states.add(row["outcome"]["state"])
        else:
            problems.append(row["problem"])
    test.assertEqual(states, set(R.OUTCOMES))
    test.assertEqual(reports, {"succeeded", "failed", "unknown"}, "a receipt of each reported outcome")
    unknown = next(row for row in rows if row.get("id") == "r-20261025-013")
    test.assertIn("system:S-9", unknown["items"], "an unknown outcome opens its tracker item")
    test.assertTrue(any("nested more than 64 levels deep" in p for p in problems))
    test.assertTrue(any("NaN is not JSON" in p for p in problems))
    test.assertTrue(any(p.startswith("could not be read (") for p in problems))
    for flag in ("gone", "changed_at", "duplicate_of", "harness_writable"):
        test.assertTrue(any(row.get(flag) for row in rows), flag)
    test.assertTrue(all({"file", "why"} <= set(r) <= {"file", "why", "unreadable"} for r in listing["refused"]))
    test.assertTrue(any("symbolic link" in r["why"] for r in listing["refused"]))
    times = [row.get("time") or row.get("mtime") for row in rows]
    test.assertEqual(times, sorted(times, reverse=True), "newest first")

    status, one = get("/api/operator/receipts/r-20261022-006")
    test.assertEqual((status, set(one)), (200, {"receipt", "duplicates"}))
    test.assertEqual(one["duplicates"], ["copies/r-20261022-006.json"])
    test.assertIn("before", one["receipt"])
    status, big = get("/api/operator/receipts/r-20261022-002")
    test.assertGreater(len(json.dumps(big["receipt"]["before"])), 4000, "a large before")
    test.assertEqual(test.request("GET", "/api/operator/receipts/r-unknown")[0], 404)
    test.assertEqual(test.request("GET", "/api/operator/receipts/bad%20id")[0], 400)
    status, _, raw = test.request("POST", "/api/operator/receipts/scan", {})
    test.assertEqual(status, 200)
    test.assertEqual(set(json.loads(raw)["scan"]), {"scanned_at", "receipts_dir", "read", "cannot_read", "refused", "past_limit",
                                                    "unreadable", "writable", "new", "changed", "gone", "items", "problem",
                                                    "skipped"})

    since = _iso(time.time() - 30 * 86400)
    status, act = get(f"/api/activity?agent=kate&since={since}")
    test.assertEqual(status, 200)
    test.assertEqual(set(act), {"agent", "operator", "since", "until", "entries", "count", "truncated", "next_after", "sources", "errors"})
    test.assertTrue(act["operator"])
    test.assertEqual(set(act["sources"]), set(A.SOURCES))
    test.assertTrue(all(act["sources"][name] for name in A.SOURCES), act["sources"])
    test.assertTrue(any((e.get("evidence") or {}).get("also") for e in act["entries"] if e["source"] == "receipt"))
    status, first = get(f"/api/activity?agent=kate&since={since}&limit=5")
    test.assertTrue(first["truncated"] and first["next_after"])
    status, second = get(f"/api/activity?agent=kate&since={since}&limit=500&after={first['next_after']}")
    test.assertEqual(first["entries"] + second["entries"], act["entries"], "after= continues where the first page ended")
    test.assertIsNone(second["next_after"])
    status, _, raw = test.request("GET", "/api/activity?agent=kate&after=bm90LWEtY3Vyc29y")
    test.assertEqual((status, json.loads(raw)["error"]), (400, "after must be the next_after value an activity query returned"))
    # the glue is activity.activity's: the same answer for an agent that is not the operator
    data_dir = Path(test.mock.STATE["area"][AREA]["dir"])
    reader = R.Receipts(lambda: SimpleNamespace(raw={}, path=Path(CONFIG_DIR)), data_dir)
    real = A.activity(SimpleNamespace(raw={}, data_dir=data_dir), agent="builder", since=since, receipts=reader)
    status, mine = get(f"/api/activity?agent=builder&since={since}")
    test.assertEqual(mine, shown_paths(SimpleNamespace(STATE=test.mock.STATE), real))
    test.assertTrue(all(e["evidence"]["file"].startswith(DATA_DIR + "/") for e in mine["entries"]), "paths under data_dir")

    # GET /api/custody and /api/drift-sources are answered by the routing and tracker areas, which come first in
    # gui_mock.AREAS; the operator's smoke steps route the page to this area's rows (/__mock/operator/...). Both
    # answers are held to the server's shapes; this area's own rows also cover every state.
    settings = C.CustodySettings(url="http://127.0.0.1:1")
    client = C.CustodyClient(settings)
    residency = C.Residency(resident=[C.Resident("p", "m")], generation=1)
    lease = C.Lease(client, C.Answer(True, "L-1", "h", time.time() + 60, None, 1, "OK"), provider="p", model="m", purpose="x",
                    holder="h", harness_provider="p", model_id="m", before=residency)
    view = set(DS.SourceSettings("x", "http://127.0.0.1:1", "f", None, None, ("1",)).view())
    import inspect
    from swfactory.orchestrator import Factory
    shown = inspect.getsource(Factory.custody)   # the row's keys as Factory.custody writes them
    row_keys = {"provider", "lazy_load", "allow_model_swap", "needs_lease", "custody_from", "held_wait_seconds", "settings", "register"}
    test.assertTrue(all(f'"{key}":' in shown for key in row_keys | {"providers", "reason_codes"}), "Factory.custody's keys")
    ops = set(re.findall(r'\._note\("([a-z_]+)"', inspect.getsource(C)))   # every op a register's history may hold
    for where in ("/api/", "/__mock/operator/"):
        status, custody = get(where + "custody")
        test.assertEqual(set(custody), {"providers", "reason_codes"})
        test.assertEqual(custody["reason_codes"], C.REFUSAL_WORDS, "every code in plain words (custody.REFUSAL_WORDS)")
        for row in custody["providers"]:
            test.assertEqual(set(row), row_keys)
            test.assertEqual(set(row["register"]), set(client.view()))
            test.assertLessEqual(set(row["settings"]), set(C.SETTINGS_KEYS))
            test.assertLessEqual(len({"token_file", "token_env", "token_credential"} & set(row["settings"])), 1, "one token source")
            if row["register"]["residency"]:
                test.assertEqual(set(row["register"]["residency"]), set(residency.view()))
            for held in row["register"]["leases"]:
                test.assertEqual(set(held), set(lease.view()))
                test.assertIn(held["state"], ("held", "lost", "unknown"))   # CustodyClient.release drops the lease first
            for line in row["register"]["history"]:
                test.assertIn(line["op"], ops)
                if line["op"] == "claim" and line["outcome"] == "denied" and line.get("words"):
                    test.assertEqual(line["words"], C.refusal_words(line["reason"], line.get("model")))
        status, drift = get(where + "drift-sources")
        for row in drift["sources"]:
            test.assertEqual(set(row), view | {"status", "detail", "checked_at", "observed_at", "valid_until", "schema_version", "next_at",
                                               "failures", "http", "last_result", "present", "items", "manifest"})
            test.assertIn(row["status"], DS.STATES)
    test.assertEqual({row["status"] for row in drift["sources"]}, set(DS.STATES))
    histories = [line for row in get("/__mock/operator/custody")[1]["providers"] for line in row["register"]["history"]]
    test.assertTrue(any(x["op"] == "claim" and x.get("reason") == "FULL" for x in histories), "a FULL denial")
    test.assertTrue({"waiting", "gave_up"} <= {x["outcome"] for x in histories if x["op"] == "wait"}, "a HELD wait and how it ended")
    test.assertTrue(any(x["op"] == "release" and str(x.get("reason", "")).startswith("not sent: ") for x in histories), "not sent")
    status, act = get(f"/api/activity?agent=kate&since={since}")
    kinds = {e["kind"] for e in act["entries"] if e["source"] == "custody"}
    test.assertTrue({"claim outcome unknown", "lease denied", "lease wait", "lease wait gave up"} <= kinds, kinds)

    def post(body):
        status, _, raw = test.request("POST", "/api/config", body)
        return status, json.loads(raw)

    status, err = post({"agents": {"seraphina": {"role": "operator"}}})
    test.assertEqual(status, 400)
    test.assertEqual(err["error"], OP.rule_problem({"agents": {"kate": {"role": "operator"}, "seraphina": {"role": "operator"}}}))
    test.assertEqual(post({"agents": {"seraphina": {"role": "operator"}}, "operator": {"several": ["kate", "seraphina"]}})[0], 200)
    test.assertEqual(get("/api/operator")[1]["operators"], ["kate", "seraphina"])
    test.request("POST", "/__mock/operator/scenario", {"name": "normal"})
    status, err = post({"agents": {"kate": {"role": None}, "builder": {"role": "operator"}}})   # the one-operator rule first
    test.assertEqual((status, err["error"]), (400, "agents.builder.role: builder is a factory agent, not a persona: only a "
                                                   "persona can be the operator"))
    status, err = post({"operator": {"receipts_dir": "/srv/ai-work/factory/receipts"}})
    test.assertEqual(status, 400)
    test.assertIn("overlaps the harness's data directory", err["error"])
    test.request("POST", "/__mock/operator/scenario", {"name": "normal"})
