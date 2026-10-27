"""Mock backend for the Backups area: DESIGN §41 "GUI interface notes (disk protection)" and
docs/seraphina/BACKUPS_SNAPRAID.md §12.6, phase P1 (status and guard, read-only). See tests/gui_mock/__init__.py
for the hooks.

GET /api/snapraid answers the card as swfactory/snapraid.py builds it (the light is computed by snapraid.light from
each state's facts, so tones, codes and words are the module's own). POST /api/snapraid/refresh {what} answers 202
{refreshing} and publishes snapraid.updated; GET /api/snapraid/jobs[/<J-n>?log=1&offset=N], GET
/api/snapraid/guard/<G-n>?disk=&kind=&q=&offset= (3,000 changes on two disks, some replaced, with hostile names: markup,
a right-to-left override, a 600-character name, colons and backslashes, and the escaped forms \\d and \\\\ shown as
names), GET /api/snapraid/files, GET /api/snapraid/setup and POST /api/snapraid/setup/stage|check, and POST /api/config
{snapraid: {enabled}} (the card's Turn it on).

States (POST /__mock/backups/<state>): absent, no_reader, off, not_set_up, unsafe, unreachable, never, healthy (the
seed), would_hold, interrupted, bad_blocks, failing_disk, missing_disk, health_unknown, outside_run, unsandboxed,
stale, damaged_list (the last status failed on a damaged file list: red, the last good facts kept) and
stale_failing (a failing disk read 5 hours ago: red stays, with the note), taking_out (a disk being taken out of
protection, amber) and with_care (SnapRAID 14.10 read with care, amber).

The SnapRAID Daemon route (BACKUPS_SNAPRAID.md §12.6): daemon_no_link (only /metrics: no link yet), daemon_healthy
(the link allows actions), daemon_read_only (the link allows reading only: no action button), daemon_auth_off,
daemon_beyond (reachable from other computers, with a password), daemon_down, daemon_frozen (25 files deleted
against a limit of 20; the review list holds 3,000 entries on two disks with hostile names), daemon_running (a run
in progress, Stop safely), daemon_unknown (an action's outcome unknown), daemon_lapsed (a skip undone by a restart),
daemon_two (two schedulers), daemon_report_failed, daemon_engine_old, daemon_damage (hidden damage: Repair it) and
daemon_skip (tonight's run skipped). The light is snapraidd.light over each state's daemon view. POST
/api/snapraid/actions answers a preview (refused in words where the module refuses: two schedulers, read only);
POST .../approve answers {outcome}, or 409 {error: changed} after POST /__mock/backups/changed; GET
/api/snapraid/review pages the change list; GET /api/snapraid/runs[/<number>] the daemon's runs and one step.

The checklist for a failed disk (BACKUPS_SNAPRAID.md §9.4, P4): daemon_failing (disk 2 missing on the protection
service's route; failing_disk is the reader's). GET /api/snapraid/rebuild, POST /api/snapraid/rebuild {disk} and
POST /api/snapraid/rebuild/step|prepare|close answer with swfactory/snapraid_rebuild.py's own build, advance and
prepare over placeholder facts: the current step's check fails until POST /__mock/backups/rebuild_good, which makes
that step's facts what the owner's blocks would leave (the service off, the fstab line turned off, the new disk
mounted, the rebuild ended with some files not back, the service on again). POST /__mock/backups/rebuild_mounted
leaves the new disk mounted with the reader not told yet (its block 4e still to run). POST
/__mock/backups/rebuild_at {step} opens a checklist at that step (for screenshots).
"""

from __future__ import annotations

import json
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from swfactory import snapraid, snapraid_rebuild, snapraidd

from . import store

AREA = "backups"
READER_STATES = ("absent", "no_reader", "off", "not_set_up", "unsafe", "unreachable", "never", "healthy", "would_hold",
                 "interrupted", "bad_blocks", "failing_disk", "missing_disk", "health_unknown", "outside_run",
                 "unsandboxed", "stale", "damaged_list", "stale_failing", "taking_out", "with_care")
DAEMON_STATES = ("daemon_no_link", "daemon_healthy", "daemon_read_only", "daemon_auth_off", "daemon_beyond",
                 "daemon_down", "daemon_frozen", "daemon_running", "daemon_unknown", "daemon_lapsed", "daemon_two",
                 "daemon_report_failed", "daemon_engine_old", "daemon_damage", "daemon_skip", "daemon_failing")
STATES = READER_STATES + DAEMON_STATES
CFG = {"stale_minutes": 180, "light": {"amber_after_hours": 36, "red_after_days": 7, "fp_amber_percent": 20,
                                       "smart_unreadable_ok": []}, "daemon": dict(snapraidd.DEFAULTS)}
HOSTILE = ["<img src=x onerror=alert('backups')>.jpg", "invoice‮gpj.exe", "x" * 600 + ".txt", "a:b.txt",
           "back\\slash.txt", "esc\\daped.txt", "two\\\\slashes.txt", "line\nbreak.txt"]


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _changes() -> list[dict]:
    """3,000 changes on two disks: removals under one folder (most), updates, moves, additions, replaced files and
    hostile names."""
    out = []
    for i in range(1800):
        out.append({"disk": "d1", "kind": "removed", "path": f"Photos/2019/IMG_{i:04d}.jpg", "size": 2_400_000 + i})
    for i in range(600):
        out.append({"disk": "d2", "kind": "updated", "path": f"Music/Album {i // 12}/track {i % 12:02d}.flac",
                    "size": 31_000_000})
    for i in range(300):
        out.append({"disk": "d2", "kind": "moved", "path": f"Films/new/film {i}.mkv", "from": f"Films/film {i}.mkv"})
    for i in range(280):
        entry = {"disk": "d1", "kind": "removed", "path": f"Films/old cut {i}.mkv", "size": 4_000_000_000}
        if i < 120:
            entry["replaced_by"] = f"Films/better cut {i}.mkv"
        out.append(entry)
    for i, name in enumerate(HOSTILE):
        out.append({"disk": "d1" if i % 2 else "d2", "kind": "removed", "path": f"Notes/{name}", "size": 1000 + i})
    while len(out) < 3000:
        out.append({"disk": "d2", "kind": "added", "path": f"Downloads/file {len(out)}.zip"})
    return out


def _counts(changes: list[dict]) -> dict:
    disks: dict[str, dict] = {}
    for name, files in (("d1", 9000), ("d2", 12000), ("d3", 800)):
        disks[name] = {"added": 0, "removed": 0, "updated": 0, "moved": 0, "copied": 0, "restored": 0, "replaced": 0,
                       "lost": 0, "files": files}
    for entry in changes:
        row = disks[entry["disk"]]
        row[entry["kind"]] += 1
        if entry["kind"] in ("removed", "updated"):
            row["lost"] += 1
        if entry.get("replaced_by"):
            row["replaced"] += 1
    array = {key: sum(row[key] for row in disks.values()) for key in
             ("added", "removed", "updated", "moved", "copied", "restored", "replaced", "lost", "files")}
    array["changed"] = sum(array[k] for k in ("added", "removed", "updated", "moved", "copied", "restored"))
    return {"disks": disks, "array": array}


