# Backups, part 1: SnapRAID disk protection through the SnapRAID Daemon

**Status.** Revision 2, written 2026-10-25: **daemon-first**. The first
revision (written the same day, checked by its author and then revised after
36 critic findings; the Review log keeps every line) made Seraphina SnapRAID's
runner: a root helper that synced, scrubbed and restored, her own sync guard
as the gate, and her own scheduler. Its phase P1 is built (DESIGN.md §41): a
**read-only reader** of SnapRAID. Since then the owner has chosen SnapRAID on
the host, run by the author's own SnapRAID Daemon. This revision redesigns
everything after P1 around that daemon, and keeps P1 as a read-only fallback
(§2.3 says what P1 keeps and what it drops). **Built since** (2026-10-25,
DESIGN.md §41): P2a (the reader's fixes), P2b (reading the daemon: the link,
`/metrics`, the forwarder, the mock daemon, the recorded 2.1 fixtures) and P3
(the five actions with their approvals), each tested against a mock daemon
built from the recorded exchanges; and since 2026-10-26 P4 (the owner's
checklist for a whole failed disk, §9.4: seven steps with the blocks he
pastes and a read-only check between them), tested against placeholder facts,
the mock daemon and the GUI mock. Nothing is installed, and nothing was run
against a real daemon or the owner's server: the labs L13 to L17, L19 and L20
have not run, and the link ships allowing reading only, so P3's actions stay
off until the owner widens it after L15 (§10.4). Every daemon
behaviour marked verified was observed in three scratch labs against
SnapRAID Daemon 2.1 and SnapRAID 15.1 (Appendix C). The owner decides the
choices in §13.1; until he answers, the defaults named there apply and this
document is a proposal.

**Where the owner's server is** (reported: relayed by the owner's operator,
not checked from here). The target is SnapRAID 15.1 plus SnapRAID Daemon 2.1,
after one supervised sync with SnapRAID 14.10 that upgrades the content
file's format (§8.9). Until then:

- the host runs its distribution's SnapRAID 13.0 package, whose `--version`
  prints `v0`, with the content file in `/var/lib/snapraid/`;
- one data disk is being taken out of the array through an empty placeholder
  folder;
- the owner's existing backup job owns parity, and its SnapRAID step is held
  until the transition;
- no daemon is installed, and no account or credential for Seraphina exists.

The daemon cannot come first: Daemon 2.x cannot drive SnapRAID 13.0 or 14.10.
Both refuse the daemon's `--lock-options` with "Unknown option" and exit 1,
and the daemon turns its log reading off below 15.0 (verified: daemon lab B;
source: `daemon/parser.c:2681-2692`).

**What it serves.** The owner's decisions in [DESIGN.md](../../DESIGN.md) §30
(nothing is off limits; everything has a way back) and §31: any novice can
install it (point 1), one Restore button (point 2), maintenance that runs
manually, at a set time or automatically (point 3), Tailscale only with no
forwarded ports (point 4), and Backups as one of the cockpit's sections
(point 6). The v2 design gave SnapRAID and its schedules to Cosmos
(SERAPHINA_OS_HARNESS §3.2). Cosmos is gone. SnapRAID's schedule now belongs
to the SnapRAID Daemon, or, until the daemon is in, to the owner's existing
job (§1.4). **Seraphina never schedules SnapRAID.** For SnapRAID, point 3 is
met by the daemon's window (at a time the owner sets) and the owner's buttons
(manual); there is no automatic mode for it (§1.4).

**What it keeps to.** DESIGN.md, the v1 contract: Python 3.10+ standard library
only, threads, the EventBus (§10), config and settings (§11), the HTTP API (§14),
the GUI and its view registry (§15), the CLI (§16), security (§17), the
Housekeeping Tracker (§32), the notifier and its event sources (§34), operator
personas (§36), drift sources (§38), the outcome rule (§40: an unknown outcome
is never replayed) and P1 as built (§41).
[SERAPHINA_OS_HARNESS.md](SERAPHINA_OS_HARNESS.md), the v2 design: Backups and
Restore (§5), reversibility classes (§5.8), maintenance windows and holds (§6),
the executor (§9.3), plain words (§2.4). Where this design differs from any of
them it says so in place, and Appendix A lists every difference.

**What it is built from.**

| Source | What it gave this design |
| ------ | ------------------------ |
| SnapRAID Daemon v2.1 (commit `675e46eb`): its source, its OpenAPI file `snapraidd.yaml`, its manual and its example config | every route, field and refusal; the login and its rate limit; the scheduler, the runner, the Safety Freeze and the notification command. Where the documents and the source disagree, the source wins (Appendix C lists the six places) |
| SnapRAID v15.1 (commit `4054832c`), and v14.10 | the engine the daemon runs: the thresholds, the filters an undelete passes, stops, and the content file's formats |
| Three daemon labs (Appendix C) | **A**: the daemon with a fake engine, for the HTTP surface; **B**: real 15.1 on loop ext4 disks, for behaviour, plus content-format tests with 13.0, 14.10 and 15.1; **C**: real 15.1 on partitioned loop images, 217 recorded exchanges |
| The SnapRAID 13.0 lab, source and manual (Appendix C) | P1 as built, and the fallback reader (§8): about forty runs covering every exit code, the machine log, interrupts, the lock and the guards |
| snapraid-runner v0.5, snapraid-aio-script v3.4 | what the common schedulers check before a sync (§5.1) |
| Dockge 1.5.0 and master | what Dockge runs and deletes, for the scheduler inventory and the disk checklist |
| The harness at this branch's HEAD | the code this fits into (§12), and P1 (DESIGN.md §41) |
| Revision probes PR1, PR2 and PR4 (Appendix C) | an ACL round trip, sudo with a piped request, and glibc `fnmatch` escapes (13.0's filters) |

**Evidence conventions.** Every factual claim carries one label and a citation.
**Verified** means observed in a lab: "daemon lab A", "B" or "C" for the daemon
labs, with a fixture's phase name where useful, or "lab `<run>`" for the
SnapRAID 13.0 lab. **Source** means read in pinned code but not run: `daemon/<file>:<line>`
is SnapRAID Daemon v2.1, "15.1 `cmdline/<file>:<line>`" is the SnapRAID
engine, "13.0 `<file>:<line>`" the old engine, and `swfactory/…` the harness.
**Docs** means read in pinned documentation. **Reported** means told about the
owner's server by the owner or his operator and not checked from here.
**Judgement** means this design's own reasoning or a proposed default; numbers
such as intervals, limits and timeouts are judgement unless a citation says
otherwise, and the owner confirms them (§13.1). **UNVERIFIED** marks a claim
whose source could not be opened from here or that nothing checked.

**Publication.** This file is public. It names products (SnapRAID, the
SnapRAID Daemon, Dockge, restic, smartctl, systemd, sudo) and no host,
address, account, disk model, serial number, share name, mount path or
person of the owner's. Example paths use the SnapRAID manual's placeholders
(`/mnt/disk1`), the daemon's documented defaults (`127.0.0.1:7627`,
`/etc/snapraidd.conf`) and a distribution's package defaults
(`/var/lib/snapraid/`). Facts about the owner's own disks, shares, schedules
and jobs stay in the owner's private notes; where this design needs one, it
says "the owner's backup job" or "a disk being taken out". The cockpit shows
the owner his real disk names, models and serials on his own machine; none of
them is ever written to this repository, to a notification or to a model by
default (§11.3).

**Words used here.**

| Term | Meaning, and the words a novice sees |
| ---- | ------------------------------------ |
| array | the data disks SnapRAID protects, plus its parity disks. Novice: "the protected disks" |
| parity | the extra data SnapRAID computes from the data disks and keeps on a parity disk, so that one dead disk can be rebuilt from the others. Novice: "disk protection" |
| sync | the SnapRAID command that brings parity up to date with the data disks. It is the only thing that makes new files protected, and also the thing that makes a deletion permanent. Novice: "protect the latest changes"; "protected up to Tuesday 02:42" |
| scrub | the command that reads data and parity and compares them with the recorded checksums, to find hidden damage. Novice: "check for hidden damage" |
| content file | SnapRAID's record of every protected file, kept in several copies. Novice: "the protection's file list" |
| fix | the command that rebuilds files or disks from parity. Novice: "bring back" |
| stripe | the blocks at the same position on every data disk, which parity protects together. Never shown to a novice: the screens say "other files stored alongside it" |
| the engine | SnapRAID itself, the `snapraid` program the daemon runs. Never shown to a novice |
| the daemon | SnapRAID Daemon (`snapraidd`), the author's service that runs the engine on a schedule and answers a REST API on loopback. Novice: "the protection service" |
| window | the daemon's scheduled maintenance (`maintenance_schedule`): up, sync, scrub, report, in one group. Novice: "the nightly protection run", "the next protection run" |
| freeze | the daemon's **Safety Freeze**: a sync the engine refused before writing anything, because its scan found at least as many removed files as `sync_threshold_deletes`, or updated files as `sync_threshold_updates`. Novice: "waiting for you before protecting the changes" (the words the first revision gave a held sync) |
| threshold | `sync_threshold_deletes` or `sync_threshold_updates`. Novice: "the limit" |
| hold-off | the daemon's `hold_off` switch: the next scheduled window is skipped, once. Novice: "skip the next run" |
| task, run | a task is one daemon step (`up`, `sync`, `scrub`, `report`, …) with a number; a run is the group of tasks one action or one window queued. Novice: "run" |
| accept | the owner's decision, after reviewing a freeze, that one maintenance may run with the thresholds off. Novice: "Protect these changes anyway" |
| approval | the owner's recorded yes to one action, bound to what the cockpit showed (§6.1). Novice: "your go-ahead" |
| outcome | DESIGN §40's word for how a call to the daemon ended: succeeded, failed or unknown. Novice: "done", "not done", "Seraphina could not tell whether it happened" |
| the link | `sf-snapraidd serve`, a small program systemd starts for each request, that holds the daemon's credential and makes the calls this design allows, for Seraphina (§3.2). Novice: "Seraphina's link to the protection service" |
| the forwarder | `sf-snapraidd notify`, the small root-run command the daemon calls after each report, which tells the harness through its hook API (§7.1). Never shown to a novice |
| the reader | `sf-snapraid`, P1's root helper, kept as a read-only fallback (§8). Novice: "the disk protection reader" |
| P1 to P4 | this design's phases (§12.9): P1, the reader (built); P2a, the reader's fixes for the owner's current host; P2b, reading the daemon; P3, the five actions with approvals; P4, the disk rebuild checklist. They replace the first revision's P2 to P4, and are not the v2 roadmap's phases A0 to G |

---

## 0. Decisions in one screen

1. **SnapRAID is disk protection, not a backup.** It rebuilds a dead data disk,
   or files deleted or damaged since the last sync, as they were at that sync.
   It keeps one copy, on the same machine, of the configured data disks only,
   and root can delete it. The page says so in three short sentences (§1.1),
   and parity never turns the Backups card green: that still needs a versioned,
   append-only copy in a second failure domain (v2 §5.3).
2. **One scheduler, and it is never Seraphina.** Exactly one thing starts
   SnapRAID runs: the owner's existing job until the daemon is in, and the
   daemon's own window after it. Seraphina has no timer, no automatic mode and
   no adopt step for SnapRAID. She counts every scheduler she can find, and
   when there are two she says so and refuses every action that would start a
   run (§1.4, §2.4).
3. **Daemon-first.** Once SnapRAID 15 and the daemon are in, Seraphina reads
   the daemon, never the disks. She reads `/metrics`, which the daemon serves
   without a login, herself; every other read, and every action, goes through
   **the link**, a small unit that systemd starts per request, which holds the
   daemon's credential as a systemd credential and allows only the calls this
   design names. The harness never holds the credential, because the daemon
   has one login with no roles, and whoever holds it can also switch the
   freeze off (§3.2).
4. **Reads that change nothing, at safe intervals.** Every read is a GET that
   only serializes the daemon's memory, so polling wakes no disk and queues no
   task (verified: daemon lab A, 240 GETs; lab C). `/metrics` every 60 seconds
   (10 while a run is active); the rest only when one of the daemon's change
   counters moved. Seraphina detects daemon restarts from its uptime and
   knows what a restart loses (§3.3, §3.4).
5. **Five actions, each after the owner's own approval.** Stop, skip the next
   run (`hold_off`), protect the latest changes (`maintenance`), bring files
   back (`undelete`) and repair hidden damage (`heal`). Each approval is bound
   to what the cockpit showed and is checked again just before sending; each
   answer is matched to its run as the daemon's task list allows; an unknown
   outcome is looked at again, read-only, and never sent again by itself
   (DESIGN §40; §6). Every action is refused unless the engine is 15 or newer,
   the daemon's login is on and one scheduler runs (§2.4).
6. **Never `/schedule`, never a config change.** `POST /schedule` passes raw
   options to the engine and applies no thresholds; `PATCH /config` can change
   the window and the thresholds. Seraphina uses neither, nor `/clear`,
   `/report`, `/refresh` or `/suspend_idle`; the link cannot build them (§6.12).
7. **The daemon's Safety Freeze is the gate, and the card says what it does
   not cover.** The engine itself refuses a sync over the thresholds, before
   writing anything, on its own scan. It counts across the whole array, not per
   disk, and an accepted run has no upper bound. Seraphina shows a freeze
   honestly, offers the owner's four ways out, and keeps her per-disk counts
   as a report (§5).
8. **Reports reach Seraphina's notices and tracker through the forwarder.** The
   daemon runs a small root-owned command after each report; it passes only
   the report's level, kind and exit code to the harness's hook API, never the
   report's text. The harness then reads the daemon's task list and decides
   from typed fields (§7).
9. **P1's reader stays, read-only, as the fallback.** It reads the host's
   SnapRAID while no daemon is installed, under a version policy (13.x,
   including a distribution build that prints `v0`; 14.10 read-only; 15.x), with
   the layout fixes the owner's host needs (a content file under
   `/var/lib/snapraid/`, a removal placeholder under `/var`), and with rules
   that keep its reads out of the way of the owner's job. With the daemon
   installed it runs no SnapRAID command at all (§8).
10. **Restores are the daemon's.** `undelete` brings back missing files only and
    never replaces an existing file; `heal` repairs blocks a scrub found
    damaged. A whole failed disk is a checklist the owner runs, with the daemon
    stopped (§9).
11. **Private by default.** File names in the daemon's change lists, task
    messages and reports can be personal. The link passes them only for the
    review page, a page at a time; the harness keeps no copy; notices,
    tracker items and events carry counts and disk numbers (§11.3).
12. **Phases.** P1 is built. P2a fixes the reader for the owner's current
    host. P2b reads the daemon (developed against a mock daemon until the host
    upgrade is done). P3 adds the five actions, after the lab with a real
    daemon passes. P4 is the disk checklist (§12.9; built, its labs L13 and
    L20 not run).

---

## 1. What SnapRAID is for, told honestly

### 1.1 The sentences on the page

The Backups page shows these three sentences at the top of the disk-protection
card, word for word (what it brings back, until when, what it never covers):

> Disk protection can bring back a dead data disk, or deleted and damaged
> files, exactly as they were at its last run.
>
> It can do that only until its next run, which makes deletions permanent, and
> it never had anything added since.
>
> It keeps no older versions, sits on this same machine, covers only the disks
> listed below, and an administrator can delete it: it works beside your
> backups, never instead of them.

Each clause is a fact:

| Clause | Evidence |
| ------ | -------- |
| brings back a dead data disk | verified: lab `fix-d1`, a failed disk rebuilt onto a fresh filesystem; every file unchanged since the sync came back byte-exact |
| or deleted and damaged files | verified: lab `fix-m` (undelete), `fix-zero` (a truncated file), `fix-e` (silent corruption found by scrub); with 15.1 under the daemon, its `undelete` and `heal` (verified: daemon labs B and C, `08-undelete`, `15-heal`) |
| exactly as they were at its last run | verified: lab `fix-edit`, an owner's edit made after the sync was silently reverted by `fix`, with exit 0; docs: 13.0 `snapraid.txt:627-662` ("the fix command does not differentiate between errors and intentional modifications") |
| only until its next run, which makes deletions permanent | docs: 13.0 `snapraid.txt:61-92`; once a sync records a deletion or an overwrite, the old version is gone |
| it never had anything added since | verified: lab, `fix -m -f` on a file created after the sync printed "Nothing to check." and exited 0 |
| keeps no older versions | docs: 13.0 `snapraid.txt:61-92` |
| sits on this same machine | judgement: parity disks sit in the same case, on the same power supply, under the same root account; fire, theft, a power event that kills several disks, and ransomware that writes through to the data and is then synced all take parity with them |
| covers only the disks listed | docs: 13.0 `snapraid.txt:98-117`; paths not listed, excluded or unmounted (the root disk, `/home`, app data elsewhere) are not covered |
| an administrator can delete it | source: parity, content and lock files are created mode 0600 owned by the running user, root (13.0 `cmdline/util.c:638`, `stream.c:116`, `parity.c:200`); the daemon runs as root (source: `snapraidd.service.in:30`) |
| beside your backups, never instead of them | v2 §5.2 and §5.3: data goes to restic behind an append-only server; the Backups card stays amber until each irreplaceable set has a copy in a second failure domain |

Under the sentences the card always carries one line that says when the chance
to undo a deletion ends, built from the next run that will start (judgement):

> Deleted something by mistake? It can be brought back until the next
> protection run: tonight at 02:00. **[Bring files back]** **[Skip the next run]**

On the daemon route the time is the daemon's next window, from its
`maintenance_schedule` (§3.6).
**Skip the next run** is the `hold_off` action (§6.8): it skips one window,
needs the owner's approval like every action, and the line then says
"Tonight's run will be skipped. A restart of the protection service undoes
this. [Don't skip]". With no window set the line reads "until the next
protection run, which nothing has scheduled". While the owner's own job
starts the runs, Seraphina knows its time only when he has told her the job's
unit (§8.1), and the buttons are not shown, because she sends nothing to that
job. While only the reader runs (P1), the line reads "Seraphina is only
watching: nothing is protected by it yet."

### 1.2 What it can and cannot do

| It can | Evidence |
| ------ | -------- |
| Rebuild up to as many failed data disks as there are parity disks | docs: 13.0 `snapraid.txt:293-314` |
| Bring back a deleted file, a truncated file or a damaged block, if nothing else in the same stripe changed since the sync | verified: lab `fix-m`, `fix-zero`, `fix-e`; daemon labs B and C for `undelete` and `heal` |
| Find silent corruption that has the same size and time as the original | verified: lab silent-corruption test, invisible to `diff` (exit 0), found by `scrub` and by `check -a` |
| Stop itself before a sync that would forget many deleted or changed files (15.x under the daemon, with its thresholds) | source: 15.1 `cmdline/snapraid.c:1551-1560`; verified: daemon labs B and C, `07-threshold-abort`, `16-update-threshold` (§5.1) |

| It cannot | Evidence |
| --------- | -------- |
| Bring back a file created after the last sync | verified: lab, `fix -m -f` on such a file printed "Nothing to check." and exited 0 |
| Bring back an older version of a file once a sync has recorded the change | docs: 13.0 `snapraid.txt:61-92` |
| Restore owner, group, mode or extended attributes | verified: lab chown test, a file owned by 1234:1234 with mode 0664 came back as root:root 0600; 15.1 under the daemon brings files back with mode 0600 too (verified: daemon labs B and C) |
| Always recover a failed disk when other disks changed after the sync | verified: lab `fix-d1`, one file came back as `<file>.unrecoverable` with exit 1 |
| Notice a file changed during a sync that it had already opened | verified: lab `sync-live`, exit 0, then the next `diff` reported the file as updated |
| Protect the root disk, folders outside the array, or anything in a second place | docs: 13.0 `snapraid.txt:98-117` |

### 1.3 Where it sits beside restic and an off-site copy

| Layer | Protects against | Cockpit's word | Turns the Backups card green? |
| ----- | ---------------- | -------------- | ----------------------------- |
| SnapRAID parity (this design) | as many dead data disks as there are parity disks; deletions and damage since the last sync | disk protection | no, never |
| restic behind rest-server in append-only mode (v2 §5.2, phase B) | mistakes, deletions, ransomware (old snapshots cannot be changed from the machine), app upgrades | versioned backup | only with a copy in a second failure domain |
| Off-site copy, with consent (v2 §5.3) | the house: fire, theft, a power event | off-site copy | yes, with restic, for the sets it holds |

The Backups area shows them in that order, with an explicit **Not covered** list
(v2 §5.3): the root disk, folders outside the array, folders the config
excludes, a disk being taken out of the array (§8.3), and every change made
after the last sync. Until restic exists (v2 phase B), the restic part of the
page says: "No versioned backup exists yet. Nothing Seraphina knows of keeps
older versions of your files, or a copy away from this machine. Disk
protection can do neither." (judgement; built: DESIGN §41).

### 1.4 Who owns SnapRAID now: one scheduler

The v2 design gave "Storage pools (mergerFS, SnapRAID) and their schedules" to
Cosmos, with Seraphina only reading them (v2 §3.2). Cosmos is gone. The
first revision made Seraphina the owner through an adopt step; this revision
does not (judgement, following the owner's choice of the daemon):

- **The rule.** Exactly one thing starts SnapRAID runs on an array. Two
  starters on one content file make one of them fail with "already in use"
  at best (verified: lab `sync-lock`), and two programs that read different
  content formats can do worse (§8.9).
- **Seraphina never schedules SnapRAID.** She has no timer for it, no
  automatic mode and no "Protect now" that runs without the owner's press.
  Every run she asks for is one press, one approval, one run (§6).
- **Who starts runs, stage by stage:**

  | Stage | SnapRAID on the host | What starts runs | What Seraphina does |
  | ----- | -------------------- | ---------------- | ------------------- |
  | **Now** | the distribution's 13.0, which prints `v0` (reported) | the owner's existing job; its SnapRAID step is held until the transition (reported) | reads through the reader (P1, with P2a's fixes), under the quiet rules of §8.1; sends nothing |
  | **The transition** | 14.10, for one supervised sync (§8.9) | the owner, or his operator, at the console, once | reads only; her reads of SnapRAID wait while any SnapRAID process runs |
  | **The daemon in** | 15.1 and SnapRAID Daemon 2.1 | the daemon's own window, which the owner sets in its config | reads the daemon (§3); the five actions after the owner's approval (§6); the reader runs no SnapRAID command (§8.1) |

- **After the daemon is in, the owner's job stops calling SnapRAID.** It may
  skip the daemon's next window around its own backup (the daemon's
  `hold_off`), but it never runs the engine and never asks the daemon for runs
  on a timer of its own, which would be a second scheduler (D2).
- **How Seraphina counts schedulers** (judgement). The reader's inventory (P1:
  cron, anacron, systemd system and user units and timers, Dockge stacks,
  matched by content one level deep; DESIGN §41) lists every place that
  mentions SnapRAID. The daemon's own unit is not counted as a place; its
  window counts once when `maintenance_schedule` is set (§3.2). A place the
  owner marks "does not start runs" (a job that only skips the window, or
  only reads) is not counted, until its contents change. More than one
  starter is the amber reason "Two things start protection runs: … Only one
  should.", a tracker finding, and the refusal of the three actions that
  start runs (§2.4). With none, the card says "Nothing starts protection
  runs." (amber). Without the reader Seraphina cannot look at all; the card
  says "Seraphina cannot see what else might start protection runs. [Set up
  the reader]", and the count is unknown, which refuses those three actions
  too (§2.4).
- **Pools stay out of scope.** mergerFS or any other pooling is still read-only.

---

## 2. The route: SnapRAID 15 and the SnapRAID Daemon

### 2.1 What the daemon does, and what Seraphina leaves to it

| The daemon | Evidence | Seraphina's part |
| ---------- | -------- | ---------------- |
| Runs maintenance at its window: `up`, `sync`, an optional `scrub --plan P --older-than D`, optional spin-down, then `report`, all queued as one group | source: `daemon/scheduler.c:68-135`; verified: daemon lab C, `03-first-maintenance`, `11-scheduled-run` | reads when it ran and how it ended (§4); never schedules |
| Enforces the sync thresholds (the Safety Freeze): the engine checks its own scan and exits 2 before writing anything | source: 15.1 `cmdline/snapraid.c:1551-1560`; verified: daemon lab B (content unchanged), lab C `07-threshold-abort` | shows a freeze and the four ways out (§5) |
| Refuses a sync that would accept a truncated file, when `sync_prevent_truncations = 1` | source: `daemon/scheduler.c:89-91`, 15.1 `cmdline/scan.c:973-990`; verified: daemon labs B and C, `13-truncation-abort` | expects it on (§10.3) |
| Reads SMART and disk power, and keeps per-disk and array health | source: `daemon/elem.c:798-877`; the power probe uses `smartctl -n standby,3` (source: 15.1 `cmdline/unixapp.c:3746-3830`), not checked on real disks | shows health, opens findings, sends `disk.failing` (§4, §7) |
| Cancels `sync`, `scrub`, `fix`, `check` and `diff` at run time while health is `degraded`, `prefail` or `failing` | source: `daemon/runner.c:1684-1727` | says so before an approval (§2.4) |
| `undelete` (`fix -m`, missing files only) and `heal` (`fix -e`, then `scrub -p bad`) | source: `daemon/scheduler.c:145-188, 267-354`; verified: daemon labs B and C | after the owner's approval (§6.10, §6.11) |
| Runs a command after each report (`notify_result`) | source: `daemon/notify.c:112-235, 381-405` | the forwarder (§7.1) |
| Serves a REST API and a web page on one port, with one HTTP Basic login, on loopback by default | source: `daemon/rest.c:3140-3420`; verified: daemon labs A and C | reads through `/metrics` and the link (§3) |
| Lets a person stop the running task, and skip the next window | source: `daemon/rest.c:1551-1592, 1641-1726`; verified: daemon labs A and C | after the owner's approval (§6.7, §6.8) |

