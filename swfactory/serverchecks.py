"""Your server's Check buttons (DESIGN.md section 42; docs/seraphina/YOUR_SERVER.md §7.2), phase P1b. **They only
look.** Each builds one result from what Seraphina's own user can read and from the read verbs of her system access
(bin/sf-system); none installs, removes, restarts, upgrades or cleans anything.

1. Updates available: `apt-get -s upgrade` and `apt-get -s dist-upgrade` (simulations, as Seraphina's own user),
   parsed from their Inst lines and summary line; a security fix is a line whose suite ends in -security; the
   families of §7.6 (kernel and boot, GPU, Docker, remote access and the system manager, Seraphina's own) and the
   packages tied to them by exact versions (`apt-cache show <package>=<version>`, Depends, Pre-Depends and Breaks
   with `=`) are left out, each with the copy block the owner runs at home; `apt-mark showhold`; the list's age
   from /var/lib/apt/periodic/update-success-stamp; whether Ubuntu's automatic updates are on. Refreshing the list
   is the helper's apt.refresh (class refresh).
2. Restart needed?: /run/reboot-required and its .pkgs, and the helper's needrestart (the kernel and one row per
   service, never one button for all; the services Ubuntu's own needrestart never restarts, and those that reach
   the server, say so).
3. Disk use: the host strip's disks, Docker's own sums (GET /system/df through the helper), the journal's size, the
   apt cache, the previous versions kept; on request, the biggest folders of one disk (the helper's du), shown and
   never stored.
4. Failed services: `systemctl list-units --failed` and `list-timers --all` (JSON, as Seraphina's own user), each
   timer's service Result, and the apps whose light says they stopped with an error or keep restarting.
5. Disk health: the helper's smart, its exit status decoded bit by bit (asleep is never failing; can't read is never
   failing).
6. What is listening: the helper's ports joined to apps, each with its scope (this machine only, every address, the
   Tailscale network, the home network, one home-network address of this server, public), the login gateway as one
   row, and a direct port that skips the gateway's sign-in named as such.
7. Tailscale: the Tailscale containers, their serve entries and Funnel.

Words a person reads are templates here (U8 checks them); names come from the server and are shown as text.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
from typing import Any, Callable

# ---------------------------------------------------------------- the checks and their words (novice; U8)

CHECKS = (
    {"id": "updates", "number": 1, "title": "Updates available",
     "what": "Which Ubuntu updates are waiting, which are security fixes, and which must be done at home.",
     "commands": ["apt-get -s upgrade", "apt-get -s dist-upgrade", "apt-cache show <package>=<version>",
                  "apt-mark showhold"]},
    {"id": "restart", "number": 2, "title": "Restart needed?",
     "what": "Whether a new kernel waits for a restart, and which services still use old libraries.",
     "commands": ["cat /run/reboot-required.pkgs", "needrestart -b -r l (through Seraphina's system access)"]},
    {"id": "disk", "number": 3, "title": "Disk use",
     "what": "How full each disk is, what Docker and the system logs use, and the previous versions kept.",
     "commands": ["statvfs of each disk", "GET /system/df (through Seraphina's system access)",
                  "journalctl --disk-usage", "du -x -d 2 -B1 <disk> (on request)"]},
    {"id": "failed", "number": 4, "title": "Failed services",
     "what": "Services and timers that failed, and apps that stopped with an error or keep restarting.",
     "commands": ["systemctl list-units --failed --output=json", "systemctl list-timers --all --output=json"]},
    {"id": "health", "number": 5, "title": "Disk health",
     "what": "What each disk says about its own health. A sleeping disk is never woken.",
     "commands": ["smartctl -H -A -j -n standby,3 /dev/<disk> (through Seraphina's system access)"]},
    {"id": "listening", "number": 6, "title": "What is listening",
     "what": "Every program that waits for connections, and who can reach it.",
     "commands": ["ss -Htlnp and ss -Hulnp (through Seraphina's system access)",
                  "systemctl list-sockets --all --output=json"]},
    {"id": "tailscale", "number": 7, "title": "Tailscale",
     "what": "Each Tailscale connection, what it serves, and whether anything is open to the whole internet.",
     "commands": ["the state files of the Tailscale containers (through Seraphina's system access)"]},
)
CHECK_IDS = tuple(c["id"] for c in CHECKS)
NEVER_RUN = "Not checked yet."
NEEDS_ACCESS = "This check needs Seraphina's system access, which is not set up yet."
FAMILIES = (
    {"id": "kernel", "name": "the kernel", "patterns": (r"^linux-", r"^grub", r"^shim"),
     "words": "These change what the server starts with. Run this when you can be at the machine, then restart it "
              "and watch it come back. If it does not start, choose the previous entry in the boot menu."},
    {"id": "gpu", "name": "the graphics driver", "patterns": (r"^nvidia-", r"^libnvidia-", r"^cuda-"),
     "words": "These change the graphics driver. Run this at home; the AI apps stop working until the server "
              "restarts."},
    {"id": "docker", "name": "Docker", "patterns": (r"^docker-ce", r"^containerd\.io$", r"^docker-compose-plugin$",
                                                  r"^docker-buildx-plugin$"),
     "words": "Every app stops for a minute or two, and you lose remote access while it happens. Run this at home."},
    {"id": "access", "name": "remote access and systemd",
     "patterns": (r"^tailscale$", r"^openssh-server$", r"^netplan\.io$", r"^network-manager$", r"^systemd"),
     "words": "These change how you reach the server. Run this at home, from Cockpit's terminal on your home network "
              "or at the machine itself."},
    {"id": "seraphina", "name": "Seraphina", "patterns": (r"^seraphina",),
     "words": "Seraphina is updated her own way, never in the middle of a change."},
)
DOCKER_PACKAGES = ("docker-ce", "docker-ce-cli", "containerd.io", "docker-compose-plugin", "docker-buildx-plugin")
AUTO_UPDATES = ("Ubuntu's automatic updates are on, so the security fixes among these may install by themselves some "
                "morning. A kernel installed that way starts at the next power-on, with nobody watching.")
# needrestart's own never-restart list in Ubuntu's config (override_rc), and the units that reach the server (§7.4 F1)
NEVER_RESTART = (r"^dbus", r"^gdm", r"^lightdm", r"^sddm", r"^xdm", r"^kdm", r"^systemd-networkd", r"^NetworkManager",
                 r"^networking", r"^wpa_supplicant", r"^getty@", r"^serial-getty@", r"^user@", r"^docker",
                 r"^containerd", r"^systemd-logind", r"^unattended-upgrades", r"^apt-daily", r"^cloud-",
                 r"^ssh", r"^sshd", r"^tailscaled", r"^cockpit", r"^factory", r"^seraphina", r"^emergency",
                 r"^rescue", r"^systemd-journald", r"^systemd-udevd")
WORDS = {
    "updates_none": "No updates are waiting.",
    "updates_line": "{count}, {security} of them security fixes.",
    "updates_plain": "{count} are ordinary updates (Seraphina does not install them yet).",
    "updates_home": "{count} must be done at home:",
    "updates_held": "{count} need more than an update (new or removed packages). Do them in Cockpit's terminal.",
    "updates_phased": "Ubuntu is still rolling {count} out.",
    "updates_listed": "List checked {age}.",
    "updates_never_listed": "Seraphina can't tell when the list was last refreshed.",
    "updates_failed": "The list of updates could not be read.",
    "updates_tied": "waits with {family}",
    "updates_hold": "First, so a plain upgrade in Cockpit or a terminal cannot restart every app in the middle of "
                    "the day, hold Docker's packages:",
    "restart_kernel": "Restart needed: a new kernel is installed but not running yet.",
    "restart_abi": "A restart would start an updated kernel of the same version.",
    "restart_file": "Ubuntu says a restart is needed for updates.",
    "restart_none": "No restart is needed.",
    "restart_services": "{count} use old libraries.",
    "restart_service": "Uses old libraries. Seraphina cannot restart it yet; Cockpit can, under Services.",
    "restart_never": "This one is restarted only by restarting the server.",
    "restart_no_access": "Which services use old libraries needs Seraphina's system access.",
    "disk_system": "System disk: {free} free of {total}.",
    "disk_other": "{label}: {free} free of {total}.",
    "disk_docker": "Docker uses {total}: {images} of images ({unused} that no app uses), {cache} of leftover build "
                   "files.",
    "disk_volumes": "Docker's storage that no container uses: {size}.",
    "disk_logs": "System logs: {size}.",
    "disk_logs_unknown": "System logs: unknown.",
    "disk_apt": "Downloaded packages: {size}.",
    "disk_kept": "Previous versions kept: {size}.",
    "disk_docker_off": "Docker's own numbers need Seraphina's system access and Docker running.",
    "failed_none": "Nothing has failed.",
    "failed_some": "{count} failed.",
    "failed_unit": "The service failed.",
    "failed_timer": "Its last run failed.",
    "health_ok": "All {count} healthy.",
    "health_asleep": "It was asleep, so it was not woken. Its last reading is not here.",
    "health_failing": "It reports a failure. Replace it soon.",
    "health_warning": "It reports errors in its own logs. Keep an eye on it.",
    "health_unreadable": "Seraphina could not read it.",
    "health_good": "Healthy.",
    "health_some": "{bad} of {count} need a look.",
    "health_no_tool": "Seraphina could not check the disks: the disk health tool (the smartmontools package) is not "
                      "installed.",
    "health_none": "Seraphina found no disk to check.",
    "health_unread": "Seraphina could not read any of the {count}.",
    "health_part": "{good} of {count} healthy.",
    "health_not_read": "{unread} could not be read.",
    "listening_line": "{count} listen.",
    "listening_home": "{count} can be reached from your home network without Tailscale: {names}.",
    "listening_gateway": "Your login gateway: {ports} on your home network only{every}.",
    "listening_bypass": "It also answers on its own port, without your gateway's sign-in.",
    "listening_control": "which can control the whole server",
    "tailscale_line": "{count}, {running} running.",
    "tailscale_funnel_off": "Funnel is off everywhere.",
    "tailscale_funnel_on": "Funnel is on: anyone on the internet can reach it.",
    "tailscale_none": "No Tailscale connection was found on this server.",
    "tailscale_policy": "Who may reach what is set in Tailscale's admin console and cannot be read from here.",
    "tailscale_stopped": "Its connection is stopped, so this app can't be reached through Tailscale.",
    "tailscale_host": "This server's own Tailscale",
    "only_looks": "Checks only look: nothing on your server changes when you press one.",
}
SCOPE_WORDS = {"loopback": "this machine only", "all": "every address", "tailnet": "your Tailscale network",
               "home": "one home-network address of this server", "private": "your home network",
               "link-local": "this network cable only", "public": "the internet"}


def novice_strings() -> list[str]:
    """Every novice template of this module (U8)."""
    return [NEVER_RUN, NEEDS_ACCESS, AUTO_UPDATES, *WORDS.values(), *SCOPE_WORDS.values(),
            *(f["words"] for f in FAMILIES), *(c["title"] for c in CHECKS), *(c["what"] for c in CHECKS)]


def _size(count: float | None) -> str:
    from .yourserver import size_words

    return size_words(count)


def _plural(count: int, word: str, many: str | None = None) -> str:
    return f"{count} {word if count == 1 else many or word + 's'}"


# ---------------------------------------------------------------- Check 1: updates (§7.2, §7.3 M1, §7.6)

INST_RE = re.compile(r"^Inst (\S+)(?: \[([^\]]*)\])? \((\S+) (.*?)\)(?: \[[^\]]*\])?\s*$")
SUMMARY_RE = re.compile(r"(\d+) upgraded, (\d+) newly installed, (\d+) to remove and (\d+) not upgraded")


def parse_apt_simulation(text: str) -> dict:
    """`apt-get -s upgrade` or `dist-upgrade` (apt warns its other commands have no stable interface): {packages:
    [{package, old, new, suites, security}], summary {upgraded, new, remove, held}, deferred (phased), kept_back,
    removed, parsed}. A package is a security fix when one of its suites ends in -security."""
    packages, deferred, kept, removed = [], [], [], []
    summary = None
    section = None
    for line in (text or "").split("\n"):
        if line.startswith("Inst "):
            match = INST_RE.match(line)
            if not match:
                continue
            body = match.group(4)
            body = re.sub(r"\s*\[[^\]]*\]\s*$", "", body)
            suites = []
            for origin in body.split(","):
                origin = origin.strip()
                if "/" in origin:
                    suites.append(origin.rsplit("/", 1)[1].strip()[:60])
            packages.append({"package": match.group(1)[:120], "old": (match.group(2) or None),
                             "new": match.group(3)[:120], "suites": suites,
                             "security": any(s.endswith("-security") for s in suites)})
            continue
        if line.startswith("Remv "):
            removed.append(line.split()[1][:120])
            continue
        if re.match(r"^The following upgrades have been deferred due to phasing", line):
            section = deferred
            continue
        if re.match(r"^The following packages have been kept back", line):
            section = kept
            continue
        if line.startswith("  ") and section is not None:
            section.extend(word[:120] for word in line.split())
            continue
        if line and not line.startswith(" "):
            section = None
        match = SUMMARY_RE.search(line)
        if match:
            summary = {"upgraded": int(match.group(1)), "new": int(match.group(2)), "remove": int(match.group(3)),
                       "held": int(match.group(4))}
    return {"packages": packages, "summary": summary, "deferred": deferred, "kept_back": kept, "removed": removed,
            "parsed": summary is not None}


def family_of(package: str) -> dict | None:
    return next((f for f in FAMILIES if any(re.search(p, package) for p in f["patterns"])), None)


def exact_relations(text: str) -> list[str]:
    """The packages an `apt-cache show <package>=<version>` record names with an exact version (=) in Depends,
    Pre-Depends and Breaks."""
    out = []
    for line in (text or "").split("\n"):
        key, sep, value = line.partition(":")
        if key in ("Depends", "Pre-Depends", "Breaks") and sep:
            for part in re.split(r"[,|]", value):
                match = re.match(r"^\s*([a-z0-9][a-z0-9+.-]+)(?::\S+)?\s*\(\s*=\s*[^)]+\)", part)
                if match:
                    out.append(match.group(1))
    return out


def plan_updates(upgrade: dict, show: Callable[[str, str], str], simulate: Callable[[list[str]], dict] | None = None
                 ) -> dict:
    """§7.3 M1's list, as Check 1 counts it: the upgrade's packages minus the families of §7.6, then the dependency
    step (a package tied by an exact version to one left out goes too, repeated until nothing moves), then the
    simulation of the rest (anything it pulls in from outside the list is moved out the same way, at most five times).
    {ready, left: {family id: [packages]}, tied: {package: family name}, simulated}."""
    by_name = {p["package"]: p for p in upgrade["packages"]}
    left: dict[str, str] = {}
    for name in by_name:
        family = family_of(name)
        if family:
            left[name] = family["id"]
    relations = {name: exact_relations(show(name, p["new"])) for name, p in by_name.items()}
    tied: dict[str, str] = {}
    moved = True
    while moved:
        moved = False
        for name in by_name:
            if name in left:
                continue
            reason = next((left[dep] for dep in relations[name] if dep in left), None) or next(
                (left[other] for other in left if name in relations.get(other, [])), None)
            if reason:
                left[name] = reason
                tied[name] = reason
                moved = True
    simulated = False
    if simulate is not None:
        for _ in range(5):
            ready = [n for n in by_name if n not in left]
            if not ready:
                break
            result = simulate([f"{n}={by_name[n]['new']}" for n in ready])
            if not result.get("parsed"):
                break
            simulated = True
            extra = [p["package"] for p in result["packages"] if p["package"] not in ready]
            if not extra and not result.get("removed"):
                break
            pulled = {n for n in ready if any(n in relations.get(e, []) for e in extra) or n in extra}
            pulled = pulled or set(ready[-1:])
            for name in pulled:
                left[name] = left.get(name) or "together"
                tied[name] = "together"
    families: dict[str, list[str]] = {}
    for name, family in left.items():
        families.setdefault(family, []).append(name)
    return {"ready": [n for n in by_name if n not in left], "left": families, "tied": tied, "simulated": simulated}


def update_blocks(families: dict[str, list[str]], by_name: dict[str, dict]) -> list[dict]:
    """One copy block per family left out (§7.6): exactly the family and its tied packages at their new versions,
    never a bare upgrade; Docker's starts with a hold."""
    out = []
    names = {f["id"]: f for f in FAMILIES}
    names["together"] = {"id": "together", "name": "these packages",
                         "words": "These need to go together. Do them at home."}
    for family_id, packages in sorted(families.items()):
        family = names.get(family_id) or names["together"]
        pairs = " ".join(f"{p}={by_name[p]['new']}" for p in sorted(packages) if p in by_name)
        commands = [f"sudo apt-get install --only-upgrade {pairs}"]
        note = family["words"]
        if family_id == "docker":
            held = " ".join(DOCKER_PACKAGES)
            commands = [f"sudo apt-mark hold {held}", "# when you choose to update Docker, at home:",
                        f"sudo apt-mark unhold {held}", commands[0], f"sudo apt-mark hold {held}"]
            note = WORDS["updates_hold"] + " " + note
        out.append({"id": family_id, "title": family["name"][:1].upper() + family["name"][1:], "commands": commands,
                    "note": note, "packages": sorted(packages), "expect": "apt says what it installed",
                    "if_not": "Nothing changes if you do not run it."})
    return out