CHECK_WORDS = {"G1": "The reader, SnapRAID and both configs are installed safely, and the config is valid.",
               "G2": "Nothing else is running.", "G3": "Every disk is mounted with the filesystem recorded at setup.",
               "G4": "All 3 copies of the file list are present, the same size, on different disks and with room.",
               "G5": "No hidden damage is recorded.", "G6": "SMART reports every disk healthy.",
               "G7": "Not checked in this version: whether the folders you mark as live app data are left out of the "
                     "protection comes with a later one.",
               "G8": "No unfinished run is recorded."}


def _guard(now: float, held: bool) -> dict:
    changes = _changes() if held else []
    checks = [{"id": key, "result": "skipped" if key == "G7" else "pass", "words": words}
              for key, words in CHECK_WORDS.items()]
    checks.append({"id": "G9", "result": "pass", "words": f"{len(changes)} change(s) found." if held else
                   "Nothing changed since the last run: there is nothing to protect.", **({} if held else
                                                                                         {"nothing_to_do": True})})
    if held:
        checks.append({"id": "G10", "result": "hold", "reason": "lost_over", "disks": ["d1"],
                       "words": "A run now would lose the protected version of 2,089 of 9,000 files on disk 1: over "
                                "the limits."})
        checks.append({"id": "G11", "result": "pass", "words": "The protection disk has room for what was added."})
    else:
        checks.append({"id": "G10", "result": "pass", "words": "0 file(s) would be lost if protected now: under the "
                                                               "limits."})
        checks.append({"id": "G11", "result": "pass", "words": "The protection disk has room for what was added."})
    return {"guard": "G-7" if held else "G-6", "at": _iso(now - 1200), "verdict": "would_hold" if held else
            "nothing_to_do", "reasons": ["lost_over"] if held else [], "counts": _counts(changes), "checks": checks,
            "first_run": False, "list_sha256": "0" * 64, "numbers": {"d1": 1, "d2": 2, "d3": 3}, "report_only": True,
            "runs": ["J-40", "J-41"], "_changes": changes}


def _facts(state: str, now: float) -> dict:
    """The panel's facts for a state (what SnapraidPanel.facts() would hold), from which the real light is computed."""
    morning = now - 3 * 3600
    disks = {"d1": {"files": 9000, "blocks": 120000, "free_blocks": 400000, "file_size": 7_800_000_000},
             "d2": {"files": 12000, "blocks": 150000, "free_blocks": 380000, "file_size": 9_100_000_000},
             "d3": {"files": 800, "blocks": 9000, "free_blocks": 500000, "file_size": 600_000_000}}
    status = {"array_empty": state == "never", "block_size": 262144, "parity_block_free_min": 350000,
              "parity": {"parity": {"total": 520000, "free": 350000}}, "file_count": 21800,
              "disks": {} if state == "never" else disks, "has_unsynced": 812 if state == "interrupted" else 0,
              "has_unscrubbed": 0, "has_bad": {"count": 3 if state == "bad_blocks" else 0, "first": 10, "last": 99},
              "histogram": [{"time": int(morning), "blocks": 280000, "kind": "scrubbed"}],
              "oldest_scrubbed": int(now - 12 * 86400), "newest_block": None if state == "never" else int(morning),
              "unscrubbed_blocks": 0, "blocks": 280000}
    mounts = {name: {"role": "data", "number": number, "path": f"/mnt/disk{number}", "mounted": True,
                     "fstype": "ext4", "uuid_ok": True} for name, number in (("d1", 1), ("d2", 2), ("d3", 3))}
    mounts["parity"] = {"role": "parity", "number": 1, "path": "/mnt/parity1", "mounted": True, "fstype": "ext4",
                        "uuid_ok": True}
    if state == "missing_disk":
        mounts["d2"] = {**mounts["d2"], "mounted": False, "fstype": None, "uuid_ok": False}
    if state == "taking_out":
        mounts["d3"] = {**mounts["d3"], "path": "/var/lib/snapraid-out/d3", "placeholder": True, "empty": True}
    health = {name: {"health": "ok", "afr": 0.05, "probability": 0.05, "errors": 0}
              for name in ("d1", "d2", "d3", "parity")}
    if state in ("failing_disk", "stale_failing"):
        health["d2"] = {"health": "fail", "afr": 0.9, "probability": 0.6, "errors": 12}
    if state == "health_unknown":
        health = {name: {"health": "unknown", "why": "SMART is not supported on this machine"} for name in health}
    observed = now - (5 * 3600 if state in ("stale", "stale_failing") else 300)
    content = {"copies": [], "problem": None, "space_low": False, "other": False, "too_few_disks": False,
               "read_failed": None, "error": None}
    failed = None
    if state == "damaged_list":     # the last status failed: the reader's fresh file list report, the old facts kept
        content.update(problem="The protection's file list has a problem: SnapRAID could not read the copy of the "
                               "file list it opens first: it is damaged, cut short or unreadable.", other=True,
                       read_failed="SnapRAID could not read the copy of the file list it opens first: it is damaged, "
                                   "cut short or unreadable",
                       error="Error decoding '/var/snapraid/snapraid.content' at offset 84")
        failed = {"at": _iso(observed), "job": "J-42", "outcome": "failed",
                  "words": "Failed: Error decoding '/var/snapraid/snapraid.content' at offset 84"}
    facts = {"status": {"at": _iso(observed), "job": "J-41", "outcome": "ok", "words": "Done", "facts": status,
                        "facts_at": _iso(observed - (86400 if failed else 0)), "failed": failed,
                        "mounts": mounts, "content": content, "numbers": {"d1": 1, "d2": 2, "d3": 3}},
             "smart": {"at": _iso(observed), "outcome": "ok", "unsupported": state == "health_unknown",
                       "disks": health},
             "probe": {"at": _iso(observed), "disks": {"d1": "standby", "d2": "active", "d3": "standby",
                                                       "parity": "standby"}},
             "errors": {}}
    if state in ("would_hold",):
        facts["guard"] = {key: value for key, value in _guard(now, True).items() if key != "_changes"}
    elif state not in ("never",):
        facts["guard"] = {key: value for key, value in _guard(now, False).items() if key != "_changes"}
    if state == "unreachable":
        facts["errors"]["status"] = {"at": _iso(now - 60), "code": "sudo_refused",
                                     "words": "sudo would not run the reader. Check that its sudoers line is "
                                              "installed (the setup page has the copy block)."}
    if state == "with_care":
        facts["info"] = {"snapraid": {"version": "14.10", "read_only": True,
                                      "note": "read with care: some facts may be missing"}}
    if state == "unsafe":   # the reader refuses every verb: info and status fail alike
        for verb in ("info", "status"):
            facts["errors"][verb] = {"at": _iso(now - 60), "code": "unsafe_install",
                                     "words": "/etc/snapraid.conf can be changed by users other than root."}
    return facts


