"""The owner's checklist for a whole failed disk (DESIGN.md section 41 "P4"; docs/seraphina/BACKUPS_SNAPRAID.md
§9.4, §9.5 and D18).

The SnapRAID Daemon has no action that rebuilds a disk, and Seraphina runs no SnapRAID command that writes, so a
rebuild is a checklist the owner follows: seven steps, each with what it does in plain words, the copy blocks the
owner pastes himself where root is needed, and a read-only check before the next step. **Seraphina runs none of the
blocks.** The only thing she may send on the owner's behalf is the skip of the next run, through the existing
approval of P3 (BACKUPS_SNAPRAID §6.8); the page offers its button and nothing else.

The checks read only: the reader's ``info`` and ``disks`` (bin/sf-snapraid, which runs no SnapRAID command while the
daemon is installed), the daemon's ``/metrics`` (no login), ``systemctl show`` of the daemon's unit and of the two
units the owner's blocks start, ``/proc`` for a running ``snapraid``, ``/etc/fstab`` and Dockge's stacks folder (both
readable by any user). Nothing here starts a process that changes anything.

The pure parts (``open_record``, ``build``, ``advance``, ``prepare``) take a facts dict, so the tests and the GUI mock
use the very words and rules the cockpit shows; ``Checklist`` gathers the live facts, keeps the record
(``<data_dir>/snapraid/rebuild.json``, 0600: the disk's SnapRAID name, number, mount point, filesystem type and its
recorded filesystem UUID, the steps done, the confirmations and the rebuild's two exit codes; never a file name, a
disk's model, serial or /dev/disk/by-id name), writes the activity journal and publishes ``snapraid.rebuild``.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

STEPS = ("stop", "name", "boot", "disk", "rebuild", "lost", "done")
FILE = "rebuild.json"
DAEMON_UNIT = "snapraidd.service"          # the daemon's unit (BACKUPS_SNAPRAID §10.3: systemctl reload snapraidd)
REBUILD_UNIT = "seraphina-disk-rebuild.service"
CHECK_UNIT = "seraphina-disk-check.service"
REBUILD_DIR = "/var/lib/seraphina-snapraid/rebuild"    # root-only (the reader's state folder): the names stay there
READER_CONFIG = "/etc/seraphina/snapraid-helper.json"
READER = "/usr/local/libexec/seraphina/sf-snapraid"
DEFAULT_BINARY = "/usr/bin/snapraid"
DEFAULT_CONF = "/etc/snapraid.conf"
DOCKGE_STACKS = "/opt/stacks"
FSTYPES = ("ext4", "xfs")
SHOW_PROPS = ("LoadState,ActiveState,SubState,Result,ExecMainCode,ExecMainStatus,UnitFileState,"
              "ExecMainStartTimestampMonotonic,ExecMainExitTimestampMonotonic")
SAFE_MOUNT = re.compile(r"^/[A-Za-z0-9._@+-]+(?:/[A-Za-z0-9._@+-]+)*/?$")
SAFE_BY_ID = re.compile(r"^/dev/disk/by-id/[A-Za-z0-9._:@+#=-]+$")
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
UUID_RE = re.compile(r"^[0-9A-Fa-f][0-9A-Fa-f-]{3,63}$")
BAD_HEALTH = ("degraded", "prefail", "failing")

# ---------------------------------------------------------------- the words on the page (novice; U7 checks them)

TITLE = "Replace a failed disk"
SUB = ("A checklist that walks you through it, one step at a time. You run each command yourself; Seraphina checks "
       "each step before the next one.")
REASON = "{disk} is being replaced: follow the checklist to the end."
NOTE = "Until it ends, the other disks are less protected than usual."
LAB_LINE = ("Not checked on a real server yet: starting up without the failed disk, and this whole checklist on test "
            "disks. Read each step before you run it.")
CHOOSE = "Which disk failed?"
SUGGESTED = "this one looks failed"
NO_PROTECTION = "A protection disk that failed is not covered by this checklist yet."
CLOSE_WORDS = "This closes the checklist. It undoes nothing you ran."
IN_ORDER = "Do the steps in order: {title} comes first."
TICK_FIRST = "Tick each line above first."
CLOSED = "The checklist is closed."
START_REFUSALS = {
    "no_reader": ("The checklist needs the disk protection reader, to name the disks and check each step. Set it up "
                  "first."),
    "reader_error": "The disk protection reader did not answer: {why}",
    "open": "A checklist is already open. Finish it or close it first.",
    "unknown_disk": "Seraphina does not know that disk. Choose one from the list.",
    "not_data": NO_PROTECTION,
    "taking_out": "That disk is being taken out of protection: there is nothing to bring back onto it.",
    "bad_name": "Seraphina will not build commands for a disk with that name.",
    "bad_mount": ("Seraphina will not build commands for the folder {mount}: its name has characters she does not "
                  "put into a command."),
}
TITLES = {
    "stop": "Stop changes",
    "name": "Name the failed disk",
    "boot": "Make sure the server can start without it",
    "disk": "Fit and prepare the new disk",
    "rebuild": "Rebuild the disk",
    "lost": "What could not be brought back",
    "done": "Finish",
}
WHAT = {
    "stop": ("Anything written to the protected disks now can stop other files from coming back. So, before anything "
             "else, stop what writes to them."),
    "stop_daemon": ("First skip the next protection run, so that none starts while you work. Then, once nothing is "
                    "running, turn the protection service off with the block below. It stays off, even when the "
                    "server restarts, until the last step turns it on again."),
    "stop_reader": ("Turn off, for now, whatever starts protection runs on this server. Seraphina cannot do this for "
                    "you: each one is turned off in its own way."),
    "apps": ("Stop the apps that use these disks. In Dockge, press Stop & Inactive on each of them, not just Stop: an "
             "app that was only stopped starts again by itself when the server restarts, and would write to the new, "
             "empty disk."),
    "name": ("This is the disk to take out. Check the model and serial number on its label before you pull it out: "
             "taking out the wrong disk would lose more files."),
    "boot": ("If the server cannot find a disk listed in /etc/fstab when it starts, it stops before the network is up, "
             "and you could not reach it from another computer. So the failed disk's line must say that the server "
             "may start without it."),
    "disk": ("Turn the server off, take out the failed disk, put the new one in its place, and turn the server on "
             "again."),
    "disk_size": ("The new disk must hold at least as much as the old one held. A bigger one is fine, but only as "
                  "much of it is protected as the protection disk can hold."),
    "disk_erase": ("Preparing the new disk erases it: whatever is on it now cannot be brought back. Choose it below, "
                   "then type its serial number to see the commands."),
    "rebuild": ("The protection service stays off. You start the rebuild yourself with the block below; it runs on "
                "its own, so it carries on if you close the terminal."),
    "rebuild_time": ("It can take many hours: about as long as reading the whole disk. Keep the server on until it "
                     "ends. If it is turned off or stopped, start it again: it starts again from the beginning."),
    "rebuild_power": ("This server turns itself off at {off} every night (Settings): change that until the rebuild "
                      "has ended, or it stops."),
    "rebuild_as_of": "Files come back exactly as they were at the last protection run, {when}.",
    "lost_all": "Every protected file on {disk} came back as it was at the last protection run, {when}.",
    "lost_unknown": ("Seraphina cannot tell whether every file came back: the rebuild or its check ended with an "
                     "error, or she could not read how they ended. The last block shows the end of what each one "
                     "printed. The first lists the files that were left under their own name with .unrecoverable at "
                     "the end; the names show only in your terminal."),
    "lost_added": "Files added to {disk} after the last protection run, {when}, are gone: the protection never had them.",
    "lost_changed": "Files changed after that run came back as they were at that run, or not at all.",
    "lost_elsewhere": ("Nothing else on this machine has an older copy of these files. If you keep copies somewhere "
                       "else (another computer, a cloud service, an app's own export), look there. Once you have a "
                       "file back, delete its .unrecoverable copy."),
    "lost_owner": ("Every file that came back belongs to the administrator, and only the administrator can open it. "
                   "If an app cannot open its files, give them back to the app's own user with the second block."),
    "done": ("Once the next protection run has happened, the rebuild can no longer be tried again. Start the "
             "protection service only when you are done with the steps above."),
    "done_apps": "Start your apps again: in Dockge, press Start on each one you stopped.",
    "done_next": ("The next protection run, {window}, takes the new disk in. If many files did not come back, it may "
                  "stop and wait for you to look at the changes: that is expected. Look, then protect them."),
    "done_reader": "Turn on again what you turned off in the first step. Its next run takes the new disk in.",
}
FACT_WORDS = {
    "where": "{label} ({name}), mounted at {mount}.",
    "model": "Model {model}, serial number {serial}.",
    "by_id": "Its name that never changes: {by_id}.",
    "seen": "Last seen {when}.",
    "health": "Its health: {words}.",
    "running": "Running now: {what}.",
    "stacks": "Apps Seraphina found that use these disks: {names}.",
    "no_stacks": "Seraphina found no app in Dockge that uses these disks. Check any app you installed another way.",
    "stacks_unknown": "Seraphina cannot look at your apps (Dockge's folder cannot be read): check each one yourself.",
    "schedulers": "What starts protection runs here: {places}.",
    "no_schedulers": "Seraphina found nothing else that starts protection runs here.",
    "mounted_now": "Mounted at {mount} now: {model}, serial number {serial}.",
}
HEALTH_WORDS = {"passed": "healthy", "ok": "healthy", "fail": "failing", "prefail": "failing", "failing": "failing",
                "degraded": "missing or not answering", "corrupt": "hidden damage found", "pending": "not read yet",
                "unknown": "can't be read", None: "not read yet"}
CHECKS = {
    "task_running": "The protection service is running now ({what}). Wait until it ends, or stop it safely on the "
                    "Backups page, then turn the service off.",
    "service_up": "The protection service is still on. Run the block below.",
    "service_enabled": ("The protection service is off, but it would start again when the server restarts. Run the "
                        "block below: it turns it off for good, until the last step."),
    "service_unknown": "Seraphina cannot tell whether the protection service is off: systemctl did not answer.",
    "service_back": "The protection service is on again. Turn it off with the block of the first step before going on.",
    "snapraid_running": "SnapRAID is still running. Wait until it ends.",
    "stopped": "The protection service is off and nothing is running.",
    "quiet": "Nothing is running.",
    "identity": "Seraphina's record names this disk.",
    "no_identity": ("Seraphina has no record of this disk's model or serial number. Find it by its place in the "
                    "server, or by the label on each disk."),
    "too_many": ("{count} disks have failed, more than the protection can rebuild ({levels}). Nothing here can bring "
                 "them back: look for your files in your other copies."),
    "nofail": "Its line in /etc/fstab lets the server start without it.",
    "line_off": "Its line in /etc/fstab is turned off.",
    "needs_change": ("Its line in /etc/fstab does not let the server start without it. Run the block below: it keeps "
                     "a dated copy of the file first."),
    "no_line": "No line in /etc/fstab mounts {mount}. Check how this disk is mounted before you turn the server off.",
    "fstab_unreadable": "Seraphina cannot read /etc/fstab.",
    "not_mounted": "Nothing is mounted at {mount} yet. Run the blocks for the new disk.",
    "old_disk_back": ("The disk mounted at {mount} is the old one. If it works after all, undo the first steps: turn "
                      "on again what they turned off (with the blocks below, when there are any), start your apps "
                      "again, then close this checklist. If it does not work, take it out and fit the new one."),
    "reader_old": ("The new disk is mounted, but the reader still expects the old one. Tell the reader about it with "
                   "the block below."),
    "fstab_twice": ("Two lines in /etc/fstab mount {mount}, and the server would use only the first one. Put a # at "
                    "the start of the old one."),
    "fstab_unsafe": ("The new disk's line in /etc/fstab does not let the server start without it. Add "
                     "nofail,x-systemd.device-timeout=30s to its options."),
    "old_rebuild": ("The server still holds a rebuild or its check from an earlier attempt. Its end would be read as "
                    "the end of this rebuild, and the new rebuild could not start. Clear it with the block below: it "
                    "also stops one that still runs."),
    "mounted": "The new disk is mounted at {mount}, and the reader knows it.",
    "disk_not_mounted": ("The new disk is not mounted at {mount}, or the reader does not know it: the rebuild would "
                         "write its files to the wrong place. Mount it (sudo mount {mount}), then check again."),
    "not_started": "The rebuild has not started yet. Run the first block.",
    "rebuild_running": "The rebuild is running. Check again later: this page does not watch it by itself.",
    "rebuild_stopped": ("The rebuild stopped before it ended. Start it again with the block below: it starts again "
                        "from the beginning."),
    "rebuild_failed": ("The rebuild ended with an error: some files could not be brought back, or it could not run "
                       "at all. Look at the end of what it printed: sudo tail -n 20 {out}. If it did not run, start "
                       "it again with the last block; if it did, run the check, the second block."),
    "check_not_started": 'The rebuild has ended. Now run the check, with the block "{title}".',
    "check_running": "The check is running. Check again later.",
    "check_stopped": "The check stopped before it ended. Start it again with the block below.",
    "check_too_soon": ("The check started before the rebuild ended, so it says nothing about the new disk. Start it "
                       "again with the block below."),
    "order_unknown": "Seraphina cannot tell whether the check ran after the rebuild ended: systemctl did not say.",
    "unit_unknown": "Seraphina cannot read the state of the rebuild: systemctl did not answer.",
    "units_unproven": ("The rebuild and its check have ended. But when the new disk went in, systemctl did not "
                       "answer, so Seraphina could not see whether a rebuild from an earlier attempt was left on the "
                       "server: she cannot tell that these are the ones you started."),
    "rebuilt": "The rebuild and its check have ended.",
    "read": "Read this, then tick the line below.",
    "not_answering": "The protection service is not answering yet. Run the block above.",
    "still_missing": ("The protection service still reads {disk} as missing or changed, so it protects nothing. This "
                      "case is not checked on a real server yet: ask for help before going on."),
    "answering": "The protection service is on again.",
    "reader_done": "Tick the lines below once you have turned it back on.",
    "reader_unknown": "The disk protection reader did not answer: {why}",
}
CONFIRMS = {
    "apps": "I stopped every app that uses these disks with Stop & Inactive.",
    "schedulers": "I turned off what starts protection runs.",
    "service_off": ("I checked myself that the protection service is off, and that it will not start again when the "
                    "server restarts."),
    "found": "I found this disk in the server.",
    "boot": "I checked myself that the server can start without this disk.",
    "rebuild_self": "I checked myself that the rebuild ended, and that its check ran after it and ended too.",
    "read": "I have read this, and looked for the files that did not come back.",
    "ready": "I am done with the rebuild, and want the protection to go on with the new disk.",
    "apps_back": "I started my apps again.",
}
PREPARE_REFUSALS = {
    "not_now": "The new disk's commands come in the step to fit and prepare it.",
    "not_spare": "That disk is not one of the free disks Seraphina sees now. Look again.",
    "mounted": "A new disk is mounted at {mount} already, so Seraphina builds no commands for another one.",
    "protected": ("That disk is one of the disks Seraphina protects, or the failed one, so she will not build commands "
                  "that erase it."),
    "no_by_id": "This disk has no name in /dev/disk/by-id, so Seraphina will not build commands for it.",
    "no_serial": "Seraphina cannot read this disk's serial number, so she cannot confirm it is the one you mean.",
    "typed": "That is not this disk's serial number.",
}
TYPED_ASK = {"blank": "Type the last 4 characters of its serial number:",
             "signed": "This disk already holds something. Type its whole serial number:"}
SPARE_WORDS = {"blank": "New disks with nothing on them", "signed": "Disks that already hold something (erased too)",
               "none": "Seraphina sees no free disk. Fit the new disk, turn the server on, then look again.",
               "smaller": "smaller than the old disk"}
BLOCK_WORDS = {
    "stop_title": "Turn the protection service off",
    "stop_expect": "Nothing printed, or a line saying a link was removed.",
    "stop_note": ("Run it only while nothing is running: turning the service off during a run stops SnapRAID without "
                  "saving."),
    "stop_if_not": "sudo systemctl status snapraidd.service",
    "boot_title": "Turn off the failed disk's line",
    "boot_expect": "findmnt reports 0 errors (warnings are fine).",
    "boot_note": "Only the line for {mount} changes. The copy keeps the file as it was.",
    "boot_manual": ("Seraphina will not build this command for that folder name. Edit /etc/fstab yourself: put a # at "
                    "the start of the line for {mount}."),
    "off_title": "Turn the server off",
    "off_expect": "The server turns off. Swap the disks, then turn it on again.",
    "wipe_title": "Erase what is on it",
    "wipe_expect": "A few lines naming what was erased.",
    "part_title": "Make one partition and its filesystem",
    "part_expect": "The filesystem's details, ending without an error.",
    "mount_title": "Mount it where the old disk was",
    "mount_expect": "findmnt reports 0 errors, and the last line shows the new disk at {mount}.",
    "mount_note": "The old line stays in the file as a comment; the copy keeps the file as it was.",
    "reader_title": "Tell the reader about the new disk",
    "reader_expect": "It names the new filesystem for {name}. Run the second step only then.",
    "reader_note": "Nothing else in the reader's settings changes.",
    "reader_manual": ("Seraphina could not read the old disk's filesystem in the reader's settings. In the copy the "
                      "second line opens, put the new one in place of the old one for {name} (the first line shows "
                      "it), then run the second step only when the reader names it."),
    "rebuild_title": "Start the rebuild",
    "rebuild_expect": "Running as unit: seraphina-disk-rebuild.service",
    "rebuild_note": ("To watch it: sudo tail -f {out} (Ctrl+C stops the watching, not the rebuild). The names it prints "
                     "stay in that folder, which only the administrator can read."),
    "check_title": "Then check what came back",
    "check_expect": "Running as unit: seraphina-disk-check.service",
    "check_note": "Run it only once the rebuild has ended.",
    "again_title": "Start it again",
    "again_expect": "Running as unit: {unit}",
    "list_title": "List what could not be brought back",
    "list_expect": ("The names of the files left with .unrecoverable at the end, one per line. An empty list alone "
                    "does not prove that every file came back."),
    "ends_title": "See how the rebuild and its check ended",
    "ends_expect": "The last lines each one printed: how many files it could not bring back, or why it stopped.",
    "owner_title": "Give files back to an app",
    "owner_note": "Put the app's own user and its folder in place of the examples (ls -ln shows the numbers).",
    "tidy_title": "Tidy up the rebuild's records",
    "tidy_expect": "Nothing printed.",
    "start_title": "Turn the protection service on again",
    "start_expect": "Nothing printed, or a line saying a link was created.",
    "line_back_title": "Turn the disk's line in /etc/fstab on again",
    "line_back_expect": "findmnt reports 0 errors (warnings are fine).",
    "line_back_note": ("The first line lists the copies step 3 kept: put the newest one in place of the example. It "
                       "puts back the whole file as it was then."),
}
WAY_BACK = ("If you are not replacing the disk after all, undo what the first steps turned off. Do it only while no "
            "new disk is mounted at {mount}: the protection would take an empty disk in, and the files it can still "
            "bring back would be lost.")


def novice_strings() -> list[str]:
    """Every sentence of this module a person reads (U7's word check)."""
    def flat(values: Any) -> list[str]:
        out: list[str] = []
        for value in values:
            out += flat(value.values()) if isinstance(value, dict) else [value]
        return out
    keep = {key: value for key, value in BLOCK_WORDS.items() if not key.endswith("_if_not")}
    return flat([TITLE, SUB, REASON, NOTE, LAB_LINE, CHOOSE, SUGGESTED, NO_PROTECTION, CLOSE_WORDS, IN_ORDER,
                 TICK_FIRST, CLOSED, START_REFUSALS, TITLES, WHAT, FACT_WORDS, HEALTH_WORDS, CHECKS, CONFIRMS,
                 PREPARE_REFUSALS, TYPED_ASK, SPARE_WORDS, keep, WAY_BACK])


# ---------------------------------------------------------------- reading the facts (read-only)


def unit_props(text: str) -> dict:
    """systemctl show's key=value lines."""
    out = {}
    for line in (text or "").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip()] = value.strip()
    return out