What Seraphina adds: the honest card and its light, approvals bound to what
was shown, outcomes by DESIGN §40, notices and tracker items with counts only,
the count of schedulers, and the reader as a fallback.

### 2.2 Why drive the daemon instead of running SnapRAID

- **One runner on the content file.** The daemon is SnapRAID's runner on the
  host. If Seraphina ran SnapRAID beside it, two programs would take the
  same lock and one would fail (verified: lab `sync-lock`); P1's own reads take
  that lock too (source: 13.0 `cmdline/util.c:634-672`).
- **The freeze has no gap between count and sync.** The first revision's
  guard counted a `diff`, and the sync then made its own later scan, so
  changes in between were protected without being counted (first revision
  §4.1). The daemon's freeze is checked by the engine on the sync's own scan,
  before it writes anything (source: 15.1 `cmdline/snapraid.c:1551-1560`;
  judgement: that window is gone).
- **Restores never replace a file.** The daemon's `undelete` passes `-m`, which
  restores missing files only (source: `daemon/scheduler.c:267-354`; verified:
  daemon lab C, `08-undelete`), so the first revision's rename-aside steps are
  not needed.
- **What it costs** (judgement): a second root service with an HTTP API on
  the server (the daemon runs as root; source: `snapraidd.service.in:30`), and
  one login with no roles. Whoever holds it can also change the window and the
  thresholds through `PATCH /snapraid/v2/config` without the daemon's "full
  access" switch, and pass raw options to the engine through
  `POST /snapraid/v1/schedule` (source: `daemon/rest.c:1098-1108, 1393-1546`;
  verified: daemon lab A). §3.2 keeps that login away from the harness.
- **The alternative** stays open in D1: SnapRAID 15 on the host with no daemon,
  and the first revision's runner, guard and scheduler ported to 15.1.

### 2.3 What P1 keeps and what it drops

P1 is the read-only reader, its harness side, the Backups area and their tests
(DESIGN §41).

| P1 part | In revision 2 |
| ------- | ------------- |
| The reader's shape: a root-owned program at a fixed path, one JSON request per line on standard input, a verb table, no shell, fixed places written in its code, refusal codes with plain words, timeouts that never send SIGKILL, the job lock, 0700 run folders | **kept**, as the fallback (§8) |
| Its read verbs (`info`, `status`, `smart`, `probe`, `disks`, `guard`, `list`, `files`, `changes`, `job`, `log`) | **kept**. With the daemon installed, only `info` and `disks` run, and neither starts a SnapRAID command (§8.1) |
| The 13.0 log reader, outcome rules and the 48 recorded 13.0 runs | **kept**, for the host now, the transition and as regression tests |
| The config validator's core: disks bound to recorded filesystems by UUID, parity never on the system disk, plain names and paths, the `smartctl` allow-list | **kept**, with the layout fixes (§8.3) |
| The guard's checks G1 to G11 and its thresholds, as a report | **kept as a report only**: "this sync would be held" before the transition (§8.9), and the per-disk counts on the review page (§5.5). It holds nothing |
| The scheduler inventory and the content fingerprint | **kept**: the inventory feeds the scheduler count (§1.4); the fingerprint now flags a SnapRAID run outside the daemon (§11.5) |
| The card, the light's structure and staleness rules, the three sentences, novice words, disk labels, health findings, the notices' fixed texts, the privacy rules, the journal, events, the copy-block pattern (install to `.new`, check, then move), the doctor, the mock and the GUI structure | **kept**, extended with the daemon's states (§4, §11.6, §12) |
| Stop means a safe stop, never a kill | **kept**: the daemon's `/stop` sends SIGTERM, which 15.1 traps, saving its state before it exits (verified: daemon lab C, `05-stop-during-sync`); but stopping the daemon's own service during a run kills the engine (§5.4) |
| The root helper's mutating design: the verbs `sync`, `scrub`, `stop`, `release`, `drill`, `check`, `stat`, `restore`, `aside.trash`, `aside.back`, `repair.bad`, `restore.disk`; the `run` mode and its two transient units; the allow classes `sync`, `scrub` and `restore`; power-hold records; the no-follow restore walks and the metadata record | **dropped**: Seraphina runs no SnapRAID command that writes. The reader still answers `not_in_this_version` to them, and its `run` mode stays refused |
| Its own guard as a gate: holds, release tickets, `releases.jsonl`, the after-check, override options derived from a hold, the guard drill | **dropped**: the gate is the daemon's freeze (§5); Seraphina's counts are a report |
| Its own scheduler: maintenance modes for SnapRAID, the schedule tick, **Skip the next run** as Seraphina's own state, the scrub planner, the early stop before the power-off, the adopt step | **dropped**: the daemon's window, or the owner's job, starts runs (§1.4); Skip is the daemon's `hold_off` (§6.8). The `snapraid.schedule`, `snapraid.restore`, `snapraid.live_folders` and `maintenance` settings, read by nothing in P1, leave in P2b, except the scrub target the light reads |
| The setup wizard that writes a SnapRAID config (first revision P4) | **dropped**: the owner writes SnapRAID's and the daemon's configs; Seraphina checks them and stages only her own files (§10) |
| The reader's version check (13.x and 12.x only), content rule, mount check, and calls from the harness's own process | **changed** in P2a: the version policy (§8.2), the layout fixes (§8.3), and running outside the harness's mount namespace (§8.4) |
| The card's list of lab checks not run here (L8, L9, L10, L13) | **changed** in P2a: L9 and L10 tested Seraphina's own job units and leave the list; the daemon route adds its own (§13.2) |

### 2.4 Before Seraphina acts at all

These checks run before every action, in the link and again in the harness,
and refuse with the reason in plain words (judgement unless cited):

| Check | How | Refused with |
| ----- | --- | ------------ |
| The engine is 15 or newer | `engine_version` from `/snapraid/v1/array`, or `snapraid_build_info{engine_version}` from `/metrics`; empty counts as too old (verified: daemon lab C shows `"15.1"`; daemon lab A, with no engine, shows `""`) | "The protection service runs SnapRAID {version}, which it cannot drive. Nothing was sent." (`engine_too_old`) |
| The daemon's login is on | an unauthenticated `GET /snapraid/v1/state` must answer 401. With `net_auth_credential` empty, every route is open (source: `daemon/rest.c:3176-3180`). A request with no header is rejected before the rate limiter, so this check never causes a 429 (source: `daemon/rest.c:3188-3190`; verified: daemon labs A and C) | "The protection service accepts commands without a password. Seraphina will not use it until it has one." (`auth_off`) |
| One scheduler | the count of §1.4. Two or more refuse `maintenance`, `heal` and `undelete`, which would start runs; `stop` and `hold_off` stay possible, because they only hold work back (judgement; the research recommends acting on nothing, and this keeps the two that reduce activity). Without the reader the count is unknown, which refuses the same three | "Two things start protection runs. Turn one off first." (`two_schedulers`); "Seraphina cannot see what else might start protection runs." (`schedulers_unknown`) |
| The link allows the action | the link's root-owned config lists allow classes `read`, `act` and `accept` (§3.2) | "Seraphina may only read the protection service. [How to allow more]" (`not_allowed`) |
| Health permits the run | `/snapraid/v1/state` `health`: with `degraded`, `prefail` or `failing`, the daemon cancels `sync`, `scrub` and `fix` steps at run time (source: `daemon/runner.c:1684-1727`), so an approval would queue steps that do nothing | not offered: the card says "Fix the disk first: the protection service would cancel this run." |
| The approval is fresh and unchanged | the approval's binding (§6.1), read again just before sending | "Something changed since you looked. Look again." (`changed`) |

---

## 3. Reading the daemon

### 3.1 Two paths: `/metrics` without the credential, the rest through the link

**`/metrics`, read by the harness itself.** The daemon serves the exact local
URI `/metrics` with no login (`/metrics?x=1` counts, `/metrics/` does not), and
answers every `OPTIONS` request with no login too (source: `daemon/rest.c:3147-3155`;
verified: daemon lab A). Its Prometheus text (4.6 to 6.6 KB; verified: daemon
labs A and C) carries most of what the card needs (source:
`daemon/rest.c:2800-3125`; verified: daemon lab C, `04-after-maintenance`,
`07-after-threshold-abort`):

- array and per-disk health (`snapraid_array_health`, `snapraid_disk_health{disk,role}`,
  `snapraid_disk_transient_error`);
- counts: files, blocks, bad, unsynced and unscrubbed blocks, data and parity
  disks, block size;
- when the last `sync`, `scrub`, `diff` and `fix` started
  (`snapraid_last_command_timestamp_seconds{command}`, the same times as
  `/array`'s `last_sync_at` and `last_scrub_at`; source: `daemon/rest.c:2829-2846, 2543-2545`);
- the last scan's change counts (`snapraid_diff_files{change}`: equal, added,
  removed, updated, moved, copied, relocated, restored);
- the settings that matter here: both thresholds, `hold_off`, the scrub
  percentage and age, spin-down and probe intervals;
- the queue and the running task: `snapraid_task_queue_depth`,
  `snapraid_task_active{command,high_command}` with its progress, time left,
  speed and elapsed seconds, and per command the last exit code, duration and
  error counts;
- the daemon's change counters (`snapraid_exporter_last_pulse{subsystem}`),
  its uptime and its version and the engine's (`snapraid_build_info`).

It holds counts, settings and SnapRAID's own disk names (`d1`, `parity`), and
no file name or path (verified: daemon lab C fixtures). The harness reads it
over plain HTTP on loopback, parses only the metric names it knows, and
ignores the rest (judgement).

**Everything else through the link** (§3.2): `/snapraid/v1/state`, `/tasks`,
`/activity`, `/array`, `/snapraid/v2/disks`, `/snapraid/v2/config` and the five
actions all need the login, which only the link holds.

**Transport** (judgement, on verified facts). Every daemon answer carries
`Connection: close`, and its web server ends a request after 10 seconds
(verified: daemon labs A and C; source: `daemon/rest.c:3417-3429`). So one
connection per request, one request at a time, 2 seconds to connect and 15 to
read. Paths are exact and lowercase: an uppercase path is 404 "REST endpoint
paths must be lowercase", a trailing slash 404 "Resource not found"
(verified: daemon labs A and C). `/snapraid/v1/disks` and `/v1/config` are
deprecated aliases with other shapes; the v2 routes are used. A client the
daemon's `net_acl` refuses is accepted and then closed with no answer, which
looks like a crash (source: civetweb `civetweb.c:11034-11040`), so the doctor
checks loopback is allowed (§12.7).

### 3.2 The link

**Why it exists.** The daemon has one credential line and no roles (source:
`daemon/rest.c:3140-3390`). The same login that reads `/state` can change the
window and the thresholds with `PATCH /snapraid/v2/config` and pass raw options
to the engine through `/schedule`, where its `--lock-options` blocks only `-c`,
`-C`, `-l`, `-H`, `-V`, `-T` and the test options, so `-E`, `-Z`, `-D` and the
`--gui-threshold-*` options get through (source: `daemon/rest.c:1098-1108,
1393-1546`, 15.1 `cmdline/snapraid.c:467-483`; verified: daemon lab A, a
scheduled sync with no thresholds; lab B, `-c` refused). Every agent runs as
the harness's user while `harness.sandbox` is `none`, the shipped default
(source: `swfactory/config.py:110`). A credential in the harness's process, or
in a systemd credential of the harness's own unit, would therefore let any
agent switch the freeze off. So the credential sits in a separate unit, and
the list of calls it may make is enforced there, out of the harness's reach
(judgement; D7).

**Its shape** (judgement; the systemd behaviour is docs, **UNVERIFIED** here,
and L14 checks it):

- `bin/sf-snapraidd`, one standard-library Python file, installed root-owned at
  `/usr/local/libexec/seraphina/sf-snapraidd` (0755), `#!/usr/bin/python3 -I`,
  never imported by the harness. It has exactly two modes: `serve` (the link)
  and `notify` (the forwarder, §7.1). Any other argument is refused.
- `seraphina-snapraidd.socket`: `ListenStream=/run/seraphina-snapraidd/link.sock`,
  `SocketUser=root`, `SocketGroup=<the harness's group>`, `SocketMode=0660`,
  `Accept=yes`, `MaxConnections=4`.
- `seraphina-snapraidd@.service`, one short-lived instance per connection:
  `ExecStart=/usr/local/libexec/seraphina/sf-snapraidd serve`,
  `StandardInput=socket`, `StandardOutput=socket`, `StandardError=null` (so
  nothing it might print reaches the caller or the journal), `DynamicUser=yes`,
  `LoadCredential=basic:/etc/seraphina/snapraidd-basic.cred`,
  `StateDirectory=seraphina-snapraidd`, `RuntimeMaxSec=60`, and the usual
  fences: `ProtectSystem=strict`, `ProtectHome=yes`, `PrivateTmp=yes`,
  `PrivateDevices=yes`, `NoNewPrivileges=yes`, `CapabilityBoundingSet=`,
  `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`, `IPAddressDeny=any`,
  `IPAddressAllow=localhost`.
- The credential file `/etc/seraphina/snapraidd-basic.cred` (root, 0600) holds
  one line, `user:password`; the link reads it from `$CREDENTIALS_DIRECTORY/basic`,
  builds one constant `Authorization` header, and never logs, prints or returns
  it. The daemon caches the last accepted header and skips its checks for the
  same one (source: `daemon/rest.c:3182, 3367`), so a constant header costs no
  password hashing and never meets the one-check-a-second rate limit after the
  first request.
- Its config `/etc/seraphina/snapraidd.json` (root, 0644, no secret):
  `{v, caller, allow, daemon, hook}`: the harness's user name; the allow
  classes (`read`; `act` for stop, skip, maintenance, undelete and heal;
  `accept` for a maintenance with the thresholds off); the daemon's base URL,
  which must be `http://127.0.0.1:<port>` or `http://[::1]:<port>`; and the
  forwarder's hook URL and header file (§7.1).

**Why this shape** (judgement): it needs no sudo, so it works from a harness
unit with `NoNewPrivileges=yes` and from any mount namespace, which the
reader does not (§8.4); it holds no root; it writes nothing to the system
journal but systemd's own start and stop lines for each instance (Q9); and
`Accept=yes` keeps the program a plain read-one-line, answer-one-line
filter, with no state between requests but its journal.

**What it checks, on every request** (judgement): the caller (the socket's
`SO_PEERCRED` uid must be the configured user's, on top of the socket's
group and mode); its own installation (its file, its config and the folders
above them owned by root and writable by nobody else, as the reader checks
its own, else `unsafe_install`); the request (one line, at most 16 KiB, UTF-8
JSON, `v` the integer 1, a known `op`, only that op's fields, each of the
right type and range: the reader's rules, DESIGN §41); and, before any action,
the checks of §2.4.

**Its operations and the daemon calls each may make** (the complete list;
nothing else can be built):

| Op | Daemon calls | Returns |
| -- | ------------ | ------- |
| `info` | `GET /snapraid/v1/state` without a login (must be 401), then with it | its own version, allow classes and daemon URL; `auth_on`; the engine and daemon versions |
| `read` `{what: [state, tasks, activity, array, disks, config]}` | `GET /snapraid/v1/state`; `/tasks?limit_history=20&limit_messages=0`; `/activity?limit_messages=0`; `/array?limit_diffs=0&limit_fixes=0`; `/snapraid/v2/disks`; `/snapraid/v2/config` | the fields of §3.6 only, with the change counters |
| `run` `{number}` | `GET /tasks?limit_history=50&limit_messages=20` (or `/activity?limit_messages=20` for the running task) | one task of a run Seraphina shows, with its messages as text for the cockpit (§3.6) |
| `review` `{disk?, change?, q?, offset}` | `GET /snapraid/v1/array?limit_diffs=<offset + 500>&limit_fixes=0` | one page of 500 entries `{change, disk, path}` of the last scan's change list, the scan's identity (§6.1) and per-disk counts |
| `fixes` `{offset}` | `GET /snapraid/v1/array?limit_diffs=0&limit_fixes=<offset + 500>` | one page of an undelete's or heal's results, `fix_recovered` and `fix_unrecoverable` |
| `act` `{approval, action, body, expect}` | the reads its `expect` needs (§6.1), then exactly one of `POST /snapraid/v1/stop`, `/hold_off`, `/maintenance`, `/undelete`, `/heal` | the outcome (§6.3), the daemon's status and message, `number` and `pid` for a stop, and the counters taken just before the send (§6.4) |
| `journal` `{limit}` | none | its last actions (below), at most 50 |

Each `limit_*` value is an integer the link sets itself; the daemon refuses
negative, non-numeric or too large ones with 400 "Invalid limit parameter"
(source: `daemon/rest.c:2087-2107`; verified: daemon lab A). `/array` has no
offset, so a review page asks for the first `offset + 500` entries and keeps
the last 500; the daemon keeps at most 100,000 changed files in memory, while
its counters stay exact (source: `daemon/state.h:330`), and the review page
says so when the list is cut (judgement).

**Its journal.** `actions.jsonl` in its state directory, one line before each
send (`{at, approval, action, body_sha256, expect_sha256}`) and one after
(`{at, approval, outcome, status}`); never a file name (an undelete is its
count of paths and a hash). The harness's user cannot write it. The harness
and the doctor match every line to an approval the cockpit recorded; a line
with none is the high notice `parity.released_outside` and a tracker item
"The protection service was asked to act outside the cockpit" (§7.3, §7.4).
That catches an agent or a script that used the socket directly; it cannot
stop one that also forges the harness's own record (judgement; the limit the
first revision stated as V9).

### 3.3 What is read, and how often

| Read | Through | When (judgement unless cited) |
| ---- | ------- | ----------------------------- |
| `/metrics` | the harness | every `metrics_seconds` (60); every `active_seconds` (10) while `snapraid_task_active` is present; right after any action's answer or a transport error; never faster than every 5 seconds |
| `state` | the link | with every link read |
| `tasks` | the link | when `pulse.tasks` changed; at most every `active_seconds` while a run Seraphina asked for is active (a run request the daemon took within the last day whose run has not ended; never a stop, a skip, a refusal or an unknown outcome), else every `reads_min_seconds` (60) |
| `activity` | the link | when `pulse.activity` changed and a run Seraphina asked for is active, at most every `active_seconds`; a 204 is normal (§3.5) |
| `array` (counts only) | the link | when `pulse.array` changed |
| `disks` (v2) | the link | when `pulse.disks` changed, at most every 60 seconds: temperature readings move that counter too (source: `daemon/state.h:140-165`) |
| `config` (v2) | the link | when `pulse.config` changed, and before every action |
| everything above | the link | once every `full_read_minutes` (30) whatever the counters say, and at start |
| `review`, `fixes`, `run` | the link | only when the owner opens that page, a page at a time |

The counters come from `/metrics` (`snapraid_exporter_last_pulse`), so an idle
array costs one small unauthenticated GET a minute and no link call. A link
call costs one short process and two log lines from systemd (Q9). No read
wakes a disk: every GET handler only serializes the daemon's memory under its
lock, and none queues a task or starts `snapraid`, `smartctl`, `docker` or
`curl` (source: the GET handlers in `daemon/rest.c`; the only places that
start programs are `runner.c`, `version.c` and `unixapp.c`; verified: daemon
lab A, 240 GETs over all twelve GET routes left every counter unchanged and
started no engine; lab C, hundreds of `/state` reads every 0.5 to 2 seconds
never moved `pulse.tasks`). Disk activity comes only from the daemon's own
scheduler (its start-up probe and read, its periodic probe, its windows) and
from the actions (source: `daemon/runner.c:1729-1749`, `scheduler.c:653-677`;
verified: daemon lab C, `17-periodic-probe`).

Answers are built while the daemon holds its global lock, which its log
reader also takes for every line of engine output, so an unlimited `/tasks`
on a long history briefly delays the runner (source: `daemon/parser.c:2585-2700`).
That is why every read names its limits (judgement; Q7 measures a long
history).

### 3.4 Change counters and restarts

- **Counters.** `pulse.{array, config, disks, tasks, activity}` live in the
  daemon's memory and start again from zero at each start; replaying its logs
  then moves `array`, `disks`, `tasks` and `activity` (verified: daemon lab C,
  `tasks` 488 before a restart and 1332 after, `config` 1; source:
  `daemon/state.h:140-165`). Seraphina compares each only for equality: any
  change means "read that one" (judgement).
- **A restart.** `snapraid_daemon_uptime_seconds` is the time since the
  daemon started (source: `daemon/rest.c:3121-3124`). The harness keeps
  `start = now - uptime`; a move of `start` by more than 10 seconds is a
  restart candidate, confirmed by `pulse.config` going back down or a new
  `startup` probe in `/tasks`. A clock step alone moves `start` without
  either, and only re-bases the time (judgement).
- **What a restart loses** (verified: daemon labs A, B and C; source:
  `daemon/parser.c:2800-2971`):
  - the queue: queued steps of an approved run vanish without a trace;
  - the history except what per-task log files rebuild: start, report and
    cancelled tasks disappear;
  - task numbers: new ones start after the highest number in the logs, so
    numbers of tasks that left no log are used again (source:
    `daemon/parser.c:2184-2189`);
  - a task that was running comes back as `signaled` with `exit_sig 9`,
    whatever really happened (verified: daemon lab B, `40-tasks-after-restart`);
  - `hold_off`, which returns to false (§6.8);
  - then a start-up `probe`, and a `read` if the content file changed.
- **What Seraphina does after one** (judgement, under DESIGN §40): every run
  she asked for before the restart that the daemon took (or whose answer
  never came) and that had not ended becomes **unknown**, with the tracker
  item "Outcome unknown: check before retrying (a protection run)"; a recorded
  refusal (a 409, say), a send that did not take effect and an outcome already
  decided keep their outcome, because a restart adds no doubt about them; an
  `exit_sig 9` is unknown, never "failed"; the counters are read afresh; a skip
  the owner approved and the restart undid raises `parity.lapsed` and asks
  again (§6.8), once, and only when his latest skip answer was an "on" (a skip
  he turned off again has nothing to lapse); nothing is sent again by itself. The card notes "The
  protection service restarted at 03:12."

### 3.5 Reading the answers correctly

Each rule is a unit test (§13.2):

1. **A `GET /snapraid/v1/activity` of 204 is normal.** It means "no latest
   task", and happens at every start for a moment, when no engine is found, and
   when pruning leaves the history empty (source: `daemon/rest.c:2347-2351`,
   `parser.c:2951`, `daemon.c:331-385`, `runner.c:1800-1806`; verified: daemon
   lab A).
2. **Health `pending` is not an error.** It means "no engine, no disks or no
   SMART yet" (`health_reason` says which) and blocks no work (source:
   `daemon/elem.c:921-926`; verified: daemon lab C, "SMART telemetry could not
   be obtained for disk d1").
3. **A disk with `devices: []` is unknown, not healthy.** Health over an empty
   device list reads `passed` (source: `daemon/elem.c:798-818, 843-877`;
   verified: daemon lab C, whole-disk loop images).
4. **"Protected up to" needs `blocks_unsynced == 0`.** A sync that was stopped
   still moves `last_sync_at` (verified: daemon lab C,
   `05-after-stop-during-sync`, 63 blocks unsynced and `last_sync_at` moved;
   lab B). The card then says "The last run was stopped part way: some
   changes are not protected yet." and keeps the earlier "protected up to"
   time it knew.
5. **A freeze leaves `last_sync_at` unchanged, and is `sync` with exit 2.** In
   15.1 a sync's exit 2 comes only from the two threshold checks; `diff`'s exit
   2 is a separate path (source: 15.1 `cmdline/snapraid.c:1512-1522,
   1551-1560`). Which limit tripped is read by comparing `/array`'s
   `diff_removed` with the deletes threshold and `diff_updated` with the updates
   threshold (verified: daemon lab C, `07-after-threshold-abort`). No message
   text is parsed for it.
6. **History is in the order tasks ended, not by number**, and a cancelled
   task's `started_at` is its cancel time (source: `daemon/rest.c:2412-2424`,
   `elem.c:347-357`, `runner.c:1874-1875`; verified: daemon lab B, #27, #29,
   #28). The next task to run is the last element of `pending`.
7. **A task's status** is `starting`, `processing`, `finalizing` or `stopping`
   while it runs; `queued`, `terminated` (with `exit_code`), `signaled` (with
   `exit_sig`) or `canceled` (with `exit_msg`) otherwise (source:
   `daemon/rest.c:2161-2317`). `exit_code` 0 is done; 3 is done with a warning
   (degraded); 2 is a freeze for `sync` and "changes found" for `diff`; −1 is a
   failure to run or a cancel before start ("Notification failed" on a report);
   −2 and −3 are a failed hook before or after.
8. **What a failed step takes with it.** A failed step cancels the rest of its
   run except `report`, `down` and `shutdown`; a person's stop keeps only
   `report`; a health-gate cancel cancels nothing else (source:
   `daemon/elem.c:364-397`, `runner.c:1872-1883`).
9. **Health names are words in JSON and numbers in `/metrics`.** JSON says
   `passed`, `pending`, `corrupt`, `degraded`, `prefail`, `failing`;
   `/metrics` maps them to 1, 0, 2, 3, 4, 5 (source: `daemon/rest.c:2671-2683`).
10. **A window and an API maintenance look the same** in `/tasks`: both are
    `high_command: "maintenance"` (verified: daemon lab C,
    `11-after-scheduled-run`). Seraphina tells them apart only by her own
    approval record and the window's time (§11.5).
11. **A stop is told from a failure** by Seraphina's own record of the stop's
    answer, or by the daemon's fixed message "Stop requested", which it adds
    also when its web page stopped the task; a `sync` exit 1 alone is
    ambiguous (verified: daemon labs B and C; judgement). Engine messages are
    shown as text and never parsed for a decision.
12. **An interrupted run after a restart** reads `signaled 9` (rule of §3.4):
    unknown.

### 3.6 What the link passes on, and what it keeps back

The daemon's answers hold private and secret fields (source:
`daemon/rest.c:1141-1208, 1902-2085, 2228-2232, 2505-2588`; verified: daemon
labs A and C, where a planted token came back in clear from `/v2/config`).
The link passes only typed fields (judgement):