def _daemon(st: dict, now: float) -> tuple[dict, list]:
    """The daemon part of the card for a daemon state, with DaemonRoute.view()'s keys, and the approvals."""
    state = st["state"]
    morning = now - 3 * 3600
    window = snapraidd.next_window("02:00", now)
    limits = {"deletes": 20, "updates": 50}
    disks = [{"name": name, "key": key, "label": label, "role": role, "health": "passed"} for name, key, label, role in
             (("d1", "1", "Disk 1", "data"), ("d2", "2", "Disk 2", "data"), ("parity", "p1", "The protection disk",
                                                                             "parity"))]
    if state == "daemon_failing":
        disks[1]["health"] = "degraded"
    allow = ["read"] if state == "daemon_read_only" else ["read", "act", "accept"]
    link = state != "daemon_no_link"
    view = {
        "answering": state != "daemon_down", "seen": True,
        "down_since": now - 45 * 60 if state == "daemon_down" else None,
        "error": "the protection service did not answer (ConnectionRefusedError)" if state == "daemon_down" else None,
        "auth_on": state != "daemon_auth_off", "listen": {"beyond_loopback": state == "daemon_beyond"},
        "engine": "14.10" if state == "daemon_engine_old" else "15.1", "version": "2.1",
        "started_at": now - 86400, "restarted_at": now - 600 if state == "daemon_lapsed" else None,
        "window": {"next": window, "words": snapraidd.window_words(window, now), "source": "the protection service",
                   "schedule": "02:00"},
        "hold_off": state == "daemon_skip", "lapsed": {"at": now - 600, "approval": "A-3"}
        if state == "daemon_lapsed" else None,
        "frozen": {"counts": {"removed": 25, "updated": 3, "added": 140}, "limits": limits, "which": ["deletes"],
                   "since": now - 5 * 3600} if state == "daemon_frozen" else None,
        "active": {"command": "sync", "high_command": "maintenance", "progress": 37, "eta": 540, "elapsed": 120}
        if state == "daemon_running" else None,
        "queue": 2 if state == "daemon_running" else 0, "protected_up_to": morning, "partial": False,
        "last_run": {"verdict": "frozen" if state == "daemon_frozen" else "done", "high_command": "maintenance",
                     "finished_at": morning, "numbers": [3, 4, 5, 6, 7]},
        "schedulers": {"count": 2 if state == "daemon_two" else 1, "window": "02:00",
                       "places": [{"place": "/etc/systemd/system/backup.timer", "kind": "unit", "schedule": [],
                                   "sha256": "a" * 64, "counted": True}] if state == "daemon_two" else []},
        "link": {"installed": link, "allow": allow if link else None, "words": None if link else snapraidd.NO_LINK},
        "report_failed": state == "daemon_report_failed", "outside": None,
        "health": "corrupt" if state == "daemon_damage" else "degraded" if state == "daemon_failing" else "passed",
        "blocks_bad": 3 if state == "daemon_damage" else 0, "disks": disks, "limits": limits,
        "limits_line": snapraidd.limits_line(limits),
        "lab": [{**check, "status": "not_run"} for check in snapraidd.LAB_CHECKS],
        "offers": {},
    }
    act = link and state not in ("daemon_auth_off", "daemon_engine_old", "daemon_read_only")
    # no run while a disk is being replaced (§9.1) or while a disk is missing (the service would cancel it)
    one = state not in ("daemon_two", "daemon_failing") and snapraid_rebuild.current(st.get("rebuild")) is None
    view["offers"] = {"stop": act and state == "daemon_running", "hold_off": act,
                      "maintenance": act and one and state != "daemon_running", "accept": act and one and
                      state == "daemon_frozen", "undelete": act and one, "heal": act and one and state == "daemon_damage"}
    actions = list(st.get("actions") or [])
    if state == "daemon_unknown" and not actions:
        actions = [{"approval": "A-2", "action": "maintenance", "state": "sent", "outcome": "unknown",
                    "outcome_words": "outcome unknown: it may have happened; check before retrying",
                    "words": "Seraphina could not tell whether it happened: no answer after the request was sent.",
                    "shows": {}, "sends": {"action": "maintenance", "body": {}}, "binding": {}, "shown_sha256": "0" * 64,
                    "expires_at": now, "typed": False, "run": None}]
    return view, actions


def _daemon_view(core, st: dict) -> dict:
    """GET /api/snapraid on the daemon route: the reader's card with route, daemon and actions."""
    now = time.time()
    st_reader = {**st, "state": "healthy"}
    view = _view(core, st_reader, daemon_state=st["state"])
    daemon, actions = _daemon(st, now)
    unknown = any(a.get("outcome") == "unknown" and a.get("state") == "sent" for a in actions)
    light = snapraid_rebuild.light(st.get("rebuild")) or snapraidd.light(daemon, CFG, now, unknown_open=unknown)
    route = "metrics" if st["state"] == "daemon_no_link" else "daemon"
    window = daemon["window"]["words"]
    undo = (f"{window[:1].upper()}{window[1:]}'s run will be skipped. A restart of the protection service undoes this."
            if daemon["hold_off"] else f"Deleted something by mistake? It can be brought back until the next "
                                       f"protection run: {window}.")
    view.update(route=route, daemon=daemon, actions=actions, light=light, undo_line=undo, phase=snapraid.PHASE,
                lab=[], lab_line=None)
    return view