def unit_state(props: dict | None) -> tuple[str, int | None]:
    """A unit the owner's block started, as the checklist reads it: ("unknown", None) when systemctl did not answer,
    ("absent", None) never started (or gone), ("running", None), ("stopped", None) ended by a signal or a stop, or
    with systemd's own exit status (200 and up: the program never ran), or ("ended", exit status)."""
    if not props:
        return "unknown", None
    if props.get("LoadState") in ("not-found", "") and props.get("ActiveState") in ("inactive", "", None):
        return "absent", None
    active, sub = props.get("ActiveState"), props.get("SubState")
    if active in ("activating", "deactivating", "reloading") or (active == "active" and sub not in ("exited",)):
        return "running", None
    code = props.get("ExecMainCode")
    try:
        status = int(props.get("ExecMainStatus") or 0)
    except ValueError:
        status = None
    if code in ("2", "3") or props.get("Result") in ("signal", "core-dump", "timeout", "watchdog"):
        return "stopped", None
    if code == "1" and status is not None and status >= 200:    # systemd's own (203: could not run), not SnapRAID's
        return "stopped", None
    if code == "1" or (active == "active" and sub == "exited"):
        return "ended", status
    return "absent", None


def unit_stamp(props: dict | None, key: str) -> int | None:
    """One of a unit's monotonic timestamps (microseconds since boot), or None when systemctl did not give it."""
    try:
        value = int((props or {}).get(key) or 0)
    except ValueError:
        return None
    return value or None