def updates_result(upgrade_text: str, dist_text: str, show: Callable[[str, str], str],
                   simulate: Callable[[list[str]], dict] | None, holds: list[str], listed_age: float | None,
                   auto_updates: bool, age_words: Callable[[float], str]) -> dict:
    upgrade = parse_apt_simulation(upgrade_text)
    dist = parse_apt_simulation(dist_text)
    if not upgrade["parsed"]:
        return {"tone": "warn", "words": WORDS["updates_failed"], "rows": [], "blocks": []}
    everything = {p["package"]: p for p in dist["packages"] if dist["parsed"]} or {}
    for package in upgrade["packages"]:
        everything.setdefault(package["package"], package)
    plan = plan_updates(upgrade, show, simulate)
    by_name = {p["package"]: p for p in upgrade["packages"]}
    held_back = [p for p in everything if p not in by_name]
    for name in held_back:   # the kernel's new packages come from dist-upgrade: they join their family's block
        family = family_of(name)
        if family:
            plan["left"].setdefault(family["id"], []).append(name)
            by_name[name] = everything[name]
    count = len(everything)
    security = sum(1 for p in everything.values() if p["security"])
    if not count:
        words = [WORDS["updates_none"]]
        tone = "ok"
    else:
        words = [WORDS["updates_line"].format(count=_plural(count, "update"), security=security)]
        if plan["ready"]:
            words.append(WORDS["updates_plain"].format(count=len(plan["ready"])))
        home = sum(len(v) for v in plan["left"].values())
        if home:
            parts = [f"{len(v)} {'is' if len(v) == 1 else 'are'} {next((f['name'] for f in FAMILIES if f['id'] == k), 'tied together')}"
                     for k, v in sorted(plan["left"].items())]
            words.append(WORDS["updates_home"].format(count=home) + " " + "; ".join(parts) + ".")
        others = [p for p in held_back if not family_of(p)]
        if others:
            words.append(WORDS["updates_held"].format(count=len(others)))
        tone = "warn" if security else "ok"
    if upgrade["deferred"]:
        words.append(WORDS["updates_phased"].format(count=len(upgrade["deferred"])))
    words.append(WORDS["updates_listed"].format(age=age_words(listed_age)) if listed_age is not None else
                 WORDS["updates_never_listed"])
    rows = [{"key": name, "tone": "warn" if p["security"] else "muted", "words": f"{name} {p.get('old') or ''} → "
             f"{p['new']}".replace("  ", " "), "security": p["security"],
             "sub": WORDS["updates_tied"].format(family=next((f["name"] for f in FAMILIES if f["id"] == plan["tied"][name]),
                                                             "the packages it needs")) if name in plan["tied"]
             else None, "family": next((k for k, v in plan["left"].items() if name in v), None)}
            for name, p in sorted(everything.items())]
    blocks = update_blocks(plan["left"], by_name)
    if auto_updates and blocks:
        for block in blocks:
            block["note"] = f"{block['note']} {AUTO_UPDATES}"
    return {"tone": tone, "words": " ".join(words), "rows": rows, "blocks": blocks,
            "counts": {"updates": count, "security": security, "ready": len(plan["ready"]),
                       "home": sum(len(v) for v in plan["left"].values()), "phased": len(upgrade["deferred"]),
                       "held": len(holds)}, "holds": holds, "auto_updates": auto_updates}