| Answer | Passed on | Never passed on |
| ------ | --------- | --------------- |
| `/state` | the counters, `health`, `health_reason`, `active_command`, `next_command`, `progress`, `eta_seconds` | `instance` |
| `/tasks`, `/activity` | per task: `number`, `command`, `high_command`, `status`, `health`, `exit_code`, `exit_sig`, `exit_msg` when it is one of the daemon's fixed messages (copied from its source: `daemon/runner.c` and `elem.c`, signals as `SIGKILL(9)`), `scheduled_at`, `started_at`, `finished_at`, progress fields, error counts; `notify_failed` when a report's fixed fatal message says its notification failed; through `run`, `stop_requested` (the daemon's own "Stop requested" among all of a task's messages, read from `/activity` while it is the latest task) | `log_file`, `report_output`, `messages[]` (except through `run`, for the cockpit only), and any `exit_msg` that is not a fixed message |
| `/array` | counts, times, `hold_off`, `engine_version`, `daemon_version`, the eight `diff_*` counters, `fix_recovered`, `fix_unrecoverable`, `last_command`, `last_command_at` | `daemon_conf`, `engine_conf`, `engine_content`, `diffs[]` and `fixes[]` (except through `review` and `fixes`) |
| `/v2/disks` | per disk: its SnapRAID name, role, health, free and total space, error counts, and per device `health`, `power`, `rotational`, `smart` flags | `splits[].path`, `label`, `uuid`, device `node`, `ids` (serials, WWNs) |
| `/v2/config` | `maintenance_schedule`, both thresholds, `sync_prehash`, `sync_prevent_truncations`, `scrub_percentage`, `scrub_older_than`, `touch_zero_subseconds`, `probe_interval_minutes`, `spindown_idle_minutes`, `notify_result_level`, `notify_syslog_enabled`, `notify_syslog_level`, `notify_differences`, `check_updates`, `config_full_access`; and as yes or no only: whether `notify_result` names the forwarder's fixed path, whether `notify_run_as_user` is blank, whether a hook script is set | `notify_result`, `notify_start`, `notify_heartbeat`, `hook_script`, `hook_docker_pause`, `hook_run_as_user`, `notify_run_as_user` as text |

The `/v2/config` answer has no listener, login or shutdown setting
(verified: daemon lab C, `04-after-maintenance`); the reader reads those few
from `/etc/snapraidd.conf` as root, and returns only their values or a yes or
no, never the credential line (§8.5). The disks' models and serials reach the
cockpit live, as P1 shows them; the harness keeps none (§11.3).

---

## 4. The status card

### 4.1 What it shows, and where each fact comes from

| Fact | Daemon route | Reader route (P1, P2a) |
| ---- | ------------ | ---------------------- |
| Protected up to: when the last complete sync ran, and how it ended | `/metrics` sync time with `blocks_unsynced == 0`; `/tasks` for how it ended (§3.5 rules 4, 5) | `status` and the reader's run records (DESIGN §41) |
| A run in progress: what, how far, time left | `/metrics` `snapraid_task_active` with progress, time left and speed | the reader's own run only |
| Frozen: what the last scan found, which limit tripped, since when | `/metrics` last sync exit 2 and the diff counts, against both thresholds; `/tasks` for when (§5) | not applicable: no daemon |
| Last check for hidden damage, and how much of the disks was checked within the target | `/metrics` scrub time and `blocks_unscrubbed`; `/array`'s `scrub_history` for the oldest checked block | `status` (`info_time`, `has_unscrubbed`) |
| Damage recorded | `snapraid_blocks_bad`; health `corrupt` | `status` `has_bad` |
| Each disk: name, health, free space, errors, asleep or awake | `/metrics` disk health; `/v2/disks` (via the link); models and serials live from the reader's `disks` (cockpit only; the link never passes them on) | `status`, `smart`, `probe`, `disks` |
| Room on the protection disk | `/v2/disks` parity free space, with the growth samples P1 keeps | `status` |
| Who starts runs: the window and its next time, other schedulers found | `maintenance_schedule` (link `config`), the reader's inventory (§1.4) | the reader's inventory |
| Skip set for the next window | `/metrics` `snapraid_config_hold_off` | not applicable |
| What Seraphina asked for, and its outcome | the harness's approval records (§6) | not applicable |
| The protection service itself: answering, login on, versions, restarted when | `/metrics`, the link's `info`, the restart rule (§3.4) | not applicable |

The card's novice text, for example (judgement):

- green: "Protected up to this morning at 02:21 (took 6 minutes). Every part of
  the disks was checked for hidden damage in the last 30 days. All 3 disks are
  healthy. Next run: tonight at 02:00."
- amber, frozen: "Waiting for you: 25 files were deleted since the last
  protection run, more than the 20 the protection service allows. It stopped
  before changing anything, so the protection still has them. Files added
  since Tuesday are not protected yet. [See what changed]"
- amber, two schedulers: "Two things start protection runs: the protection
  service at 02:00, and a timer that mentions SnapRAID. Only one should."
- red: "Disk 2 is missing or not answering. The protection service will not
  run until it is back. [What to do]"
- red, no login: "The protection service accepts commands from any program on
  this server without a password. [How to fix it]"
- grey: "Last checked 5 hours ago. [Check now]"

### 4.2 The light

The card keeps P1's tones, staleness rules and fixed sentences (DESIGN §41:
red and amber stand on old facts with a note; grey only where the light would
otherwise be green or unknown; never green on stale facts). On the daemon
route the first rule that matches gives the light (judgement unless cited):

| Light | When | Reason, in plain words |
| ----- | ---- | ---------------------- |
| **Red** | the daemon answers without a login (§2.4) | "The protection service accepts commands from any program on this server without a password." |
| | the daemon listens on an address other than loopback, without a login; with one it is amber (§12.7) | "The protection service can be reached from other computers without a password." |
| | array or disk health `failing` or `prefail` | "Disk 1 says it is failing. Replace it soon." |
| | health `degraded` (a disk missing, or its filesystem not the one recorded) | "Disk 2 is missing or not answering. The protection service will not run until it is back." |
| | bad blocks, or health `corrupt` | "Hidden damage was found in 3 places. [Repair it]" (the button from P3) |
| | the engine is older than 15 under the daemon | "The protection service cannot run this version of SnapRAID." |
| | frozen for `red_after_days` (7) | "Waiting for you for 9 days." |
| | no complete sync for `red_after_days` while files may have changed | "Nothing new has been protected for 9 days." |
| **Amber** | frozen | "Waiting for you before protecting the changes." with the counts (§5.2) |
| | the last run stopped part way (`blocks_unsynced > 0`) | "The last run was stopped before it finished. It carries on at the next run." |
| | an action's outcome is unknown | "Seraphina could not tell whether your request reached the protection service. Check before trying again." |
| | a skip the owner approved was undone by a restart | "The protection service restarted, so the next run will not be skipped after all." |
| | two schedulers, or none | §1.4's sentences |
| | the daemon listens beyond loopback, with a login | "The protection service can be reached from other computers. Only you should hold its password." |
| | the daemon has not answered for `down_minutes` (30) | "The protection service is not answering. Nothing is being protected meanwhile." |
| | the daemon could not tell Seraphina about a run (a report task's "Notification failed") | "The protection service could not tell Seraphina about its last run." |
| | a run the protection service did not make (the content fingerprint, §11.5), or a run with the limits off that Seraphina did not ask for | "A protection run was started outside the protection service." / "A run with the limits off was started outside Seraphina." |
| | a disk's health is unknown (`devices: []`, `pending` past a day) and the owner has not accepted it | "Disk 3's health can't be read." |
| | the last complete sync is older than `amber_after_hours` (36) while files may have changed; part of the disks not checked for hidden damage for the target plus 15 days; the protection disk almost full | P1's sentences |
| **Green** | none of the above, and the facts are fresh | "Protected up to …" |
| **Grey** | where it would be green or unknown, the facts are older than `stale_minutes` (180), or neither `/metrics` nor the link answers | "Last checked 5 hours ago." |

"Files may have changed" is true unless a scan since the last complete sync
counted nothing removed, updated or added (the `/metrics` diff counts with a
`diff` or `sync` time after it); P1's rule that an old "nothing changed" says
nothing about now stays (DESIGN §41). The reader's route keeps P1's table as
built, with P2a's new reasons for a disk being taken out (amber: "Disk 2 is
being taken out of protection.") and for a SnapRAID it may only read (§8.2).

### 4.3 Not waking the disks

On the daemon route Seraphina's reads touch no disk (§3.3). The daemon's own
probes do: its periodic probe and spin-down use `smartctl -n standby,3`,
meant to leave a sleeping disk asleep, which no lab could check on real disks
(source: 15.1 `cmdline/unixapp.c:3746-3830`; Q5). The power and SMART values
`/v2/disks` shows are whatever the daemon's last probe or SMART read left in
its memory (docs: `snapraidd.yaml:484-486`). On the reader's route P1's
rules stand: `status` reads only the first content copy and stats the
others; `smart` and `diff` wake every array disk (first revision §2.4,
DESIGN §41), so the reader runs `diff` only on the owner's button, never on a
timer (§8.1).

---

## 5. Freezes and other stops, shown honestly

### 5.1 The Safety Freeze, and what it does not cover

**How it works.** After its scan, the engine's `sync` checks
`removed >= N`, then `updated >= N`, against the `--gui-threshold-removes`
and `--gui-threshold-updates` values the daemon passes from
`sync_threshold_deletes` and `sync_threshold_updates`. It logs a fatal "Too
many files were removed (R, limit is N). Sync aborted." (or "updated") and
exits 2 before writing anything (source: 15.1 `cmdline/snapraid.c:1551-1560`,
`daemon/scheduler.c:84-104`; verified: daemon lab B, content file unchanged;
lab C, 25 removed against a limit of 20, and 55 updated against 50). A
threshold of 0 turns that check off (docs: `/metrics` help, "0 = disabled").

**What the card must not overclaim** (judgement unless cited):

- **It counts the whole array, not each disk.** A small disk emptied inside
  a large array passes when the count is under the limit; the first revision
  rejected the common runners for the same weakness (first revision §4.1).
  Seraphina cannot add a per-disk gate without running SnapRAID herself,
  which would make a second runner (§2.2). So the card says "The protection
  service stops a run when more than 100 files were deleted or 100 changed
  across all disks." in those words, low limits are recommended (D3), and the
  review page shows the counts per disk (§5.5).
- **Rewrites count separately from deletions**, with their own limit. Both
  lose the version parity had; ransomware that encrypts in place shows as
  updates (first revision §4.3, R10). D3 recommends the same limit for both.
- **An accepted run has no upper bound.** `ignore_thresholds: true` drops both
  limits for that one run; the sync protects whatever its scan finds when it
  runs, which may be more than was reviewed (source: `daemon/rest.c:1344-1361`,
  `scheduler.c:95-104`; verified: daemon lab C, `16-after-ignore-thresholds`,
  exit 0 and no threshold option in the engine's arguments).
- **What bypasses it:** an accepted maintenance (Seraphina's only way, after
  approval); `/schedule` with a sync (no thresholds, raw options); a `PATCH`
  that sets a threshold to 0; the engine run by hand. Seraphina uses only the
  first; the doctor reports a threshold of 0 (§12.7), and a run outside the
  daemon is flagged (§11.5).
- **A disk whose files are all missing** (unmounted, for example) is refused
  by the engine with exit 1, "use 'snapraid --force-empty sync'", and the
  daemon's maintenance never passes `-E` (source: 15.1 `cmdline/scan.c:2198-2245`;
  verified: daemon lab B). The card says to fix the mount; Seraphina never
  offers a bypass.
- **Truncated files** are refused only with `sync_prevent_truncations = 1`;
  without it the daemon passes `-Z` and a file cut to zero bytes is accepted
  into parity (source: `daemon/scheduler.c:89-91`, `conf.c:1275`; the example
  config leaves it commented out, docs: `snapraidd.conf.example:353`;
  verified: daemon labs B and C, `13-truncation-abort`). The doctor expects 1.
- **Every window freezes again on the same changes.** Scheduled runs always
  apply the thresholds (source: `daemon/scheduler.c:592`), so the owner gets
  one `[ERROR]` report per window until he chooses (verified: daemon lab B).
  Seraphina keeps one tracker item and one open notice incident for them.

### 5.2 What the card and the phone say after a freeze

What Seraphina sees (verified: daemon lab C, `07-*`, `16-*`; lab B,
`11-tasks-after-threshold`, `13-array-after-threshold`): `sync` terminated
with exit 2 and task health `passed`; `scrub` cancelled with "The preceding
sync operation failed with exit code 2"; the report runs (`[ERROR]`, exit 2,
no DIFFERENCES section); array health unchanged; `last_sync_at` unchanged;
`/array` holds the scan's counts and its change list. (The daemon's OpenAPI
file says a freeze shows as `status=canceled` with an `exit_msg`; only the
scrub that depends on it does; docs: `snapraidd.yaml:1185-1189`.)

- **Card** (amber, red after `red_after_days`): "Waiting for you: 25 files
  were deleted since the last protection run, more than the 20 the protection
  service allows. It stopped before changing anything, so the protection
  still has them. Files added since Tuesday are not protected yet." with
  **See what changed**. The skipped check for hidden damage is shown as "not
  run", never as failed.
- **Phone:** `parity.held`, high, with P1's fixed text and counts only (§7.3).
- **Tracker:** one `decision` item `snapraid:frozen`, "Review the changes the
  protection service stopped for", its detail updated at each freeze; closed by
  the factory with `receipt` evidence when a sync completes, or when a later
  scan is under both limits (§7.4).

### 5.3 The owner's four ways out

Every way starts with **See what changed**, the review page: the scan's change
list a page of 500 at a time, by disk, kind and search text, live from the
daemon through the link's `review` (§3.2), with the counts per disk and the top
folders, names shown as text and never kept (judgement; §11.3). The page says
when the scan ran, and that the list is what the protection service found
then, not now. Then (judgement, on verified mechanics):

1. **Bring the files back.** Select files or folders that were deleted by
   mistake: `undelete` (§6.10) restores missing files only, to their old
   place. A normal **Protect the latest changes** afterwards, or the next
   window, counts again.
2. **Protect these changes anyway** (accept): a `maintenance` with
   `ignore_thresholds: true` (§6.9), offered only after the list was opened,
   and with the typed count when the changes are more than ten times a limit
   (the first revision's rule, §4.5 there). The approval says plainly: "This
   protects whatever is on the disks when it runs, which may be more than
   you looked at. After it, the record of what was accepted is only Seraphina's
   counts and the protection service's own log." (verified: after a run with
   the limits off the diff counters go back to 0; daemon lab C,
   `16-after-ignore-thresholds`).
3. **Not tonight**: skip the next window (`hold_off`, §6.8) while the owner
   looks; it skips one window only.
4. **Change the limits**: the owner edits `/etc/snapraidd.conf` himself (a
   copy block shows the two lines and `systemctl reload snapraidd`); Seraphina
   never changes the daemon's config.

Not available, ever: a sync through `/schedule`, which would bypass every
limit (§6.12).

### 5.4 Other ways a run stops

| Cause | What Seraphina sees | What the card says, and what to do |
| ----- | ------------------- | ---------------------------------- |
| A truncated file, with `sync_prevent_truncations = 1` | `sync` exit 1; the fatal message "…use 'snapraid --force-zero sync'"; raised before the scan ends, so the diff counts do not change (source: 15.1 `cmdline/scan.c:973-990`; verified: daemon labs B and C) | "A file became empty. Check whether that was meant." The fix: delete the empty file, then bring it back with `undelete` (verified: daemon lab C, `13-after-truncation-abort`) |
| A data disk with all its files missing | `sync` exit 1, "…use 'snapraid --force-empty sync'" | "Disk 2 looks empty: is it mounted?" No bypass is offered |
| `up` fails | `up` exit 1, then `sync` and `scrub` cancelled "The preceding up operation failed with exit code 1" (verified: daemon labs B and C) | "The disks did not start. Check the hardware and the mounts." |
| The health gate | a step `canceled` "Array is DEGRADED! Task aborted!" (or PREFAIL, FAILING); the action's POST was 202 (source: `daemon/runner.c:1684-1727`) | "The run was cancelled because a disk is not healthy. It did nothing." Health recovers by a clean probe for a missing disk, or `heal` for damage (source: `daemon/parser.c:455-480, 1020-1028`) |
| A person's stop | §6.7 | "Stopped safely." |
| The daemon stops or crashes during a run | after its restart: `signaled`, `exit_sig 9`, and `blocks_unsynced > 0` (verified: daemon lab B) | unknown (§3.4); "The protection service stopped during a run." |
| **The server's power-off during a run** | stopping the daemon's service sends SIGKILL to the engine's process group (source: `daemon/runner.c:1940-1974`; verified: daemon lab B), so a run stopped that way does not save its state | before the daemon's unit stops, the owner's power-off preparation must stop the engine safely (SIGINT to `snapraid`, or the daemon's `/stop`) and wait for it to exit; the doctor and Q10 ask whether it does. That wait must stay well inside systemd's own limit: `poweroff.target` forces the power-off 30 minutes after it starts (`JobTimeoutSec=30min` with `JobTimeoutAction=poweroff-force`: source, systemd's own `poweroff.target`; observed on one server), so a preparation that waits longer is cut by a forced power-off, which kills the engine as above; about 25 minutes is the most to allow. Windows are set to end well before the power-off (§10.3) |
| The daemon's emergency shutdown (`sys_shutdown_on` set) | the queue is cancelled ("Canceled before shutdown"), a report runs, then the machine powers off; new actions may still get 202 and never run (source: `daemon/runner.c:89-118, 2258-2263`; not run in any lab) | recommended empty (D16); the card shows it when set |

### 5.5 Seraphina's own counts, as a report

The first revision's per-disk rule (10 files that are 5% of a disk, half a
disk, 100 across the array, or 10 that are 2%) is kept as a **report**, never a
gate (judgement):