def daemon_service(facts: dict) -> tuple[str, bool | None]:
    """The daemon's unit: ("up" | "off" | "unknown", enabled?)."""
    props = (facts.get("units") or {}).get(DAEMON_UNIT)
    if not props:
        return "unknown", None
    enabled = props.get("UnitFileState") in ("enabled", "enabled-runtime", "linked", "alias") \
        if props.get("UnitFileState") else None
    if props.get("LoadState") == "not-found":
        return "off", False
    if props.get("ActiveState") in ("active", "activating", "reloading", "deactivating"):
        return "up", enabled
    return "off", enabled


def fstab_lines(text: str, mount: str, uuid: str | None = None) -> list[dict]:
    """The lines of /etc/fstab for mount (by its second field) or for the recorded filesystem: {line, active, options}."""
    point = mount.rstrip("/") or "/"
    out = []
    for raw in (text or "").split("\n"):
        stripped = raw.strip()
        active = not stripped.startswith("#")
        fields = stripped.lstrip("#").split()
        if len(fields) < 2:
            continue
        by_uuid = bool(uuid) and uuid.lower() in fields[0].lower()
        if fields[1].rstrip("/") == point or by_uuid:
            out.append({"line": raw, "active": active, "options": fields[3].split(",") if len(fields) > 3 else []})
    return out


def safe_boot(options: list[str]) -> bool:
    return "nofail" in options and any(option.startswith("x-systemd.device-timeout=") for option in options)


def _bre(text: str) -> str:
    """text as a literal in a sed basic regular expression delimited by |."""
    return re.sub(r"([\\.*\[\]^$|])", r"\\\1", text)


def comment_out_sed(mount: str) -> str:
    """The sed command that puts a # before every active /etc/fstab line whose mount point is mount (with or without a
    final slash), indented or not, as fstab_lines reads them. mount must match SAFE_MOUNT."""
    point = _bre(mount.rstrip("/"))
    pattern = rf"^\([[:space:]]*[^#[:space:]][^[:space:]]*[[:space:]]\+{point}/\?\)\([[:space:]].*\)\?$"
    script = "s|" + pattern + "|# \\1\\2|"     # the rest of the line, or nothing: a line of two fields
    return f"sudo sed -i '{script}' /etc/fstab"


def snapraid_running(proc: str = "/proc") -> bool:
    with contextlib.suppress(OSError):
        for pid in os.listdir(proc):
            if pid.isdigit():
                with contextlib.suppress(OSError):
                    if Path(proc, pid, "comm").read_text(encoding="utf-8", errors="replace").strip() == "snapraid":
                        return True
    return False


def stacks_using(points: list[str], folder: str = DOCKGE_STACKS) -> list[str] | None:
    """The Dockge stacks whose compose file names one of points (the protected disks' folders), by stack folder name;
    None when Dockge's folder cannot be read. Read as plain files, as any user may."""
    try:
        names = sorted(os.listdir(folder))
    except OSError:
        return None
    wanted = [point.rstrip("/") for point in points if point]
    found = []
    for name in names:
        for compose in ("compose.yaml", "compose.yml", "docker-compose.yml", "docker-compose.yaml"):
            path = Path(folder, name, compose)
            with contextlib.suppress(OSError, UnicodeDecodeError):
                text = path.read_bytes()[: 1 << 20].decode("utf-8", "replace")
                if any(re.search(re.escape(point) + r"(?:/|\s|:|\"|'|$)", text, re.M) for point in wanted):
                    found.append(name)
                    break
    return found


def _systemctl_show(unit: str) -> dict | None:
    try:
        done = subprocess.run(["systemctl", "show", "-p", SHOW_PROPS, unit], capture_output=True, text=True,
                              timeout=10, stdin=subprocess.DEVNULL, check=False,
                              env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})
    except (OSError, subprocess.SubprocessError):
        return None
    return unit_props(done.stdout) if done.returncode == 0 and done.stdout else None


def summarize_info(info: dict) -> dict:
    """What the checklist reads from the reader's info answer."""
    cfg = info.get("helper_config") or {}
    parsed = info.get("parsed") or {}
    return {"conf": cfg.get("conf") or DEFAULT_CONF, "binary": (info.get("snapraid") or {}).get("binary"),
            "mounts": info.get("mounts") or {}, "uuids": dict(cfg.get("mounts") or {}),
            "data": [entry.get("dir") for entry in parsed.get("data") or []],
            "schedulers": [entry.get("place") for entry in (info.get("schedulers") or {}).get("found") or []
                           if entry.get("place")],
            "snapraid_running": bool(info.get("snapraid_running"))}


def summarize_disks(answer: dict) -> tuple[dict, list[dict]]:
    disks = {row.get("name"): {key: row.get(key) for key in ("model", "serial", "by_id", "size", "present", "seen_at",
                                                              "fstype", "mount", "uuid")}
             for row in answer.get("disks") or [] if row.get("name")}
    spare = [{key: row.get(key) for key in ("device", "by_id", "size", "model", "serial", "signature")}
             for row in answer.get("spare") or []]
    return disks, spare