# ---------------------------------------------------------------- Check 2: restart needed

def never_restart(unit: str) -> bool:
    return any(re.search(p, unit) for p in NEVER_RESTART)


def restart_result(required: bool, packages: list[str], needrestart: dict | None) -> dict:
    words, rows = [], []
    tone = "ok"
    if needrestart and needrestart.get("kernel_status") == "version_upgrade":
        words.append(WORDS["restart_kernel"])
        tone = "warn"
    elif needrestart and needrestart.get("kernel_status") == "abi_upgrade":
        words.append(WORDS["restart_abi"])
        tone = "warn"
    elif required:
        words.append(WORDS["restart_file"])
        tone = "warn"
    services = (needrestart or {}).get("services") or []
    if services:
        words.append(WORDS["restart_services"].format(count=_plural(len(services), "service")))
        tone = "warn"
        for unit in services:
            rows.append({"key": unit, "tone": "warn", "words": unit,
                         "sub": WORDS["restart_never"] if never_restart(unit) else WORDS["restart_service"],
                         "unit": unit})
    if needrestart is None:
        words.append(WORDS["restart_no_access"])
    if not words:
        words.append(WORDS["restart_none"])
    return {"tone": tone, "words": " ".join(words), "rows": rows, "packages": packages[:50],
            "kernel": {"running": (needrestart or {}).get("kernel_running"),
                       "expected": (needrestart or {}).get("kernel_expected")}}