def _view(core, st: dict, daemon_state: str | None = None) -> dict:
    now = time.time()
    state = st["state"]
    if state in DAEMON_STATES:
        return _daemon_view(core, st)
    phase_state = {"absent": "absent", "no_reader": "no_reader", "off": "off"}.get(state, "ready")
    facts = _facts(state, now)
    if phase_state != "ready":
        light = {"tone": "muted", "code": state, "reason": snapraid.REASONS[state]}
    elif state == "not_set_up":
        light = {"tone": "muted", "code": "not_set_up", "reason": snapraid.REASONS["not_set_up"]}
    else:
        light = snapraid.light(facts, CFG, now, outside_open=state == "outside_run")
        if state == "taking_out":
            light = {"tone": "warn", "code": "taking_out", "reason": snapraid.REASONS["taking_out"].format(disk="Disk 3")}
    ready = phase_state == "ready" and state != "not_set_up"
    status = facts["status"]
    disks = []
    if ready:
        for name, mount in status["mounts"].items():
            entry = status["facts"]["disks"].get(name) or {}
            health = facts["smart"]["disks"].get(name) or {}
            disks.append({"name": name, "number": mount["number"] if mount["role"] == "data" else None,
                          "level": 1 if mount["role"] == "parity" else None,
                          "label": snapraid.disk_label(name, status["numbers"], 1),
                          "role": mount["role"], "mount": mount["path"], "mounted": mount["mounted"],
                          "uuid_ok": mount["uuid_ok"], "files": entry.get("files"),
                          "free_bytes": entry["free_blocks"] * 262144 if entry.get("free_blocks") else None,
                          "health": health.get("health"), "probability": health.get("probability"),
                          "power": facts["probe"]["disks"].get(name), "model": "Example Disk 8TB",
                          "serial": f"SN-EXAMPLE-{mount['number']}", "by_id": None})
    words = snapraid.fact_words(facts, CFG, now) if ready else []
    view = {
        "phase": snapraid.PHASE, "state": phase_state, "installed": state != "absent",
        "route": "none" if state in ("absent",) else "reader", "daemon": None, "actions": [],
        "waiting": None, "waiting_words": None,
        "version_note": "read with care: some facts may be missing" if state == "with_care" else None,
        "snapraid": {"binary": None if state == "absent" else "/usr/local/bin/snapraid",
                     "version": None if state == "absent" else "13.0",
                     "configs": {"/etc/snapraid.conf": state != "absent"}, "readable_disks": 3},
        "helper": {"path": snapraid.HELPER, "installed": state not in ("absent", "no_reader"),
                   "sha256": "ab" * 32 if ready else None, "release_sha256": "ab" * 32,
                   "matches_release": True if ready else None, "error": facts["errors"].get("info") or
                   facts["errors"].get("status")},
        "enabled": state != "off", "allowed": ["read"], "light": light, "sentences": list(snapraid.SENTENCES),
        "undo_line": snapraid.WATCHING,
        "facts": {"status": status["facts"] if ready else None, "observed_at": status["at"] if ready else None,
                  "facts_at": status["facts_at"] if ready else None, "failed": status["failed"] if ready else None,
                  "stale": state in ("stale", "stale_failing"),
                  "diff": (facts.get("guard") or {}).get("counts") if ready else None,
                  "smart": facts["smart"] if ready else None,
                  "last_run": {"at": _iso(status["facts"]["newest_block"]), "inferred": True}
                  if ready and status["facts"]["newest_block"] else None,
                  "list": {"at": status["at"], "job": "J-40", "good": True, "files": 21800} if ready else None,
                  "words": words},
        "disks": disks, "job": None, "guard": facts.get("guard") if ready else None, "schedule": None,
        "scrub_plan": None, "not_covered": snapraid.not_covered(facts) if ready else list(snapraid.NOT_COVERED),
        "set_up": state != "not_set_up",
        "conf_ok": state not in ("not_set_up",), "conf_problems": [],
        "schedulers": [{"place": "/etc/cron.d/snapraid", "kind": "cron", "via": "direct",
                        "schedule": ["30", "3", "*", "*", "*"]}] if state == "healthy" else [],
        "outside": [{"seen_at": _iso(now - 900), "modified": _iso(now - 3600)}] if state == "outside_run" else [],
        "warnings": [{"code": "agents_unsandboxed", "words": snapraid.SANDBOX_LINE}] if state == "unsandboxed" else [],
        "lab": [{**check, "status": "not_run", "why": snapraid.LAB_WHY} for check in snapraid.LAB_CHECKS],
        "lab_line": snapraid.LAB_LINE, "lab_words": snapraid.LAB_WORDS,
        "versioned": {"set_up": False, "words": snapraid.NO_VERSIONED},
        "refreshing": st.get("refreshing"), "errors": facts["errors"],
    }
    if state == "with_care":
        view["light"] = {"tone": "warn", "code": "read_with_care",
                         "reason": snapraid.REASONS["read_with_care"].format(version="14.10")}
    view["rebuild"] = snapraid_rebuild.summary(st.get("rebuild")) if view["enabled"] else None
    if snapraid_rebuild.light(st.get("rebuild")) is not None:
        view["light"] = snapraid_rebuild.light(st["rebuild"])
    return view


def seed(core, fresh: bool) -> None:
    st = store(core, AREA)
    st.clear()
    st.update(state="absent" if fresh else "healthy", refreshing=None, staged=False, link_staged=False, actions=[],
              changed=False, reviewed=False, rebuild=None, rb_good=[])


# ---------------------------------------------------------------- the checklist for a failed disk (§9.4)

RB_U = {"d1": "11111111-1111-4111-8111-111111111111", "d2": "22222222-2222-4222-8222-222222222222",
        "parity": "33333333-3333-4333-8333-333333333333", "new": "44444444-4444-4444-8444-444444444444"}
RB_OLD_ID = "/dev/disk/by-id/ata-Example_Disk_8TB_SN-EXAMPLE-2"
RB_NEW_ID = "/dev/disk/by-id/ata-Example_Disk_8TB_SN-EXAMPLE-NEW1"
RB_USED_ID = "/dev/disk/by-id/usb-Example_Portable_SN-EXAMPLE-USED9-0:0"
RB_FSTAB = (f"UUID={RB_U['d1']} /mnt/disk1 ext4 defaults,nofail,x-systemd.device-timeout=30s 0 2\n"
            f"UUID={RB_U['d2']} /mnt/disk2 ext4 defaults 0 2\n"
            f"UUID={RB_U['parity']} /mnt/parity1 ext4 defaults,nofail,x-systemd.device-timeout=30s 0 2\n")


def _rb_unit(state: str, status: int = 0, at: int = 1) -> dict:
    """systemctl show's properties; a unit that ran started `at` seconds after the boot and exited a second later."""
    return {"absent": {"LoadState": "not-found", "ActiveState": "inactive"},
            "on": {"LoadState": "loaded", "ActiveState": "active", "SubState": "running", "UnitFileState": "enabled"},
            "off": {"LoadState": "loaded", "ActiveState": "inactive", "SubState": "dead", "UnitFileState": "disabled"},
            "running": {"LoadState": "loaded", "ActiveState": "active", "SubState": "running", "ExecMainCode": "0"},
            "ended": {"LoadState": "loaded", "ActiveState": "failed" if status else "active",
                      "SubState": "failed" if status else "exited", "ExecMainCode": "1",
                      "ExecMainStatus": str(status), "ExecMainStartTimestampMonotonic": str(at * 10 ** 6),
                      "ExecMainExitTimestampMonotonic": str((at + 1) * 10 ** 6)}}[state]