# ---------------------------------------------------------------- the pure checklist


def _when(at: Any, now: float, zone: Any = None) -> str:
    from .snapraid import when_words

    if not at:
        return "at a time Seraphina does not know"
    return when_words(float(at), now, zone)


def _size(count: Any) -> str:
    from .snapraid import size_words

    return size_words(float(count)) if isinstance(count, (int, float)) and count else "size unknown"


def choices(facts: dict) -> list[dict]:
    """The data disks the checklist may be opened for, the failed-looking ones marked."""
    from .snapraid import disk_label

    info = facts.get("info") or {}
    mounts = info.get("mounts") or {}
    numbers = {name: entry.get("number") for name, entry in mounts.items() if entry.get("role") == "data"}
    health = ((facts.get("daemon") or {}).get("disk_health") or {})
    smart = facts.get("smart") or {}
    out = []
    for name, entry in mounts.items():
        if entry.get("role") != "data" or entry.get("placeholder"):
            continue
        state = health.get(name) or smart.get(name)
        failed = not entry.get("mounted") or entry.get("uuid_ok") is False or state in BAD_HEALTH + ("fail",)
        out.append({"name": name, "number": entry.get("number"), "label": disk_label(name, numbers),
                    "mount": entry.get("path"), "mounted": bool(entry.get("mounted")), "health": state,
                    "health_words": HEALTH_WORDS.get(state, HEALTH_WORDS[None]), "suggested": bool(failed)})
    return sorted(out, key=lambda row: row.get("number") or 0)


def open_record(facts: dict, disk: str, now: float, number: int = 1) -> dict:
    """A new checklist for disk; ValueError with the refusal's words."""
    from .snapraid import disk_label

    if (facts.get("reader") or {}).get("error"):
        raise ValueError(START_REFUSALS["reader_error"].format(why=facts["reader"]["error"]))
    if not facts.get("info"):
        raise ValueError(START_REFUSALS["no_reader"])
    mounts = facts["info"].get("mounts") or {}
    entry = mounts.get(disk)
    if entry is None:
        raise ValueError(START_REFUSALS["unknown_disk"])
    if entry.get("role") != "data":
        raise ValueError(START_REFUSALS["not_data"])
    if entry.get("placeholder"):
        raise ValueError(START_REFUSALS["taking_out"])
    if not SAFE_NAME.match(disk):
        raise ValueError(START_REFUSALS["bad_name"])
    mount = str(entry.get("path") or "")
    if not SAFE_MOUNT.match(mount):
        raise ValueError(START_REFUSALS["bad_mount"].format(mount=mount))
    numbers = {name: row.get("number") for name, row in mounts.items() if row.get("role") == "data"}
    old = (facts.get("disks") or {}).get(disk) or {}
    fstype = old.get("fstype") or entry.get("fstype")
    uuid = (facts["info"].get("uuids") or {}).get(disk)
    return {"v": 1, "id": f"R-{number}", "state": "open", "disk": disk, "number": entry.get("number"),
            "label": disk_label(disk, numbers), "mount": mount.rstrip("/"),
            "fstype": fstype if fstype in FSTYPES else "ext4", "old_uuid": uuid if uuid and UUID_RE.match(uuid) else None,
            "old_size": old.get("size") if isinstance(old.get("size"), int) else None,
            "route": facts.get("route") or "reader", "opened_at": now, "done": {}, "confirmed": {}, "results": {},
            "closed_at": None}


def current(record: dict | None) -> str | None:
    if not record or record.get("state") != "open":
        return None
    return next((step for step in STEPS if step not in (record.get("done") or {})), None)


def _check(ok: bool | None, code: str, **values: Any) -> dict:
    return {"ok": ok, "code": code, "words": CHECKS[code].format(**values)}


def check_step(step: str, record: dict, facts: dict) -> dict:
    """The read-only check of one step: {ok (True, False, or None when Seraphina cannot tell), code, words}. A step
    whose check cannot tell passes only with the owner's tick that he checked himself (SELF_TICKS); the steps that
    lead to a command writing to the disk (4 and 5) need the reader's answer."""
    daemon = facts.get("daemon")
    on_daemon = record.get("route") == "daemon"
    mount = record["mount"]
    service_back = on_daemon and (daemon_service(facts)[0] == "up" or bool((daemon or {}).get("answering")))
    reader_error = (facts.get("reader") or {}).get("error")
    if reader_error and step == "name":
        return _check(None, "reader_unknown", why=reader_error)
    if step == "stop":
        if facts.get("snapraid_running"):
            return _check(False, "snapraid_running")
        if on_daemon:
            if daemon and daemon.get("answering") and daemon.get("active"):
                from .snapraidd import WORDS_OF

                return _check(False, "task_running", what=WORDS_OF.get(daemon["active"].get("command"), "a step"))
            service, enabled = daemon_service(facts)
            if daemon and daemon.get("answering") or service == "up":
                return _check(False, "service_up")
            if service == "unknown":
                return _check(None, "service_unknown")
            if enabled:
                return _check(False, "service_enabled")
            return _check(True, "stopped")
        return _check(True, "quiet")
    if step == "name":
        info = facts.get("info") or {}
        mounts = info.get("mounts") or {}
        levels = sum(1 for entry in mounts.values() if entry.get("role") == "parity")
        health = (daemon or {}).get("disk_health") or {}
        failed = {name for name, entry in mounts.items() if entry.get("role") == "data" and not entry.get("placeholder")
                  and (not entry.get("mounted") or health.get(name) in BAD_HEALTH)} | {record["disk"]}
        if levels and len(failed) > levels:
            return _check(False, "too_many", count=len(failed), levels=levels)
        known = (facts.get("disks") or {}).get(record["disk"]) or {}
        return _check(True, "identity") if known.get("serial") or known.get("by_id") else _check(None, "no_identity")
    if step == "boot":
        if facts.get("fstab") is None:
            return _check(None, "fstab_unreadable")
        lines = fstab_lines(facts["fstab"], mount, record.get("old_uuid"))
        live = [line for line in lines if line["active"]]
        if not lines:
            return _check(None, "no_line", mount=mount)
        if not live:
            return _check(True, "line_off")
        if all(safe_boot(line["options"]) for line in live):
            return _check(True, "nofail")
        return _check(False, "needs_change")
    if step == "disk":
        if service_back:
            return _check(False, "service_back")
        if reader_error:              # the step's proof is the reader's: it knows the new disk (cannot be undone)
            return _check(False, "reader_unknown", why=reader_error)
        entry = ((facts.get("info") or {}).get("mounts") or {}).get(record["disk"]) or {}
        if not entry.get("mounted"):
            return _check(False, "not_mounted", mount=mount)
        recorded = ((facts.get("info") or {}).get("uuids") or {}).get(record["disk"])
        if entry.get("uuid_ok") and record.get("old_uuid") and recorded == record["old_uuid"]:
            return _check(False, "old_disk_back", mount=mount)
        if not entry.get("uuid_ok"):
            return _check(False, "reader_old")
        live = [line for line in fstab_lines(facts.get("fstab") or "", mount) if line["active"]]
        if facts.get("fstab") is not None and len(live) > 1:
            return _check(False, "fstab_twice", mount=mount)
        if facts.get("fstab") is not None and live and not all(safe_boot(line["options"]) for line in live):
            return _check(False, "fstab_unsafe")
        # step 5 reads the two units by name: none may be left from an earlier checklist (closed, or finished without
        # its tidy block, in this boot), or step 5 would take that run's end for this one's. systemctl not answering
        # does not hold the step (what it proves is the reader's), but then nothing proved them gone: advance keeps
        # that in the record, and step 5 does not take their end for this rebuild's on its own (units_unproven)
        units = facts.get("units") or {}
        states = [unit_state(units.get(unit))[0] for unit in (REBUILD_UNIT, CHECK_UNIT)]
        if any(state not in ("absent", "unknown") for state in states):
            return _check(False, "old_rebuild")
        return {**_check(True, "mounted", mount=mount), **({"units_unread": True} if "unknown" in states else {})}
    if step == "rebuild":
        if service_back:
            return _check(False, "service_back")
        units = facts.get("units") or {}
        state, status = unit_state(units.get(REBUILD_UNIT))
        check, check_status = unit_state(units.get(CHECK_UNIT))
        entry = ((facts.get("info") or {}).get("mounts") or {}).get(record["disk"]) or {}
        if not reader_error and not (entry.get("mounted") and entry.get("uuid_ok")):
            return _check(False, "disk_not_mounted", mount=mount)
        # when what comes next would start the rebuild (again), it waits for the reader to say the new disk is mounted:
        # the rebuild writes to whatever is at the folder, the system disk too
        again = state in ("unknown", "absent", "stopped") or (state == "ended" and status != 0 and check == "absent")
        if reader_error and again:
            return _check(False, "reader_unknown", why=reader_error)
        if state == "unknown":
            return _check(None, "unit_unknown")
        if state == "absent":
            return _check(False, "not_started")
        if state == "running":
            return _check(False, "rebuild_running")
        if state == "stopped":
            return _check(False, "rebuild_stopped")
        if check == "unknown":
            return _check(None, "unit_unknown")
        if check == "absent":      # the check's block by its title: the rebuild's is hidden while the reader errs
            return _check(False, "rebuild_failed", out=f"{REBUILD_DIR}/rebuild.out") if status != 0 else \
                _check(False, "check_not_started", title=BLOCK_WORDS["check_title"])
        if check == "running":
            return _check(False, "check_running")
        if check == "stopped":
            return _check(False, "check_stopped")
        ended = unit_stamp(units.get(REBUILD_UNIT), "ExecMainExitTimestampMonotonic")
        began = unit_stamp(units.get(CHECK_UNIT), "ExecMainStartTimestampMonotonic")
        if ended is None or began is None:
            return _check(None, "order_unknown")
        if began < ended:                # pasted right after the rebuild's block: SnapRAID's lock stopped it
            return _check(False, "check_too_soon")
        if record.get("units_unread"):   # step 4 could not read them: they may be an earlier checklist's
            return _check(None, "units_unproven")
        return {**_check(True, "rebuilt"), "fix": status, "check": check_status}
    if step == "lost":
        return _check(True, "read")
    # done
    if not on_daemon:
        return _check(True, "reader_done")
    if not daemon or not daemon.get("answering"):
        return _check(False, "not_answering")
    health = (daemon.get("disk_health") or {}).get(record["disk"])
    if health == "degraded" or daemon.get("health") == "degraded":
        return _check(False, "still_missing", disk=record["label"])
    return _check(True, "answering")