# ---------------------------------------------------------------- Check 3: disk use

def docker_sums(df: dict, kept_ids: set[str]) -> dict:
    """Docker's own numbers (GET /system/df): images (each counted once), what no container uses, leftover build files
    (records not in use, not shared, not internal or frontend), storage no container links, and the kept versions."""
    images = df.get("images") or []
    total_images = sum(i.get("size") or 0 for i in images)
    unused = sum(i.get("size") or 0 for i in images if not i.get("containers") and i.get("id") not in kept_ids)
    cache = sum(b.get("size") or 0 for b in df.get("build_cache") or [] if not b.get("in_use") and not b.get("shared")
                and b.get("type") not in ("internal", "frontend"))
    cache_all = sum(b.get("size") or 0 for b in df.get("build_cache") or [])
    volumes = sum(v.get("size") or 0 for v in df.get("volumes") or [] if (v.get("refs") or 0) == 0)
    kept = sum(i.get("size") or 0 for i in images if i.get("id") in kept_ids and not i.get("containers"))
    containers = sum(c.get("size_rw") or 0 for c in df.get("containers") or [])
    return {"images": total_images, "unused": unused, "cache": cache, "cache_all": cache_all, "volumes": volumes,
            "kept": kept, "containers": containers, "total": total_images + cache_all + containers +
            sum(v.get("size") or 0 for v in df.get("volumes") or [])}