- **On the daemon route**, the review page applies it to the frozen scan's list,
  per disk, and says which disk the changes are on ("1,198 of them on Disk 2,
  which is 60% of its files."). The link computes the per-disk counts from
  `diffs[]`, so the harness gets counts, not names.
- **Before the transition** (§8.9), the reader's guard report ("this sync would
  be held", DESIGN §41) is the check the owner reads before the one supervised
  sync.
- **After any complete sync**, nothing can be compared: the diff counters go
  back to 0 and the engine's scan of that sync is only in the daemon's root
  log (verified: daemon lab C, `16-after-ignore-thresholds`). The first
  revision's after-check is therefore dropped, and the card does not claim one.

---

## 6. The five actions

All five are `POST /snapraid/v1/<name>` with `Content-Type: application/json`,
a JSON object body (`{}` when it has no fields) and a `Content-Length`. Any
unknown field, wrong type or repeated key is 400 `{"success": false,
"message": "Unrecognized json"}` (source: `daemon/rest.c:1231-1391, 1641-1726`;
verified: daemon labs A and C). A POST without `Content-Length` is read as an
empty body, so the action runs with its defaults (source: `daemon/rest.c:359-362`):
the link always sends one. The only defence the daemon has against a web page
posting to it is the required JSON content type (source: `daemon/rest.c:314-345`).

| Action | Body Seraphina sends | What the daemon queues, as one run with one `scheduled_at` | Success | Refusals |
| ------ | -------------------- | ---------------------------------------------------------- | ------- | -------- |
| `maintenance` | `{"ignore_thresholds": false, "spindown_on_finish": false}`, or `true` for an accept (§6.9) | `[start]`, `up`, `sync`, `[scrub]`, `report` (`start` only with `notify_start`; `scrub` only with `scrub_percentage > 0`) | 202 `{"success": true}` | 409 "Maintenance already running or scheduled"; 400, 401, 411, 413, 415, 429; 500 or 503 |
| `heal` | `{"spindown_on_finish": false}` | `[start]`, `up`, `fix -e`, `scrub -p bad`, `report` | 202 | 409 "Heal already running or scheduled", … |
| `undelete` | `{"filters": [...], "filter_disks": [...], "spindown_on_finish": false}` | `[start]`, `up`, `fix --gui-rescan-after -m [-d D]… [-f F]…`, `report` | 202 | 409 "Undelete already running or scheduled"; 400 also for a filter longer than the system's path limit |
| `stop` | `{}` | nothing: it signals the running task (§6.7) | 202 `{"success": true, "message": "Signal sent", "number": N, "pid": P}` | 409 "No task running"; 400 "Expected empty JSON object" |
| `hold_off` | `{"enabled": true}` or `{"enabled": false}` | nothing (§6.8) | 200 `{"success": true, "message": "Hold off enabled"}` or `"…disabled"` | 400 |

(source: `daemon/scheduler.c:68-188, 267-354`, `rest.c:1551-1592, 1641-1726`;
verified: daemon labs A, B and C.) Every engine call the daemon makes is
`snapraid [-c CONF] <command> --log ">&2" --gui --lock-options [arguments]`
(source: `daemon/runner.c:2018-2031`; verified: daemon lab C engine logs).
`spindown_on_finish` is always false: the daemon's own idle spin-down applies,
and Seraphina adds no disk power steps (judgement). No action's 202 carries a
task number; only `/stop` returns one (verified: daemon labs A, B and C).

### 6.1 One approval per send, bound to what was shown

**Who.** Only the owner, in the cockpit, under the master token. No route for
an action is in `AGENT_TOKEN_ROUTES` (source: `swfactory/server.py`), no
persona or agent has a tool for one, and the operator persona sees freezes,
unknown outcomes and the rest among her open items but cannot approve (D13,
the first revision's rule).

**How** (judgement). The owner presses a button; the harness asks the link
for fresh facts and builds a **preview**, an approval record `A-<n>` in
`<data_dir>/snapraid/actions/` (0600) holding what will be shown, what will be
sent, and the **binding**: the facts that must still hold at the moment of
sending. The cockpit shows the preview and sends back its sha256 with the
press; the server refuses a hash that does not match the record. An approval
lasts `approval_minutes` (15) and allows **one** send; after a refusal, or a
send that never left, the owner presses again and gets a fresh preview (a new
call under DESIGN §40). Just before sending, the link reads the binding's
facts again and refuses with `changed` when any differs. The binding and the
one send are the link's rules too, not only the harness's (judgement, after
the build's review): it refuses an act whose `expect` lacks a fact the table
below binds for that action (`no_binding`), and a second act of an approval
id its own journal already holds (`used`), so a process that reaches its
socket without the cockpit cannot send an unbound action or replay one. The
harness numbers approvals above the highest id in the link's journal, so a
fresh data folder never reuses one.

| Action | The approval shows | Bound to, read again just before sending |
| ------ | ------------------ | ---------------------------------------- |
| `stop` | the running task: what it is in words ("protecting the latest changes", "checking for hidden damage", "bringing files back"), how far, since when | the daemon's start time; `active[0].number` equal to the shown task's number |
| `hold_off` on | the next window's time; "only that one run is skipped"; "a restart of the protection service undoes this" | the daemon's start time; `maintenance_schedule` unchanged; the shown window still ahead and no window passed since the preview; `hold_off` now off |
| `hold_off` off | that the next window will run | the daemon's start time; `hold_off` now on |
| `maintenance` | what the last scan found and when; both limits, and that the run stops itself over them; health; that nothing else is queued; the check for hidden damage that follows; the last run's duration as the estimate | the daemon's start time; both thresholds and `sync_prevent_truncations` unchanged; no maintenance queued or running; health not `degraded`, `prefail` or `failing` |
| `maintenance`, accept | the frozen scan: its counts by kind and by disk, the time it ran, the limits; that the review list was opened; the unbounded-accept sentence of §5.3 | the daemon's start time; the same scan (`last_command` is `sync` with its `last_command_at`, the eight `diff_*` counters, the last sync's exit 2); both thresholds unchanged; the review opened (the server's record); the typed count when over ten times a limit; the link's `accept` class |
| `undelete` | the chosen files and folders, by disk; "missing files only: nothing that exists is replaced"; "they come back readable only by an administrator" (§9.2) | the daemon's start time; every path a `removed` entry of the same scan on that disk (read again through `review`); each target disk mounted with its recorded filesystem (the reader's `info`, §8.5) and with its devices resolved (§3.5 rule 3); health not `degraded` |
| `heal` | how many places are damaged, the health, what it does ("repairs the damaged places from the protection data, then checks them again") | the daemon's start time; `blocks_bad` and health unchanged |

Every action also needs the checks of §2.4.

### 6.2 Sending: the intent first

In this order (judgement, under DESIGN §40's "the intent comes first"):

1. The harness writes the approval's `sent_at` and `outcome: null` to its
   record and to the journal (`<data_dir>/snapraid/journal-YYYY-MM.jsonl`,
   P1's file), then calls the link's `act`.
2. The link checks §2.4 and the binding, takes the counters of §6.4, writes
   its own journal line, sends the one POST with its constant header, and
   answers with the outcome.
3. The harness records the answer with `outcomes.response` (status, ids and
   the body's sha256; DESIGN §40), then follows the run (§6.5).

A crash between 1 and 3 leaves an intent without an outcome: the outcome is
unknown, and the next step is §6.4's read-only look.

### 6.3 The outcome of the request

| Answer | DESIGN §40 outcome | Why |
| ------ | ------------------ | --- |
| 202 (`maintenance`, `heal`, `undelete`, `stop`) or 200 (`hold_off`) | **succeeded**: the daemon took it. For the first three that means queued; the run's own result is a separate observation (§6.5) | 2xx |
| 400, 401, 404, 405, 409, 411, 413, 415, 429 | **failed (refused)**: nothing was queued or changed | the login check returns before any route handler (source: civetweb `civetweb.c:8465-8476`, `daemon/rest.c:3140-3390`); validation and the "already running" check come before scheduling (source: `daemon/rest.c:1231-1391`); verified: daemon labs A and C, a 401 POST created no task |
| 500 "SnapRAID executable not found", 503 "Daemon is terminating" | **unknown** under DESIGN §40's rule for 5xx, though the source shows both are returned before anything is queued (source: `daemon/runner.c:1976-1990`); the look of §6.4 then shows nothing was queued | D6 decides whether these two exact bodies become documented refusals |
| the connection refused, never made | **failed (never left)** | DESIGN §40 |
| the link refused before its POST (`changed`, `no_binding`, `used`, `engine_too_old`, `health`, `auth_off`, `not_allowed`, `one_disk`, `too_slow`, a bad request, its installation or caller) | **failed (refused)** | every such refusal comes before the POST, or after a 429 that queued nothing (judgement; `snapraidd.LINK_REFUSALS`) |
| the link's own failure (`failed`) or no answer from the link | **unknown** | it may come after the POST; once the POST is answered the link writes its journal line only as a best effort, so nothing after it changes the outcome (judgement) |
| a reset, an end of stream or a timeout after the request was written; a close by `net_acl` | **unknown** | seen as a reset from a dead daemon (verified: daemon lab A; source: civetweb `civetweb.c:11034-11040`) |

A 429 on an action is a refusal, so the link waits the `Retry-After` second
and sends once more within the same approval, after checking the binding
again: nothing ran, so it is a new call (judgement, allowed by DESIGN §40). Nothing
whose outcome is unknown is ever sent again by itself; that rule has no
exception here (DESIGN §40). When this is built, §40's audit table gains the
two rows "SnapRAID daemon reads" (GETs, retried after a transport error or a
429, and named in `outcomes.RETRIED`) and "SnapRAID daemon actions" (never
retried after an unknown outcome).

### 6.4 Matching the answer to its run

The daemon's answers carry no run id (the group is internal; source:
`daemon/rest.c:2161-2316`, `elem.c:342-345`), so Seraphina matches by what
she recorded (judgement, on the verified facts of §3.5):

1. **Just before the POST**, the link records the highest task number in
   `pending`, `active` and `history` (`/tasks?limit_history=50&limit_messages=0`),
   `pulse.tasks`, the daemon's start time and the clock.
2. **After a 2xx**, the run is the set of tasks with `high_command` equal to
   the action, a number above the recorded highest, and one shared
   `scheduled_at` within a few seconds of the POST. `started_at` values are
   made unique by adding a second (source: `daemon/elem.c:463-469`).
3. **The run has ended** when its `report` task is `terminated`, or every step
   is `terminated`, `signaled` or `canceled`.
4. **After an unknown outcome** the same search runs, read-only:
   - tasks found: the request was taken. Recorded "succeeded (queued), seen
     afterwards", and followed;
   - none found and no restart since: it did not take effect. Recorded so,
     with nothing sent again;
   - a restart in between: the queue may have been lost, so it stays
     **unknown**, with the tracker item "Outcome unknown: check before retrying
     (a protection run)" (`outcomes.UNKNOWN_TITLE`).

   For `hold_off` the look is `snapraid_config_hold_off` (§6.8); for `stop` it
   is the daemon's own mark on that task (§6.7). When the link itself gave no
   answer the look waits: the link's instance may still be working, so the
   harness waits 75 seconds for it (`ACT_TIMEOUT`, longer than the unit's
   `RuntimeMaxSec` of 60), the link sends no POST after 35 seconds of its
   instance (`SEND_DEADLINE`; its POST then ends by 52), and the look runs at
   the first poll after 75 seconds from the send, when the link can no longer
   send (judgement: a look before then could call a skip still on its way "did
   not take effect").
5. **Duplicates.** The daemon refuses an action of the same kind while one is
   queued or running (source: `daemon/runner.c:40-55`), so a person's fresh
   approval while a duplicate is still queued gets 409, which is harmless. A
   heal during a maintenance queues behind it (verified: daemon lab A). A
   second undelete with other files is refused while the first is queued, so
   those files need a new approval later (verified: daemon lab A). A window
   that fires while Seraphina's maintenance is queued is dropped by the daemon;
   if the window fired first, her POST gets 409 (source: `daemon/scheduler.c:74-78, 586-596`).

### 6.5 Following the run, and its verdict

While the run is active, `/metrics` gives progress every `active_seconds`, and
the link reads `/tasks` when `pulse.tasks` moves. The verdict, per run
(judgement, on §3.5's rules):

- **the worst step decides**: `terminated` 0 is done; 3 is done with a warning
  (degraded); `sync` exit 2 is a freeze (§5); any other failure is "failed",
  with the step and the daemon's fixed cancel message, if any, in words;
  `signaled 9` after a restart is unknown;
- **a maintenance is complete** only with `blocks_unsynced == 0` afterwards;
- **an undelete** is judged by `/array`'s `fix_recovered` and
  `fix_unrecoverable` and its `fixes[]`, not by the `fix` exit code: in one
  run a restore succeeded while `--gui-rescan-after` exited 1 on an unrelated
  empty file (verified: daemon lab B). `fixes[]` and `fix_recovered`
  accumulate across fix runs until the next sync (verified: daemon lab C,
  `09-after-undelete-rest`), so Seraphina counts the difference from before
  the run;
- **a heal** is judged by `blocks_bad` going to 0 and health leaving `corrupt`.

The verdict goes to the approval record, an activity entry, the tracker
(§7.4) and, for a failure, the `parity.failed` notice (§7.3).

### 6.6 After a restart

§3.4's rules apply to every approval: queued or running runs become unknown;
a skip lapses and is asked again; an approval not yet sent is void, because
its binding includes the daemon's start time.

### 6.7 Stop

The daemon's `/stop` acts on whatever task runs when it arrives, and cannot
name one (source: `daemon/runner.c:2195-2248`). It marks the task cancelled,
adds the message "Stop requested", sends SIGTERM to the task's process group
and answers with that task's `number` and `pid` (`pid` 0 if no process had
started); it never touches the hook script (verified: daemon labs A and C;
docs: `snapraidd.yaml:300-302`). With a real 15.1 engine the task ends
`terminated` with exit 1, saving its content file and keeping partial parity;
the rest of the run is cancelled ("The preceding sync operation was
canceled"), the report runs (`[ERROR]`, exit 1), and the daemon is idle about
two seconds later (verified: daemon lab C, `05-stop-during-sync`; lab B). A
process killed by the signal instead reads `signaled` 15 (verified: daemon lab
A, the fake engine).

**It is not idempotent.** A quick second stop reaches the same task while 15.1
saves; a later one may reach the next task, which the fake engine showed
(verified: daemon lab C, the same number and pid; lab A, task #7, the next
one). So (judgement):

1. the approval names the task number, and the link sends only if
   `active[0].number` still equals it;
2. the answer's `number` is compared: a different one means the wrong task
   was stopped, which raises a high notice and a tracker item, and nothing is
   sent again;
3. an unknown outcome is followed by a read of that task only (the link's
   `run`); a second stop is never sent by itself, because it may reach the
   next task. The daemon adds "Stop requested" to the task's messages at once
   (source: `daemon/runner.c:2219`): there, the stop was taken. The task ended
   with no such message while it is still the daemon's latest task (so no
   later task can have been reached): it did not take effect. Anything else
   stays unknown. The task's status decides nothing: 15.1 traps the signal and
   stays `processing` for about two seconds while it saves (verified: daemon
   lab C, `05-stop-during-sync`), and a task that ended by itself is no
   evidence that the stop arrived;
4. 409 "No task running" is recorded as "nothing to stop";
5. the step the stop's answer named (its number and `scheduled_at`) ending
   `terminated` 1 or `signaled` 15 is "stopped safely", never "failed", also
   when it is the run's last engine step (an undelete's `fix`, a heal's or a
   maintenance's `scrub`, a `sync` with no scrub after it), where no later
   step carries "was canceled" (§3.5 rule 11).

The cockpit says: "Stopping safely: the protection service saves what was done.
Don't turn the server off for a minute." There is no Kill button.

### 6.8 Skip the next run (`hold_off`)

`POST /hold_off {"enabled": true|false}` sets the switch to that value and moves
`pulse.array` each time (source: `daemon/rest.c:1704-1705`; verified: daemon
lab A). It is read back from `/metrics` `snapraid_config_hold_off`, which is
always present (source: `daemon/rest.c:2899-2901`). What it does (source:
`daemon/scheduler.c:571-596`; verified: daemon lab A, a 14:42 window skipped and
14:43 run; lab C, `10-after-scheduled-window-skipped`, no task, no report):

- the next scheduled window is skipped, and the switch resets to off;
- several windows caught up in one pass of the scheduler set one switch, so
  one skip swallows all of them;
- it does nothing to an API maintenance, `/schedule`, or the daemon's probes
  and spin-down;
- it is lost when the daemon restarts and kept across a reload (verified:
  daemon labs A, B and C). Windows missed while the daemon was down are not
  run later, and a clock jump of more than five minutes skips catching up
  (source: `daemon/scheduler.c:522-583`).

So (judgement, under DESIGN §40):

- **an unknown outcome** is read from `snapraid_config_hold_off`: on means it
  took effect; off, with no window passed since the send, lets the owner send
  again with a fresh approval; off with a window passed in between stays
  unknown, because the first send may have been used up by that window, and
  a repeat would skip a second window he did not approve. Sending "off" again
  is harmless, but is still the owner's press. `hold_off` is not added to
  `outcomes.RETRIED`;
- **after a restart** an approved skip has lapsed: the card's amber line, the
  `parity.lapsed` notice and a tracker item "The skip of tonight's run was
  undone" ask the owner again. Seraphina never sets it again by herself,
  which would replay an approval;
- the owner's backup job may set the switch itself before its backup, right
  before it, because a restart undoes it (§1.4).

### 6.9 Protect the latest changes (`maintenance`), and accept a freeze

**Protect the latest changes** sends `{"ignore_thresholds": false,
"spindown_on_finish": false}`: the same run a window makes, with the limits
(verified: daemon lab C, `03-first-maintenance`, `06-recovery-maintenance`).
The sync's options are `[-h]` when `sync_prehash = 1`, `[-Z]` unless
`sync_prevent_truncations = 1`, `[--gui-touch-before]` when
`touch_zero_subseconds = 1`, then both threshold options (source:
`daemon/scheduler.c:84-104`). It is offered when nothing is queued or running
and health allows it; it is the way to protect changes today instead of
waiting for the window.

**Protect these changes anyway** (accept) sends `ignore_thresholds: true`, on
the binding of §6.1, with the `accept` allow class. It drops both limits for
that one run and changes neither `-Z` nor the truncation rule (source:
`daemon/rest.c:1344-1361`, `scheduler.c:89-104`; verified: daemon lab C,
`16-after-ignore-thresholds`; lab B, `02-override`). The approval's sentence
says it is unbounded (§5.3). Seraphina saves the reviewed counts (per kind and
per disk, never names) with the approval, because after the run the diff
counters go back to 0 (§5.5).

### 6.10 Bring files back (`undelete`)

The review page (§5.3) is where files are chosen: only entries the last scan
lists as `removed`, on one or more disks. The harness sends paths; the link
builds the filters itself (judgement, on 15.1's matching rules):

- each pattern is `/` followed by the path, with every `\`, `*`, `?`, `[` and
  `]` preceded by a backslash, and a final `/` for a folder. In 15.1 a leading
  `/` anchors the pattern at the disk's root; without it a name matches at any
  depth; `**` next to a `/` matches across folders; and a backslash makes the
  next `*`, `?`, `[`, `]` or backslash plain (source: 15.1 `cmdline/str.h:130-146`,
  `cmdline/elem.c:236-270`). 15.1 matches with its own function, not the
  system's `fnmatch`, so PR4's result for 13.0 does not carry over; T42 checks
  the escaping against the real 15.1;
- a path with an empty, `.` or `..` component, a NUL or a newline, or longer
  than the system's path limit is refused before sending (15.1 itself refuses
  empty, `.` and `..` components, source: 15.1 `cmdline/elem.c:66-140`; the
  daemon refuses a filter over the limit with 400, verified: daemon lab A);
- at most 1,000 filters per approval (the first revision's limit); a folder
  chosen whole is one pattern, which brings back every missing file under it
  that the protection knows;
- `filter_disks` is the one disk of the chosen entries, by SnapRAID name:
  **one disk per approval**. 15.1 keeps `-d` and `-f` as two separate lists
  (source: 15.1 `cmdline/snapraid.c:630-649`), so a file is brought back when
  its disk is any chosen disk and its path matches any chosen path; with
  `docs/a` on `d1` and `photos/b` on `d2`, a missing `photos/b` on `d1` would
  come back too. The harness refuses a choice across disks, the link refuses
  it too (`one_disk`), and the review page lets the owner choose on one disk
  at a time (judgement, after the build's review).

The daemon runs `fix --gui-rescan-after -m [-d D]… [-f F]…`: missing files
only, and nothing that exists is opened for writing (source:
`daemon/scheduler.c:267-354`; verified: daemon lab C, `08-undelete`,
`fix --gui-rescan-after -m -d d1 -f docs/d1-docs-003.bin`; docs: 15.1
`snapraid.txt:316-319, 845-852`). Before sending, the link refuses when a
target disk has `devices: []` or health `degraded`, and the harness refuses
when the reader's mount check does not show that disk mounted with its
recorded filesystem: on real hardware it is unknown whether an undelete onto
an unmounted disk is refused or writes into the bare mount folder on the
system disk (Q3; judgement, D20). The result page shows the counts, the
files that could not be brought back (live, through the link's `fixes`), and
§9.2's note on owners and permissions.

### 6.11 Repair hidden damage (`heal`)

Offered when `blocks_bad > 0` or health is `corrupt`. The daemon runs `fix -e`
(repairs only the blocks a scrub marked bad) then `scrub -p bad` (clears the
marks) (source: `daemon/scheduler.c:145-188`; verified: daemon labs B and C,
`15-heal`). Health `corrupt` does not cancel `fix`; `degraded`, `prefail` and
`failing` do (source: `daemon/runner.c:1684-1727`), so with a missing or
failing disk the card says to deal with the disk first. A clean heal reports at
the `info` level, which reaches the forwarder only with `notify_result_level =
info` (source: `daemon/notify.c:381-405`; verified: daemon lab C, `15-heal`).

### 6.12 What Seraphina never sends

The link has no code path for these; T27 checks that it cannot build them
(judgement unless cited):

- `POST /snapraid/v1/schedule`: raw engine options, no thresholds (§3.2);
- `PATCH /snapraid/v1/config` or `/snapraid/v2/config`: it would change the
  window or the limits, and any `PATCH`, from anyone, also rewrites the
  daemon's config file and clears its login cache, so Seraphina's next request
  pays the password check again (source: `daemon/rest.c:1098-1108`,
  `conf.c:1387-1388`);
- `POST /clear`, which cancels every queued task of every run (source:
  `daemon/runner.c:2250-2280`); `/report`; `/refresh`; `/suspend_idle`;
- `GET /snapraid/v1/system`, which returns the host name and hardware (source:
  `daemon/rest.c:776-819`); the deprecated `/v1/disks` and `/v1/config`;
- any method or path not in §3.2's table, and any request without a
  `Content-Length` or with `Transfer-Encoding: chunked` (411; source:
  `daemon/rest.c:346-391`).

---

## 7. Reports into notices and the tracker

### 7.1 The forwarder (`sf-snapraidd notify`)

**How the daemon runs it** (source: `daemon/notify.c:112-235`, `os/unix.c:1362-1556`;
verified: daemon labs B and C): `/bin/sh -c "<notify_result after
substitution>"`, in its own process group, as `notify_run_as_user` or root when
that is blank, with the environment cut to `PATH`, `TERM=dumb`, `LANG=C`, `IFS`
and `PWD`, and its output thrown away. **There is no time limit**: the runner
waits for it, so a slow command delays every queued task (it logs a warning
after 30 seconds). The command after substitution must fit in 1,024 bytes.
The daemon logs the whole command line at `info` on every run, and
`GET /v2/config` returns it as text (source: `os/unix.c:1544-1550`,
`rest.c:1141-1208`), so it must hold no secret.

**The line the owner adds** to `/etc/snapraidd.conf` (judgement; §10.3):

```
notify_result = /usr/local/libexec/seraphina/sf-snapraidd notify result --narrow %l %h %c "%s"
notify_result_level = info
```

`%l` is `critical`, `error`, `warning` or `info`; `%h` is `maintenance`,
`heal`, `undelete` or `refresh`, or `report` for a report of its own and a
health change; `%c` is the run's exit code (the first failure in time, else a
non-zero success code such as 3, else 0); `%s` is `[LEVEL] SnapRAID <Kind>`
(source: `daemon/notify.c:112-235`, `runner.c:400-456`). All four come from
fixed sets, so the command line carries no file name and no text a person
wrote; the substitution is not quoted by the daemon, so `%s` sits in double
quotes. `--narrow` is taken out of the command by the daemon and fixes the
report's format (source: `daemon/report.c:154-295`).

**What it does** (judgement): it runs as root under `python3 -I`; checks its
arguments against those fixed sets (anything else: it records nothing and
exits 2); reads the report on standard input to the end, at most 1 MiB, and
throws it away, because the report is untrusted text with raw file paths (both
real-engine labs saw stray tag fragments inside a warning line; verified:
daemon labs B and C) and Seraphina takes no decision from it; reads the hook's
header line from `/etc/seraphina/snapraidd-hook.header` (root, 0600); and
sends one event to the harness's hook API on loopback:

```
POST /api/hooks/event
Authorization: Bearer <hook token, from the header file>
Idempotency-Key: snapraidd-<sha256 of the arguments and the minute>
{"kind": "parity.report", "subject": "<%h>", "detail": "level=<%l> code=<%c>"}
```

with 1 second to connect and 3 seconds in all. It exits 0 when the harness
answered 2xx, and 1 otherwise: the daemon then marks that report task
`exit_code -1` and adds the fixed fatal message "Notification failed (check
syslog for details)" (source: `daemon/runner.c:577-587`; verified: daemon lab
B). That text is a message, not the report's `exit_msg`, which the daemon
serializes only for a cancelled task (source: `daemon/rest.c:2184-2198`), so
Seraphina reads a failed notification from the report task being `terminated`
with −1, the only way a report gets that code, or from the link's
`notify_failed` flag (fatal messages are listed even with
`limit_messages=0`, source: `daemon/rest.c:2234-2245`), and shows it (§4.2). Nothing is lost when the harness is down:
the daemon's task list is the record, and Seraphina reads it at her next read
(§7.2). The hook API's rules apply unchanged (DESIGN §34): POST only, the
token in a header, 401 after a fixed delay for a wrong one, the source's own
rate limit, a body of at most 2 KB, and the `Idempotency-Key` honoured for 24
hours, so a repeat changes nothing.

**Why not a spool file** (judgement). An earlier plan asked for a forwarder that
holds no secret, writes a spool file, makes no network call and exits in
milliseconds. This design keeps the first and departs from the other three on
purpose: it POSTs to the hook, bounded, and writes nothing.

- **The secret.** The forwarder holds none of its own: the hook token sits in
  a root-only header file it reads, never on its command line, which the
  daemon logs and `/v2/config` returns (above). A spool writer would need no
  token, but the harness would then need a root-owned spool folder it can
  read and a watcher for it, a second channel from root to the harness with
  its own ownership, naming and clean-up rules, where the hook API's checks
  (the token, the rate limit, the size cap, the idempotency key) already
  exist and are tested (DESIGN §34).
- **The delay.** The POST is bounded: 1 second to connect and 3 in all, so
  with the harness down each report holds the daemon's runner for at most
  3 seconds (the daemon waits for its notify command with no limit, above).
  A report comes once per run, so that is at most a few seconds a night.
- **Nothing is lost** either way: the daemon's task list is the record, and
  a report the harness did not get is read at her next read and shown as
  "not delivered" (§4.2), where a spool file would also wait for a reader.

If labs L16 or L17 show the 3 seconds matter (a runner held while queued
work waits on a power-off), the spool shape is the fallback: the forwarder
writes one fixed-format line to a root-owned spool folder and exits, and the
link reads it with its `read` operation (judgement).

`notify_start` and `notify_heartbeat` are not needed by Seraphina (she sees a
start from `/metrics`), and stay the owner's for his own uses (judgement).

### 7.2 What the harness does with a report

A `parity.report` event never makes a notice by itself (judgement). It is a
prompt: the panel reads `/metrics` and, through the link, `/tasks`, `/array`
and `/state` at once, finds the report task the event is about (the newest
`report` of that `high_command`, finished within a few minutes of the event),
and classifies its run by §6.5's rules. The same classification runs when a
read finds a finished report task the panel has not handled yet, so a report
the forwarder could not deliver is handled too, only later. Each run is
handled once, keyed by its report task's number and finish time (numbers can
be used again after a restart, §3.4).

| The run | Notice (§7.3) | Tracker (§7.4) |
| ------- | ------------- | -------------- |
| a maintenance, complete (`blocks_unsynced == 0`) | `parity.ok` (low), which closes an open `parity.failed` | closes `snapraid:frozen` and `snapraid:failed` |
| a maintenance, frozen (`sync` exit 2) | `parity.held` (high), one incident while the freeze lasts | `snapraid:frozen` opened or updated |
| a maintenance or another run, failed | `parity.failed` (high) | `snapraid:failed`, with the step and the cancel message in words |
| a run Seraphina asked for | as above | a note on the approval's item, with the verdict |
| a health change (`%h` is `report`, `%l` `critical`) | `disk.failing` per disk number, from `/v2/disks` health | `snapraid:health:<n>`, as P1 |
| a heal or undelete | none for a success; `parity.failed` for a failure | the approval's item closed or noted |
| a run with the limits off that Seraphina did not approve (§11.5) | `parity.released_outside` (high) | `snapraid:outside` |

### 7.3 Notices

Phone text is built by code from typed fields only, and `filter_text` replaces
anything that looks like a path, a URL, a token or a long number with the fixed
short notice (source: `swfactory/notify.py`). P1 already has the kinds' fixed
texts (DESIGN §34, §41); on the daemon route (judgement):

| Kind | Priority | When | Text |
| ---- | -------- | ---- | ---- |
| `parity.held` | high | a freeze (§5.2) | P1's text: "{removed} files deleted and {updated} changed on {disks} disk(s). Open the cockpit to look before protecting them." (the per-disk count comes from the link, §5.5) |
| `parity.failed` | high | a run failed (§7.2) | P1's text |
| `parity.ok` | low | a complete maintenance after a failure or a freeze | P1's text; closes `parity.failed` |
| `disk.failing` | high, breaks through quiet hours | a disk `prefail` or `failing` | P1's text, one incident per disk number |
| `parity.released_outside` | high | a run with the limits off, or an action in the link's journal, that the cockpit did not approve | P1's text |
| `parity.unchecked` | — | — | **dropped** on the daemon route: there is no after-check (§5.5). Its text stays in the code until P2b removes the kind |
| `parity.down` | high | the daemon has not answered for `down_minutes` | new: "Disk protection is not running" / "The protection service has not answered for {minutes} minutes. Nothing is being protected meanwhile. Open the cockpit." Closed quietly when it answers |
| `parity.lapsed` | default | a restart undid an approved skip (§6.8) | new: "The next protection run will not be skipped" / "The protection service restarted, so the skip you set was undone. Open the cockpit to set it again." |

No disk's SnapRAID name and no file name is ever in a field. Adding the two new
kinds touches `RULE_IDS`, `RULE_ROUTES`, `operational_text`, the
`rule_priority` defaults, settings validation and the DESIGN §34 table, as the
first revision's kinds did; the hook kind `parity.report` is added to the
kinds an event source may send, routed to the panel, never to the
incident rules (§12.3).

### 7.4 Tracker items and activity

Items open in the system scope through `Factory.system_item`, keyed so a new
event never opens a second item, and are closed by the factory with `receipt`
evidence naming the daemon task or the approval, as P1's findings are
(DESIGN §41; judgement):

| Key | Kind | Opened when | Closed when |
| --- | ---- | ----------- | ----------- |
| `snapraid:frozen` | decision | a freeze | a complete sync, or a later scan under both limits |
| `snapraid:failed` | finding | a run failed | a later run of the same kind completes |
| `snapraid:health:<n>` | finding | a disk is `prefail`, `failing`, `degraded` or unreadable | a fresh readable healthy observation (P1's rule) |
| `snapraid:unknown:<A-n>` | commitment, titled "Outcome unknown: check before retrying (…)" | an action's outcome stays unknown (§6.4) | a person closes it (DESIGN §40) |
| `snapraid:lapsed` | decision | a restart undid a skip | the owner sets it again or lets the window run |
| `snapraid:schedulers` | finding | two starters, or none (§1.4); counts only, never which places | one starter again |
| `snapraid:service` | finding | no login, a listener beyond loopback, the engine too old, the daemon not answering, a report not delivered, the link's password refused (401: the link's calls then wait for the next full read) | the condition is gone at a fresh read |
| `snapraid:outside` | finding | a run outside the daemon, or a run or action Seraphina did not ask for (§11.5) | a person closes it |
| `A-<n>` notes | note on the item above | each approval, send, outcome and verdict | — |

The activity source `snapraid` (DESIGN §36, P1's journal) gains entries for
each approval, send, outcome, run verdict, freeze and daemon restart: ids,
counts and codes, never a name.

---

## 8. The read-only fallback: the P1 reader

P1 built the reader, `bin/sf-snapraid`, and DESIGN §41 describes it as built:
its fixed places, its checks on every call, its verbs, its log reading, its
inventory and its guard report. This section says when it runs, and what P2a
changes so that it works on the owner's current host.

### 8.1 When it runs SnapRAID, and when it must not

The reader's `status`, `guard` (its `diff`) and `list` take SnapRAID's lock,
and `diff` and `smart` wake every array disk (source: 13.0 `cmdline/util.c:634-672`;
first revision §2.4). A read that holds the lock when the owner's job or the
daemon starts a sync makes that sync fail with "already in use" (verified: lab
`sync-lock`). So (judgement):

- **With the daemon installed** (its unit file exists, or its port answers
  `/metrics`) the reader runs no SnapRAID command at all. Only `info` and
  `disks` run: the version check, the config check, the mounts and their
  UUIDs, the scheduler inventory, the content fingerprint and the daemon
  config summary (§8.5); and the disks' models and serials from `lsblk` and
  `blkid`, which the link never passes on (§3.6). Every other verb answers
  `daemon_runs` with "The protection service reads the disks now; Seraphina
  asks it instead." On the daemon route the reader is still needed: for the
  scheduler count (§1.4) and an undelete's mount check (§6.10).
- **Without the daemon**, the reader's SnapRAID reads wait (the card keeps its
  last facts and says why) while:
  - any `snapraid` process runs (read from `/proc`, as the inventory already
    reads it);
  - a unit named in `snapraid.quiet_units` is active (the owner names his
    backup job's service here; the shipped list is empty), or that unit's
    timer starts within `quiet_margin_minutes` (30);
  - the time is inside the power schedule's quiet periods or the power-off
    (`clock.PowerSchedule`).
- **`diff` never runs on a timer**: only the owner's **Check for changes now**
  runs it, and only when none of the waits above applies. The hourly status
  and the daily health read stay, with the waits.

### 8.2 The version policy

The reader reads the version from the binary it will run, never from a package
database alone (P2a; judgement unless cited):

| What the system has | The reader | The daemon route |
| ------------------- | ---------- | ---------------- |
| 13.x | reads, as P1 | impossible: daemon 2.x needs 15 (verified: daemon lab B) |
| a distribution build whose `--version` prints `snapraid v0` (reported for the owner's package) | treated as 13.0 only when the binary is the file the package database says it owns (`dpkg -S`) and that package's version is `13.0-*` (`dpkg-query -W -f '${Version}'`); otherwise refused. The card says "SnapRAID 13.0 (the system's package, which does not report its own version)" | impossible |
| 14.x (14.10, for the transition) | reads `status`, `smart`, `probe` and `disks` with the note "This version is read with a 13.0 reader: some facts may be missing."; `guard` and `list` refused until 14.10 runs are recorded; the transition's count is made with 13.0 before 14.10 goes in (§8.9) | impossible |
| 15.x with no daemon installed | refused in those words ("needs the SnapRAID Daemon, and the SnapRAID Daemon is not installed"); the doctor's line passes for 15.x only while a daemon answers. Reading it could come once 15.1 runs are recorded as fixtures (15.1 added log tags, changed what "fatal" means, recomputes parity after an interrupted sync, and refuses `scrub` and `touch` before the first sync; reported from the upstream history, to be pinned by those fixtures) | not in use |
| 15.x with the daemon installed | `info` only (§8.1) | yes |
| 12.x | reads with a note, as P1 | impossible |
| anything else, an unsafe binary, or **two SnapRAID binaries of different versions** | refused, naming them ("not a version Seraphina reads" below 12 or unrecognised). The reader takes `/usr/bin/snapraid` first, while systemd's and login shells' search paths take `/usr/local/bin` first, so a newer build beside the package would have the reader run 13.0 against content the newer one upgraded (source: `bin/sf-snapraid` `BINARIES`) | refused |

The doctor's and the setup page's version lines follow the same table (P1 says
"13.x" in both today; DESIGN §41).

### 8.3 The layout fixes

What the owner's host needs that P1's checks refuse (P2a; each with tests
that fail first; judgement unless cited):

1. **Content copies in a system folder.** P1 accepts a content copy only in a
   disk's `.snapraid/` folder or in `/var/snapraid/` (DESIGN §41). A
   distribution package keeps it in `/var/lib/snapraid/` (reported), and
   SnapRAID's own manual puts copies at a disk's root (docs: 13.0
   `snapraid.txt:139-141`). P2a accepts a copy in a root-owned folder on the
   system disk, `/var/snapraid/` or `/var/lib/snapraid/`, and at the root of,
   or in a `.snapraid/` folder on, a recorded data or parity disk; every copy
   still on a different disk, and at least parity levels + 1 of them (source:
   13.0 `cmdline/state.c`, around line 467). The doctor's "first copy on the
   system disk" check accepts both system folders.
2. **A disk being taken out.** A `data` line that points at an empty,
   root-owned placeholder folder under `/var/lib/` is not refused as "a
   system folder" or "a disk with no filesystem recorded" when the reader's
   config names it as a placeholder (`placeholders: {"<disk>": "<folder>"}`).
   The card shows it amber, "Disk 2 is being taken out of protection.", and
   lists the disk under **Not covered**; the placeholder must stay empty, or
   it is a problem. The rest of the config is checked as before.
3. **A disk mounted at two places.** P1 takes the last mount listed for a
   filesystem. A path in the config is accepted when it equals any mount point
   of the recorded filesystem UUID whose root (mountinfo's fourth field) is the
   filesystem's own root; a bind mount of a subfolder is still refused. The
   reader reports which mount point the config uses. Tests: two sibling
   mounts in both orders, for a data and a parity disk, and a subfolder bind.
4. **15.x config keywords** for the case of §8.2's fourth row: `extra`,
   `.snapraidignore`, `**` in filters, and `smartctl … [info: …]` from an
   allow-list; and `-W`/`--force-realloc-tail` and every `--gui-*` option on
   the reader's never-used list. Built: `extra` and the never-used options;
   not built: `.snapraidignore`, `**` and `[info: …]`, moot while 15.x is read
   through the daemon only (DESIGN §41 "Not built yet").
5. **SnapRAID in a container, or managed by a daemon.** P1 says "not set up,
   optional" whenever no host binary exists. P2a reads `/metrics` on the
   daemon's default port without a login (200 means a daemon answers) and
   probes `GET /snapraid/v1/state` (401 means a login is on, 200 means none),
   and uses the inventory's Dockge scan for a SnapRAID stack; the card then
   says "SnapRAID runs as an app on this server; Seraphina only reads it." or
   shows the daemon route's states (§11.6). Built: the daemon's detection;
   not built: the container state, and translating container paths, dropped
   with the container route (DESIGN §41 "Not built yet").
6. **The reader's advice.** A sudo refused under no-new-privileges now says
   "run the harness from a unit without NoNewPrivileges: on a server, the
   harness's own system unit", never "from its user unit", which on a shared
   server sends it back to the owner's login account (§11.7).

### 8.4 Where the reader runs

sudo changes the user, not the mount namespace (judgement; docs: systemd.exec,
**UNVERIFIED** here). A harness unit hardened with `ProtectSystem=strict`,
`ReadWritePaths=` limited to its own state, `PrivateTmp=yes` or a
`TemporaryFileSystem=` over the data folders would hide the array from the
reader and make its own state writes fail, with `NoNewPrivileges` removed or
not. So in P2a the reader runs each verb through PID 1, as the sibling design
prescribes for its helper (YOUR_SERVER §6.1): `systemd-run --wait --pipe
--collect --quiet` with the reader's own fences, which puts it in the host's
namespace; until that is built, the harness's unit must leave that hardening
out, and the doctor says so (judgement). The link (§3.2) needs none of this:
it is its own unit and needs no sudo.

Some Ubuntu releases ship `sudo-rs` as `sudo`, with the classic program as
`sudo.ws`. The reader's sudoers lines (`Defaults!<path> !use_pty`, the `""`
no-arguments rule, the `.new` name that `@includedir` skips, `visudo -cf`) were
checked only against classic sudo (first revision, PR2); L19 repeats L8 under
`sudo-rs` and `sudo.ws`, and if `sudo-rs` refuses a line, the block uses a form
both accept (Q11).

### 8.5 What the reader keeps doing

As built (DESIGN §41): `info`, `status`, `smart`, `probe`, `disks`, `guard`,
`list`, `files`, `changes`, `job` and `log`, each checked as there, with the
run records, the job lock, the log reader, the inventory and the fingerprint.
P2b adds to `info`, as root, a **daemon config summary** read from
`/etc/snapraidd.conf`, with only these values: `net_enabled`, the addresses in
`net_port`, whether `net_acl` is set, whether `net_auth_credential` is set (yes
or no), `net_config_full_access`, `sys_shutdown_on`, `check_updates`, and
whether `notify_result` names the forwarder. Never the credential line, never
a notify or hook command as text (judgement). That covers the settings
`/v2/config` does not return (§3.6). Once the daemon is installed, `info` no
longer hashes the content file (the daemon rewrites it at every run and the
reader keeps no mark then, so a hash would be the whole file every time) and
checks only the copies on the system disk, listing the others as not checked:
no data disk is opened or woken by a read (§4.3; judgement, after the
build's review).

### 8.6 The guard, as a report

The checks G1 to G11 and their thresholds stay exactly as DESIGN §41 states
them, with the verdict `would_hold`, `would_wait`, `would_run`,
`nothing_to_do` or `error`, and `report_only: true`. Nothing is held, now or
later, because nothing the reader does writes. They are used for the
transition's count (§8.9) and, through their thresholds, for the per-disk
lines of the daemon route's review page (§5.5).

### 8.7 Install and removal

The reader's five copy blocks and two removal blocks stay as built (DESIGN
§41), each installing to a root-owned `.new` path, checking that copy, and
moving it into place only when the check passes again. P2a changes their
version line to the policy of §8.2, adds `placeholders` to the drafted
config, and their sudoers lines follow L19. The link has its own blocks (§10.4).

### 8.8 What the reader never does

Unchanged from P1: no shell; no path, binary or config from the caller; no
config it has not validated; no SnapRAID test, expert or pool options; no
config writes; nothing written on the array; no file contents read or returned;
no SIGKILL. And now: no SnapRAID command while the daemon is installed, and no
`diff` on a timer (§8.1).

### 8.9 The transition sync, supervised

The move from 13.0 to 15.1 needs one sync with 14.10, which reads 13.0's
content format and rewrites it in the newer one; 15.1 refuses the old format
(verified: daemon lab B, content-format tests: 13.0 writes `SNAPCNT2`, and a
14.10 sync rewrote it to `SNAPCNT3`; reported: the owner's content is in the
old format and 15.1 refused it in a check on his host). It is the first sync
in a long time, it may take a disk out of the array, and it rewrites the
content file, so it gets the strictest rules (judgement):

- **Before it:**
  - a fresh full backup exists and was checked;
  - the owner has confirmed any disk leaving the array;
  - the reader's guard report with 13.0 shows the counts, and no disk other
    than the one being taken out loses many files (§5.5);
  - the first eight bytes of every content copy are read (`head -c 8 <copy> |
    od -c`): `SNAPCNT2` needs the 14.10 sync, `SNAPCNT3` can be read by 15.1;
  - the sync starts early enough to end well before the power-off.
- **Installing 14.10, then 15.1, beside the package.** Back up
  `/etc/snapraid.conf` and record its sha256; remove the distribution's
  package with `apt remove`, never `apt purge` (which would delete the config),
  or keep it and `apt-mark hold` it; keep exactly one SnapRAID binary, on one
  path, and record which. The reader refuses two binaries of different
  versions (§8.2).
- **During it:** `-E` (force-empty) only for the disk being taken out, and
  only for this sync. Seraphina only reads; her reader's SnapRAID reads wait
  while it runs (§8.1).
- **After it:** the new content fingerprint is recorded; every content copy is
  copied aside, root-only, before the first 15.1 sync, because 15.x writes a
  newer format (`SNAPCNT4`) once it has deallocated or rebuilding blocks, and
  refuses the oldest ones itself (source: 15.1 `cmdline/state.c:2575-2592,
  4195-4197`); which older versions can read `SNAPCNT4` was not checked, so
  the way back is the copy (Q4).
- **Who runs it:** the owner, or his operator, at the console. Seraphina's
  part is the checklist, the counts and the before-and-after facts.

### 8.10 Reading SnapRAID 13.0 correctly (kept from the first revision)

These rules exist because SnapRAID's exit codes do not say what a health check
needs; each is a unit test of P1 (T1 to T5, §13.2). They hold for the reader's
13.0 and, until 14.10 and 15.1 runs are recorded, only for 13.0 (§8.2):

1. **`status` exits 0 in every non-fatal state.** Bad blocks, an unfinished
   sync, an empty array and a data mount point that does not exist all exit 0
   (verified: lab `status-bad`, `status-int`, mount-point-removed run; source:
   13.0 `snapraid.c:1869-1874`). The panel parses the log; it never reads exit 0 as
   healthy, and it checks mounts itself, because `status` never touches the data
   disks.
2. **`diff` exit 2 means a sync is needed, even with zero counts.** After an
   interrupted sync, `diff` prints "No differences" and logs
   `summary:exit:equal`, yet exits 2 (verified: lab, `diff` after the SIGINT and
   SIGKILL syncs; source: 13.0 `scan.c:2021-2031`). The panel takes the exit code, or
   `status`'s `has_unsynced`, over the counts. snapraid-runner gets this wrong: it
   logged "No changes detected, no sync required" while `status` said "You have a
   sync in progress at 68%" (verified: lab runner-after-SIGINT run).
3. **SMART exit 0 does not prove SMART was read.** `smart`, `probe` and
   `devices` exit 0 with "Smart is unsupported in this platform." (verified: lab
   `smart`, `probe` on loop devices). A disk without SMART data is "unknown",
   never "healthy". USB bridges often need `smartctl NAME -d sat %s` in the
   config (docs: 13.0 `snapraid.txt:1369-1388`; untested on real disks, Q5), and the
   reader accepts only the `-d` types on its allow-list (DESIGN §41).
4. **A busy lock exits 1 like any error.** The text "is already in use" tells
   it apart (verified: lab concurrent runs during a throttled sync). The panel
   shows "busy" and keeps the last good facts.
5. **Read the log after the process exits.** `log_tag` does not flush each line
   (source: 13.0 `cmdline/support.c:149-233`), except the `run:pos` progress lines
   (first revision §3.9).
6. **Unescape fields.** In log fields `\d` is a colon, `\n` a newline, `\r` a
   carriage return and `\\` a backslash (source: 13.0 `support.c:459-495`). A
   multi-line fatal message can continue on a line with no tag prefix (verified:
   lab `diff-unmounted`); the parser attaches such a line to the message before
   it.
7. **Human output is for people only.** The per-file lines of human `diff`
   output carry no disk name and change order between runs (verified: lab
   `diff0`, `diff1`); the panel counts from the log's `scan:` tags.

---

## 9. Restores

### 9.1 Rules for every restore

- **Say what will happen before it happens.** Every step shows a sentence of
  what it will do, then one button (the first revision's rule).
- **Nothing is replaced.** The daemon's `undelete` brings back missing files
  only (§6.10); `heal` rewrites only blocks a scrub marked damaged (§6.11).
- **The date is the last sync.** Every screen names it: "as it was on Tuesday
  at 02:42, the last time the protection was brought up to date".
- **No run is started while a restore is open** by Seraphina: the card says
  "A restore is in progress" and offers no **Protect the latest changes** until
  the run ends. The daemon's own window may still fire; a restore approved
  late in the evening says so (judgement).

### 9.2 Files and folders: `undelete`

From the review page (§5.3) or from **Bring files back** on the card, which
opens the same list. Only files the last scan found missing can be chosen:
the protection can bring back nothing else (§1.2). The result page says
(judgement):

> 12 files are back as they were on Tuesday at 02:42. 0 could not be brought
> back. They belong to the administrator now and only the administrator can
> open them: if an app needs them, give them back to their owner.

Files come back owned by root with mode 0600 (verified: lab chown test;
daemon labs B and C). The first revision put owners and permissions back from
its own record, which the reader would have had to write; that is dropped
with the mutating helper. Until D19 is decided, the result page lists the
restored files' folders and shows, for each, a copy block the owner may run,
which sets owner and mode for the restored files of that folder only, from the
folder's own owner; Seraphina runs nothing.

### 9.3 Hidden damage: `heal`

**Repair it** on the red card (§4.2) opens the heal approval (§6.11). After
the run, the light returns to green only when `blocks_bad` is 0 and health has
left `corrupt` (§6.5).

### 9.4 A whole failed disk: the owner's checklist

The daemon has no action that rebuilds a disk (its `fix -d` would need
`/schedule`, which Seraphina never uses), so a rebuild is a checklist the owner
follows, with copy blocks where root is needed and a read-only check after
each step (P4; judgement unless cited). It keeps the first revision's
safety steps (its §6.3, verified end to end on a lab disk image there):

1. **Stop changes.** Skip the next window (§6.8), then stop the daemon's
   service only when no task runs (stopping it during a run kills the engine,
   §5.4). Stop the apps that write to the array: in Dockge, **Stop &
   Inactive** (`docker compose down`), not **Stop**, because a container with
   `restart: always` comes back at the next boot (source: Dockge
   `backend/stack.ts:419-421, 437-439`; docs: Docker's restart policies,
   **UNVERIFIED** here).
2. **Name the failed disk**, from the daemon's disk health and the reader's
   `disks` record (models and serials in the cockpit only).
3. **Make sure the server can boot without it**: every array line in
   `/etc/fstab` with `nofail` and `x-systemd.device-timeout=`; otherwise a copy
   block comments out the failed disk's line, with a dated backup,
   `findmnt --verify` and `systemctl daemon-reload` (L13).
4. **Fit and prepare the new disk** (`cannot_undo`: formatting erases it), with
   the commands built against `/dev/disk/by-id/…` and the serial typed, as
   the first revision's step 5 says.
5. **Rebuild**: the owner runs `snapraid -d <name> -l <log> fix` at the
   console, with the daemon stopped, then `snapraid -d <name> -a check`. The
   engine is the same 15.1 the daemon runs. The card shows the reader's facts
   only (the reader may read while the daemon is stopped, under §8.1's waits).
6. **What could not be brought back**: the `.unrecoverable` files and the
   files added after the last sync, with where to look instead (§9.5).
7. **Done**: start the daemon again, start the apps; the next window's sync
   records the new disk.

**Built (P4, 2026-10-26; DESIGN §41 says how).** The seven steps above, in
order, on the daemon's route and on the reader's, each with its plain words,
its blocks, its check and what the owner ticks; Seraphina sends nothing but
the skip of step 1, through its usual go-ahead, and runs none of the blocks.
Where the build goes beyond this text, on purpose (judgement): the disk is
chosen when the checklist is opened, and step 2 shows it; step 1 uses
`systemctl disable --now`, not `stop`, because step 4's restart would start the
daemon again; step 4's commands appear only after the serial number is typed,
mount the new disk where the old one was (SnapRAID's settings never change),
add its line with `nofail`, and give the reader its new filesystem UUID
through the install-explain-move block; step 5 runs in transient units
(`seraphina-disk-rebuild`, `seraphina-disk-check`, `RemainAfterExit=yes`,
`SendSIGKILL=no`) so it outlives the owner's terminal and its exit codes can be
read with `systemctl show`, with its output in the reader's root-only state
folder; "the card shows the reader's facts only" becomes the units' states,
because with the daemon's unit installed the reader runs only `info` and
`disks` (§8.1). While a checklist is open the card is red, no run is offered
or sent (§9.1), and the stopped daemon raises no `parity.down`. Not built: a
failed protection disk, a new disk at another folder, owners and permissions
put back (D19: a `chown` example only), and following the rebuild's progress
from the page.

### 9.5 What cannot be restored here, and where to go instead

| Cannot be restored by disk protection | Go instead to |
| ------------------------------------- | ------------- |
| a file created or changed after the last sync | restic snapshots, if they ran after the change (v2 §5.9); the app's own export |
| an older version than the last sync | restic snapshots |
| a disk or folder not in the array, excluded, or being taken out | restic |
| the root (system) disk | the OS-only snapshot of v2 §5.2, or a rebuild from the recovery kit |
| more failed disks than parity levels | restic, the off-site copy |
| files whose stripe changed on other disks after the sync (`.unrecoverable`) | restic, then delete the `.unrecoverable` file |

Until restic exists (phase B), the page says plainly when nothing else holds a
copy: "Nothing else on this machine has an older copy of this file."

---

## 10. Setup

### 10.1 Detect, read-only

With nothing installed, the harness can already tell (judgement; §11.6 lists
the states): whether a SnapRAID binary is on the host (as P1); whether a
daemon answers `/metrics` on loopback, and whether its login is on (an
unauthenticated `/snapraid/v1/state`: 401 or 200); which version the engine
and the daemon are (`snapraid_build_info`); and, with the reader, the rest of
§8.5. From `/metrics` alone, without the link, the card can show health,
when the last run was, a freeze and a run in progress; it says "To see more and
to ask for runs, set up Seraphina's link. [Set it up]".

### 10.2 Installing SnapRAID 15.1 and the daemon

The owner's step, after the transition (§8.9); Seraphina's setup page lists it
as a checklist and checks each result (judgement, on cited facts):

- From the author's own releases, with their published checksums checked (the
  daemon's repository has a `CHECKSUMS` file). Exactly one SnapRAID binary on
  the system (§8.2).
- **Installing the daemon's Debian package starts the service at once**, with
  the example config installed as `/etc/snapraidd.conf`, which sets a window at
  02:00, the API on, and no login (source: `pkg/postinst:4-15`,
  `makedeb.sh.in:48`; docs: `snapraidd.conf.example:121, 287, 318`). So: stop
  the service right after installing, write the config (§10.3), then start it.
- The daemon runs as root (source: `snapraidd.service.in:30`).
- `snapraidd -g USER:PASSWORD` makes the credential line, and puts the
  password on its own command line while it runs, where `ps` can see it
  (source: `daemon/daemon.c:21-35`): run it at the console, alone.

### 10.3 The daemon's settings Seraphina expects

The doctor checks each one (§12.7), from `/v2/config` through the link, or the
reader's summary (§8.5). Seraphina never writes them; a copy block shows the
lines and `sudo systemctl reload snapraidd`, which keeps a skip in place but
clears the login cache (verified: daemon labs A, B and C; source:
`daemon/conf.c:565, 1265, 1387-1388`).

| Setting | Expected | Why | Doctor |
| ------- | -------- | --- | ------ |
| `net_port` | `127.0.0.1:7627` (loopback only) | the API and the web page are root's controls; the default is loopback with IPv6 (source: `daemon/rest.c:3392-3420`) | error when it listens beyond loopback without a login, warn with one |
| `net_auth_credential` | set | without it every route is open to every program on the host (source: `daemon/rest.c:3176-3180`) | error |
| `net_config_full_access` | 0 (the default) | it opens the notify and hook commands to `PATCH` (docs: `snapraidd.conf.example:206, 294`) | warn |
| `maintenance_schedule` | one time, or empty while another job owns parity (§1.4); not inside the power-off, and the last sync's duration before it; not in the first half hour after the server wakes (below) | one scheduler; no run cut by the power-off | warn |
| `sync_threshold_deletes`, `sync_threshold_updates` | non-zero, as D3 decides | 0 turns a limit off | error at 0 |
| `sync_prevent_truncations` | 1 | without it a file cut to zero bytes is protected as empty (§5.1) | warn |
| `sys_shutdown_on` | empty (D16) | the server's own power schedule decides power | warn when set |
| `notify_result`, `notify_result_level` | the forwarder's line, `info` (§7.1) | every report reaches Seraphina, clean ones too | warn |
| `notify_syslog_level` | `error` (the code default; the example says `info`) | the daemon's own warnings and the engine's errors may carry file names into the system journal (source: `daemon/log.c:81-153`, `conf.c:1286`; Q8) | info |
| `touch_zero_subseconds` | as D8 decides | 1 rewrites sub-second file times on the data disks before every sync (docs: `snapraidd.conf.example:381`; source: `daemon/scheduler.c:84-104`) | info |
| `sync_prehash` | as D11 decides | | info |
| `scrub_percentage`, `scrub_older_than` | as D9 decides: all blocks checked within the target | the card's "checked within 30 days" rule | warn when the target cannot be met |
| `spindown_idle_minutes`, `probe_interval_minutes` | as D12 decides | the probe's standby check is unverified on real disks (Q5) | info |
| `check_updates` | 0 | the daemon would otherwise reach the internet | info |

**Choosing the window** (judgement, from one server's timers; observed):

- The daemon's shipped example sets `maintenance_schedule = 02:00`. On a server
  that powers off at night that window never comes: a window missed while the
  daemon was not running is not run later (source: `daemon/scheduler.c:522-583`),
  so nothing is protected and nothing says so until the card's age rules fire.
- The first half hour after the server wakes is crowded: every timer with
  `Persistent=true` that missed its time while the server was off runs at
  once (on one server: the package updates, the package database backup, a
  weekly filesystem check and a weekly TRIM). A window there competes with
  them for the disks; pick one at least an hour after the wake time.
- Times are per date in the server's own time zone: a window or a power-off
  time is a wall-clock time, so the day the clocks change it falls an hour
  from where it fell the day before in UTC (the harness's clock is tested
  across both changes: `tests/test_clock.py`). Check the window against the
  power-off and the backup job on both sides of a change.

### 10.4 Installing the link and the forwarder

The copy-block pattern of P1 (DESIGN §41): each block installs to a root-owned
`.new` path, checks that copy, and only then moves it into place; the harness
never runs sudo. The blocks, in order (judgement):

1. **Install `sf-snapraidd`** to `/usr/local/libexec/seraphina/sf-snapraidd.new`,
   check its sha256 against the release notes read on another device, move.
2. **Install its config** `/etc/seraphina/snapraidd.json` (`caller` the
   harness's user, `allow: ["read"]`, the daemon's URL, the hook URL and header
   file), then `sf-snapraidd explain`, which prints what it allows and every
   change from the installed one, then move.
3. **The credential**: the owner writes `user:password` into
   `/etc/seraphina/snapraidd-basic.cred` himself (root, 0600): `sudo -v` first
   (so sudo's own prompt comes alone), then `systemd-ask-password '…' | sudo
   install -m 0600 -o root -g root /dev/stdin <file>`. The prompt shows a star
   per character, so the password never reaches the terminal's scrollback, a
   multiplexer's log or a recording (a plain `install … /dev/stdin` echoes
   it); never on a command line and never through the cockpit. UNVERIFIED on
   the owner's host until lab L14.
4. **The units**: `seraphina-snapraidd.socket` and `seraphina-snapraidd@.service`
   to `/etc/systemd/system/`, `systemctl daemon-reload`, `systemctl enable --now
   seraphina-snapraidd.socket`.
5. **The hook**: the cockpit adds an event source `snapraidd` that may send
   `parity.report` only (`POST /api/notify/hooks`; DESIGN §34); its token is
   shown once; the block writes it into `/etc/seraphina/snapraidd-hook.header`
   (root, 0600) as an `Authorization` line, the way DESIGN §34's own snippets do.
6. **The daemon's lines** of §10.3, then `systemctl reload snapraidd`.
7. **Check**: the harness calls the link's `info` (expect `auth_on`, the
   engine 15 or newer, `allow: ["read"]`) and sends itself a test report through
   the forwarder (`sf-snapraidd notify test`), which must arrive.

**Allowing actions (P3)** is the same install-explain-move block for the
link's config, changing `allow` to `["read", "act"]`, and later adding
`"accept"`. A compromised harness user cannot widen it, and the owner reads
the change in the link's own words (the first revision's rule for the
reader's `allow`). **Removal** disables the socket, removes the units, the
program, its config, the credential and the hook's header file, deletes the
hook in the cockpit, and takes the `notify_result` line out of the daemon's
config; the daemon and SnapRAID are untouched.

### 10.5 What the owner must confirm

- That the protection service's login is his, and is not used by anything else
  that could be compromised (§11.2).
- One scheduler: which one, at each stage (§1.4).
- The limits (D3) and what an accept means (§5.3).
- "I understand disk protection is not a backup" (the sentences of §1.1).

---

## 11. Safety and privacy

### 11.1 What Seraphina can never do

- send any daemon call outside §3.2's table: no `/schedule`, no config
  change, no `/clear`, `/report`, `/refresh` or `/suspend_idle` (§6.12);
- send an action without the owner's approval, bound to what was shown, or
  send one twice after an unknown outcome (§6);
- hold or see the daemon's credential in the harness's process, its config,
  its data folder or its logs (§3.2);
- run a SnapRAID command that writes, or any SnapRAID command while the daemon
  is installed (§8.1);
- schedule SnapRAID (§1.4);
- keep a file name in its data folder, a notice, an event, a tracker item, the
  journal or a model's prompt by default (§11.3).

Each is a test (§13.2: T27, T28, T34, T35, T37, T48, T50).

### 11.2 Who else can change the protection data

If something else can write to the parity disk or a content copy, or can use
the daemon's login, protection can break without anyone noticing (judgement;
the first revision's §8.2 for shares):

- **A share that can write to the parity disk or a content copy**: a finding
  with the fix "make the share read-only, or remove it" (D15), as the first
  revision designed; P1 reads no share configuration yet.
- **An app with the parity disk mounted read-write**, such as a file-manager
  container: the reader's Dockge scan lists stacks that mount a parity or
  content path writable, as a finding (not built yet: DESIGN §41).
- **Whoever holds the daemon's login** can change the limits and the window,
  run any engine command through `/schedule`, and cancel the queue (§3.2). The
  login is the owner's and the link's only; the doctor reports a listener
  beyond loopback and a missing login (§10.3).
- **The daemon's web page** shows the same controls to anyone with the login;
  runs started there are runs Seraphina did not ask for (§11.5).
- **Any program on the host** may read `/metrics`, which needs no login; it
  holds counts and settings only (§3.1).

### 11.3 File names stay in the cockpit

File names can be personal. So (judgement unless cited):

- the daemon's change list, fix list, task messages and reports reach the
  cockpit only through the link's `review`, `fixes` and `run`, a page at a
  time; the harness passes them to the browser and keeps no copy;
- the forwarder throws the report's text away (§7.1);
- `<data_dir>/snapraid/` (0700, files 0600, in `sandbox_masks`) holds counts,
  ids, reason codes, approval records and the per-disk counts of a reviewed
  freeze, never a name (P1's rule);
- an undelete's approval record keeps the number of paths and their sha256,
  not the paths; the link's journal the same (§3.2);
- while the daemon runs an undelete, its `-f` filters are on the engine's
  command line, which any local user can read in `/proc/<pid>/cmdline` unless
  `/proc` has `hidepid` (reported: the owner's server does not); the approval
  says so;
- the daemon itself keeps names in its per-task logs and, at
  `notify_syslog_level = info`, may send some to the system journal (Q8;
  §10.3 recommends `error`);
- no file or folder name appears in an event, a notice, a tracker item's title
  or detail, an activity summary, the doctor's output or a model's prompt by
  default. The AI may explain a freeze from counts and reason codes; sending
  names to a model is the owner's choice per request, to the local model only,
  with the turn tainted `private` (D14);
- **while the agents' sandbox is off, an agent can connect to the link's socket
  and call the reader**, as the harness can; root-side storage cannot stop that.
  The doctor reports an error while disk protection is on and
  `harness.sandbox` is `none` (P1's check), and the link's socket path joins
  `sandbox_masks`, so sandboxed agents cannot reach it (D17).

### 11.4 What reaches a phone

§7.3's kinds, with fixed texts filled from typed counts. Nothing else.

### 11.5 Runs Seraphina did not ask for

- **A window**: expected; shown as "the nightly protection run".
- **A run of the daemon with no approval and not at the window's time** (the
  daemon's web page, another API client, the owner's job): shown in the run
  list as "started outside Seraphina", a note, not amber, because the daemon's
  limits applied (judgement).
- **A run with the limits off that Seraphina did not approve**: Seraphina
  cannot read the run's options without the daemon's root log, so she infers
  it: a complete sync whose scan was over a limit that did not change since
  (the scan's counts against the thresholds, both from before the run) means
  the limits were off. That is amber, the high `parity.released_outside`
  notice and a `snapraid:outside` finding (judgement).
- **A line in the link's journal with no approval**: the same (§3.2).
- **The engine run outside the daemon**: the reader's content fingerprint
  changes with no daemon task that writes the content file at that time
  (P1's mechanism, DESIGN §41): amber and a `snapraid:outside` finding.

### 11.6 When SnapRAID or the daemon is absent, or unsafe

| State | Seen by | The card says (judgement) | Light |
| ----- | ------- | ------------------------- | ----- |
| No SnapRAID, no daemon | P1's check, and `/metrics` not answering | "Disk protection is not set up. It is optional: it rebuilds a dead data disk from the others." (P1's sentence) | none |
| SnapRAID on the host, no reader, no daemon | P1 | "To show its health, Seraphina needs a small program that reads the disks for her." (P1's sentence) | none |
| SnapRAID as an app in a container (not built yet: DESIGN §41) | the inventory's Dockge scan | "SnapRAID runs as an app on this server; Seraphina only reads it." | amber |
| A daemon answering, no link | `/metrics` | the facts `/metrics` gives, and "To see more and to ask for runs, set up Seraphina's link." | from those facts |
| A daemon with no login | §2.4 | §4.2's red sentence | red |
| A daemon beyond loopback | `/proc/net/tcp` and `/proc/net/tcp6`, readable by any user, or the reader's summary | §4.2's sentence | red without a login, amber with one |
| A daemon not answering | `/metrics` | "The protection service is not answering. Nothing is being protected meanwhile." after `down_minutes` | amber, then `parity.down` |
| The engine too old for the daemon | `snapraid_build_info` | "The protection service cannot run this version of SnapRAID." | red |
| The reader refuses (`unsafe_install`, two binaries) | P1, §8.2 | names the file | grey (P1) |

### 11.7 The harness's own user on a shared server

The first revision let the harness run as the owner's login account. On a
server where that account is in the `docker` or `lxd` group, or may use sudo,
every agent running as it holds root whatever the sandbox does (judgement;
reported: the owner's login account is in both groups). So on a shared server
a dedicated system user for the harness, outside `docker`, `lxd` and `sudo`,
is a precondition of the daemon route and of the reader, and the doctor
reports an error when the harness's user is in one of those groups (D17).

---

## 12. Fit with the harness

### 12.1 Files

| File | New or changed | Phase | What |
| ---- | -------------- | ----- | ---- |
| `bin/sf-snapraid` | changed | P2a | the version policy, the layout fixes, the quiet rules, running through PID 1, `daemon_runs`, the daemon config summary in `info` (§8) |
| `bin/sf-snapraidd` | new | P2b | the link (`serve`) and the forwarder (`notify`), one standard-library file, never imported by the harness; tests load it by path (§3.2, §7.1) |
| `systemd/seraphina-snapraidd.socket`, `systemd/seraphina-snapraidd@.service` | new | P2b | the link's units (§3.2) |
| `swfactory/snapraidd.py` | new | P2b, P3 | the harness side of the daemon route: `MetricsReader` (the Prometheus text, strict names), `LinkClient` (one JSON line each way over the socket, through an injectable connector), the counters and the restart rule, the classification of runs and reports, approvals and their binding, the matching of §6.4, the outcomes |
| `swfactory/snapraid.py` | changed | P2a, P2b | the route choice (daemon, reader or none), the light's new rules and reasons, the card, the doctor checks, the lab list, the setup blocks for the link |
| `swfactory/notify.py`, `swfactory/rules.py` | changed | P2b | `parity.down`, `parity.lapsed`, the hook kind `parity.report`; `parity.unchecked` removed |
| `swfactory/server.py`, `swfactory/orchestrator.py`, `swfactory/__main__.py`, `swfactory/config.py`, `swfactory/settings.py`, `swfactory/doctor.py`, `swfactory/activity.py` | changed | P2b, P3 | §12.2 to §12.8 |
| `swfactory/web/views/backups.js` | changed | P2b, P3 | the daemon's states, the review page, the approvals, the run pages |
| `tests/mock_snapraidd.py` | new | P2b | a mock daemon built from `snapraidd.yaml` and the recorded exchanges (§12.6) |
| `tests/fixtures/snapraidd21/` | new | P2b | daemon lab C's exchanges and daemon lab B's reports, with every path replaced by a placeholder |
| `tests/fixtures/snapraid15/` and a 13.0 `v0` fixture | new | P2a | 15.1 runs for the reader; a package build that prints `v0`, with the package database's answers |
| `tests/test_snapraidd.py`, `tests/test_snapraidd_link.py`, `tests/test_snapraidd_lab.py` | new | P2b, P3 | §13.2 |
| `tests/gui_mock/backups.py`, `tests/gui/backups.mjs` | changed | P2b, P3, P4 | the new states and flows |
| `swfactory/snapraid_rebuild.py`, `tests/test_snapraid_rebuild.py` | new | P4 | the disk checklist of §9.4: its steps, blocks and read-only checks, the record, the light while it is open (built) |

### 12.2 Settings

Harness config, GUI-editable (judgement for every default):

```json
{
  "snapraid": {
    "enabled": false,
    "daemon": {
      "metrics_url": "http://127.0.0.1:7627/metrics",
      "link": "/run/seraphina-snapraidd/link.sock",
      "metrics_seconds": 60, "active_seconds": 10, "reads_min_seconds": 60,
      "full_read_minutes": 30, "down_minutes": 30, "approval_minutes": 15
    },
    "quiet_units": [], "quiet_margin_minutes": 30,
    "not_schedulers": []
  }
}
```

added to P1's `snapraid` keys. `metrics_url` must be `http` on `127.0.0.1` or
`[::1]`; `not_schedulers` holds, per place the owner marked, its id and the
sha256 of its contents (§1.4). The link's allow classes, the daemon's URL for
the link and the hook live in the link's root-owned config, never here.
`snapraid.schedule`, `snapraid.restore`, `snapraid.live_folders` and the
`maintenance` section leave, except `schedule.scrub.target_days`, which moves
to `snapraid.light.scrub_target_days` (§2.3).

### 12.3 Routes

All under the master token; none in `AGENT_TOKEN_ROUTES`; the GUI mock answers
with the same shapes:

| Method and path | Does |
| --------------- | ---- |
| `GET /api/snapraid` | P1's card, plus `route` (`daemon`, `metrics`, `reader` or `none`), `daemon {answering, auth_on, listen, engine, version, started_at, restarted_at, window {next, source}, hold_off, frozen {counts, limits, since}, active {command, progress, eta}, last_run {verdict, at}, schedulers}`, and `actions` (open approvals and recent outcomes) |
| `POST /api/snapraid/actions` `{action, …}` | a preview (§6.1): `{approval, shows, sends, binding, shown_sha256, expires_at}`. `action` is `stop`, `hold_off`, `maintenance`, `accept`, `undelete` (with `paths` by disk) or `heal` |
| `POST /api/snapraid/actions/<A-n>/approve` `{shown_sha256, typed?}` | sends through the link: `{outcome, status, run?}`; 409 `{error: "changed"}` when the binding no longer holds |
| `POST /api/snapraid/actions/<A-n>/cancel` | drops an unsent approval |
| `GET /api/snapraid/actions`, `GET /api/snapraid/actions/<A-n>` | approval records and their outcomes, runs and verdicts |
| `GET /api/snapraid/review` `?disk=&change=&q=&offset=` | one page of the last scan's change list, live from the daemon, with per-disk counts; never stored |
| `GET /api/snapraid/runs`, `GET /api/snapraid/runs/<number>` | the daemon's recent runs, and one task with its messages as text, live |
| `GET /api/snapraid/setup` | P1's blocks, plus the link's and the daemon's lines (§10.4) |
| `POST /api/hooks/event` with kind `parity.report` | the forwarder's event (§7.1), under its hook token, never the master token (DESIGN §34) |
| `GET /api/snapraid/rebuild`, `POST /api/snapraid/rebuild {disk}`, `POST /api/snapraid/rebuild/step {step, confirm?}`, `…/prepare {by_id, typed}`, `…/close` | P4 (built): the disk checklist of §9.4, its read-only checks, the new disk's blocks after its serial number is typed (never stored), closing it |

### 12.4 CLI

`factory snapraid` prints the card, with the daemon's facts. `factory snapraid
link install|check|remove` prints the link's copy blocks. `factory snapraid
rebuild` (P4, built) prints the disk checklist's steps and the current step's
blocks. There is no action
command: an approval must follow seeing what the cockpit shows (the first
revision's rule for a release, kept).

### 12.5 The GUI

The Backups area keeps P1's layout (DESIGN §41) and adds, on the daemon route
(judgement):

- on the **Disk protection** card: the window line with **Skip the next run**,
  the running run with its progress and **Stop safely**, **Protect the latest
  changes**, **Repair it** when damage is recorded, and the outcome of the last
  request in words (DESIGN §40's GUI notes: "succeeded", "failed", "outcome
  unknown: it may or may not have happened");
- **#/backups/review**: the frozen scan's list, the four ways out (§5.3), and
  **Bring files back** for the selected entries;
- **an approval dialog** for each action: what it shows (§6.1), what will be
  sent in words, the sentences of §5.3 for an accept and of §9.2 for an
  undelete, and one button;
- **#/backups/run/<number>**: one daemon task, its messages as text;
- **Who runs it**: the window, other schedulers found, and "does not start
  runs" for each place (§1.4);
- **Home** and **Needs you**: a freeze, an unknown outcome, a lapsed skip, two
  schedulers, no login and a failing disk;
- **#/backups/rebuild** (P4, built): the checklist of §9.4, its steps as cards,
  the current one with its blocks, check and ticks; **Replace a failed disk**
  on the card while a disk is missing or failing, and the open checklist's
  step on the card and in Needs you.

Novice words: the right-hand column of the words table. U7's list of terms
that never reach a novice screen gains `daemon`, `threshold`, `hold off`,
`hold-off`, `undelete`, `heal` and `engine`.

### 12.6 The mock backend and the mock daemon

`tests/mock_snapraidd.py` (judgement): a standard-library HTTP server that
answers the daemon's routes from the recorded exchanges, with a login, the
one-check-a-second limit and its 429, 401 bodies, the 409, 500 and 503 bodies,
a 204 activity, and scripted scenarios: a window, a freeze (deletes and
updates), an accept, a stop with both timings of §6.7, a skip used by a window,
an undelete, a heal, a restart (uptime back to 0, counters re-based, numbers
used again, `hold_off` off, the queue lost, a running task `signaled 9`), a
dead daemon (connection refused, reset after the request). It records every
request, so a test can assert that nothing outside §3.2's table was sent.
`tests/gui_mock/backups.py` gains the daemon's states: answering with no link,
no login, beyond loopback, not answering, frozen (with 3,000 entries on two
disks with hostile names), running, stopping, an unknown outcome, a lapsed
skip, two schedulers, a failed report delivery, the engine too old, and a disk
being taken out.

### 12.7 Doctor checks

P1's checks stay (DESIGN §41), with the version rows following §8.2. Added
(judgement for every level):

| Check | Level when it fails |
| ----- | ------------------- |
| the daemon answers `/metrics` on loopback | warn (info when no daemon is installed) |
| an unauthenticated `/snapraid/v1/state` is 401 | error |
| the daemon listens only on loopback | error without a login, warn with one |
| the engine is 15 or newer under the daemon | error |
| the link is installed, its files root-owned, its socket's group the harness's and mode 0660, and its `info` answers | warn |
| the link's socket is in `sandbox_masks` | warn |
| the link allows `act` while `harness.sandbox` is `none` (any agent then runs as the harness's user and can reach the link) | error |
| §10.3's daemon settings, each at its level | as §10.3 |
| one scheduler (§1.4) | warn |
| the window inside the power-off, or the last sync's duration not fitting before it | warn |
| a report task whose notification failed (`terminated` −1), or a link journal line with no approval | warn, error |
| the harness's user in `docker`, `lxd` or `sudo` | error |
| the harness unit's hardening hides the array from the reader (§8.4) | warn |
| the lab checks not run here (§13.2), always | warn |
| a disk checklist open (P4, built), with its step | warn |

### 12.8 Events

On the EventBus (DESIGN §10), with ids, counts and codes only:
`snapraid.updated` (P1's), `snapraid.daemon {answering, restarted, active}`,
`snapraid.action {approval, action, state, outcome}` and
`snapraid.run {approval?, high_command, verdict}`, and (P4)
`snapraid.rebuild {checklist, state, step, disk}`. `snapraid.guard` stays for
the reader's reports.

### 12.9 Phases

| Phase | Ships | Done when |
| ----- | ----- | --------- |
| **P1: the reader** (built) | DESIGN §41 | its tests (§13.2, the P1 rows) |
| **P2a: the reader on the owner's current host** | §8: the version policy with the `v0` package rule and the two-binary refusal; the layout fixes; the quiet rules; running through PID 1; the container and daemon detection by `/metrics`; the advice wording; the doctor and setup version lines; L9 and L10 out of the card's lab list | T50, T51, T52; L18; L19 |
| **P2b: reading the daemon** | the link with `read`; `/metrics`; the counters and restarts; the card and light; the freeze shown; the review page; the forwarder and the hook kind; notices and tracker items from reports; the run pages; the doctor's daemon checks; the mock daemon and the GUI states | T27 to T33, T36, T40, T44 to T49, T53; U9, U11 to U13; L14, L16 |
| **P3: the five actions** | approvals and their binding; `act`; outcomes, matching, verdicts; stop, skip, maintenance, accept, undelete, heal; the link's `act` and `accept` classes | L15 first (no action ships before it passes); then T34, T35, T37 to T39, T41 to T43; U10; L17 |
| **P4: the disk checklist** (built, 2026-10-26) | §9.4's steps and blocks | T54, U14 (built); L13; L20 (not run: a server started by systemd and a real protection service) |

The order follows the stages (§1.4): P2a matters now, while the host runs 13.0;
P2b can be built and tested against the mock daemon before the host upgrade,
and needs the real daemon only for L14 and L16; P3 waits for the upgrade and a
real-daemon lab (D21).

---

## 13. Decisions, tests and questions

### 13.1 Decisions for the owner

| # | Question | Options | Recommended |
| - | -------- | ------- | ----------- |
| D1 | The route for disk protection | (a) SnapRAID 15.1 and the SnapRAID Daemon 2.1 after the supervised 14.10 sync, Seraphina reading the daemon and asking it for runs; (b) SnapRAID 15.1 without the daemon, with the first revision's runner, guard and scheduler ported to 15.1; (c) stay on the package's 13.0, read-only | (a), which the owner chose |
| D2 | Who starts runs, at each stage | now: the owner's existing job, its SnapRAID step held until the transition; after: the daemon's window, and the job only skips the window around its backup; or the job keeps asking the daemon for a run after its backup (a second starter) | the first |
| D3 | The daemon's limits | deletes and updates both 100; deletes 100 and updates 500 (fewer freezes for a re-tagged library); stricter; and `sync_prevent_truncations = 1` in every case | both 100, revisited after a month of freezes: rewrites lose the protected version just as deletions do (§5.1) |
| D4 | Accepting a freeze | review, then an accept with no upper bound, typed count over ten times a limit; or only after bringing back what was deleted by mistake, with the next window skipped while the review is open | the first, with the second offered on the review page |
| D5 | A skip undone by a daemon restart | ask again; set it again by itself | ask again (§6.8) |
| D6 | The daemon's 500 "SnapRAID executable not found" and 503 "Daemon is terminating" on an action | unknown, then looked at (DESIGN §40's rule for 5xx); documented refusals, as the custody register's 503 is | unknown until the bodies are pinned by a test against the real daemon; the look of §6.4 settles each case meanwhile |
| D7 | Who holds the daemon's credential | the link, a separate unit; the harness's own unit, as a systemd credential like the drift sources' tokens | the link: the login has no roles, and an agent running as the harness's user could otherwise switch the freeze off (§3.2) |
| D8 | `touch_zero_subseconds` | 0; 1 (the example's), which rewrites sub-second file times on the data disks before every sync | 0, unless moved and copied files are often missed |
| D9 | How much of the disks the nightly check covers | `scrub_percentage` so that every block is checked within 30, 60 or 90 days at one window a day; `scrub_older_than` 10 | about 3.4% a night with 10 days, so every block within 30 days (the first revision's target) |
| D10 | Notification and journal levels | `notify_result_level = info` and `notify_syslog_level = error`; the example's `warning` and `info` | the first (§7.1, §11.3) |
| D11 | Pre-hash on sync (`sync_prehash`) | on (new data read twice, checked against memory errors before parity is written); off | off until a stop during hashing is measured on 15.1 (Q12) |
| D12 | The daemon's spin-down and periodic probe | off; on with its idle minutes | off until a real-disk check shows the probe leaves sleeping disks asleep (Q5) |
| D13 | Who may approve an action | you only, in the cockpit; also the operator persona | you only; she sees the items but cannot approve |
| D14 | May an AI see changed file names? | never; counts and reason codes by default, the list to the local model only when you ask each time; freely | the second |
| D15 | A share that can write to the parity disk | make it read-only; remove it | remove it |
| D16 | The daemon's emergency shutdown (`sys_shutdown_on`) | empty; on prefail or failing | empty: the server's own power schedule decides power |
| D17 | The agents' sandbox and the harness's user | sandbox on (`bwrap`) and a dedicated harness user outside `docker`, `lxd` and `sudo`; either alone | both (§11.3, §11.7) |
| D18 | A whole failed disk | the owner's checklist with the daemon stopped (§9.4); a later executor action | the checklist |
| D19 | Owners and permissions of restored files | a copy block per folder that sets the folder's owner on its restored files (§9.2); leave it to you; a later executor action that puts recorded values back | the copy block, until the executor exists |
| D20 | An undelete onto a disk that is not mounted, or whose devices are not resolved | refuse; allow | refuse, until Q3 is answered |
| D21 | Phase order | P2a now; P2b against the mock daemon before the upgrade; P3 after the real-daemon lab; P4 | as listed |
| D22 | Content copies | the first in a system folder (`/var/snapraid/` or `/var/lib/snapraid/`), one on each data disk (at its root or in `.snapraid/`), optionally one on the parity disk; only on data disks | the first |
| D23 | Live app data on the array | exclude it and back it up with restic; accept errors on files that change during a sync | exclude it |
| D24 | A disk whose health cannot be read | accept it per disk as "health not readable", kept amber; keep trying a `smartctl -d` type first | try first, then accept per disk |

The first revision's decisions map as follows: D1 became D1 and D2; D2
(Seraphina's thresholds) became D3 and a report (§5.5); D3, D4, D5 and D10
(her schedule, automatic sync, scrub plan and power-off holds) are dropped
with her scheduler, and D9 and the window take their place; D6 and D19
(restore target and folder mode) are dropped with the rename-aside restore;
D7 became D22; D8 became D23; D9 (notice priorities) stands in §7.3; D11 became
D21; D12 became D15; D13, D14 and D17 stand as D13, D14 and D24; D15 (install the
helper) stands for the reader (§8.7); D16 became D11; D18 became D18; D20
became D17.

### 13.2 Acceptance tests

"Unit" (T) means `unittest` against fakes and recorded runs; "GUI" (U) the mock
backend and the smoke suite; "lab" (L) a real program, as root where needed,
gated by an environment variable. A test passes only on what code records:
statuses, exit codes, counters, request logs and file hashes, never on the
words a program prints.

**Kept from P1** (built; DESIGN §41 says what each checks, and they keep
running): T1 to T9, T15, T16 (health), T21 to T23, T25, U1, U6 to U8, L1
(report), L3, L8. T6, T7 and T8 keep only their reader parts.

**Dropped with the mutating design, the gate and the scheduler** (the first
revision's text is in this file's history): T10 (stop of Seraphina's job), T11
and T12 as gates (their counts stay as P1's report tests), T13 and T14 (the
release), T17 (modes and toggles), T18 (the scrub planner), T19 (power holds),
T20 (rename-aside restore), T24 (the after-check), T26's skip state (its
sentences stay, with the window's time); U2 (the release page), U3, U4 and U5;
L2, L4, L5, L6, L7, L9, L10, L11 and L12.

**New** (P2a, P2b, P3, P4):

| # | Kind | Passes when |
| - | ---- | ----------- |
| T27 | unit | the link can build only §3.2's requests: a table test over every op asserts each method, path and query; `/schedule`, `/clear`, `/report`, `/refresh`, `/suspend_idle`, `/system`, `/v1/disks`, `/v1/config` and any `PATCH` cannot be produced from any request, and the mock daemon's request log after the whole suite holds nothing else |
| T28 | unit | the credential: the harness's process, config, data folder, events and logs never hold it (a canary credential in the link's test credential file is searched for everywhere afterwards); the link sends one constant header, never logs it, refuses a caller whose `SO_PEERCRED` uid is not the configured one, refuses an unsafe installation, and refuses a request over 16 KiB, not UTF-8, with `v` other than the integer 1, an unknown op or field, or a value out of range |
| T29 | unit | `/metrics` parsing: every recorded `/metrics` (labs A and C) reads into typed facts; unknown metrics are ignored; a malformed line fails that read only; health numbers map as §3.5 rule 9 |
| T30 | unit | reading: no link call while every counter stays the same; a moved counter reads only its endpoint; `disks` at most once a minute; `/metrics` every 60 s, every 10 s with an active task, never faster than 5 s; a 204 activity is "no latest task"; every link read names its limits |
| T31 | unit | an unauthenticated 200 on `/state` refuses every action with `auth_off` and turns the light red; a 401 lets them through |
| T32 | unit | engine `13.0`, `14.10`, `""` and a missing build line each refuse every action with `engine_too_old`; `15.1` passes |
| T33 | unit | schedulers: a window plus one inventory place is two, refusing `maintenance`, `heal` and `undelete` and allowing `stop` and `hold_off`; a place marked "does not start runs" is not counted until its contents' sha256 changes; the daemon's own unit is never a place; none is amber |
| T34 | unit | binding: for each action, each fact of §6.1's table changed between preview and send refuses `changed` and sends nothing (the mock's log); an approval older than 15 minutes, a second send of one approval, and a hash that does not match the stored preview are refused; a restart voids an unsent approval |
| T35 | unit | outcomes: 202 and 200 succeeded; each 4xx failed (refused); 500 and 503 unknown (D6); connection refused failed (never left); a reset after the request, an end of stream and a read timeout unknown; a 429 is sent once more after its second, with the binding checked again; nothing unknown is sent again; the intent is on disk before the POST (a crash injected between them leaves an unknown) |
| T36 | unit | matching: the run is found by `high_command`, numbers above the recorded highest and one `scheduled_at` near the POST; history out of number order and numbers used again after a restart do not fool it; a window and an API run of the same kind are told apart only by the approval |
| T37 | unit | after an unknown outcome: tasks found, recorded "succeeded (queued), seen afterwards"; none and no restart, "did not take effect"; a restart in between, unknown with the tracker item `outcomes.UNKNOWN_TITLE`; in no case a second POST |
| T38 | unit | stop: sent only while `active[0].number` is the approved number; an answer naming another number raises the alarm and sends nothing more; 409 "No task running" is "nothing to stop"; an unknown outcome reads that task only; both timings of §6.7 replayed from the fixtures |
| T39 | unit | skip: an unknown outcome reads `snapraid_config_hold_off`; off with a window passed since the send stays unknown; a restart with an approved skip raises `parity.lapsed` and a tracker item and sends nothing; `hold_off` is not in `outcomes.RETRIED` |
| T40 | unit | a freeze: `sync` exit 2 with `last_sync_at` unchanged reads "frozen", naming the limit from the counters; the cancelled scrub reads "not run"; three windows that freeze again on the same scan make one tracker item and one open notice incident; a complete sync closes both |
| T41 | unit | accept: offered only after the review was opened, with the typed count above ten times a limit (checked by the server); bound to the scan's identity; the counts per kind and per disk are saved with the approval, never names |
| T42 | unit and lab | undelete: only `removed` entries of the reviewed scan on their disk; the link's patterns are `/` plus the escaped path (`\ * ? [ ]`) and `/` for a folder, and against the real 15.1 (lab) a name with each of those characters selects exactly that file and a top-level name selects only the top-level file; empty, `.` and `..` components, NUL, a newline and over-long paths are refused before sending; `devices: []`, a `degraded` disk or the reader's mount check failing refuses; the verdict comes from `fix_recovered`, `fix_unrecoverable` and `fixes[]` against their values before the run, not from the exit code |
| T43 | unit | heal: offered only with bad blocks or `corrupt`; not offered while `degraded`, `prefail` or `failing`; the light turns green only with `blocks_bad` 0 and health no longer `corrupt` |
| T44 | unit | the forwarder: arguments outside the fixed sets exit 2 and send nothing; standard input is read to the end, at most 1 MiB, and never sent or kept (a hostile report's names are searched for in everything it sent); the token is read from the header file and is never on its command line; one POST with `Idempotency-Key`; 1 s to connect and 3 s in all; exit 0 on 2xx, else 1 |
| T45 | unit | a `parity.report` event makes no notice by itself; the panel reads the daemon and classifies by §7.2's table; the same report reached by a read and by the event is handled once; a report never forwarded is handled at the next read |
| T46 | unit | "protected up to" never moves on a stopped sync (`blocks_unsynced > 0`), which reads "stopped part way" |
| T47 | unit | a restart (from the uptime, confirmed by the counters or a start-up probe) makes queued and running approved runs unknown, an `exit_sig 9` unknown, and re-bases the counters; a clock step alone does neither |
| T48 | unit | privacy: with hostile names in every recorded list, no file name, disk path, serial or credential reaches `<data_dir>`, an event, a notice, a tracker item, the journal, the activity query or the doctor; `/v2/config`'s notify and hook commands never leave the link |
| T49 | unit | health: `devices: []` and `pending` past a day read unknown, never healthy; a health-gate cancel reads "did nothing"; `degraded` is red |
| T50 | unit | version policy: `snapraid v0` with the package database owning the binary at `13.0-*` reads as 13.0; the same with a binary the database does not own, or another version, is refused; two binaries of different versions are refused naming both; 14.10 reads `status`, `smart`, `probe` and `disks` with the note and refuses `guard` and `list`; 15.x without fixtures is refused; with the daemon installed every verb but `info` and `disks` answers `daemon_runs` and SnapRAID is never started |
| T51 | unit | layout: content copies in `/var/lib/snapraid/` and at a disk's root pass, two copies on one disk fail; a declared empty root-owned placeholder passes and reads amber, the same placeholder not empty fails; sibling mounts of one filesystem pass in both orders for a data and a parity disk; a bind of a subfolder fails |
| T52 | unit | quiet rules: no SnapRAID read while a `snapraid` process runs, a listed unit is active or its timer starts within the margin, or inside the power schedule's quiet periods; `diff` never starts from a timer |
| T53 | unit | the hook: `parity.report` is accepted only from a source whose scope names it, under its own token; a second post with the same `Idempotency-Key` changes nothing |
| T54 | unit | the disk checklist (P4): every step's check and its ways to fail, the ticks, the order; the fstab block changes only that folder's line (real `sed`); the new disk's blocks only for a free disk by its `/dev/disk/by-id` name after the typed serial number; the units' states; the record holds no model, serial or by-id name; red while open, no run offered or sent, the skip allowed, no `parity.down` once the owner turned the service off; the harness runs only `systemctl show` and asks the reader only `info` and `disks` |
| U9 | GUI | each daemon state of §12.6 draws the card with the right tone, reason and buttons; actions not allowed by the link are absent |
| U10 | GUI | every approval dialog shows what it will do and what was shown before its button; the accept dialog shows the unbounded-accept sentence and asks for the typed count when needed; Stop names the task; no Kill control exists |
| U11 | GUI | the review page pages through 3,000 entries, filters by disk and kind, shows hostile names as text, and shows the per-disk counts and the cut-list note |
| U12 | GUI | Home and Needs you show a freeze, an unknown outcome, a lapsed skip, two schedulers, no login and a failing disk |
| U13 | GUI | every new page works at phone width with no horizontal scroll |
| U14 | GUI | the disk checklist: opened from the card with the failed disk chosen, seven steps in order, each check holding its step until the mock's facts change, the new disk's commands only after the right serial number, the end; the card's and Home's lines; closing; the reader's route; 1440 and 390 px, light and dark |
| L13 | lab (systemd-booted) | kept for P4: a boot with the failed disk's fstab line commented out, and one with a `nofail` line for an absent disk, both reach `multi-user.target` with the network up |
| L14 | lab (systemd-booted) | the link's units: the socket's owner, group and mode; `DynamicUser` with `LoadCredential` lets the instance read its credential and nothing else can (the harness's user, an agent in its sandbox); a connection from another uid is refused; the harness under `NoNewPrivileges=yes` and `ProtectSystem=strict` connects; a sandboxed agent cannot reach the socket; nothing but systemd's start and stop lines reaches the journal |
| L15 | lab | against a real SnapRAID 15.1 and Daemon 2.1 on loop disks, through the link and the harness: reads leave every counter unchanged; a freeze, its review and an accept; an undelete and its verdict; a heal; a stop during a sync; a skip used by a window; a restart during a queued run. Must pass before P3 ships |
| L16 | lab | the forwarder under the real daemon: run as root with the daemon's cut environment, its command line logged with no secret, the harness's hook receiving one event per report, and a harness that is down giving "Notification failed" and a report handled at the next read |
| L17 | lab (systemd-booted) | a power-off during a daemon run with a preparation that stops the engine first: the engine saves and the next run carries on; and without it: the engine is killed, which the card then shows as "stopped without saving" |
| L18 | lab | the reader against a 13.0 package build that prints `v0`, with the content in `/var/lib/snapraid/`, a placeholder disk and sibling mounts: every read verb works and the config is accepted |
| L19 | lab (systemd-booted) | L8 repeated with `sudo-rs` and with `sudo.ws`, and from a hardened harness unit through PID 1 |
| L20 | lab | the disk checklist of §9.4 on loop images with 15.1: rebuild with the daemon stopped, `check -a`, and the next window records the new disk |

L8, L13, L14, L17 and L19 need a **systemd-booted lab**: a virtual machine or
a `systemd-nspawn --boot` container (the first revision's research container
could not run one).

### 13.3 Open questions

| # | Question |
| - | -------- |
| Q1 | DESIGN §40 and the daemon's two 5xx bodies: should "SnapRAID executable not found" (500) and "Daemon is terminating" (503) become documented refusals, as the custody register's 503 is? The source shows nothing is queued (D6). |
| Q2 | Is "review, then an unbounded accept" acceptable to the owner, or should an accept require bringing back unintended deletions first, with the window skipped while the review is open (D4)? The daemon has no "accept exactly these N" short of `/schedule`, which is forbidden. |
| Q3 | An undelete with no filters, or onto a data disk that is not mounted, on real hardware: does 15.1 refuse (a UUID mismatch reads `degraded`, which cancels `fix`), or write into the empty mount folder on the system disk? Untested in every lab; refused meanwhile (D20). |
| Q4 | The transition on the owner's array: are the content copies `SNAPCNT2` (the 14.10 sync is needed) or already `SNAPCNT3`; does a 14.10 sync rewrite the header there as in the lab; and since 15.x writes `SNAPCNT4` once it has deallocated or rebuilding blocks (source: 15.1 `cmdline/state.c:2575-2592, 4195-4197`), which older versions can then read it, so the owner knows when going back stops being possible? |
| Q5 | Real disks: SMART `prefail` and `failing`, whether the daemon's standby probe leaves sleeping disks asleep, the UUID-mismatch `degraded`, spin-down, and whether a clean maintenance reports `info` rather than `warning` on a host with udev (the labs used loop devices with no SMART). |
| Q6 | After a daemon crash, the engine may live on as an orphan and exit 0 later (seen with the fake engine only): does 15.1's content lock stop the restarted daemon's start-up probe and read from running beside it? |
| Q7 | How much do `/tasks` and `/array` cost on a realistic 120-day history with large change or fix lists? Only the bounds are known: 10,000 tasks, 5,000 messages per task, 100,000 files (source: `daemon/state.h:324-330`). |
| Q8 | At `notify_syslog_level = info`, which daemon lines carry file names into the system journal? |
| Q9 | How noisy are systemd's start and stop lines for the link's per-connection instances over a day, and is a long-running instance (`Accept=no`) better? |
| Q10 | Does the owner's power-off preparation stop the engine safely (SIGINT, then wait) before systemd stops the daemon's unit, which would otherwise kill the engine (§5.4)? Reported: it sends SIGINT to `snapraid` and waits up to ten minutes, never killing; whether that runs before the daemon's unit stops is not known. L17. |
| Q11 | `sudo-rs`: which of the reader's sudoers lines it accepts (`Defaults!… !use_pty`, `""`, `@includedir` skipping dotted names, `visudo -cf`), and a piped request with no terminal. L19. |
| Q12 | 15.1 with `sync_prehash = 1`: is the hashing lost when a sync is stopped during it, as 13.0's source suggests (first revision §5.4, L12)? |
| Q13 | The `'d1'msg:error: ` fragment inside the "UUID is unsupported" message, seen in both real-engine labs, and the six places where the daemon's documents and its source disagree (Appendix C): report them upstream? |
| Q14 | Untested setups: a reverse proxy in front of the daemon, its IPv6 loopback listener, and the start-up 204 window on `/activity`, which is from the source only. |
| Q15 | Kept from the first revision: Q5 (first-sync duration, now the transition's), Q7 (`fix -d` stopped and run again, now the checklist's), Q9 (which folders hold live app data, D23) and Q12 (which Dockge stacks use `restart: always` and mount the array, for the checklist). Answered since (reported): the Dockge stacks folder is the image's default; `/proc` has no `hidepid` (§11.3). |
| Q16 | The owner's choice: should the link's password being refused (the daemon's 401, after its password was changed) also reach the phone? Built: a tracker finding (`snapraid:service`, §7.4) and the link's calls held until the next full read; no notice kind exists for it, so the phone hears nothing (`/metrics` needs no password and keeps answering) and the card shows the link's words. A `parity.login` notice (normal priority, counts only) is the alternative. |
| Q17 | The owner's choice: the 30-minute forced power-off of `poweroff.target` (§5.4). Should the power-off preparation (and later the harness's holds, [SERAPHINA_OS_HARNESS.md](SERAPHINA_OS_HARNESS.md) §6.3) wait for a stopping run up to about 25 minutes, or should the window simply end an hour before the power-off so that nothing needs to wait? Built: nothing waits on Seraphina's side (the reader stops nothing, and its `stop_wait_minutes` is ignored). |

---

## Appendix A. Consistency with DESIGN.md, the v2 design and the other designs

| # | Where | What this design does differently, and why |
| - | ----- | ------------------------------------------ |
| A1 | v2 §3.2 (SnapRAID and its schedules belong to Cosmos) | The row should read "SnapRAID and its schedule: the SnapRAID Daemon, or before it the owner's existing job; Seraphina reads it, and asks it for runs after the owner's approval" (§1.4) |
| A2 | v2 §3.4 (native backend: "no pool management in v2") | Unchanged: Seraphina manages no pool. She asks the daemon for its five actions; she runs no SnapRAID command that writes |
| A3 | v2 §5.2 (engines) | A "Parity (SnapRAID)" row that is explicitly not a backup engine and never satisfies §5.3's second failure domain |
| A4 | v2 §5.3 (Backups card) | Unchanged and applied: parity alone keeps the card amber |
| A5 | v2 §6 (windows, modes, holds) | SnapRAID has no Seraphina window, mode or power hold: the daemon's window is its schedule (§1.4), and the owner's power-off preparation must stop the engine before the daemon's unit stops (§5.4). v2's windows should treat a daemon run as "a storage job running" when they decide whether to start |
| A6 | v2 §6.3 (holds registry) and the first revision's A6 | The first revision's `/run/seraphina/holds/` records for SnapRAID jobs are dropped with Seraphina's jobs |
| A7 | v2 §6.6 (`maintenance`, `services.<name>.maintenance`) and YOUR_SERVER A7 | P1's interim `maintenance` keys and `snapraid.schedule` leave (§12.2); YOUR_SERVER's move of `snapraid.schedule.*` into `services.snapraid-sync` and `services.snapraid-scrub` no longer applies |
| A8 | v2 §9.3 (approval on the safety page) | Before the executor, an approval is the cockpit's record, checked against the link's journal (§3.2, §6.1); with the executor, the five actions become executor actions, with an approval code where their class needs one |
| A9 | v2 §9.2 ("only the executor holds root") | The link holds no root (a dynamic user with one credential); the forwarder runs as root because the daemon runs notification commands as root unless told otherwise, and it is a fixed program that checks its arguments and makes one loopback call (§7.1); the reader stays the narrow root helper P1 built |
| A10 | DESIGN §34 (notices, event sources) | The hook kind `parity.report`, routed to the panel and never to the incident rules; the kinds `parity.down` and `parity.lapsed`; `parity.unchecked` removed (§7.3) |
| A11 | DESIGN §40 (outcomes) | Applied unchanged. Two audit rows when built, "SnapRAID daemon reads" (in `outcomes.RETRIED`) and "SnapRAID daemon actions" (never retried after an unknown outcome); the two 5xx bodies stay unknown until D6 |
| A12 | DESIGN §41 (P1) | P1 as built; its "P2" now means this revision's P2a to P4, and its references to this design use the new section numbers |
| A13 | `config.sandbox_masks` | Adds the link's socket; P1 added `<data_dir>/snapraid` |
| A14 | the catalog item snapraid-parity-health (`CATALOG.md:11531-11612`) | Kept: "last sync under 36 h" (amber) and "whole array scrubbed within 30 days" (the scrub target). Changed: the limits are the daemon's (D3), and `fix -e` runs only as the daemon's `heal` after the owner's approval |
| A15 | the first revision's A15 (`NoNewPrivileges` and sudo) | Applies to the reader only. The link needs no sudo, so the daemon route works from v2's hardened unit; the reader moves its verbs through PID 1 (§8.4) |
| A16 | YOUR_SERVER.md, which read the first revision at `7b7cd36` | Its references are to the first revision's sections. For it: the shared `HelperClient` stays for the reader; SnapRAID's power holds and its `snapraid-sync` and `snapraid-scrub` services are gone; its Check 5 may read the array disks' health from the daemon instead of the reader's daily SMART; the one-paste `setup` mode it proposed for both helpers stays proposed for the reader |
| A17 | the daemon research's recommendations | Two departures, both judgement: `/state` is not polled every 5 seconds, because `/metrics` carries the change counters and needs no login, while each link call costs a process (§3.3); with two schedulers, `stop` and `hold_off` stay possible, because they only hold work back (§2.4) |

Unchanged on purpose: no agent ever holds root (v2 §9.2); phone text is built by
code from typed fields (DESIGN §34); file names are data, never instructions.

---

## Appendix B. Sources

**SnapRAID Daemon v2.1** (commit `675e46eb`; the read-only clone used was at a
later commit that differs only in `CHECKSUMS`): `daemon/rest.c` (routes, login,
rate limit, metrics, answers), `runner.c` (queue, stop, health gate, runs,
reports), `scheduler.c` (maintenance, heal, undelete, windows, hold-off),
`parser.c` (log reading, replay after a restart), `elem.c` (tasks, health,
failure propagation), `state.h` (counters, limits), `conf.c` (defaults, reload,
login cache), `notify.c`, `report.c`, `log.c`, `daemon.c`, `os/unix.c`,
`civetweb/civetweb.c`, `snapraidd.service.in`, `pkg/postinst`, `makedeb.sh.in`;
documents `snapraidd.yaml` (OpenAPI), `doc/snapraidd.txt` (manual) and
`snapraidd.conf.example`. Read on 2026-10-25.

**SnapRAID v15.1** (commit `4054832c`): `cmdline/snapraid.c` (thresholds,
`--lock-options`), `scan.c` (truncation and empty-disk guards), `state.c`
(content formats), `elem.c` and `str.h` (filters and their matching),
`unixapp.c` (the standby probe). **SnapRAID v14.10**: content-format tests only
(daemon lab B). **SnapRAID 13.0**, built from the `v13.0` tag (commit
`744d5b4`), as the first revision cites it: `cmdline/snapraid.c`, `scan.c`,
`sync.c`, `scrub.c`, `check.c`, `status.c`, `list.c`, `device.c`, `state.c`,
`stream.c`, `support.c`, `util.c`, `unix.c`, `parity.c`, `handle.c`, and the
manual `snapraid.txt`.

**Runners**: snapraid-runner v0.5 (`8f78f9f`), snapraid-aio-script v3.4
(`8dbad5d`). **Dockge** 1.5.0 (`bac498f`) and master (`0329dab`):
`backend/stack.ts`, `backend/dockge-server.ts`, `compose.yaml`.

**The harness** at this branch: DESIGN.md §10, §11, §14, §16, §32, §34, §36,
§38, §40 and §41; `swfactory/config.py`, `server.py`, `notify.py`,
`outcomes.py`, `snapraid.py`, `bin/sf-snapraid`. SERAPHINA_OS_HARNESS.md §2.4,
§3.2-§3.4, §5, §6, §9.2, §9.3. YOUR_SERVER.md §6.1.

**The owner's server** (reported): the operator's capture of 25 October and
her later reports, kept in the owner's private notes.

**Documentation not fetched here (UNVERIFIED in this design):** systemd.socket
on `Accept=yes`, `SocketUser`, `SocketGroup` and `SocketMode`; systemd.exec on
`DynamicUser` with `LoadCredential` and `StateDirectory`, `IPAddressAllow`,
and the mount namespace a hardened unit gives the programs it runs;
systemd-run with `--pipe` from a hardened unit; `sudo-rs`'s support for the
reader's sudoers lines; Docker's restart policies; Python's documentation on
`-I` (well known, not re-read).

---

## Appendix C. The labs and the revision probes

### The daemon labs (2026-10-25)

Three research agents built and ran SnapRAID Daemon 2.1 in scratch folders on
loopback ports, never on the owner's server; a fourth reconciled their results
against the source and re-read every artifact it cites. Their artifacts hold
scratch paths and a lab-only credential, so they are not published; P2b's
fixtures are copies with every path replaced by a placeholder.

| Lab | Engine | Good for | Limits |
| --- | ------ | -------- | ------ |
| **A** | a compiled fake engine that records its arguments, sleeps, exits 0 and does not trap SIGTERM | the HTTP surface: every route, every refusal, the login, its cache and its rate limit, OPTIONS and `/metrics` without a login, restarts with the queue lost | never reaches `processing`; health stays `pending`; `blocksize` is 0, so `/array` has no `hold_off` |
| **B** | real SnapRAID 15.1 on loop-device ext4 disks; also 13.0, 14.10 and 15.1 content-format tests | behaviour: freezes, accepts, truncation, undelete, corrupt then heal, stop, restarts, the notification command's arguments, environment and reports in both formats | loop devices, no SMART, no `/dev/disk/by-uuid` |
| **C** | real SnapRAID 15.1 on partitioned loop images, sync slowed through a block-I/O cgroup | 217 recorded exchanges in 31 phases: first maintenance, during a sync, stop during a sync, a freeze on deletions and one on updates, accepts, undeletes, a skip used by a window, a scheduled run, the login, truncation, SMART and probe, heal, the periodic probe, a restart; plus 56 captured notifications and 38 per-task engine logs | the same; a whole-disk variant resolved no devices (§3.5 rule 3) |

The phases this design cites by name: `01-before-first-sync`, `03-first-maintenance`,
`03-during-sync`, `04-after-maintenance`, `05-stop-during-sync`,
`05-after-stop-during-sync`, `06-recovery-maintenance`, `07-threshold-abort`,
`07-after-threshold-abort`, `08-undelete`, `09-after-undelete-rest`,
`10-after-scheduled-window-skipped`, `11-after-scheduled-run`, `12-auth`,
`13-truncation-abort`, `13-after-truncation-abort`, `15-heal`,
`16-update-threshold`, `16-after-ignore-thresholds`, `17-periodic-probe`,
`18-after-restart` (lab C); `02-override`, `11-tasks-after-threshold`,
`13-array-after-threshold`, `22-tasks-after-stop`, `40-tasks-after-restart` and
the content-format tests (lab B).

**Where the daemon's documents and its source disagree** (the source wins;
Q13): the manual's maintenance `diff` step does not exist (docs:
`snapraidd.txt:1116-1117`; source: `daemon/scheduler.c:113-129`); "failing"
never means "a disk disappeared", which is `degraded` (docs:
`snapraidd.txt:263-266`; source: `daemon/elem.c:807-815`); the OpenAPI's
wording for a freeze (§5.2); `status` cannot be scheduled (docs:
`snapraidd.txt:1338`; source: `daemon/rest.c:1393-1412`); the example config
mentions an `/api/v1/sync` that does not exist (docs:
`snapraidd.conf.example:323-326`); `/stop` says "Stop signal scheduled"
though it signals only the current task (docs: `snapraidd.yaml:286-316`).

**Where the labs' results differed, and why** (each settled from the source):
a second stop reached the same task with the real engine and the next task
with the fake one (timing, §6.7); a stopped task reads `terminated` 1 with the
real engine and `signaled` 15 with the fake one (§6.7); task numbers carry on
after a restart when the last task left a log and are used again when it did
not (§3.4); `/array` has `hold_off` only when `blocksize` is not 0, while
`/metrics` always has it (§6.8).

### The SnapRAID 13.0 lab and the revision probes (kept from the first revision)

**The lab.** SnapRAID 13.0 was built from source into a scratch prefix (the
Ubuntu package in the research container is 12.3, so 13.0 has to come from
source or another build). Five loopback ext4 images were mounted as data disks
`d1`, `d2`, `d3`, `parity` and `2-parity`; content copies sat on a root-filesystem
folder and on `d1` and `d2`; `blocksize 64`, `autosave 1`. SnapRAID refuses
plain folders on one filesystem as disks ("Disks … are on the same device", and
too few content copies on different disks), so a test rig needs separate
filesystems (verified: lab fake-config run; source: `state.c:297-300, 440-470`).
The lab lives outside the repository, and its logs hold scratch paths, so they
are not published; T1's fixtures are copies with every path replaced by a
placeholder. The runs this design cites by name:

| Run | What it showed |
| --- | -------------- |
| `status1`, `status-bad`, `status-int` | status exits 0 with bad blocks, with a sync in progress, and with `has_bad`, `has_unsynced`, `info_time` tags |
| mount-point-removed | `status` exits 0, `diff`, `sync`, `check` and `scrub` exit 1 |
| `diff0`, `diff1` | human diff lines carry no disk name and change order |
| `diff-unmounted`, `sync-unmounted` | the empty-disk guard: diff exit 2 with the warning, sync exit 1 |
| `diff-partial` | 4 of 9 files deleted: diff exit 2, no guard |
| zero-size test | diff exit 2, sync exit 1, `fix -f` restores byte-exact |
| `sync1`, `sync-resume`, `sync-afterkill` | normal and resumed syncs, exit 0 |
| `sync-int`, `sync-term`, `sync-kill` | SIGINT and SIGTERM exit 130 after saving, with "Everything OK"; SIGKILL exits 137, and the next run is not locked out |
| `sync-live`, `sync-live3` | a file appended after opening goes unnoticed; files changed before sync reaches them give 46 file errors, exit 1 |
| `sync-live2` | a full data disk holding a content copy: exit 1, "No space left on device", a zero-byte `.tmp` left and cleaned up next time |
| `sync-lock`, concurrent runs | a second command exits 1 "already in use"; `smart`, `probe`, `devices` run beside a sync |
| `scrub-default`, `scrub-new` | the default plan does nothing on a fresh array; `-p new` checks it |
| `scrub-corrupt`, silent-corruption test | same-size, same-time corruption is invisible to diff and found by scrub (exit 1) and `check -a` |
| `scrub-int` | an interrupted scrub keeps its progress |
| `check-a`, `check-e`, `check-m` | per-file `status:` lines; exit 1 on any error |
| `fix-e`, `scrub-bad` | `fix -e` repairs byte-exact; the bad mark stays until `scrub -p bad` |
| `fix-m`, chown test | undelete; the file comes back root:root 0600 with its time |
| `fix-edit` | an edit made after the sync is reverted, exit 0 |
| `fix-zero` | a truncated file restored |
| `fix-d1`, disk replacement | a disk rebuilt onto a new filesystem; one `.unrecoverable` file with exit 1 when two other disks changed on its stripe; a file added after the sync is gone; the next sync logs the UUID change and needs no `-U` |
| stale-copy and corrupt-copy tests | an older first copy is used silently; a corrupt first copy aborts with no fallback |
| non-root runs | the lock and content permissions stop a non-root user; group read is lost after each root sync |
| option-misuse loop | which misused options exit 1 and which are accepted silently |
| fake-device `smart`, `smart`, `probe`, `devices` | the FAIL row exits 1; loop devices report "unsupported" with exit 0 |
| `list` | `file:DISK:PATH:SIZE:MTIME:NSEC:INODE` lines |
| `--gui` sync | `run:begin`, `run:pos`, `run:end` lines |
| runner-after-SIGINT | snapraid-runner skips the needed sync after an interrupted one and reports success |

**PR1, an ACL round trip in the standard library** (2026-10-25, ext4, as root in
the research container): `setfacl -m u:nobody:rw` on one file; `os.listxattr`
showed `system.posix_acl_access`; `os.getxattr` read it, `os.setxattr` wrote it to
a second file, and `getfacl` showed the same entries on both.

**PR2, sudo with a piped request** (2026-10-25, sudo 1.9.15p5 with `Defaults
use_pty` in `/etc/sudoers`, as root): a request piped into `sudo -n -- python3 -I
…` arrived intact on standard input, and the program saw standard input and
output as pipes, not terminals.

**PR3, refused.** A probe of the `""` sudoers rule as a non-root user needed a
throwaway user and a sudoers grant in the research container. The permission was
refused and the probe was not run; L8 covers it. A temporary file installed for
the probe was removed.

**PR4, `-f` patterns and backslashes** (2026-10-25, glibc 2.39, in the research
container): glibc's `fnmatch` called through Python's `ctypes` with
`FNM_PATHNAME`, the flags SnapRAID's path filters use on Linux (source:
`elem.c:184`, `portable.h:288`). The pattern `a\b.txt` matched `ab.txt` and not
`a\b.txt`; with every `\`, `*`, `?` and `[` escaped by a backslash, `a\\b.txt`,
`x\*y.txt`, `q\?.txt` and `\[a].txt` each matched only the literal name, and
`dir/*` did not match `dir/sub/f`. The SnapRAID build uses the system's
`fnmatch` (`config.h` defines `HAVE_FNMATCH`, and `cmdline/fnmatch.c` compiles
only without it). SnapRAID itself was not run with such a pattern (the first
revision's Q6). 15.1 matches filters with its own function, so this result
does not carry over to the daemon's undelete (§6.10, T42).

**What the research container could not run.** Its PID 1 is not systemd
(`ps -p 1` prints `process_api`), so transient units, their stop at shutdown,
sudo as a non-root user and booting were not exercised: L8, L9, L10 and L13 need
a systemd-booted lab (§13.2; L9 and L10 are dropped). The lab's `sync-int`
and `sync-resume` runs used a plain sync without `-h`
(`-c CONF -l LOG --bw-limit 1M sync`), so they say nothing about a stop
during the pre-hash phase (L12, dropped; Q12 asks the same of 15.1).

---

## Review log

### Build of P4 (2026-10-26)

Built as §9.4 says (its "Built" paragraph and DESIGN.md §41 list the choices
made on purpose), with T54 and U14. L13 and L20 were not run: both need a
server started by systemd and L20 a real SnapRAID 15.1 and daemon on test
disks; the card, the checklist page and the doctor list them as not checked
yet. Found while building: §9.1's "no run while a restore is open" applies to
the checklist too, so the daemon route refuses run-starting actions while one
is open (`rebuild`), and the "not answering" alarm is held back once the
owner turned the service off for it.

### Review of the build (2026-10-25)

Eighteen findings on the build, all against the mock and the daemon's source;
each change is in the section named:

- §40 kept: a link failure after its POST is unknown, never failed, and its
  journal line after the POST is best effort (§6.3); a restart relabels only
  runs the daemon took or whose answer never came, never a recorded refusal
  (§3.4); a skip turned off again never lapses, and a lapse is told once
  (§3.4); a look after no answer from the link waits until it can no longer
  send (§6.4 rule 4); a stop's look reads the daemon's "Stop requested", not
  the task's status (§6.7 rule 3).
- The link enforces the binding and the one send itself (§6.1), refuses an
  undelete across two disks (§6.10), and its fixed-message list is copied from
  the daemon's source (§3.6).
- A stopped last step is "stopped safely" (§6.7 rule 5); a failed
  notification is read from the report's −1 (§7.1); runs belong to an
  approval only with the same `scheduled_at` (§3.4); only taken run requests
  hold the poller at the active rate (§3.3).
- The reader's `info` on the daemon route hashes nothing and opens no copy off
  the system disk (§8.5); its version words are honest for 15.x without a
  daemon and for versions it does not read (§8.2).
- Words: a limit of 0 reads "no limit is set", never "any"; the password
  prompt does not echo (§10.4); the doctor's error for `act` without the
  sandbox (§12.7).

### Build of P2a, P2b and P3 (2026-10-25)

Built as specified above, tested against `tests/mock_snapraidd.py` (DESIGN.md
§41 lists the files and what was checked where). Where the build differs from
this text, on purpose:

- **The daemon's login** is read by the link on every call from the systemd
  credential its settings name (`credential`, default `basic`) or, as an
  alternative for hosts without systemd credentials, from the root-only file
  `credential_file` (only `/etc/seraphina/snapraidd-basic.cred`); never by the
  harness (D7 unchanged).
- **The binding**: a heal and an undelete are not bound to "nothing of that
  kind queued" (§6.1's table does not list it): a duplicate gets the daemon's
  409, which is harmless (§6.4 rule 5). A folder chosen for an undelete must
  still hold at least one `removed` entry of the scan.
- **Words**: "Fix the disk first" became "See to the disk first", because
  "fix" is on the words table's list of terms a novice never sees (U7).
- **Runs** are grouped by `high_command` and `scheduled_at` and end at their
  report, so two runs queued in the same second never merge.
- **Restarts** without the link (only `/metrics`) are confirmed by the config
  counter going back; a start-up check in the task list confirms one when the
  link reads it.
- **Not built**: P4; the reader's reading of 15.x without the daemon (it waits
  for recorded 15.1 runs, §8.2) and its `smartctl … [info: …]` lines; a
  button for the owner's "does not start runs" marks (a setting,
  `snapraid.not_schedulers`, for now); the writable share check (§11.2).

### Revision 2 (2026-10-25): daemon-first

Written after the owner chose SnapRAID on the host, run by the SnapRAID Daemon,
and after the operator's capture of his server and her later reports. Each
line is one change from the first revision and its reason. The audit's and any
critic's findings on this revision should be added below as further lines.

| # | Change | Why |
| - | ------ | --- |
| N1 | Seraphina is no longer SnapRAID's runner or scheduler: the daemon's window, or the owner's job until the daemon is in, starts every run, and Seraphina counts schedulers and refuses run-starting actions when there are two (§1.4, §2.4) | the owner's choice; two starters on one content file fail or worse |
| N2 | The gate is the daemon's Safety Freeze; Seraphina's guard is a report (the transition's count and the review page's per-disk lines); the card says what the freeze does not cover (§5) | the engine checks its own scan before writing, which closes the first revision's gap between count and sync; Seraphina cannot add a per-disk gate without a second runner |
| N3 | The daemon's credential lives in the link, a separate unit with a systemd credential, never in the harness (§3.2) | the login has no roles: whoever holds it can change the limits and pass raw options through `/schedule` |
| N4 | Polling reads `/metrics` without the credential, and the rest only when a change counter moved (§3.3) | every GET is side-effect free, but each link call costs a process; `/metrics` carries the counters |
| N5 | Restarts are detected from the uptime and handled as unknown outcomes and lapsed skips (§3.4, §6.6) | a restart loses the queue, the skip and the truth about a running task |
| N6 | Five actions, each after the owner's approval bound to what was shown, matched to their run, never replayed after an unknown outcome (§6) | DESIGN §40, and the daemon's answers carry no run id |
| N7 | Reports reach Seraphina through a root-run forwarder and the hook API, with decisions taken from the REST API, never from the report's text (§7) | the report is untrusted text with file names, and the daemon waits for its notification command with no time limit |
| N8 | The reader stays as a read-only fallback with a version policy and the layout fixes the owner's host needs; with the daemon installed it runs no SnapRAID command (§8) | the host runs a 13.0 package that prints `v0`, with its content under `/var/lib/snapraid/` and a disk being taken out (reported) |
| N9 | Restores are the daemon's `undelete` and `heal`; the rename-aside wizard, its no-follow walks and the metadata record are dropped; owners and permissions after a restore are D19 (§9) | `undelete` never replaces a file, and a mutating root helper is no longer wanted |
| N10 | A whole-disk rebuild is the owner's checklist with the daemon stopped (§9.4) | the daemon has no rebuild action, and `/schedule` is never used |
| N11 | A dedicated harness user outside `docker`, `lxd` and `sudo` on a shared server (§11.7) | otherwise every agent holds root whatever the sandbox does |
| N12 | `sudo-rs` named, and L19 added (§8.4) | the target release ships it as `sudo` |
| N13 | The power-off rule: the engine must be stopped safely before the daemon's unit stops (§5.4) | stopping the daemon during a run kills the engine |
| N14 | Undelete patterns follow 15.1's own matcher, escaping `]` too; PR4's 13.0 result does not carry over (§6.10, T42) | 15.1 does not use the system's `fnmatch` |
| N15 | Section numbers changed; the code's and DESIGN §41's references were updated in the same commit; YOUR_SERVER.md keeps its pinned references to the first revision (Appendix A16) | daemon-first order |

### First revision (2026-10-25)

Two passes, before the system audit: one by the author (V1-V20), then one by
critics (R1-R36). Section numbers in these lines are the first revision's, and
many of the parts they fixed were dropped in revision 2 (§2.3). "Fixed" or
"Accepted" means the first revision's text carried the change.

| # | Issue | Decision |
| - | ----- | -------- |
| V1 | The guard was first drawn on the harness side, where anything running as the harness's user could weaken the thresholds | Fixed: the guard and its thresholds live in the root helper and its root-owned config (§4.5, §3.10) |
| V2 | "Hold" meant both a held sync and v2's power hold | Fixed: "held sync" and "power hold" are separate terms (words table) |
| V3 | A restore "to a separate folder" is not something `fix` can do, since it writes the original path | Fixed: set aside, `fix -m`, then move (§6.2); the separate folder is on the same disk |
| V4 | `fix -f` on an existing file silently reverts later edits | Fixed: every restore renames the current version aside and uses `-m` (§6.1) |
| V5 | A job started through sudo from the harness would die with the harness's user service, or be killed after its stop timeout | Fixed: long jobs run in a transient root unit with `SendSIGKILL=no` (§3.6) |
| V6 | The harness importing the helper's code would let the harness user's checkout inject code into a root program | Fixed: the helper is one standalone file installed by copy and never imported at run time; its hash is checked on the installed copy, with what that proves stated (§3.10, §9.1; R11) |
| V7 | `NOEXEC` in the sudoers line would stop the helper from starting SnapRAID | Fixed: not used; the reason is stated (§3.10) |
| V8 | File names would have reached tracker items, events and notices | Fixed: counts and ids only, the list stays in 0600 files masked from agents (§8.3) |
| V9 | A release cannot prove the owner pressed the button before the executor exists | Not fixable before v2; stated plainly and recorded in three places (§4.5) |
| V10 | Syncs reading data disks writable is more than they need | Fixed where the content layout allows: `ProtectSystem=strict` with content copies in `.snapraid/` folders (§3.6, D7) |
| V11 | The light could show green on facts hours old | Fixed: grey after `stale_minutes`, never green on stale facts (§2.2) |
| V12 | The default scrub plan does nothing on a fresh array | Fixed: plans are computed from a target, with `-p new` after syncs (§5.5) |
| V13 | The first threshold rule let a nearly emptied small disk (8 of 9 files removed) pass under the floor of 10 | Fixed: a third clause holds when half a disk's files are removed or rewritten (§4.3, T11) |
| V14 | The light's red had no tone on Home, whose card `load()` takes no error tone | Fixed then, superseded by R35: the Home card does accept `err`, and now uses it (§2.2) |
| V15 | The sync was first classed `undo_instantly`, which hides that it makes parity forget deletions | Fixed: no class fits; the cell says why the guard, not a code, protects it (§3.2) |
| V16 | Read verbs were given only the state directory as writable, but SnapRAID opens its lock file for writing | Fixed: the lock file is writable for every verb that takes the lock (§3.6) |
| V17 | "Rename aside, then `fix -m`" read as verified, but the lab ran only plain undeletes | Fixed: labelled judgement, with T20 and L6 to check it (§6.1) |
| V18 | The replacement-disk rule said the new disk must be no larger than parity covers, which is not what the manual says | Fixed: a larger disk is fine; parity protects only as much as the parity disk holds (§6.3) |
| V19 | After a disk rebuild, the guard's own mount check would have held the first sync on the new filesystem UUID | Fixed: the disk checklist stages the helper config with the new UUID (§6.3 step 6) |
| V20 | The guard checks and the GUI tests both used the ids G1 and up | Fixed: GUI tests are U1-U8 (§10.2, §9.9) |

Totals for the author's pass: 20 issues, 19 fixed, 1 stated as a known limit until v2 (V9).

**Critic round** (36 findings, ranked by the critics; one line each):

| # | Finding | Decision |
| - | ------- | -------- |
| R1 | high: restore steps ran as root through symbolic links; `os.chmod` and `os.setxattr` follow links, and SnapRAID's `fix` follows links in parent folders | Accepted: every array path is walked with `dir_fd` and `O_NOFOLLOW`, metadata is set through the entry's own descriptor, a link gets only its owner (§3.3, §6.4); SnapRAID's restore unit can write only the target disk without its `.snapraid/`, the lock and its own log folder, never parity or the state directory; the run mode rebuilds argv from the verb table; the separate folder is made through the same walk; a no-follow verify follows the fix (§3.6, §6.2); T20 and L6 cases |
| R2 | high: configs the harness stages could run commands as root (`smartctl` lines reach `popen`) or point writes anywhere, and only their owner was checked | Accepted: the helper validates `CONF`'s contents on every call, with a `smartctl` allow-list and rules for parity, content and data paths and keywords (§3.4 item 7); binary, config paths, state and holds folders are in its code; `explain` prints every change in the helper's own words before a config is moved into place (§3.10); the doctor re-validates; T8 cases |
| R3 | high: the guard counts one diff, but the sync protects its own later scan | Accepted in part: the window is stated (§0 item 4, §4.1); the diff is the last check, immediately before the sync in the same job; the after-check compares the sync's own scan with the guarded list and raises a red light, a high notice and a tracker item (§4.2); `--force-empty` only for one named disk, re-checked just before the sync; T24. Rejected: keeping the pre-sync content file, because once a sync has recomputed a stripe's parity the old content file cannot bring the data back; the previous index is kept instead, so the list can still name what was lost |
| R4 | medium: after an interrupted sync the diff shows zero, so an out-of-band partial sync passes | Accepted: G8 `unguarded_partial_sync` compares a fresh `list` with the last good index under the same thresholds; the wrong sentence is replaced (§4.2); T12 case |
| R5 | medium: a release can be forged straight through sudo, its options were not tied to the hold, and the records were never reconciled | Accepted: every verb has an allow class (§3.2); a release only for the latest held guard, with options derived from its reasons (§4.6); `info` returns the last `releases.jsonl` lines and the harness and doctor flag any with no cockpit release; the server checks `typed`; §0 item 4 and §4.5 say what is and is not proved; `repair.bad` is a stated exception to the ticket rule (§3.3) |
| R6 | medium: file names were readable by agents under the shipped `sandbox: none`, mirrored into `<data_dir>`, and could reach the journal | Accepted: names stay root-side and reach the cockpit a page at a time; no copies in `<data_dir>`; no journal output, and `-q -q -q` for diff and list (§3.6, §8.3); the doctor reports an error and the card warns while the sandbox is off (D20); the `/proc/<pid>/cmdline` exposure is stated (Q11); T21. The design now also says root-side storage alone cannot stop an unsandboxed agent that calls the helper |
| R7 | medium: `-f` patterns as specified matched names at any depth, and a backslash was an unrefused escape | Accepted: patterns are `/` + escaped path, with a final `/` for folders; `\`, `*`, `?` and `[` are escaped, so such names are restored directly; dot-only components are refused (§3.4 item 5); Q6 answered by PR4; T6 cases |
| R8 | medium: during a disk rebuild, containers could write to the replacement disk, and `fix -d` reverts by path | Accepted: the checklist asks for Dockge's **Stop & Inactive** (`compose down`) and says why `restart: always` makes **Stop** unsafe; before the rebuild and each resume the helper checks open files, working directories, foreign mount namespaces and files newer than the ticket (§6.1, §6.3 step 1); L7 variants. The suggested `noauto` line is replaced by the fstab step of R17 |
| R9 | medium: id fields were not validated, so `log` and `job` could read root-only files | Accepted: fixed id shapes, records opened inside their folder with `dir_fd` and `O_NOFOLLOW`, an id without a record refused (§3.4 item 2); T6 cases |
| R10 | medium: no array-wide clause, and rewrites had looser limits than removals | Accepted: removals and rewrites share one count of files that would be lost, per disk and across the array (100, or 10 that are 2%); the ransomware sentence covers encryption in place (§4.3, D2); T11's three-disk case |
| R11 | low: the install block hashed a user-writable staged copy and then installed from it | Accepted: every block installs to a root-owned `.new` path, checks or explains that copy, then moves it; the same for the sudoers file and the helper's config (§3.10) |
| R12 | low: `ProtectSystem=strict` made `/run` read-only for the hold record and the job lock | Accepted: the supervisor unit has `/run/seraphina/holds` and `/run/seraphina-snapraid` writable, created before it starts (§3.6, §3.7, §5.4); T19 and L9 assert both |
| R13 | low: the scheduler inventory missed common places, and outside runs were found only by inference | Accepted: a content-based inventory over system and user units, every cron location, anacron, user crontabs and Dockge stacks (§1.4); the first content file's fingerprint after each job flags any outside run at the next refresh (§8.5); T25 |
| R14 | low: a restore to the separate folder was said to be protected, but the default excludes cover that folder | Accepted: the exclude stays; the separate-folder screen, the report and the setup wizard say those copies are not protected until moved out (§6.2, §7.2); U4 |
| R15 | low: "status never spins up a sleeping disk" contradicted the source, which stats every content copy | Accepted: reworded as judgement with the source line, and Q3 measures it (§2.4) |
| R16 | high: `-h` loses all hashing when stopped, so a large first sync might never finish under the nightly power-off | Accepted: never `-h` on the first sync; `-h` only when the whole sync, read twice, fits before the next stop; the estimator and G2 count the second read (§5.4, D16); §7.4 relabelled to plain sync; L12 |
| R17 | high: the disk replacement checklist could leave a headless, Tailscale-only server unable to boot | Accepted: a boot-safety step before the shutdown checks `nofail` and `x-systemd.device-timeout=`, comments out the failed disk's line with a backup, `findmnt --verify` and `daemon-reload`; the new line replaces it (§6.3 steps 3 and 5); the setup wizard and the doctor check every array disk (§7.2, §9.7); L13 |
| R18 | high: the phase order put the guard and scrub before their remedies, and the first sync between phases | Accepted: P2 now ships the guarded sync with the first sync, scrub, the file restore and `repair.bad` together; P3 is the disk rebuild; buttons appear only when their verbs are allowed; P1 says it protects nothing (§9.9, §0 item 11, D11) |
| R19 | high: under `NoNewPrivileges` sudo cannot gain root, and the shipped system template and v2's unit set it | Accepted: the constraint is stated (§3.1); the doctor names `NoNewPrivs` as the cause (§9.7); A15 orders v2's hardened unit after the executor or a socket-activated path; L8 |
| R20 | medium: the transient units' writable paths left out the `/run` files | Accepted: the same change as R12 |
| R21 | medium: items opened through `Factory.system_item` could not be closed by code | Accepted: the panel closes them as the factory with `receipt` evidence, as the drift sources do, under stable keys such as `snapraid:held` (§4.7); T16 |
| R22 | medium: no verb for root-only reads the wizards need, including naming a dead disk | Accepted: `disks` keeps each disk's last known identity and runs `lsblk` and `blkid -p`; `stat` serves the restore preview (§3.2); G11 uses each disk's used-space growth instead of per-file sizes |
| R23 | medium: a whole-disk rebuild had no progress record and stopped every night | Accepted: a resume passes `-S` from the last logged block; **Keep the server on until it finishes** is offered with the estimate against the time left (§6.3 step 7, D18); Q7 and L7 measure it |
| R24 | medium: a folder restore mixed old and new files | Accepted: "exactly as it was" moves the whole folder aside, by default where a Dockge stack mounts it (D19); the app check is enforced before anything is renamed (§6.1, §6.2 step 2) |
| R25 | medium: a novice was never told that deletions become permanent at the next run | Accepted: the card's fixed line names the next run's time with **Skip the next run**; the held text says new files are not protected yet; §1.1 is three short sentences (§1.1, §2.1, §4.4); T26 |
| R26 | medium: the installer's hash check guarded against nothing, with a check-to-install window | Accepted: the same change as R11, and the block and the doctor say plainly that the hash catches a swap only against the release notes opened elsewhere (§3.10, §9.7) |
| R27 | medium: the lab described could not run the job-runner tests | Accepted: P1's verbs run directly under sudo with no unit; L8, L9, L10 and L13 need a systemd-booted lab, named in §10.2; L9 must pass before P2 ships |
| R28 | low: the shutdown step assumed an existing shutdown script | Accepted: no script is needed for the usual case (the harness stops jobs early, and SnapRAID's unit makes any power-off wait for the save); the script step stays as the way to withhold at the cap (§5.4, A16); L10 |
| R29 | low: the default thresholds would hold on routine media churn | Accepted in part: replaced files are shown apart, and the card lists what usually causes a hold (§4.3). Rejected: excluding replaced files from the count, because ransomware's encrypted copies look exactly like replacements |
| R30 | low: jargon on novice screens, and U7 relied on a test that does not exist | Accepted: those lines are rewritten, "stripe" is in the words table, and U7 is this design's own check in `tests/test_snapraid.py` (§9.5) |
| R31 | low: set-aside copies piled up with no cleanup path | Accepted: `aside.trash` and `aside.back` move one restore's copies to an excluded trash folder and back, the next guard expects those removals, and emptying is a copy block whose path holds only the restore id (§6.2 step 7) |
| R32 | low: the replacement-disk filter hid pre-formatted disks | Accepted: signed disks are listed apart with their label and size, need the whole serial typed, and get a staged `wipefs` block (§6.3 step 5) |
| R33 | low: restored copies in the separate folder were never protected | Accepted: the same change as R14 |
| R34 | low: D4 depended on a test hold the design gave no way to run | Accepted: the **Try the guard** drill (§5.2, D4) |
| R35 | low: two GUI facts were wrong (nav order 40 is taken; Home cards accept `err`) | Accepted: nav order 45; the Home card uses `err` directly (§2.2, §9.5); V14's workaround is superseded |
| R36 | low: the privacy claim held only with `bwrap`, which is off by default | Accepted: the same change as R6; the masks row of the doctor is checked only under `bwrap` (§9.7) |

Totals for the critic round: 36 findings, 36 accepted, 34 of them in full and 2
in part (R3 and R29, each with the rejected part and its reason in its line); 0
rejected outright. Four pairs were the same issue and got one change each (R6
and R36, R11 and R26, R12 and R20, R14 and R33).