# The tick that lets a step pass when its check cannot tell (ok None): the owner says he checked himself. A step not
# listed here (the new disk, step 4) never passes on a check that cannot tell.
SELF_TICKS = {"stop": "service_off", "name": "found", "boot": "boot", "rebuild": "rebuild_self"}
# The checks of step 4 that find a new disk mounted at the folder: from then on, no free disk's commands are offered.
NEW_DISK_IN = ("reader_old", "fstab_twice", "fstab_unsafe", "old_rebuild", "mounted")


def confirms_for(step: str, record: dict, facts: dict, check: dict | None) -> list[str]:
    """What the owner ticks before a step counts as done (an unknown check asks him to say he checked himself)."""
    unknown = check is not None and check.get("ok") is None
    if step == "stop":
        found = (facts.get("info") or {}).get("schedulers") or []
        return (["schedulers"] if record.get("route") != "daemon" and found else []) + ["apps"] + \
            (["service_off"] if record.get("route") == "daemon" and unknown else [])
    if step == "name":
        return ["found"]
    if step in ("boot", "rebuild"):
        return [SELF_TICKS[step]] if unknown else []
    if step == "lost":
        return ["read"]
    if step == "done":
        return ["ready", "apps_back"]
    return []


def _block(block_id: str, title: str, commands: list[str], **extra: Any) -> dict:
    return {"id": block_id, "title": title, "commands": commands, **{k: v for k, v in extra.items() if v}}


def _q(text: str) -> str:
    return shlex.quote(text)


def _run_unit(unit: str, what: str, record: dict, facts: dict, options: list[str], out: str) -> str:
    info = facts.get("info") or {}
    binary = info.get("binary") or DEFAULT_BINARY
    conf = info.get("conf") or DEFAULT_CONF
    folder = REBUILD_DIR
    return (f"sudo systemd-run --unit={unit.removesuffix('.service')} "
            f"--description={_q(what + ' of ' + record['label'].lower())} -p RemainAfterExit=yes "
            f"-p KillSignal=SIGINT -p SendSIGKILL=no -p StandardOutput=append:{folder}/{out}.out "
            f"-p StandardError=append:{folder}/{out}.out {_q(binary)} -c {_q(conf)} -d {record['disk']} "
            + " ".join(options))


def blocks_for(step: str, record: dict, facts: dict, check: dict | None, now: float) -> list[dict]:
    """The copy blocks of a step: the owner pastes them; Seraphina runs none."""
    mount = record["mount"]
    stamp = datetime.fromtimestamp(now).strftime("%Y%m%d-%H%M")
    if step == "stop" and record.get("route") == "daemon":
        return [_block("1a", BLOCK_WORDS["stop_title"], [f"sudo systemctl disable --now {DAEMON_UNIT}"],
                       expect=BLOCK_WORDS["stop_expect"], note=BLOCK_WORDS["stop_note"],
                       if_not=BLOCK_WORDS["stop_if_not"])]
    if step == "boot":
        if check is not None and check.get("code") != "needs_change":
            return []
        copy = f"/etc/fstab.before-disk-{record['number']}-{stamp}"
        if not SAFE_MOUNT.match(mount):
            return [_block("3a", BLOCK_WORDS["boot_title"], [], note=BLOCK_WORDS["boot_manual"].format(mount=mount))]
        return [_block("3a", BLOCK_WORDS["boot_title"],
                       [f"sudo cp -a /etc/fstab {copy}", comment_out_sed(mount), "sudo findmnt --verify",
                        "sudo systemctl daemon-reload"],
                       expect=BLOCK_WORDS["boot_expect"], note=BLOCK_WORDS["boot_note"].format(mount=mount),
                       if_not=f"sudo cp -a {copy} /etc/fstab && sudo systemctl daemon-reload")]
    if step == "disk":
        out = [_block("4a", BLOCK_WORDS["off_title"], ["sudo systemctl poweroff"], expect=BLOCK_WORDS["off_expect"])]
        code = (check or {}).get("code")
        if code == "old_disk_back":     # it works after all: the way back from steps 1 and 3
            out += way_back(record, facts)
        if code == "reader_old":        # the new disk is mounted, so no longer a free disk: its last block again
            out.append(reader_block(record))
        if code == "old_rebuild":
            out.append(_tidy("4f"))
        return out
    if step == "rebuild":
        code = (check or {}).get("code")
        if code in ("service_back", "disk_not_mounted", "reader_unknown"):
            return []             # no rebuild while the service is on, or before the new disk is known mounted
        out = []
        start = _run_unit(REBUILD_UNIT, "Rebuild", record, facts,
                          ["-l", f"{REBUILD_DIR}/rebuild.log", "fix"], "rebuild")
        check_cmd = _run_unit(CHECK_UNIT, "Check", record, facts,
                              ["-a", "-l", f"{REBUILD_DIR}/check.log", "check"], "check")
        proved = not (facts.get("reader") or {}).get("error")    # else the rebuild's own blocks wait (check_step)
        if proved:
            out.append(_block("5a", BLOCK_WORDS["rebuild_title"],
                              [f"sudo install -d -m 0700 -o root -g root {REBUILD_DIR}", start],
                              expect=BLOCK_WORDS["rebuild_expect"],
                              note=BLOCK_WORDS["rebuild_note"].format(out=f"{REBUILD_DIR}/rebuild.out")))
        out.append(_block("5b", BLOCK_WORDS["check_title"], [check_cmd], expect=BLOCK_WORDS["check_expect"],
                          note=BLOCK_WORDS["check_note"]))
        if code in ("rebuild_stopped", "rebuild_failed", "check_stopped", "check_too_soon"):
            unit, cmd = (REBUILD_UNIT, start) if code.startswith("rebuild") else (CHECK_UNIT, check_cmd)
            if proved or unit == CHECK_UNIT:
                out.append(_block("5c", BLOCK_WORDS["again_title"],
                                  [f"sudo systemctl stop {unit}", f"sudo systemctl reset-failed {unit}", cmd],
                                  expect=BLOCK_WORDS["again_expect"].format(unit=unit)))
        return out
    if step == "lost":
        out = [_block("6a", BLOCK_WORDS["list_title"],
                      [f"sudo find {_q(mount)} -name '*.unrecoverable' -printf '%P\\n'"],
                      expect=BLOCK_WORDS["list_expect"]),
               _block("6b", BLOCK_WORDS["owner_title"], [f"sudo chown -R 1000:1000 {mount}/<the app's folder>"],
                      note=BLOCK_WORDS["owner_note"])]
        if not _all_back(record):
            out.append(_block("6c", BLOCK_WORDS["ends_title"],
                              [f"sudo tail -n 20 {REBUILD_DIR}/rebuild.out {REBUILD_DIR}/check.out"],
                              expect=BLOCK_WORDS["ends_expect"]))
        return out
    if step == "done":
        out = [_tidy("7a")]
        if record.get("route") == "daemon":
            out.append(_block("7b", BLOCK_WORDS["start_title"], [f"sudo systemctl enable --now {DAEMON_UNIT}"],
                              expect=BLOCK_WORDS["start_expect"]))
        return out
    return []