def disk_result(disks: list[dict], sums: dict | None, journal: int | None, apt_cache: int | None,
                folders: dict | None) -> dict:
    words, rows = [], []
    tone = "ok"
    for disk in disks:
        label = "System disk" if disk.get("mount") == "/" else disk.get("label") or disk.get("mount")
        template = WORDS["disk_system"] if disk.get("mount") == "/" else WORDS["disk_other"]
        # a disk with no numbers (one Seraphina's service cannot see) keeps its own words
        line = (disk.get("words") or label) if disk.get("total") is None else \
            template.format(label=label, free=_size(disk.get("free")), total=_size(disk.get("total")))
        rows.append({"key": "disk:" + str(disk.get("uuid") or disk.get("mount")), "tone": disk.get("tone", "ok"),
                     "words": line, "mount": disk.get("mount")})
        if disk.get("mount") == "/":
            words.append(line)
        if disk.get("tone") in ("warn", "err") and tone != "err":
            tone = disk["tone"]
    if sums:
        words.append(WORDS["disk_docker"].format(total=_size(sums["total"]), images=_size(sums["images"]),
                                                 unused=_size(sums["unused"]), cache=_size(sums["cache"])))
        if sums["volumes"]:
            words.append(WORDS["disk_volumes"].format(size=_size(sums["volumes"])))
    else:
        words.append(WORDS["disk_docker_off"])
    words.append(WORDS["disk_logs"].format(size=_size(journal)) if journal is not None else WORDS["disk_logs_unknown"])
    if apt_cache:
        words.append(WORDS["disk_apt"].format(size=_size(apt_cache)))
    if sums and sums.get("kept"):
        words.append(WORDS["disk_kept"].format(size=_size(sums["kept"])))
    return {"tone": tone, "words": " ".join(words), "rows": rows, "docker": sums, "journal": journal,
            "apt_cache": apt_cache, "folders": folders}