def _rb_facts(st: dict) -> dict:
    """Placeholder facts for the checklist: disk 2 failed; each step "good" once POST /__mock/backups/rebuild_good
    made it so (what the owner's blocks would leave)."""
    now = time.time()
    route = "daemon" if st["state"] in DAEMON_STATES else "reader"
    record = st.get("rebuild") or {}
    good = set(st.get("rb_good") or []) | set((record.get("done") or {}))
    stopped, fitted = "stop" in good, "disk" in good
    mounted = fitted or "mounted" in good          # block 4d ran: the new disk mounted, the reader not told (4e)
    mounts = {"d1": {"role": "data", "number": 1, "path": "/mnt/disk1", "mounted": True, "uuid_ok": True,
                     "fstype": "ext4"},
              "d2": {"role": "data", "number": 2, "path": "/mnt/disk2", "mounted": mounted, "uuid_ok": fitted,
                     "fstype": "ext4" if mounted else None},
              "d3": {"role": "data", "number": 3, "path": "/mnt/disk3", "mounted": True, "uuid_ok": True,
                     "fstype": "ext4"},
              "parity": {"role": "parity", "number": 1, "path": "/mnt/parity1", "mounted": True, "uuid_ok": True}}
    fstab = RB_FSTAB
    if "boot" in good:
        fstab = fstab.replace(f"UUID={RB_U['d2']} /mnt/disk2", f"# UUID={RB_U['d2']} /mnt/disk2")
    if mounted:
        fstab += f"UUID={RB_U['new']} /mnt/disk2 ext4 defaults,nofail,x-systemd.device-timeout=30s 0 2\n"
    spare = [] if fitted else [row for row in (
        {"device": "/dev/sdx", "by_id": RB_NEW_ID, "size": 8_001_000_000_000, "model": "Example Disk 8TB",
         "serial": "SN-EXAMPLE-NEW1", "signature": False},
        {"device": "/dev/sdy", "by_id": RB_USED_ID, "size": 4_000_000_000_000, "model": "Example Portable",
         "serial": "SN-EXAMPLE-USED9", "signature": True}) if not (mounted and row["by_id"] == RB_NEW_ID)]
    disks = {"d2": {"model": "Example Disk 8TB", "serial": "SN-EXAMPLE-NEW1" if fitted else "SN-EXAMPLE-2",
                    "by_id": RB_NEW_ID if fitted else RB_OLD_ID, "size": 8_000_000_000_000, "present": fitted,
                    "seen_at": _iso(now - 2 * 86400), "fstype": "ext4", "mount": "/mnt/disk2"}}
    rebuilt = "rebuild" in good
    back = "done" in good
    daemon = None
    if route == "daemon":
        daemon = {"answering": back or not stopped, "active": None, "hold_off": bool(st.get("rb_skipped")),
                  "health": "passed" if back else "degraded",
                  "disk_health": {"d1": "passed", "d2": "passed" if back else "degraded", "d3": "passed"},
                  "window": snapraidd.window_words(snapraidd.next_window("02:00", now), now),
                  "offers": {"hold_off": True}, "protected_up_to": now - 3 * 3600}
    return {"route": route, "reader": {},
            "info": {"conf": "/etc/snapraid.conf", "binary": "/usr/local/bin/snapraid", "mounts": mounts,
                     "uuids": {"d1": RB_U["d1"], "d2": RB_U["new"] if fitted else RB_U["d2"], "d3": RB_U["d1"][:-1]
                               + "3", "parity": RB_U["parity"]},
                     "data": ["/mnt/disk1", "/mnt/disk2", "/mnt/disk3"],
                     "schedulers": ["/etc/cron.d/snapraid"] if route == "reader" else [], "snapraid_running": False},
            "disks": disks, "spare": spare, "daemon": daemon,
            "smart": {"d1": "ok", "d2": "fail", "d3": "ok"} if route == "reader" else None,
            "units": {snapraid_rebuild.DAEMON_UNIT: _rb_unit("on" if back or not stopped else "off")
                      if route == "daemon" else _rb_unit("absent"),
                      snapraid_rebuild.REBUILD_UNIT: _rb_unit("ended", 1) if rebuilt else _rb_unit("absent"),
                      snapraid_rebuild.CHECK_UNIT: _rb_unit("ended", 1, at=20) if rebuilt else _rb_unit("absent")},
            "snapraid_running": False, "fstab": fstab, "stacks": ["photos", "music"], "power_off": "23:30",
            "protected_up_to": now - 3 * 3600}


def _rebuild_api(core, handler, method, rest, body, st):
    now = time.time()
    facts = _rb_facts(st)
    record = st.get("rebuild")
    try:
        if method == "GET" and rest == ["rebuild"]:
            return handler.send_json(snapraid_rebuild.build(record, facts, now))
        if method == "POST" and rest == ["rebuild"]:
            if record and record.get("state") == "open":
                raise ValueError(snapraid_rebuild.START_REFUSALS["open"])
            number = int(str((record or {}).get("id", "R-0"))[2:]) + 1
            st["rebuild"] = snapraid_rebuild.open_record(facts, str((body or {}).get("disk") or ""), now, number)
            st["rb_good"] = []
            core.BUS.publish("snapraid.rebuild", {"checklist": st["rebuild"]["id"], "state": "open", "step": "stop"})
            return handler.send_json(snapraid_rebuild.build(st["rebuild"], _rb_facts(st), now), status=201)
        if not record or record.get("state") != "open":
            raise ValueError(snapraid_rebuild.CLOSED)
        if method == "POST" and rest == ["rebuild", "step"]:
            record, result = snapraid_rebuild.advance(record, facts, str((body or {}).get("step") or ""),
                                                      list((body or {}).get("confirm") or []), now)
            st["rebuild"] = record
            core.BUS.publish("snapraid.rebuild", {"checklist": record["id"], "state": record["state"],
                                                  "step": snapraid_rebuild.current(record)})
            page = snapraid_rebuild.build(record, _rb_facts(st), now)
            page["result"] = {"step": body.get("step"), "done": result["done"], "words": result["words"],
                              "ok": result["check"]["ok"], "code": result["check"]["code"],
                              "missing": result["missing"]}
            return handler.send_json(page)
        if method == "POST" and rest == ["rebuild", "prepare"]:
            return handler.send_json(snapraid_rebuild.prepare(record, facts, str((body or {}).get("by_id") or ""),
                                                              str((body or {}).get("typed") or ""), now))
        if method == "POST" and rest == ["rebuild", "close"]:
            record.update(state="cancelled", closed_at=now)
            core.BUS.publish("snapraid.rebuild", {"checklist": record["id"], "state": "cancelled", "step": None})
            return handler.send_json(snapraid_rebuild.build(record, facts, now))
    except ValueError as exc:
        raise core.Invalid(str(exc)) from None
    raise KeyError("/".join(rest))