def _tidy(block_id: str) -> dict:
    """The block that stops and forgets the two units (the end of the checklist, or units left from an earlier one).
    Either may not be loaded (only half of them left, or already gone): systemctl then prints that it is not loaded,
    and still stops the other one. That line is not shown, so that "Nothing printed" holds; what the block did is
    proved by the step's own check (4f), or by step 4 of the next checklist (7a)."""
    return _block(block_id, BLOCK_WORDS["tidy_title"],
                  [f"sudo systemctl stop {REBUILD_UNIT} {CHECK_UNIT} 2>/dev/null; true",
                   f"sudo systemctl reset-failed {REBUILD_UNIT} {CHECK_UNIT} 2>/dev/null; true"],
                  expect=BLOCK_WORDS["tidy_expect"])


def reader_block(record: dict) -> dict:
    """Block 4e: the reader's settings told the filesystem now mounted where the old disk was (findmnt at the folder,
    once block 4d mounted it). Built from the record alone, so the page shows it again once the new disk is mounted
    and no longer listed as free (after a reload, or on another device). With nothing mounted there, the sed leaves
    an empty name, explain refuses it, and the move does not happen."""
    mount, disk = record["mount"], record["disk"]
    found = f"sudo findmnt -n -o UUID --mountpoint {mount}"
    then = [f"sudo {READER} explain helper-config > /dev/null && sudo mv {READER_CONFIG}.new {READER_CONFIG}"]
    old = record.get("old_uuid")
    if old and UUID_RE.match(old):
        return _block("4e", BLOCK_WORDS["reader_title"],
                      [f"sudo cp -a {READER_CONFIG} {READER_CONFIG}.new",
                       f'sudo sed -i "s/{old}/$({found})/" {READER_CONFIG}.new',
                       f"sudo {READER} explain helper-config"],
                      expect=BLOCK_WORDS["reader_expect"].format(name=disk), then=then,
                      note=BLOCK_WORDS["reader_note"], if_not=f"sudo rm {READER_CONFIG}.new")
    return _block("4e", BLOCK_WORDS["reader_title"],
                  [found, f"sudo cp -a {READER_CONFIG} {READER_CONFIG}.new", f"sudoedit {READER_CONFIG}.new",
                   f"sudo {READER} explain helper-config"],
                  expect=BLOCK_WORDS["reader_expect"].format(name=disk), then=then,
                  note=BLOCK_WORDS["reader_manual"].format(name=disk), if_not=f"sudo rm {READER_CONFIG}.new")


def _all_back(record: dict) -> bool:
    result = (record.get("results") or {}).get("rebuild") or {}
    return result.get("fix") == 0 and result.get("check") == 0


def way_back(record: dict, facts: dict) -> list[dict]:
    """The blocks that undo steps 1 and 3 (the old disk works after all, or the checklist was closed before a new
    disk went in): the failed disk's line in /etc/fstab when no active line mounts it any more, and the protection
    service on the daemon's route."""
    out = []
    fstab = facts.get("fstab")
    lines = fstab_lines(fstab, record["mount"], record.get("old_uuid")) if fstab is not None else []
    if "boot" in (record.get("done") or {}) and lines and not any(line["active"] for line in lines):
        copies = f"/etc/fstab.before-disk-{record['number']}-"
        out.append(_block("3z", BLOCK_WORDS["line_back_title"],
                          [f"ls -1 {copies}*", f"sudo cp -a {copies}<the newest one> /etc/fstab",
                           "sudo findmnt --verify", "sudo systemctl daemon-reload"],
                          expect=BLOCK_WORDS["line_back_expect"], note=BLOCK_WORDS["line_back_note"]))
    if record.get("route") == "daemon" and "stop" in (record.get("done") or {}):
        out.append(_block("1z", BLOCK_WORDS["start_title"], [f"sudo systemctl enable --now {DAEMON_UNIT}"],
                          expect=BLOCK_WORDS["start_expect"]))
    return out


def _what(step: str, record: dict, facts: dict, now: float, zone: Any) -> list[str]:
    label = record["label"]
    as_of = _when(facts.get("protected_up_to"), now, zone)
    if step == "stop":
        return [WHAT["stop"], WHAT["stop_daemon"] if record.get("route") == "daemon" else WHAT["stop_reader"],
                WHAT["apps"]]
    if step == "disk":
        return [WHAT["disk"], WHAT["disk_size"], WHAT["disk_erase"]]
    if step == "rebuild":
        out = [WHAT["rebuild"], WHAT["rebuild_time"]]
        if facts.get("power_off"):
            out.append(WHAT["rebuild_power"].format(off=facts["power_off"]))
        return out + [WHAT["rebuild_as_of"].format(when=as_of)]
    if step == "lost":
        # SnapRAID's exit codes cannot tell files left as .unrecoverable from a run that stopped at its start: only
        # 0 and 0 say that every file came back
        first = WHAT["lost_all"].format(disk=label, when=as_of) if _all_back(record) else WHAT["lost_unknown"]
        return [first, WHAT["lost_added"].format(disk=label, when=as_of), WHAT["lost_changed"],
                WHAT["lost_elsewhere"], WHAT["lost_owner"]]
    if step == "done":
        if record.get("route") != "daemon":
            return [WHAT["done"], WHAT["done_reader"], WHAT["done_apps"]]
        window = (facts.get("daemon") or {}).get("window") or "the next one"
        return [WHAT["done"], WHAT["done_apps"], WHAT["done_next"].format(window=window)]
    return [WHAT[step]]


def _facts_lines(step: str, record: dict, facts: dict, now: float, zone: Any) -> list[str]:
    info = facts.get("info") or {}
    if step == "stop":
        out = []
        daemon = facts.get("daemon") or {}
        if daemon.get("answering") and daemon.get("active"):
            from .snapraidd import WORDS_OF

            out.append(FACT_WORDS["running"].format(what=WORDS_OF.get(daemon["active"].get("command"), "a step")))
        if record.get("route") != "daemon":
            places = info.get("schedulers") or []
            out.append(FACT_WORDS["schedulers"].format(places=", ".join(places)) if places
                       else FACT_WORDS["no_schedulers"])
        stacks = facts.get("stacks")
        out.append(FACT_WORDS["stacks_unknown"] if stacks is None else
                   FACT_WORDS["stacks"].format(names=", ".join(stacks)) if stacks else FACT_WORDS["no_stacks"])
        return out
    if step == "name":
        known = (facts.get("disks") or {}).get(record["disk"]) or {}
        out = [FACT_WORDS["where"].format(label=record["label"], name=record["disk"], mount=record["mount"])]
        if known.get("model") or known.get("serial"):
            out.append(FACT_WORDS["model"].format(model=known.get("model") or "unknown",
                                                  serial=known.get("serial") or "unknown"))
        if known.get("by_id"):
            out.append(FACT_WORDS["by_id"].format(by_id=known["by_id"]))
        if known.get("seen_at") and not known.get("present"):
            out.append(FACT_WORDS["seen"].format(when=_when(_stamp(known["seen_at"]), now, zone)))
        health = ((facts.get("daemon") or {}).get("disk_health") or {}).get(record["disk"]) or \
            (facts.get("smart") or {}).get(record["disk"])
        out.append(FACT_WORDS["health"].format(words=HEALTH_WORDS.get(health, HEALTH_WORDS[None])))
        return out
    if step in ("disk", "rebuild"):
        known = (facts.get("disks") or {}).get(record["disk"]) or {}
        entry = (info.get("mounts") or {}).get(record["disk"]) or {}
        if entry.get("mounted") and known.get("present") and (known.get("model") or known.get("serial")):
            return [FACT_WORDS["mounted_now"].format(mount=record["mount"], model=known.get("model") or "unknown",
                                                     serial=known.get("serial") or "unknown")]
    return []


def _stamp(text: Any) -> float | None:
    with contextlib.suppress(TypeError, ValueError):
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).timestamp()
    return None


def protected_disk(row: dict, facts: dict) -> bool:
    """row (a free disk the reader lists) is one of the disks SnapRAID protects, present or last known (the failed
    one too), by its /dev/disk/by-id name or its serial number: never offered, never erased."""
    by_id = re.sub(r"-part\d+$", "", str(row.get("by_id") or ""))
    serial = str(row.get("serial") or "").strip().upper()
    for disk in (facts.get("disks") or {}).values():
        if by_id and by_id == re.sub(r"-part\d+$", "", str((disk or {}).get("by_id") or "")):
            return True
        if serial and serial == str((disk or {}).get("serial") or "").strip().upper():
            return True
    return False


def spare_rows(record: dict, facts: dict) -> list[dict]:
    """The free disks for the new one: blank ones first, then the ones that hold something; never a serial in full
    beyond what the cockpit already shows, and never one of the protected disks."""
    rows = []
    for row in facts.get("spare") or []:
        if protected_disk(row, facts):
            continue
        signed = bool(row.get("signature"))
        rows.append({"by_id": row.get("by_id"), "device": row.get("device"), "size": row.get("size"),
                     "size_words": _size(row.get("size")), "model": row.get("model"), "serial": row.get("serial"),
                     "signed": signed, "ask": TYPED_ASK["signed" if signed else "blank"],
                     "smaller": bool(record.get("old_size") and isinstance(row.get("size"), int)
                                     and row["size"] < record["old_size"]),
                     "usable": bool(row.get("by_id") and SAFE_BY_ID.match(str(row.get("by_id"))) and row.get("serial"))})
    return sorted(rows, key=lambda r: (r["signed"], -(r["size"] or 0)))