def apt_cache_size(folder: str = "/var/cache/apt/archives") -> int | None:
    try:
        names = os.listdir(folder)
    except OSError:
        return None
    total = 0
    for name in names:
        if name.endswith(".deb"):
            try:
                total += os.lstat(os.path.join(folder, name)).st_size
            except OSError:
                continue
    return total


# ---------------------------------------------------------------- Check 4: failed services

def parse_units_json(text: str) -> list[dict]:
    try:
        rows = json.loads(text or "[]")
    except ValueError:
        return []
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def failed_result(failed: list[dict], timers: list[dict], timer_results: dict[str, str], cards: list[dict],
                  cockpit: str | None) -> dict:
    rows = []
    for unit in failed:
        name = str(unit.get("unit") or "")
        if not name:
            continue
        rows.append({"key": name, "tone": "err", "words": name, "sub": WORDS["failed_unit"], "unit": name,
                     "link": f"{cockpit.rstrip('/')}/system/services#/{name}" if cockpit else None})
    for timer in timers:
        service = str(timer.get("activates") or "")
        if service and timer_results.get(service) not in (None, "", "success") and not any(r["key"] == service
                                                                                         for r in rows):
            rows.append({"key": service, "tone": "err", "words": str(timer.get("unit") or service),
                         "sub": WORDS["failed_timer"], "unit": service,
                         "link": f"{cockpit.rstrip('/')}/system/services#/{service}" if cockpit else None})
    for card in cards:
        if card["light"]["code"] in ("down_error", "restarting", "unit_failed"):
            rows.append({"key": card["id"], "tone": "err", "words": card["name"], "sub": card["light"]["reason"],
                         "app": card["id"]})
    words = WORDS["failed_some"].format(count=_plural(len(rows), "thing")) if rows else WORDS["failed_none"]
    return {"tone": "err" if rows else "ok", "words": words, "rows": rows}