def api(core, handler, method, parts, query, body):
    if parts[:1] != ["snapraid"] and not (parts == ["config"] and method == "POST" and isinstance(body, dict)
                                          and set(body) == {"snapraid"}):
        return NotImplemented
    st = store(core, AREA)
    if parts == ["config"]:
        if (body.get("snapraid") or {}).get("enabled") is True and st["state"] == "off":
            st["state"] = "healthy"
        core.BUS.publish("snapraid.updated", {"tone": "ok", "code": "ok"})
        return handler.send_json({"saved": True})
    rest = parts[1:]
    if method == "GET" and rest == []:
        return handler.send_json(_view(core, st))
    if method == "POST" and rest == ["refresh"]:
        what = (body or {}).get("what")
        if what not in ("status", "health", "diff"):
            raise core.Invalid("give {what: status|health|diff}")
        if st["state"] in ("absent", "no_reader", "off"):
            raise core.Invalid("disk protection is not being read (see its card)")
        st["refreshing"] = None
        core.BUS.publish("snapraid.updated", {"tone": "ok", "code": "ok", "refreshing": None})
        return handler.send_json({"refreshing": what}, status=202)
    if method == "GET" and rest == ["jobs"]:
        return handler.send_json({"jobs": [_job("J-41", "status"), _job("J-40", "list")]})
    if method == "GET" and len(rest) == 2 and rest[0] == "jobs":
        if rest[1] not in ("J-40", "J-41"):
            raise KeyError(rest[1])
        out = {"job": _job(rest[1], "status" if rest[1] == "J-41" else "list")}
        if query.get("log") in ("1", "true"):
            text = _output()
            offset = int(query.get("offset") or 0)
            data = text.encode("utf-8", "surrogateescape")
            chunk = data[offset:offset + 4096]
            out["log"] = {"text": chunk.decode("utf-8", "replace"), "offset": offset, "next_offset": offset + len(chunk),
                          "size": len(data), "more": offset + len(chunk) < len(data)}
        return handler.send_json(out)
    if method == "GET" and len(rest) == 2 and rest[0] == "guard":
        return handler.send_json(_guard_page(st, rest[1], query))
    if method == "GET" and rest == ["files"]:
        return handler.send_json({"index": {"at": _iso(time.time() - 3600), "job": "J-40"}, "total": len(HOSTILE),
                                  "offset": 0, "entries": [{"disk": "d1", "path": f"Notes/{n}", "size": 1000,
                                                            "mtime": 1792900000} for n in HOSTILE], "folders": []})
    if method == "GET" and rest == ["setup"]:
        return handler.send_json(_setup(st))
    if method == "POST" and rest == ["setup", "stage"]:
        st["staged"] = True
        return handler.send_json(_setup(st))
    if method == "POST" and rest == ["setup", "check"]:
        return handler.send_json({"ok": True, "words": "The reader answers: SnapRAID 13.0, a valid config, reading "
                                                       "only."})
    if method == "POST" and rest == ["setup", "link-stage"]:
        st["link_staged"] = True
        return handler.send_json(_setup(st))
    if method == "POST" and rest == ["setup", "link-check"]:
        return handler.send_json({"ok": True, "words": "The link answers: the password is on, SnapRAID 15.1, "
                                                       "allowing read."})
    if rest[:1] == ["rebuild"]:
        return _rebuild_api(core, handler, method, rest, body, st)
    return _daemon_api(core, handler, method, rest, query, body, st)


def _daemon_api(core, handler, method, rest, query, body, st):
    now = time.time()
    if method == "POST" and rest == ["actions"]:
        return handler.send_json(_preview(core, st, body or {}, now), status=201)
    if method == "GET" and rest == ["actions"]:
        return handler.send_json({"actions": st.get("actions") or []})
    if len(rest) == 3 and rest[0] == "actions" and method == "POST":
        record = next((a for a in st.get("actions") or [] if a["approval"] == rest[1]), None)
        if record is None:
            raise KeyError(rest[1])
        if rest[2] == "cancel":
            record["state"] = "cancelled"
            return handler.send_json(record)
        if rest[2] != "approve":
            raise KeyError(rest[2])
        if record["state"] != "preview":
            raise core.Invalid(snapraidd.REFUSALS["used"])
        if (body or {}).get("shown_sha256") != record["shown_sha256"]:
            raise core.Invalid(snapraidd.REFUSALS["mismatch"])
        if record.get("typed") and (body or {}).get("typed") != 30:
            raise core.Invalid(snapraidd.REFUSALS["typed"])
        if st.get("changed"):
            record.update(state="refused", outcome="failed", words=snapraidd.REFUSALS["changed"])
            return handler.send_json({"error": "changed", "words": snapraidd.REFUSALS["changed"]}, status=409)
        record.update(state="sent", outcome="succeeded", outcome_words=snapraidd.outcomes.WORDS["succeeded"],
                      words="The protection service took it.",
                      run={"numbers": [21, 22, 23, 24], "scheduled_at": "2026-10-25T15:00:00", "verdict": "running",
                           "ended": False} if record["action"] in snapraidd.STARTS_RUNS else None)
        core.BUS.publish("snapraid.action", {"approval": record["approval"], "action": record["action"], "state": "sent",
                                             "outcome": "succeeded"})
        return handler.send_json(record)
    if method == "GET" and len(rest) == 2 and rest[0] == "actions":
        record = next((a for a in st.get("actions") or [] if a["approval"] == rest[1]), None)
        if record is None:
            raise KeyError(rest[1])
        return handler.send_json(record)
    if method == "GET" and rest == ["review"]:
        st["reviewed"] = True
        return handler.send_json(_review_page(query))
    if method == "GET" and rest == ["fixes"]:
        return handler.send_json({"fix_recovered": 1, "fix_unrecoverable": 0, "offset": 0,
                                  "entries": [{"disk": "d1", "path": "Notes/" + HOSTILE[0], "result": "recovered"}]})
    if method == "GET" and rest == ["runs"]:
        return handler.send_json({"runs": _runs()})
    if method == "GET" and len(rest) == 2 and rest[0] == "runs":
        if not rest[1].isdigit() or int(rest[1]) not in (3, 4, 5, 6, 7):
            raise KeyError(rest[1])
        return handler.send_json({"task": {"number": int(rest[1]), "command": "sync", "high_command": "maintenance",
                                           "status": "terminated", "exit_code": 2},
                                  "messages": [{"level": "fatal", "text": "Too many files were removed (25, limit is "
                                                                          "20). Sync aborted."},
                                               {"level": "error", "text": "<b>not bold</b> Notes/" + HOSTILE[1]}]})
    raise KeyError("/".join(rest))


def _review_entries() -> list[dict]:
    out = [{"change": "removed", "disk": "d1", "path": f"Photos/2019/IMG_{i:04d}.jpg"} for i in range(1800)]
    out += [{"change": "updated", "disk": "d2", "path": f"Music/Album {i // 12}/track {i % 12:02d}.flac"}
            for i in range(600)]
    out += [{"change": "removed", "disk": "d2" if i % 2 else "d1", "path": f"Notes/{name}"} for i, name in
            enumerate(HOSTILE)]
    while len(out) < 3000:
        out.append({"change": "added", "disk": "d2", "path": f"Downloads/file {len(out)}.zip"})
    return out