def build(record: dict | None, facts: dict, now: float, zone: Any = None) -> dict:
    """The checklist page (GET /api/snapraid/rebuild)."""
    page: dict[str, Any] = {"title": TITLE, "sub": SUB, "lab_line": LAB_LINE, "open": False, "record": None,
                            "current": None, "steps": [], "choices": [], "choose": CHOOSE, "suggested": SUGGESTED,
                            "no_protection": NO_PROTECTION, "close_words": CLOSE_WORDS, "start_problem": None,
                            "finished": False, "spare_words": SPARE_WORDS, "way_back": None}
    if record is None or record.get("state") != "open":
        if (facts.get("reader") or {}).get("error"):
            page["start_problem"] = START_REFUSALS["reader_error"].format(why=facts["reader"]["error"])
        elif not facts.get("info"):
            page["start_problem"] = START_REFUSALS["no_reader"]
        page["choices"] = choices(facts)
        if record is not None:
            page["record"] = public(record)
            page["finished"] = record.get("state") == "finished"
            done = record.get("done") or {}
            if record.get("state") == "cancelled" and "stop" in done and "disk" not in done:
                page["way_back"] = {"words": [WAY_BACK.format(mount=record.get("mount")), WHAT["done_apps"]],
                                    "blocks": way_back(record, facts)}
        return page
    step_now = current(record)
    page.update(open=True, record=public(record), current=step_now)
    for index, step in enumerate(STEPS, 1):
        done = step in (record.get("done") or {})
        state = "done" if done else "current" if step == step_now else "later"
        check = check_step(step, record, facts) if state == "current" else (record.get("checks") or {}).get(step)
        given = set((record.get("confirmed") or {}).get(step) or [])
        entry = {"id": step, "n": index, "title": TITLES[step], "state": state,
                 "done_at": (record.get("done") or {}).get(step), "what": _what(step, record, facts, now, zone),
                 "facts": _facts_lines(step, record, facts, now, zone) if state != "later" else [],
                 "blocks": blocks_for(step, record, facts, check, now) if state == "current" else [],
                 "check": check, "cannot_undo": step == "disk",
                 "confirms": [{"item": item, "words": CONFIRMS[item], "given": item in given}
                              for item in confirms_for(step, record, facts, check)] if state != "later" else []}
        if step == "stop" and state == "current" and record.get("route") == "daemon":
            daemon = facts.get("daemon") or {}
            entry["skip"] = bool((daemon.get("offers") or {}).get("hold_off") and daemon.get("answering")
                                 and not daemon.get("hold_off"))
            entry["skipped"] = bool(daemon.get("hold_off"))
        if step == "disk" and state == "current":
            entry["spare"] = None if check["code"] in NEW_DISK_IN else spare_rows(record, facts)
        page["steps"].append(entry)
    return page


def public(record: dict) -> dict:
    return {key: record.get(key) for key in ("id", "state", "disk", "number", "label", "mount", "route", "opened_at",
                                              "done", "closed_at", "results")}


def advance(record: dict, facts: dict, step: str, confirm: list | None, now: float) -> tuple[dict, dict]:
    """The owner's "Check and go on" on step: records what he ticked, runs the step's check, and marks the step done
    when the check passes (or cannot tell, and he ticked that he checked himself: SELF_TICKS) and every line is
    ticked. Returns
    (record, {done, check, missing}); ValueError when the step is not the one to do now."""
    if record.get("state") != "open":
        raise ValueError(CLOSED)
    step_now = current(record)
    if step not in STEPS:
        raise ValueError(f"step must be one of {', '.join(STEPS)}")
    if step != step_now:
        raise ValueError(IN_ORDER.format(title=TITLES[step_now].lower() if step_now else "nothing"))
    units = facts.get("units") or {}
    if step == "rebuild" and record.get("units_unread") and \
            all(unit_state(units.get(unit))[0] == "absent" for unit in (REBUILD_UNIT, CHECK_UNIT)):
        record.pop("units_unread")       # read now, and gone: what step 5 reads from here on is this checklist's
    check = check_step(step, record, facts)
    wanted = confirms_for(step, record, facts, check)
    items = [item for item in confirm or [] if item in wanted]
    if confirm and len(items) != len(confirm):
        raise ValueError(f"confirm only these here: {', '.join(wanted) or 'nothing'}")
    given = sorted(set((record.get("confirmed") or {}).get(step) or []) | set(items))
    record.setdefault("confirmed", {})[step] = given
    record.setdefault("checks", {})[step] = {key: check[key] for key in ("ok", "code", "words")}
    missing = [item for item in wanted if item not in given]
    done = not missing and (check["ok"] is True or (check["ok"] is None and SELF_TICKS.get(step) in wanted))
    if done:
        record.setdefault("done", {})[step] = now
        if step == "disk" and check.get("units_unread"):    # nothing proved step 5's units gone (check_step)
            record["units_unread"] = True
        if step == "rebuild" and check["ok"] is True:      # ticked by hand: no exit codes, so step 6 cannot tell
            record.setdefault("results", {})["rebuild"] = {"fix": check.get("fix"), "check": check.get("check")}
        if step == "done":
            record["state"] = "finished"
            record["closed_at"] = now
    return record, {"done": done, "check": check, "missing": missing,
                    "words": TICK_FIRST if check["ok"] is not False and missing else check["words"]}


def prepare(record: dict, facts: dict, by_id: str, typed: str, now: float) -> dict:
    """The new disk's commands (POST /api/snapraid/rebuild/prepare): only for a free disk Seraphina sees now, named by
    its /dev/disk/by-id name, once the owner typed its serial number (the last 4 characters of a blank disk's, the
    whole of one that already holds something), and only while no new disk is mounted at the folder. Nothing is
    stored: the commands are built again on each ask. ValueError with the refusal's words."""
    if current(record) != "disk":
        raise ValueError(PREPARE_REFUSALS["not_now"])
    if check_step("disk", record, facts)["code"] in NEW_DISK_IN:
        raise ValueError(PREPARE_REFUSALS["mounted"].format(mount=record["mount"]))
    row = next((r for r in facts.get("spare") or [] if r.get("by_id") == by_id), None)
    if row is None:
        raise ValueError(PREPARE_REFUSALS["not_spare"])
    if protected_disk(row, facts):
        raise ValueError(PREPARE_REFUSALS["protected"])
    if not SAFE_BY_ID.match(str(by_id)):
        raise ValueError(PREPARE_REFUSALS["no_by_id"])
    serial = str(row.get("serial") or "")
    if not serial:
        raise ValueError(PREPARE_REFUSALS["no_serial"])
    typed = str(typed or "").strip()
    signed = bool(row.get("signature"))
    if (signed and typed != serial) or (not signed and (len(typed) != 4 or typed.upper() != serial[-4:].upper())):
        raise ValueError(PREPARE_REFUSALS["typed"])
    mount, fstype = record["mount"], record.get("fstype") or "ext4"
    part = f"{by_id}-part1"
    stamp = datetime.fromtimestamp(now).strftime("%Y%m%d-%H%M")
    copy = f"/etc/fstab.before-new-disk-{record['number']}-{stamp}"
    new_uuid = f"$(sudo blkid -s UUID -o value {part})"
    blocks = []
    if signed:
        blocks.append(_block("4b", BLOCK_WORDS["wipe_title"], [f"sudo wipefs -a {by_id}"],
                             expect=BLOCK_WORDS["wipe_expect"]))
    # the new partition starts where most old ones did: what an old one held (a filesystem, LVM, RAID) shows again
    # inside it, and mkfs would stop at it; so it is erased too, before mkfs
    blocks.append(_block("4c", BLOCK_WORDS["part_title"],
                         [f"sudo parted -s -a optimal {by_id} mklabel gpt mkpart primary {fstype} 0% 100%",
                          "sudo udevadm settle", f"sudo wipefs -a {part}", f"sudo mkfs.{fstype} {part}"],
                         expect=BLOCK_WORDS["part_expect"]))
    blocks.append(_block("4d", BLOCK_WORDS["mount_title"],
                         [f"sudo cp -a /etc/fstab {copy}", comment_out_sed(mount),
                          f'echo "UUID={new_uuid} {mount} {fstype} defaults,nofail,x-systemd.device-timeout=30s 0 2" '
                          f"| sudo tee -a /etc/fstab",
                          "sudo findmnt --verify", "sudo systemctl daemon-reload", f"sudo mount {mount}",
                          f"findmnt {mount}"],
                         expect=BLOCK_WORDS["mount_expect"].format(mount=mount), note=BLOCK_WORDS["mount_note"],
                         if_not=f"sudo cp -a {copy} /etc/fstab && sudo systemctl daemon-reload"))
    blocks.append(reader_block(record))
    return {"by_id": by_id, "signed": signed, "blocks": blocks}