# ---------------------------------------------------------------- Check 5: disk health

def health_result(smart: dict | None) -> dict:
    """Check 5 from the helper's smart answer. Nothing checked is never "healthy": no tool, no disk found, or no disk
    read is a grey result that says so. Only the disks read as healthy count as healthy: when some were asleep or
    could not be read (smartctl timed out, which a failing disk may do), the result is grey and counts them apart,
    never "All N disks healthy"."""
    if smart is None:
        return {"tone": "muted", "words": NEEDS_ACCESS, "rows": []}
    if smart.get("state") == "no_tool":
        return {"tone": "muted", "words": WORDS["health_no_tool"], "rows": []}
    rows, bad = [], 0
    tone = "ok"
    for number, disk in enumerate(smart.get("disks") or [], 1):
        state = disk.get("state")
        words = {"ok": WORDS["health_good"], "asleep": WORDS["health_asleep"], "failing": WORDS["health_failing"],
                 "warning": WORDS["health_warning"]}.get(state, WORDS["health_unreadable"])
        row_tone = {"ok": "ok", "asleep": "muted", "failing": "err", "warning": "warn"}.get(state, "muted")
        if row_tone in ("err", "warn"):
            bad += 1
            tone = "err" if row_tone == "err" or tone == "err" else "warn"
        label = f"Disk {number}" + (f" ({disk['model']}, {_size(disk.get('size'))})" if disk.get("model") else "")
        rows.append({"key": disk.get("key"), "tone": row_tone, "words": label, "sub": words, "state": state,
                     "temp_c": disk.get("temp_c"), "hours": disk.get("hours"), "attributes": disk.get("attributes"),
                     "bits": disk.get("bits")})
    count = len(rows)
    if not count:
        return {"tone": "muted", "words": WORDS["health_none"], "rows": []}
    good = sum(1 for r in rows if r["tone"] == "ok")
    asleep = [r["words"] for r in rows if r["state"] == "asleep"]
    unread = count - good - bad - len(asleep)
    if not bad and not good:
        tone, words = "muted", WORDS["health_unread"].format(count=_plural(count, "disk"))
    elif bad:
        words = WORDS["health_some"].format(bad=bad, count=_plural(count, "disk"))
    elif good < count:
        tone, words = "muted", WORDS["health_part"].format(good=good, count=_plural(count, "disk"))
    else:
        words = WORDS["health_ok"].format(count=_plural(count, "disk"))
    if unread and (good or bad):
        words += " " + WORDS["health_not_read"].format(unread=unread)
    if asleep:
        words += " " + ", ".join(asleep) + (" was" if len(asleep) == 1 else " were") + " asleep."
    return {"tone": tone, "words": words, "rows": rows}


# ---------------------------------------------------------------- Check 6: what is listening

TAILNET4 = ipaddress.ip_network("100.64.0.0/10")
TAILNET6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")


def scope_of(address: str, home: list[str]) -> str:
    """The Blueprint's scopes (loopback, all, tailnet, private, link-local, public), plus "home" for a listener bound
    to one private address of this server."""
    if address in ("0.0.0.0", "::", "*", ""):
        return "all"
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return "all"
    if ip.is_loopback:
        return "loopback"
    if (ip.version == 4 and ip in TAILNET4) or (ip.version == 6 and ip in TAILNET6):
        return "tailnet"
    if ip.is_link_local:
        return "link-local"
    if ip.is_private:
        return "home" if address in home else "private"
    return "public"