def _review_page(query: dict) -> dict:
    entries = _review_entries()
    per_disk: dict[str, dict] = {}
    for entry in entries:
        row = per_disk.setdefault(entry["disk"], {})
        row[entry["change"]] = row.get(entry["change"], 0) + 1
    if query.get("disk"):
        entries = [e for e in entries if e["disk"] == query["disk"]]
    if query.get("change"):
        entries = [e for e in entries if e["change"] == query["change"]]
    if query.get("q"):
        entries = [e for e in entries if query["q"].lower() in e["path"].lower()]
    offset = int(query.get("offset") or 0)
    return {"scan": {"last_command": "sync", "last_command_at": "2026-10-25T02:01:00", "last_sync_at":
                     "2026-10-24T02:01:00", "counts": {"added": 592, "removed": 1808, "updated": 600, "moved": 0,
                                                       "copied": 0, "relocated": 0, "restored": 0}},
            "per_disk": per_disk, "total": len(entries), "offset": offset, "cut": False,
            "entries": entries[offset:offset + 500]}


def _runs() -> list[dict]:
    steps = [{"number": n, "command": c, "status": "canceled" if c == "scrub" else "terminated",
              "exit_code": 2 if c == "sync" else None if c == "scrub" else 0, "exit_sig": None,
              "exit_msg": "The preceding sync operation failed with exit code 2" if c == "scrub" else None,
              "started_at": "2026-10-25T02:00:01", "finished_at": "2026-10-25T02:01:00"}
             for n, c in ((3, "start"), (4, "up"), (5, "sync"), (6, "scrub"), (7, "report"))]
    return [{"high_command": "maintenance", "words": "the protection run", "scheduled_at": "2026-10-25T02:00:00",
             "numbers": [3, 4, 5, 6, 7], "verdict": "frozen", "step": "sync", "approval": None,
             "started_by": "the nightly protection run or outside Seraphina", "steps": steps}]


def _preview(core, st: dict, body: dict, now: float) -> dict:
    action = body.get("action")
    if action not in snapraidd.ACTIONS:
        raise core.Invalid(f"action must be one of {', '.join(snapraidd.ACTIONS)}")
    state = st["state"]
    if state == "daemon_read_only" or state == "daemon_no_link":
        raise core.Invalid(snapraidd.REFUSALS["not_allowed"] if state == "daemon_read_only" else snapraidd.NO_LINK)
    if state == "daemon_two" and action in snapraidd.STARTS_RUNS:
        raise core.Invalid(snapraidd.REFUSALS["two_schedulers"])
    if snapraid_rebuild.current(st.get("rebuild")) and action in snapraidd.STARTS_RUNS:
        raise core.Invalid(snapraidd.REFUSALS["rebuild"])
    if action == "accept" and not st.get("reviewed"):
        raise core.Invalid(snapraidd.REFUSALS["no_review"])
    limits = {"deletes": 20, "updates": 50}
    shows: dict = {}
    if action == "stop":
        shows = {"what": "protecting the latest changes", "of": "the protection run", "number": 5, "progress": 37,
                 "since": "2026-10-25T02:00:01", "line": snapraidd.STOP_LINE}
    elif action == "hold_off":
        enabled = body.get("enabled", True)
        window = snapraidd.next_window("02:00", now)
        shows = {"enabled": enabled, "window": window, "window_words": snapraidd.window_words(window, now),
                 "lines": list(snapraidd.SKIP_LINES) if enabled else ["The next run will happen as scheduled."]}
    elif action in ("maintenance", "accept"):
        shows = {"counts": {"removed": 25, "updated": 3, "added": 140}, "limits": limits,
                 "limits_line": snapraidd.limits_line(limits), "health": "passed",
                 "scan_at": "2026-10-25T02:01:00", "check_after": True}
        if action == "accept":
            shows.update(per_disk=_review_page({})["per_disk"], accept_line=snapraidd.ACCEPT_LINE)
    elif action == "undelete":
        paths = body.get("paths") or []
        if not isinstance(paths, list) or not paths:
            raise core.Invalid("choose from 1 to 1,000 files or folders to bring back")
        if len({entry.get("disk") for entry in paths}) > 1:
            raise core.Invalid(snapraidd.REFUSALS["one_disk"])
        by_disk: dict = {}
        for entry in paths:
            by_disk[entry.get("disk")] = by_disk.get(entry.get("disk"), 0) + 1
        shows = {"count": len(paths), "by_disk": by_disk, "lines": list(snapraidd.UNDELETE_LINES),
                 "as_of": "2026-10-24T02:01:00"}
    else:
        shows = {"places": 3, "health": "corrupt",
                 "line": "It repairs the damaged places from the protection data, then checks them again."}
    approval = f"A-{len(st.setdefault('actions', [])) + 10}"
    record = {"approval": approval, "action": action, "shows": shows,
              "sends": {"action": "maintenance" if action == "accept" else action, "body": {}},
              "binding": {"started_at": round(now - 86400)}, "shown_sha256": f"{len(st['actions']):064x}",
              "expires_at": now + 900, "typed": action == "accept" and st["state"] == "daemon_frozen",
              "state": "preview", "outcome": None, "words": None, "run": None, "outcome_words": None}
    st["actions"].append(record)
    return record


def _job(job: str, verb: str) -> dict:
    now = time.time()
    return {"job": job, "verb": verb, "command": verb, "argv": ["/usr/local/bin/snapraid", "-c", "/etc/snapraid.conf",
                                                                "-l",
                                                                f"/var/lib/seraphina-snapraid/runs/{job}/snapraid.log",
                                                                verb],
            "started": _iso(now - 600), "ended": _iso(now - 590), "exit": 0, "outcome": "ok", "interrupted": False,
            "words": "Done", "seconds": 9.8}


def _output() -> str:
    """A run's output with hostile names, and a name that is not UTF-8 (the page must page by bytes, not text)."""
    lines = ["Self test...", "Loading state from /var/snapraid/snapraid.content...", "SnapRAID status report:", ""]
    lines += [f"Notes/{name}" for name in HOSTILE] + ["Notes/not-utf8-\udcff\udcfe.txt", "caf\u00e9 \u2014 ok"]
    lines += [f"line {i}: <b>not bold</b> and a\\dcolon" for i in range(400)]
    return "\n".join(lines) + "\n"


def _guard_page(st: dict, guard: str, query: dict) -> dict:
    now = time.time()
    record = _guard(now, guard == "G-7")
    if guard not in ("G-6", "G-7"):
        raise KeyError(guard)
    entries = record.pop("_changes")
    if query.get("disk"):
        entries = [e for e in entries if e["disk"] == query["disk"]]
    if query.get("kind"):
        kind = query["kind"]
        entries = [e for e in entries if e["kind"] == kind or (kind == "replaced" and e.get("replaced_by"))]
    if query.get("q"):
        needle = query["q"].lower()
        entries = [e for e in entries if needle in e["path"].lower()]
    offset = int(query.get("offset") or 0)
    groups: dict[tuple, int] = {}
    for e in entries:
        key = (e["disk"], "/".join(e["path"].split("/")[:-1][:2]), e["kind"])
        groups[key] = groups.get(key, 0) + 1
    top = sorted(groups.items(), key=lambda pair: -pair[1])[:20]
    return {"guard": record, "page": {"total": len(entries), "offset": offset, "entries": entries[offset:offset + 500],
                                      "groups": [{"disk": d, "folder": f, "kind": k, "count": c}
                                                 for (d, f, k), c in top],
                                      "list_sha256": record["list_sha256"]}}


