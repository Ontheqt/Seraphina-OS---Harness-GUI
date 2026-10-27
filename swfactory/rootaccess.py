"""Who can act as root, the page (DESIGN.md section 42; docs/seraphina/YOUR_SERVER.md §8.1-§8.3, §8.5), phase P1c.
**View only.** The rows come from the helper's `access` verb (class private), which only Seraphina's own account may
call; this module keeps the owner's marks and turns the rows into the page. It changes nothing on the server: the
changes of §8.4 (removing a sudo rule, a group membership, an SSH key, locking an account) are P3 and are shown only as
disabled buttons with their reason.

The record, <data_dir>/yourserver/root.json (0600), holds no account name, key, path or comment: only each row's id
(a hash of its kind, subject and target), when it was first and last seen, when it went, and the owner's mark ("I know
about this") with his own note. A row is "new" when it first appeared after the first enumeration Seraphina kept; a
new row that is a way to take full control and is not marked raises one warning in Needs you, with fixed words (the
row's names stay in the cockpit).
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from pathlib import Path
from typing import Any, Callable

GROUPS = (("people", "People"), ("programs", "Programs"), ("doors", "Doors"), ("files", "Files"),
          ("escape", "Could matter if something escapes"))
TITLE = "Who can take full control of your server?"
HEADLINE = "Seraphina found {ways}."
HEADLINE_NEW = "{count} new since Seraphina last looked."
HEADLINE_TAIL = "Some kinds can't be checked from here."
NEVER = "Seraphina has not looked yet. Looking takes a few minutes and changes nothing."
RUNNING = "Seraphina is looking now. This takes a few minutes."
NEW_WAY = "A new way to take full control of your server appeared"
GONE = "Gone since {date}."
P3 = [{"id": "remove_rule", "label": "Remove this sudo rule"}, {"id": "remove_group", "label": "Take out of this group"},
      {"id": "remove_key", "label": "Remove this key"}, {"id": "lock", "label": "Lock this account"}]
P3_WHY = ("Changes to who can act as root come later: each will be approved with your own password in your terminal, "
          "will refuse to lock you out, and will undo itself unless you confirm it. Until then, make them in Cockpit's "
          "terminal.")
EXPLAIN = ("Until Seraphina has her own account, you can see the same list in your own terminal, with your own "
           "password. It takes a few minutes and changes nothing.")


def novice_strings() -> list[str]:
    return [TITLE, HEADLINE, HEADLINE_NEW, HEADLINE_TAIL, NEVER, RUNNING, NEW_WAY, GONE, P3_WHY, EXPLAIN,
            *(title for _, title in GROUPS), *(p["label"] for p in P3)]


def _iso(when: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when))


class RootRecord:
    """root.json: {first_scan_at, rows: {id: {first, last, gone, known, note}}}, folder 0700, file 0600."""

    def __init__(self, folder: Path, clock: Callable[[], float] = time.time) -> None:
        self.folder = folder
        self.clock = clock
        self._lock = threading.RLock()

    def load(self) -> dict:
        with contextlib.suppress(OSError, ValueError):
            doc = json.loads((self.folder / "root.json").read_text(encoding="utf-8"))
            if isinstance(doc, dict) and isinstance(doc.get("rows"), dict):
                return doc
        return {"first_scan_at": None, "rows": {}}

    def save(self, doc: dict) -> None:
        from .yourserver import _write_private

        _write_private(self.folder, "root.json", json.dumps(doc, sort_keys=True))

    def observe(self, rows: list[dict], at: float | None = None) -> dict:
        """One fresh enumeration: each row's first and last sight; rows not seen any more get their date of going."""
        with self._lock:
            doc = self.load()
            stamp = _iso(at if at is not None else self.clock())
            first = doc.get("first_scan_at") is None
            seen = set()
            for row in rows:
                entry = doc["rows"].setdefault(row["id"], {"first": stamp, "known": False, "note": None,
                                                           "first_scan": first})
                entry["last"] = stamp
                entry.pop("gone", None)
                seen.add(row["id"])
            for rid, entry in doc["rows"].items():
                if rid not in seen and not entry.get("gone"):
                    entry["gone"] = stamp
            if first:
                doc["first_scan_at"] = stamp
            self.save(doc)
            return doc

    def mark(self, rid: str, known: bool, note: str | None) -> dict:
        with self._lock:
            doc = self.load()
            if rid not in doc["rows"]:
                raise KeyError(f"row {rid}")
            doc["rows"][rid].update(known=bool(known), note=note or None, marked_at=_iso(self.clock()))
            self.save(doc)
            return doc["rows"][rid]


def page(result: dict | None, doc: dict, ways_word: Callable[[int], str]) -> dict:
    """The page from the last enumeration and the marks: {headline, groups: [{id, title, rows}], counts, cannot_check,
    unchecked, new_ways, at, sudo, gone: [...]}."""
    if not result:
        return {"title": TITLE, "headline": NEVER, "groups": [], "counts": None, "cannot_check": [], "at": None,
                "new_ways": 0}
    marks = doc.get("rows") or {}
    groups: dict[str, list[dict]] = {g: [] for g, _ in GROUPS}
    new_ways = 0
    for row in result.get("rows") or []:
        mark = marks.get(row["id"]) or {}
        is_new = bool(mark) and not mark.get("first_scan") and not mark.get("known")
        new_ways += 1 if is_new and row.get("way") else 0
        groups.setdefault(row.get("group") or "files", []).append(
            {"id": row["id"], "kind": row.get("kind"), "words": row.get("words"), "why": row.get("why"),
             "way": bool(row.get("way")), "escape": bool(row.get("escape")), "since": mark.get("first"),
             "known": bool(mark.get("known")), "note": mark.get("note"), "new": is_new,
             "p3": [p for p in P3 if _p3_fits(p["id"], row)]})
    present = {r["id"] for r in result.get("rows") or []}
    gone = [{"id": rid, "gone": entry.get("gone"), "known": bool(entry.get("known")), "note": entry.get("note")}
            for rid, entry in marks.items() if entry.get("gone") and rid not in present][:50]
    counts = result.get("counts") or {}
    headline = [HEADLINE.format(ways=ways_word(int(counts.get("ways") or 0)))]
    if new_ways:
        headline.append(HEADLINE_NEW.format(count=new_ways))
    headline.append(HEADLINE_TAIL)
    return {"title": TITLE, "headline": " ".join(headline),
            "groups": [{"id": g, "title": t, "rows": sorted(groups.get(g) or [], key=lambda r: (not r["way"],
                                                                                              not r["new"]))}
                       for g, t in GROUPS if groups.get(g)],
            "counts": counts, "cannot_check": result.get("cannot_check") or [], "unchecked": result.get("unchecked")
            or [], "at": result.get("at"), "new_ways": new_ways, "gone": gone, "sudo": result.get("sudo"),
            "p3_why": P3_WHY}


def _p3_fits(change: str, row: dict) -> bool:
    kind = row.get("kind")
    return {"remove_rule": kind == "sudo", "remove_group": kind == "group", "remove_key": kind == "ssh_key",
            "lock": kind in ("sudo", "group", "ssh_key") and row.get("group") == "people"}.get(change, False)


def explain_block(helper: str) -> dict:
    return {"id": "1", "title": "Paste this in your own terminal", "commands": [f"sudo {helper} explain access"],
            "expect": "Who can take full control of your server? Seraphina found ...", "note": EXPLAIN,
            "if_not": "It says what is wrong; nothing was changed."}