def listening_result(listeners: list[dict], owner_of: Callable[[dict], dict | None], home: list[str],
                     gateway_ids: set[str]) -> dict:
    """One row per program (its app's name when it is one), with its ports and scopes; the gateway's ports as one row;
    a finding for each one reachable from the home network without Tailscale."""
    groups: dict[str, dict] = {}
    gateway = {"ports": set(), "every": set()}
    for entry in listeners:
        scope = scope_of(str(entry.get("address") or ""), home)
        owner = owner_of(entry) or {}
        if entry.get("container") in gateway_ids:
            (gateway["every"] if scope == "all" else gateway["ports"]).add(entry.get("port"))
            continue
        key = owner.get("id") or entry.get("unit") or entry.get("process") or f"port {entry.get('port')}"
        group = groups.setdefault(key, {"key": key, "words": owner.get("name") or entry.get("unit") or
                                        entry.get("process") or key, "ports": [], "scopes": set(),
                                        "app": owner.get("id"), "control": owner.get("control", False),
                                        "bypass": owner.get("bypass", False)})
        group["ports"].append({"proto": entry.get("proto"), "port": entry.get("port"), "scope": scope,
                               "address": entry.get("address")})
        group["scopes"].add(scope)
    rows, reachable = [], []
    for group in sorted(groups.values(), key=lambda g: g["words"].lower()):
        open_home = bool(group["scopes"] & {"all", "private", "home", "public"})
        tone = "warn" if open_home else "ok"
        if "public" in group["scopes"]:
            tone = "err"
        scopes = sorted(group["scopes"], key=list(SCOPE_WORDS).index)
        ports = sorted({f"{p['port']}/{p['proto']}" for p in group["ports"]})
        sub = ", ".join(ports[:6]) + " · " + ", ".join(SCOPE_WORDS[s] for s in scopes)
        if group["bypass"] and open_home:
            sub += " · " + WORDS["listening_bypass"]
        rows.append({"key": group["key"], "tone": tone, "words": group["words"], "sub": sub, "app": group["app"],
                     "scopes": scopes, "ports": ports})
        if open_home:
            reachable.append(group["words"] + (f" ({WORDS['listening_control']})" if group["control"] else ""))
    words = [WORDS["listening_line"].format(count=_plural(len(rows) + (1 if gateway["ports"] or gateway["every"]
                                                                        else 0), "thing"))]
    if reachable:
        words.append(WORDS["listening_home"].format(count=len(reachable), names=", ".join(reachable)))
    if gateway["ports"] or gateway["every"]:
        every = (f", and port {', '.join(str(p) for p in sorted(gateway['every']))} on every address"
                 if gateway["every"] else "")
        line = WORDS["listening_gateway"].format(ports=_plural(len(gateway["ports"]), "port"), every=every)
        words.append(line)
        rows.insert(0, {"key": "gateway", "tone": "ok", "words": "Your login gateway", "sub": line, "app": None,
                        "scopes": ["home"], "ports": sorted(str(p) for p in gateway["ports"] | gateway["every"])})
    tone = "err" if any(r["tone"] == "err" for r in rows) else "warn" if reachable else "ok"
    return {"tone": tone, "words": " ".join(words), "rows": rows}


# ---------------------------------------------------------------- Check 7: Tailscale

def tailscale_result(tailscale: dict | None, app_of: Callable[[str], str | None]) -> dict:
    """Check 7: one row per Tailscale container, and one for this server's own tailscaled when it runs (its serve
    entries' Funnel counts like a container's)."""
    sidecars = (tailscale or {}).get("sidecars") or []
    host = (tailscale or {}).get("host")
    if not sidecars and not (host and host.get("running")):
        return {"tone": "muted", "words": WORDS["tailscale_none"], "rows": []}
    rows, funnel = [], False
    for sidecar in sidecars:
        entries = sidecar.get("web") or []
        on = any(e.get("funnel") for e in entries)
        funnel = funnel or on
        hosts = sorted({str(e.get("host")) for e in entries if e.get("host")})
        name = app_of(sidecar.get("container") or "") or sidecar.get("name") or "a Tailscale connection"
        sub = (", ".join(hosts[:3]) or "no web entries") + (" · " + WORDS["tailscale_funnel_on"] if on else "")
        if not sidecar.get("running"):
            sub += " · " + WORDS["tailscale_stopped"]
        rows.append({"key": sidecar.get("name"), "tone": "err" if on else "ok" if sidecar.get("running") else "warn",
                     "words": name, "sub": sub, "funnel": on, "running": bool(sidecar.get("running"))})
    if host and host.get("running"):
        entries = host.get("web") or []
        on = any(e.get("funnel") for e in entries)
        funnel = funnel or on
        hosts = sorted({str(e.get("host")) for e in entries if e.get("host")})
        sub = (", ".join(hosts[:3]) or "no web entries") + (" · " + WORDS["tailscale_funnel_on"] if on else "")
        rows.append({"key": "host", "tone": "err" if on else "ok", "words": WORDS["tailscale_host"], "sub": sub,
                     "funnel": on, "running": True})
    running = sum(1 for r in rows if r["running"])
    words = [WORDS["tailscale_line"].format(count=_plural(len(rows), "Tailscale connection"), running=running),
             WORDS["tailscale_funnel_on"] if funnel else WORDS["tailscale_funnel_off"], WORDS["tailscale_policy"]]
    tone = "err" if funnel else "warn" if running < len(rows) else "ok"
    return {"tone": tone, "words": " ".join(words), "rows": rows}