def _setup(st: dict) -> dict:
    folder = Path("/home/owner/.local/share/software-factory/snapraid/setup")
    return {"blocks": snapraid.copy_blocks(folder if st.get("staged") else None, release_sha256="ab" * 32),
            "removal": snapraid.removal_blocks(), "staged": bool(st.get("staged")), "setup_dir": str(folder),
            "release_sha256": "ab" * 32, "wizard": None,
            "link": {"blocks": snapraidd.link_blocks(folder if st.get("link_staged") else None, "cd" * 32, "seraphina"),
                     "removal": snapraidd.link_removal_blocks(), "staged": bool(st.get("link_staged")),
                     "release_sha256": "cd" * 32, "daemon_lines": snapraidd.daemon_lines()}}


def mock(core, handler, method, name, body):
    if method == "POST" and name == "changed":
        store(core, AREA)["changed"] = True
        return handler.send_json({"changed": True})
    if method == "POST" and name == "rebuild_good":
        st = store(core, AREA)
        step = snapraid_rebuild.current(st.get("rebuild"))
        if step:
            st["rb_good"] = sorted(set(st.get("rb_good") or []) | {step})
        return handler.send_json({"good": st.get("rb_good")})
    if method == "POST" and name == "rebuild_mounted":
        st = store(core, AREA)
        st["rb_good"] = sorted(set(st.get("rb_good") or []) | {"mounted"})
        return handler.send_json({"good": st["rb_good"]})
    if method == "POST" and name == "rebuild_at":
        st = store(core, AREA)
        step = (body or {}).get("step")
        if step not in snapraid_rebuild.STEPS:
            raise core.Invalid("give {step}")
        now = time.time()
        st["rebuild"], st["rb_good"] = None, []
        record = snapraid_rebuild.open_record(_rb_facts(st), "d2", now - 3600)
        st["rebuild"] = record
        for before in snapraid_rebuild.STEPS[:snapraid_rebuild.STEPS.index(step)]:
            record["done"][before] = now - 600
            if before == "rebuild":
                record["results"]["rebuild"] = {"fix": 1, "check": 1}
        core.BUS.publish("snapraid.rebuild", {"checklist": record["id"], "state": "open", "step": step})
        return handler.send_json(snapraid_rebuild.build(record, _rb_facts(st), now))
    if method == "POST" and name in STATES:
        st = store(core, AREA)
        st.update(state=name, refreshing=None, actions=[], changed=False, reviewed=False, rebuild=None, rb_good=[])
        core.BUS.publish("snapraid.updated", {"tone": None, "code": name})
        return handler.send_json(_view(core, st))
    return NotImplemented


def contract(test) -> None:
    """GET /api/snapraid has SnapraidPanel.view()'s keys; every state's light uses the module's tones and reason
    words; the guard page has the panel's keys and the guard record the summary's."""
    from swfactory.config import Config

    fixture = json.loads((Path(__file__).resolve().parent.parent / "fixtures" / "snapraidd21" / "lab" /
                          "04-after-maintenance" / "GET_metrics.json").read_text(encoding="utf-8"))["text"]
    with tempfile.TemporaryDirectory() as tmp:
        config = Config(Path(tmp) / "config.json", {"data_dir": tmp, "snapraid": {"enabled": False}})
        down = snapraidd.MetricsReader(fetcher=lambda url: (_ for _ in ()).throw(ConnectionRefusedError()))
        panel = snapraid.SnapraidPanel(config, None, None, None, local=lambda: {"binary": None}, metrics=down)
        real = panel.view()
        config = Config(Path(tmp) / "config.json", {"data_dir": tmp, "snapraid": {"enabled": True}})
        up = snapraidd.MetricsReader(fetcher=lambda url: (200, fixture.encode()))
        daemon_panel = snapraid.SnapraidPanel(config, None, None, None, local=lambda: {"binary": None}, metrics=up,
                                              link=snapraidd.LinkClient("/nonexistent", connector=lambda l, t: b""))
        daemon_panel.daemon.tick()
        real_daemon = daemon_panel.view()
        test.assertEqual(set(real_daemon), set(real))
    for state in STATES:
        status, _, raw = test.request("POST", f"/__mock/backups/{state}", {})
        test.assertEqual(status, 200, state)
        view = json.loads(raw)
        test.assertEqual(set(view), set(real), state)
        test.assertIn(view["light"]["tone"], snapraid.TONES, state)
        reasons = snapraidd.REASONS if state in DAEMON_STATES else snapraid.REASONS
        test.assertTrue(view["light"]["code"] in reasons, (state, view["light"]["code"]))
        test.assertEqual(view["sentences"], list(snapraid.SENTENCES))
        if state in DAEMON_STATES:
            test.assertEqual(set(view["daemon"]), set(real_daemon["daemon"]), state)
    status, _, raw = test.request("POST", "/__mock/backups/daemon_frozen", {})
    status, _, raw = test.request("GET", "/api/snapraid/review?offset=500&disk=d1")
    page = json.loads(raw)
    test.assertEqual(set(page), {"scan", "per_disk", "total", "offset", "cut", "entries"})
    status, _, raw = test.request("POST", "/api/snapraid/actions", {"action": "accept"})
    preview = json.loads(raw)
    test.assertEqual(set(preview), {"approval", "action", "shows", "sends", "binding", "shown_sha256", "expires_at",
                                    "typed", "state", "outcome", "words", "run", "outcome_words"})
    status, _, raw = test.request("GET", "/api/snapraid/guard/G-7?offset=500")
    page = json.loads(raw)
    test.assertEqual(set(page), {"guard", "page"})
    test.assertEqual(set(page["page"]), {"total", "offset", "entries", "groups", "list_sha256"})
    test.assertEqual(page["page"]["total"], 3000)
    test.assertEqual(set(page["guard"]), set(snapraid._guard_summary({})))
    # the checklist for a failed disk: the mock answers with the module's own build over its facts
    test.request("POST", "/__mock/backups/daemon_failing", {})
    status, _, raw = test.request("GET", "/api/snapraid/rebuild")
    page = json.loads(raw)
    test.assertEqual(set(page), set(snapraid_rebuild.build(None, {}, time.time())))
    status, _, raw = test.request("POST", "/api/snapraid/rebuild", {"disk": "d2"})
    test.assertEqual(status, 201)
    test.assertEqual(json.loads(raw)["current"], "stop")
    status, _, raw = test.request("GET", "/api/snapraid")
    test.assertEqual(json.loads(raw)["light"]["code"], "rebuild")
    test.request("POST", "/__mock/backups/healthy", {})