def light(record: dict | None) -> dict | None:
    """The card's light while a checklist is open: red, the disk named, with the note."""
    if not record or record.get("state") != "open":
        return None
    return {"tone": "err", "code": "rebuild", "reason": REASON.format(disk=record.get("label") or "A disk"),
            "note": NOTE, "disk": str(record.get("number") or "")}


def summary(record: dict | None) -> dict | None:
    """The card's part (GET /api/snapraid "rebuild"): null when no checklist was ever opened."""
    if not record:
        return None
    step = current(record)
    return {"id": record.get("id"), "state": record.get("state"), "disk": record.get("disk"),
            "label": record.get("label"), "step": step, "n": STEPS.index(step) + 1 if step else None,
            "of": len(STEPS), "title": TITLES.get(step) if step else None, "closed_at": record.get("closed_at")}


# ---------------------------------------------------------------- the checklist of one panel (I/O)


class Checklist:
    """The checklist of one SnapraidPanel: the record, the live facts, the journal and the events. Injectables for
    the tests: systemctl (unit name → systemctl show's properties, or None), fstab (the path), proc, stacks (the
    Dockge folder)."""

    def __init__(self, panel: Any, *, systemctl: Callable[[str], dict | None] | None = None,
                 fstab: str = "/etc/fstab", proc: str = "/proc", stacks: str = DOCKGE_STACKS) -> None:
        self.panel = panel
        self.systemctl = systemctl or _systemctl_show
        self.fstab = fstab
        self.proc = proc
        self.stacks = stacks

    def path(self) -> Path:
        return self.panel.folder() / FILE

    def record(self) -> dict | None:
        try:
            data = json.loads(self.path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and data.get("v") == 1 else None

    def open_one(self) -> dict | None:
        record = self.record()
        return record if record and record.get("state") == "open" else None

    def _save(self, record: dict) -> None:
        from .snapraid import _write_private

        _write_private(self.panel.folder(), FILE, json.dumps(record, indent=1, sort_keys=True))

    def facts(self) -> dict:
        """The live facts the checks read; every read changes nothing."""
        from .snapraid import HelperError

        panel = self.panel
        route = "daemon" if panel.route() in ("daemon", "metrics") else "reader"
        facts: dict[str, Any] = {"route": route, "reader": {}, "info": None, "disks": {}, "spare": []}
        if panel.reader_present() and panel.state() == "ready":
            client = panel.client()
            try:
                facts["info"] = summarize_info(client.call("info"))
                facts["disks"], facts["spare"] = summarize_disks(client.call("disks"))
            except HelperError as exc:
                facts["reader"] = {"error": exc.words}
        facts["daemon"] = self._daemon() if route == "daemon" else None
        if route == "reader":
            smart = (panel.facts().get("smart") or {}).get("disks") or {}
            facts["smart"] = {name: entry.get("health") for name, entry in smart.items()}
            newest = ((panel.facts().get("status") or {}).get("facts") or {}).get("newest_block")
            facts["protected_up_to"] = float(newest) if newest else None
        else:
            facts["protected_up_to"] = (facts["daemon"] or {}).get("protected_up_to")
        facts["units"] = {unit: self.systemctl(unit) for unit in (DAEMON_UNIT, REBUILD_UNIT, CHECK_UNIT)}
        facts["snapraid_running"] = snapraid_running(self.proc) or bool((facts.get("info") or {}).get("snapraid_running"))
        try:
            facts["fstab"] = Path(self.fstab).read_bytes().decode("utf-8", "replace")
        except OSError:
            facts["fstab"] = None
        points = list((facts.get("info") or {}).get("data") or [])
        facts["stacks"] = stacks_using(points, self.stacks) if points else None
        facts["power_off"] = self._power_off()
        return facts

    def _daemon(self) -> dict:
        from .snapraidd import MetricsError, disk_keys

        route = self.panel.daemon
        try:
            metrics = route.metrics_reader().read()
            answering = True
        except (MetricsError, ValueError):
            metrics, answering = {}, False
        view, _ = route.view()
        names = {name: (metrics.get("disks") or {}).get(name, {}).get("health")
                 for name, _, _ in disk_keys(metrics.get("disks") or {})} if answering else \
            {disk.get("name"): disk.get("health") for disk in view.get("disks") or []}
        return {"answering": answering, "active": metrics.get("active") if answering else None,
                "hold_off": bool(metrics.get("hold_off")) if answering else bool(view.get("hold_off")),
                "health": metrics.get("health") if answering else view.get("health"), "disk_health": names,
                "window": (view.get("window") or {}).get("words"), "offers": view.get("offers") or {},
                "protected_up_to": view.get("protected_up_to")}

    def _power_off(self) -> str | None:
        from .clock import PowerSchedule

        wakes = (getattr(self.panel.config, "raw", {}) or {}).get("wakes") or {}
        with contextlib.suppress(ValueError, TypeError, AttributeError):
            power = PowerSchedule.from_config(wakes.get("power") if isinstance(wakes, dict) else None)
            if power is not None:
                return power.off.strftime("%H:%M")
        return None

    # -- the routes ---------------------------------------------------------------------------------------------

    def page(self) -> dict:
        return build(self.record(), self.facts(), self.panel.clock(), self.panel._zone())

    def start(self, disk: str) -> dict:
        if self.open_one() is not None:
            raise ValueError(START_REFUSALS["open"])
        if not self.panel.reader_present() or self.panel.state() != "ready":
            raise ValueError(START_REFUSALS["no_reader"])
        facts = self.facts()
        previous = self.record()
        number = int(str((previous or {}).get("id", "R-0"))[2:] or 0) + 1
        record = open_record(facts, str(disk or ""), self.panel.clock(), number)
        self._save(record)
        self._journal(record, "opened", f"disk checklist {record['id']} opened for disk {record['number']}")
        return build(record, facts, self.panel.clock(), self.panel._zone())

    def step(self, step: str, confirm: list | None = None) -> dict:
        record = self.open_one()
        if record is None:
            raise ValueError(CLOSED)
        facts = self.facts()
        record, result = advance(record, facts, step, confirm, self.panel.clock())
        self._save(record)
        if result["done"]:
            self._journal(record, "step", f"disk checklist {record['id']}: step {STEPS.index(step) + 1} done "
                                          f"({step})", step=step)
            if record["state"] == "finished":
                self._journal(record, "finished", f"disk checklist {record['id']} finished")
        return {**build(record, facts, self.panel.clock(), self.panel._zone()),
                "result": {"step": step, "done": result["done"], "words": result["words"],
                           "ok": result["check"]["ok"], "code": result["check"]["code"], "missing": result["missing"]}}

    def prepare(self, by_id: str, typed: str) -> dict:
        record = self.open_one()
        if record is None:
            raise ValueError(CLOSED)
        return prepare(record, self.facts(), str(by_id or ""), str(typed or ""), self.panel.clock())

    def close(self) -> dict:
        record = self.open_one()
        if record is None:
            raise ValueError(CLOSED)
        record.update(state="cancelled", closed_at=self.panel.clock())
        self._save(record)
        self._journal(record, "cancelled", f"disk checklist {record['id']} closed before the end")
        return build(record, self.facts(), self.panel.clock(), self.panel._zone())

    # -- what the rest of the panel asks -------------------------------------------------------------------------

    def light(self) -> dict | None:
        return light(self.open_one())

    def summary(self) -> dict | None:
        return summary(self.record())

    def expects_down(self) -> bool:
        """The owner turned the protection service off on purpose for the rebuild (step 1 done, the checklist still
        open): its silence raises no "not answering" notice meanwhile."""
        record = self.open_one()
        return bool(record and record.get("route") == "daemon" and "stop" in (record.get("done") or {}))

    def _journal(self, record: dict, state: str, summary_text: str, **fields: Any) -> None:
        self.panel._journal("rebuild", summary_text, checklist=record["id"], disk=record.get("number"), state=state,
                            **fields)
        if self.panel.bus is not None:
            self.panel.bus.publish("snapraid.rebuild", {"checklist": record["id"], "state": record.get("state"),
                                                        "step": current(record), "disk": record.get("number")})
        with contextlib.suppress(Exception):
            self.panel._publish()


def doctor_lines(config: Any, make: Callable[..., dict]) -> list[dict]:
    """The doctor's line while a disk checklist is open (a warning: a disk is being replaced)."""
    path = Path(getattr(config, "data_dir", "") or ".") / "snapraid" / FILE
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    step = current(record if isinstance(record, dict) else None)
    if step is None:
        return []
    return [make("snapraid disk checklist", False, "warn",
                 f"{record.get('label') or 'a disk'} is being replaced: the checklist in Backups is at step "
                 f"{STEPS.index(step) + 1} of {len(STEPS)} ({TITLES[step].lower()}), open since "
                 f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(float(record.get('opened_at') or 0)))}")]
