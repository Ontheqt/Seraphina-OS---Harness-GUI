# Your server: an Apps launcher without Cosmos, system buttons with a way back, and who can act as root

**Status.** Design, written 2026-10-25, checked once by its author and then
revised after a round of 28 critic findings, before the system audit (the Review
log at the end has one line per issue found and what was done with it). Nothing
here is built or installed, and nothing was run against the owner's server.
Every behaviour marked verified was observed in a scratch research machine
(Appendix C), not on the owner's server.
Revised again on 2026-10-25 after a read-only capture of the owner's server,
made by his own assistant, showed a server that had changed again since this
design was written: a login gateway on the old reverse proxy's ports, sudo-rs
as `sudo` with classic sudo beside it, Tailscale sidecars that set no `TS_*`
variables, and an owner account in the `docker` and `lxd` groups (§1.5; the
Review log's capture round, K1-K19, has one line per change). Facts from that
capture are labelled **observed** (below).
The owner decides the choices in §12.1; until he answers, the defaults named
there apply and this document is a proposal.

**Built: phases P1a, P1b and P1c (26 Oct 2026; DESIGN.md §42).** P1a: The host strip and the
summary card without any helper; `bin/sf-system` with the `read` class and the
verbs `info`, `containers`, `compose` (the project name only), `ports`,
`tailscale`, `ingress`, `disks` and `gpu`, its one-paste `setup`, `remove` and
`explain helper-config|ingress`; the cards, grouping, old copies, the review
list with addresses and expected states, QR codes, lights, versions, badges and
deep links, the Beszel watch, "Ask about this app" (a local model only), the
maintenance toggle and the backup lines; the doctor's lines; the mock and the
GUI area. It only looks: no verb of P1b, P2 or P3 exists, and the owner's
decisions D1 (the system user `seraphina`), D2 to D7 (a), D8 (b), D17, D19 to
D21, D23 and D25 (a) apply, with D24 keeping every changing button out. Tested
against a placeholder server (`tests/sfsystem_rig.py`; T1-T7, T9-T12, T15-T17,
T25-T29, T36-T38 in their P1a parts, U1-U5, U9, U10, U12); labs L1, L3, L7,
L15 and L16 are not run. Not built in P1a, though this design places them
there or near: the How to go back panel (it needs keep tags, P1b), a probe of a
gateway route itself with `ca_file` or `pin_sha256` (T38's TLS half), the
reference QR matrices of T17 (no well-known encoder was installed; a reader in
the test checks every code instead), `channels.tailnet_status`'s fallback
(Appendix A11) and the Blueprint's helper mode (§11.9). One route was added to
§11.3 for §4.4: `POST /api/yourserver/beszel {action: password|check, email?}`,
and one for a read now, `POST /api/yourserver/refresh`.

P1b: the Check buttons 1-8 of §7.2 with **Check everything** (Check 8, one
app's logs, reads them only once P1c's preconditions hold, else links to Dockge
and Cockpit); the verbs `docker` (D3 (a): the fixed list of read-only Docker
API calls, never while `docker.service` is stopped), `compose` (now also the
service hashes and image names), `smart`, `needrestart`, `journal`, `du`,
`job`, `log`, `receipts` and `apt.refresh` (class `refresh`: `apt-get update`
in a job, which writes the package lists and runs apt's own hooks, as Ubuntu's
daily timer does; it installs nothing); the update kinds and Docker's own health
on the cards; the keep pass (`keep.images`, class `keep`, D9 (a): names under
`seraphina-keep/` only, 2 per app and 50 GB in all) with its receipts and the
How to go back panel; the records page; the Blueprint's helper mode;
`channels.tailnet_status`'s fallback to the confirmed suffix. P1c: the root page
(view only, §8.1-§8.3 and §8.5) from the `access` job and logs through the
helper's `logs`, both class `private`, refused with the reason until Seraphina
runs as her own system account, outside every root-equivalent group, with the
sandbox on and no command-line agent route; `explain access` for the owner's own
terminal meanwhile. Tested against the placeholder server (T8, T13, T14, T18,
T20's keep part, T21-T23, T30, T34, T35; U6-U8; L2's automated half). Not run:
labs L1-L5, L7, L8, L9, L15 and L16. Not built: Maintain, Fix and root-access
changes (P2, P3; D24), the activity source `system`, the phone notice
`access.new` (a Needs you item instead), the root rows from `dpkg.log` for
changes made outside Seraphina, Tailscale state's age, snap's `refresh --list`,
reusing `sf-snapraid`'s SMART answers, `tickets` (P2), and T38's TLS half and
T17's reference matrices (as in P1a).

**What it serves.** The owner's decisions in [DESIGN.md](../../DESIGN.md) §30
(nothing is off limits; everything has a way back) and §31: any novice can
install it (point 1), one Restore button (point 2), maintenance that runs
manually, at a set time or automatically, with a global toggle and per-service
toggles (point 3), and Tailscale only with no forwarded ports (point 4). It
answers two requests the owner made after his server changed:

> "In Cosmos, I used to be able to click on a button and it would take you to
> the app landing page where it would work. It would also do things like tell
> you how much RAM was left... I've got Beszel on the system, so I guess that's
> not really needed. But maybe the Seraphina OS GUI could evolve into something
> like that."

> "What if Seraphina OS had its own terminal and its own root access separate to
> mine or Kate's? Maybe we could have a list of the most useful Ubuntu server
> commands as buttons that would do things like update the system, clean,
> manage root access."

Forge (the Claude instance building the harness) answered with seven points,
and the owner accepted them ("these seem like good ideas"). §1.1 restates them;
this design turns them into something buildable and testable.

**What changed on the server.** Cosmos Cloud has been removed. Dockge now runs
every container, as compose stacks in its stacks folder, and Dockge has no REST
API. Tailscale gives remote access, through per-app sidecar containers and
Tailscale serve. Beszel is installed for monitoring history. Cockpit is
installed and has a terminal. The owner changed the root password and removed
the operator persona's unrestricted sudo. Since then the owner has put his own
login gateway on the ports Cosmos's proxy used: one Dockge stack with nginx, an
OpenID Connect sign-in service (Dex, with its own Postgres) and two
oauth2-proxy instances, one for the home network and one for the tailnet.
nginx listens on port 80 on every IPv4 address and, on the home-network
address only, on 443 and 14 route ports; one more listener, on the gateway
address of a Docker network that holds a single app, is probably nginx's too
(observed, except that last owner, which is judgement until a root read names
the process). Which app each route port serves is reported, from the old
proxy's map: the gateway's route table was not read. The v2 design was written for a server
with Cosmos: its Apps launcher installs, routes and logs in through Cosmos
(SERAPHINA_OS_HARNESS §2.3 Apps), its native backend reads the Docker Engine API
(§3.4), and its operator persona "holds root" (§0 item 9). §1.2 lists what each
change breaks.

**What it keeps to.** DESIGN.md, the v1 contract: Python 3.10+ standard library
only, threads, the EventBus (§10), config and settings (§11), the HTTP API (§14),
the GUI and its view registry (§15), the CLI (§16), security (§17), the
Blueprint (§28), the Housekeeping Tracker (§32), the notifier (§34), operator
personas and receipts (§36), the shared outcome rule (§37 rule 5) and drift
sources (§38). [SERAPHINA_OS_HARNESS.md](SERAPHINA_OS_HARNESS.md), the v2 design:
Home and the service card (§2.1), the platform modules (§2.3), plain words and
badges (§2.4), what a new install can do (§2.5), ownership of apps (§3.2), the
backend interface (§3.4), Cockpit and other surfaces (§3.8), restore tests
(§5.5), restore points (§5.7), reversibility classes and flags (§5.8),
maintenance windows, modes, the power schedule and holds (§6), how apps are
reached on the tailnet (§7.2, §7.6), the remote-access path (§7.7), system users
and the executor (§9.2, §9.3), and the phase order (§10.3).
[BACKUPS_SNAPRAID.md](BACKUPS_SNAPRAID.md), the sibling design on branch
`k4/snapraid` (the link works once both branches are merged): its narrow root helper `sf-snapraid` (one sudoers line, a JSON
request on standard input, a fixed verb table, transient systemd units, no
shell), its copy-block install, and its power holds. This design reuses those
patterns unchanged where it can, so the two helpers look and behave alike
(§6.2). Where this design differs from any of them it says so in place, and
Appendix A lists every difference.

**What it is built from.**

| Source | What it gave this design |
| ------ | ------------------------ |
| Research lab (Appendix C) | A scratch virtual machine (Ubuntu 24.04.5, kernel 6.18, hybrid cgroup layout, no systemd as PID 1) with a private Docker Engine 29.9.1 using the containerd image store, as the owner's server does. Runs R1-R22: who can read cgroup files, CPU and RAM formulas against `docker stats`, Docker's state files and their modes, how far they lag, Compose project names, sudo and sudoers parsing, sudo under `no_new_privs`, SSH key listing, setuid and capability scans, `ss` with and without root, `smartctl` without root, `apt` and `unattended-upgrade` dry runs and their side effects, `journalctl --disk-usage`. Eleven prune runs from one fixed starting state, each listing what it removed. Two probes added in the critic round: R23 (Compose run from its plugin path with no daemon, and what it prints on standard error) and R24 (`usermod` and `gpasswd` changes and their "undo" on a scratch copy of the account files) |
| Moby `docker-v29.9.2`, docker/cli `v29.9.2`, BuildKit `v0.34.0`, opencontainers/cgroups `v0.1.0`, Compose `v5.6.1` | the cgroup path of a container, `docker stats` arithmetic, what each prune removes and why, how a pull keeps the old image, what a restart does to a stopped container, when Docker marks a container as stopped by hand, Compose's `config --hash` and `--images` |
| Linux `v6.18` documentation and source | cgroup v2 files and their modes, `/proc/meminfo`, hwmon, drivetemp |
| Dockge `1.5.0` (`bac498f`), Tailscale `v1.104.2`, Cockpit `370` (`fd21ab5`), Beszel `v0.21.1` (`e2ed147`) | stacks and deep links; the LocalAPI, serve config and the sidecar state file; Cockpit's URLs, login, framing; Beszel's health, API, roles, heartbeat and links |
| needrestart `v3.11`, apt `3.1.12`, unattended-upgrades `2.9.1`, systemd `v255` and `v258`, smartmontools `RELEASE_7_4`, nvidia-smi 595 manual, sudo `1.9.15p5`, sudo-rs `v0.2.15`, OpenSSH `9.6p1`, netplan's `netplan try` | the Check and Maintain buttons' exact commands and how to read them; the who-can-act-as-root enumeration; the timed undo |
| Homepage `v2.7.0`, Homarr `v2.9.1`, CasaOS AppManagement `v0.4.16`, TrueNAS apps (library `2.3.17`), Umbrel `v1.0.4`, Cosmos `v0.23.4` | how other dashboards find an app's address and health, and the mistakes to avoid (§2.3, §5) |
| The harness at `776c539` | the code this fits into (§11), every symbol cited where it is used |
| BACKUPS_SNAPRAID.md at `7b7cd36` (branch `k4/snapraid`) | the helper shape, the install blocks, power holds, the `maintenance` settings it ships |

**Evidence conventions.** Every factual claim carries one label and a citation.
**Verified** means observed in the research lab (the run is named; Appendix C
lists them). **Source** means read in pinned code but not run. **Docs** means
read in pinned documentation. **Judgement** means this design's own reasoning
or a proposed default; numbers such as intervals, caps and thresholds are
judgement unless a citation says otherwise, and the owner confirms them (§12.1).
**UNVERIFIED** marks a claim whose source could not be opened from here or that
nothing checked. **Observed** means seen in the read-only capture of the
owner's server on 2026-10-25 (its probe names and values stay in his private
notes; this file gives only the general fact, as "a server may ..."), never
run or changed there. `LAB` below stands for the research folder outside this
repository; its logs hold scratch paths and are not published.

**What the lab could not show.** The research machine runs Ubuntu 24.04.5, not
the owner's 26.04; its cgroup layout is hybrid, with the memory, io and pids
controllers on cgroup v1; PID 1 is not systemd; there is no GPU, no SATA disk and
no Tailscale. So the cgroup v2 memory files, transient units, `systemctl` JSON
output, `needrestart`'s service list, GPU queries and SMART on real disks are
source or docs here, and the lab tests of §12.2 marked "systemd-booted" must run
before the parts that depend on them ship (verified: `LAB/logs/interactive-runs.txt`
header, R20).

**Publication.** This file is public. It names products (Dockge, Tailscale,
Cockpit, Beszel, Docker, needrestart, SnapRAID, nginx, Dex, oauth2-proxy) and no host, address, tailnet,
account, container, stack, path on the owner's machine, disk model or person.
Examples use placeholders: `<app>`, `<stack>`, `<tailnet>.ts.net`,
`<harness user>`, `<owner account>`. Facts about the owner's own server stay in
his private notes. The cockpit shows the owner his real names and addresses on
his own machine; none of them is ever written to this repository, to a
notification by default, or to a model by default (§10).

**Words used here.**

| Term | Meaning, and the words a novice sees |
| ---- | ------------------------------------ |
| the cockpit | Seraphina's own web page, as DESIGN §28 and the SnapRAID design use the word. Novice: "Seraphina" |
| Cockpit | the Cockpit web console, a separate program on the server with its own login and terminal. Novice: "Cockpit" |
| application, app | everything that together makes one thing the owner opens: its containers, a native service, its Tailscale connection (§2.1). Novice: "app" |
| member | one container or systemd unit inside an app, known by a stable identity (§2.1), never by its container id, which changes at every update. Novice: "part" |
| kept old copy | a stopped container that is an earlier copy of a running member: same Compose service, or same image repository (§2.1). The owner can mark it retired. Novice: "old copy" |
| expected state | whether an app should be running, proposed from its restart policy and confirmed by the owner (§2.5). Novice: "Should it be running?" |
| main member | the member whose state decides the light: the one the confirmed address reaches, else the one no other member depends on (§2.5). Never shown to a novice |
| stack | a Dockge folder holding one compose file. Novice: "Dockge stack", only in the "managed by" badge |
| sidecar | a small Tailscale container that gives one app its own tailnet name. Novice: "its Tailscale connection" |
| login gateway | the owner's own reverse proxy with a shared sign-in in front of several apps (on the reference server, nginx with Dex and oauth2-proxy; §1.2). Seraphina reads its route table and never sits in it (§5). Novice: "your login gateway" |
| route | one gateway entry: a listen address and port, a path, the app it forwards to, and whether it asks for the sign-in (§2.3). Novice: "through your login gateway" |
| tailnet name, address | the `https://<name>.<tailnet>.ts.net` address the owner types or taps. Novice: "its address" |
| confirmed address | an address the owner has said is right (§2.3). Novice: "the address you confirmed" |
| probe | the cockpit's own check that an app answers (§2.5). Novice: "check" |
| light | the coloured dot on a card and its one-line reason. Novice: the reason itself ("Not answering") |
| host strip | the line of server-wide numbers at the top of the Apps page (§3). Novice: "your server now" |
| the helper, `sf-system` | the one root-owned program this design adds (§6). Novice: "Seraphina's system access" |
| allow class | which kinds of verbs the helper's root-owned config lets it run: `read` (the server's layout), `private` (logs and root-access details), `refresh`, `keep`, `maintain`, `fix`, `access` (§6.3). Novice: "what Seraphina may do" |
| verb | one named request the helper understands. Never shown to a novice |
| receipt | the helper's record of one change: what it did, what was there before, and how to undo it (§6.10). Novice: "record" |
| way back | how a change is undone. Novice: "how to undo it" |
| keep tag | a tag under the reserved repository `seraphina-keep/` that stops clean-up from deleting an app's previous version (§7.3). Novice: "previous version, kept" |
| dangling image | an image with no name left, usually an app's previous version after an update. Novice: "unnamed old copies" |
| build cache | files Docker keeps from building images. Novice: "leftover build files" |
| window | a maintenance window (v2 §6). Novice: "maintenance time" |
| power hold | v2's hold (§6.3): a record that keeps the server on while a job runs. Novice: "keeping the server on for a job" |
| approval code | a one-time code from the owner's authenticator app (§6.9). Novice: "your code" |
| ticket | a root-access change waiting for the owner's own approval in a terminal (§8.6). Novice: "a change waiting for you" |
| timed undo | a root-side timer that puts a root-access change back unless the owner confirms it from a login that started after the change (§8.6). Novice: "It undoes itself in 10 minutes unless you confirm" |
| act as root | have full control of the server, directly or by a short step. Novice: "can take full control of your server" |
| way-back check | the helper's own test of a package change's way back, run inside the job before anything is installed (§7.3). Novice: "Seraphina checked it can put this back" |
| P1a, P1b, P1c, P2, P3 | this design's phases (§11.10): the launcher and host strip; the Check buttons; the root page; Maintain and Fix buttons; root-access changes. They are neither the SnapRAID design's P1-P4 nor the v2 roadmap's A0-G. "P1" means P1a to P1c together |

---

## 0. Decisions in one screen

1. **One area, "Your server".** One GUI view module holds the Apps launcher,
   the host strip, the Check, Maintain and Fix buttons, and the "Who can act as
   root" page. Its Home card is the live "Your server" card that v2 §2.1 point
   4 planned. It does not install apps, update apps, route traffic or log
   anyone in (§1.4).
2. **One card per application, grouped from evidence.** A compose project
   (keyed by its folder and its project name, so a Dockge stack is one app), a
   native service that people open, and every Tailscale sidecar whose serve
   entries reach them, become one card. Members keep a stable identity across
   updates, and stopped earlier copies are shown as old copies, out of the
   light. The owner can merge, split, rename, hide, retire and reorder; his
   grouping always wins (§2.1).
3. **The Open address is found, then confirmed once.** The cockpit reads each
   sidecar's applied Tailscale serve configuration, and the route table of the
   owner's login gateway when there is one, through the helper, proposes
   the addresses, and asks the owner once, in one review list for all apps. It
   asks again only when the serve entry or its target changes. An Open link
   outside the confirmed tailnet is never shown as an app's address (§2.3). Each
   card has a QR code drawn from the confirmed address and plain steps for the
   phone's own "Add to Home Screen" (§2.4).
4. **Seraphina is never in the request path.** No reverse proxy, login
   gateway or DNS of its own, no Tailscale serve changes. Tailscale, and the
   owner's own login gateway where he has one, decide who can reach an app.
   Seraphina reads the gateway's route table, never writes its files, and is
   never one of its upstreams or sign-in clients; if Seraphina is stopped every
   app still works, and if the gateway breaks Seraphina still works. Eleven
   requirements, each with a test (§5).
5. **Numbers without the Docker socket.** RAM, CPU, IO and process counts come
   from each container's cgroup files, which are world-readable, so
   Seraphina's own user reads them with no helper. GPU memory comes from
   `nvidia-smi` joined to containers through `/proc/<pid>/cgroup`. Names,
   stacks and privileges come from the helper reading Docker's state files as
   root. Seraphina's own user is never in the `docker` group and never holds
   the socket (§6.4).
6. **Beszel stays the history.** The cockpit shows numbers now and links into
   Beszel for graphs; it keeps no time series of its own. It watches Beszel
   from outside: the hub answering, the agent running, and data still arriving,
   so a 21-hour gap is noticed within the hour (§4).
7. **One more narrow root helper, `sf-system`, shaped exactly like
   `sf-snapraid`.** One sudoers line allows the harness's user to run it with
   no arguments; a JSON request on standard input names one verb from a fixed
   table; each verb belongs to an allow class switched on in a root-owned
   config; no shell; long jobs in transient units. It never prunes containers,
   networks or volumes, never runs `prune -a`, never reads a container's
   environment beyond a short fixed list, never copies Tailscale keys, and never
   changes its own sudoers line on a request: only its `setup` mode, which the
   owner runs once with his own sudo, writes it. Removing one file,
   `/etc/sudoers.d/seraphina-system`, ends all of its root access at once (§6).
8. **Seraphina's own user, separate from the owner's and Kate's.** On a home
   server where the harness's current account holds no root-equivalent group,
   P1a and P1b may run under it, with reads of the server's layout only. Where
   that account is in `docker`, `lxd`, `incus-admin`, `disk` or `sudo` (the
   owner's account on the reference server is in `docker` and `lxd`; observed),
   its agents can reach root: in `docker`, `lxd` or `incus-admin` through the
   engine's socket, which bwrap's read-only `/` does not hide; in `sudo` or
   `disk` through whatever the harness runs outside the sandbox (judgement;
   DESIGN §17). So the harness never runs as that account: the system user `seraphina`,
   outside those groups, is a precondition of P1a, and the doctor refuses the
   owner-account route there (§6.1, D1). The helper itself refuses logs,
   root-access details and every change to a caller that is a person's login
   account, because its sudoers line gives that account's every process, agents
   included, password-free answers. So before P1c at the latest, the harness
   moves to its own system user, `seraphina`, and the helper takes calls only
   from it; the owner's and the operator persona's accounts get nothing from
   this helper (D1, D2). There is no general root account and no built-in
   terminal (§0 item 12).
9. **Buttons in four groups, read-only first.** Check (read-only, any time:
   updates, restart needed, disk use, failed services, disk health, what is
   listening, Tailscale, logs for one app) ships in P1b. Maintain (install
   updates, security only or all; clean old packages, old logs and Docker
   leftovers) and Fix (restart a running service; a server restart that waits
   for backups and long jobs) ship in P2. A package change runs only after the
   helper has kept the previous versions and checked, in the same job, that it
   can put them back (D24). Access changes ship in P3 (§7, §8, §11.10).
10. **Every change has a class, a receipt and a way back.** Each button carries
    its v2 §5.8 class and badge. The helper writes a receipt for every change
    with what was there before and how to undo it. An outcome Seraphina cannot
    tell is "unknown", shown as unknown, and never replayed (§6.8, §6.10).
11. **Who can act as root, in plain words.** A read-only page lists every way
    the helper can find: accounts, sudo rules and their files, root-equivalent
    groups, SSH keys, Docker socket holders, powerful containers, anyone who can
    write a Dockge stack, root services and timers that run files others can
    change, cron, setuid files and capabilities, polkit. It says "found N ways;
    these kinds cannot be checked from here" rather than "only N". Removing a
    sudo rule, taking an account out of an admin group, locking an account or
    removing an SSH key (P3) is approved by the
    owner with his own `sudo` in a terminal, refuses to remove the last admin or
    lock the owner out, puts back the exact lines it changed, and undoes itself
    in 10 minutes unless he confirms from a login that started after the change
    (§8).
12. **No terminal of its own.** "Open terminal" opens Cockpit's terminal in a
    new tab, where the owner logs in as himself. A web shell inside the cockpit
    would be a root path for anything that holds the cockpit's master token,
    including its own agents (§9).
13. **Private by default.** Logs, process lists, command lines, mount paths, file
    and folder names, SSH key comments and tailnet names stay on the server, in
    the cockpit only. A phone gets fixed texts and counts; an app's name only
    when the owner allows it for that app. Agents see none of it unless the
    owner asks; the helper redacts log pages itself and gives logs and
    root-access details only to Seraphina's own user, and the doctor reports an
    error while agents could call the helper for them (§10).

---

## 1. What the owner asked for, and what this design answers

### 1.1 The two requests, and the seven points accepted

| Forge's point (accepted) | Where this design makes it concrete |
| ------------------------ | ----------------------------------- |
| (1) An Apps launcher for a server without Cosmos: one card per application with Open, QR and add-to-home-screen, a health light, version and update available, what manages it, RAM, CPU and GPU memory now, open tracker items, last backup; a host strip; Beszel kept, linked and watched; each app answers "what is this, is it healthy, what changed"; per-app maintenance toggle and backup status | §2, §3, §4 |
| (2) Seraphina is never in the request path | §5 |
| (3) Numbers from cgroup and proc files or a narrow read-only helper, never the Docker socket | §6.4 |
| (4) No built-in web terminal; "Open terminal" deep-links to Cockpit's | §9 |
| (5) No general root account: Seraphina's own system user with a fixed list of allowed actions, each recorded with a way back and the shared outcome rule; one file removes it | §6 |
| (6) Buttons in four groups: Check, Maintain, Fix, Access | §7, §8 |
| (7) Check buttons and read-only views first; buttons that change things only after backup and restore work | §11.10 |

The owner's "its own root access separate to mine or Kate's" is answered by
points 5 and 6 together: Seraphina gets a fixed list of root actions, reached
only by its own Unix user, never a root shell, a root password or the owner's or
the operator's sudo (judgement). "Manage root access" becomes the read-only page
of §8 in P1c and five reversible changes in P3.

### 1.2 What changed on the server, and what each change breaks

| Change | What it breaks in v2 or in today's harness | What this design does |
| ------ | ------------------------------------------ | --------------------- |
| Cosmos removed | v2 §2.3 Apps: the launcher's installs, names, logins and "Open in Cosmos"; v2 §3.2's owner table gives routes, TLS, logins and SnapRAID to Cosmos; v2 §3.4's Cosmos backend. Cosmos's reverse proxy is gone; a server may put its own login gateway on the same ports afterwards (next row) (docs: v2 §2.3, §3.2, §3.4) | The launcher reads what the server actually uses now (Dockge stacks, sidecars, native units) and installs nothing (§1.4). Ownership of each app is computed from that evidence (§2.7) |
| A login gateway on the old proxy's ports: nginx, a sign-in service (Dex with Postgres) and two oauth2-proxy instances, in one Dockge stack on the host network (observed) | **Address discovery**: routes live in nginx's config, not in Tailscale serve, and nginx on the host network owns every route port, so a host-port join would make the gateway "own" every app's address. **Health**: oauth2-proxy answers 200, 302 or 401 before it touches the app, so a probe of a route shows a dead app as green; the gateway's members have no Docker health check (observed). **Cookies**: one home-network host name serves the gateway's routes and several apps' direct ports, and cookies are not isolated by port. **Root page**: whoever can change the gateway's config or stack, or administer the sign-in service, decides who reaches every app behind it | The `ingress` verb reads the route table's shape, never its secrets (§6.3), and each route becomes an address candidate for the app its upstream reaches (§2.3 source 4); probes go to the upstream, never the route, and the gateway gets its own card (§2.5); the harness is never served or allowed on that host name (N10); Seraphina is never one of the gateway's upstreams or clients (N11, D25); the gateway is a row on the root page (§8.1 row 16) |
| Dockge runs every container | Dockge has no REST API: its backend speaks socket.io after a login, and its HTTP server serves only `/`, `/robots.txt` and static files (source: Dockge `backend/routers/main-router.ts:9-20`, `backend/socket-handlers/main-socket-handler.ts:30, 64`). Nothing can ask Dockge what it manages; its stacks are plain folders | The stacks folder is read as plain files through the helper (§2.1, §2.6); Dockge is linked, never driven (§2.7) |
| Tailscale through per-app sidecars | The host has no `tailscale` CLI to ask, so the Blueprint's tailscale section records a missing command and `channels.tailnet_status` returns `None` (source: `swfactory/blueprint/collect.py:820-824`; `swfactory/channels.py:287-296`). The notifier then never puts a link in a notice, because `Notifier._link_base` needs the tailnet suffix (source: `swfactory/notify.py:1204-1224`). A sidecar's LocalAPI socket sits inside the container (source: Tailscale `cmd/containerboot/main.go`, `TS_SOCKET`). Every tailnet route, Cockpit's included, disappears when the Docker engine stops (judgement, from the reference server's pattern in the private notes) | The helper decodes each sidecar's applied serve config from its state file (§6.5). The owner's confirmed tailnet suffix feeds the notifier's link check (Appendix A11). §9 recommends a path to Cockpit that does not depend on Docker |
| Beszel installed | Nothing in the harness knows Beszel. It once sat down for about 21 hours with nobody told (judgement, from the private notes) | §4 |
| Cockpit installed | v2 §3.8 already deep-links to Cockpit and uses its terminal; v2 §6.5 lists Cockpit's Software Updates as an updater outside any window | §9; Cockpit changes are detected as changed outside Seraphina (§7.7) |
| Root password changed; the operator persona's unrestricted sudo removed | `swfactory/operator.py:1-13`, DESIGN §36, v2 §0 item 9 and v2 §9.2 say the operator persona "holds root on the server and acts through her own tools"; that no longer describes the server (source: those lines). The Blueprint's `--sudo` path falls back to the harness user at the first refusal, so its Docker, `ss -p`, SMART and crontab probes are denied (source: `collect.py:466-506`) | The helper's read verbs replace the Blueprint's root probes (§11.9). The operator text is flagged stale (Appendix A14) |

### 1.3 One area: Your server

| Place | What it holds |
| ----- | ------------- |
| `#/apps` | the host strip (§3), then the app cards (§2), then a short "Not shown" line (members the owner hid or retired, old copies, one-shot containers) |
| `#/apps/<app id>` | one app: the card, its parts, its address, "Ask about this app" (§2.8), its logs (Check 8), Details |
| `#/server` | the Check, Maintain and Fix buttons (§7), Beszel's state (§4), "Open terminal" (§9), the helper's state and what it may do |
| `#/server/root` | Who can act as root (§8) |
| `#/server/receipts` | every change the helper made, newest first, each with its way back (§6.10) |
| `#/server/setup` | the helper's install, check, enrolment and removal blocks (§6.11) |
| Home | the "Your server" card (§11.5) |

v2 §2.3 lists eight platform modules under Your server: Apps, Containers, URLs &
access, Storage, Network, GPU & AI, Users and Security. This design ships the
Apps launcher, the parts of Storage and GPU & AI that are numbers now (§3), and
the read-only half of Users and Security that answers "who can become root"
(§8). Containers, URLs & access, Network, the rest of Storage and GPU & AI, and
Security's other views stay planned; their v2 text is unchanged (judgement).

### 1.4 What this design does not do

- **No installs and no market.** v2 §2.3's market was Cosmos's plus a
  Seraphina source; with Cosmos gone, installing stays in Dockge, by the owner.
- **No app updates.** The card says when an update is available (§2.6) and
  links to the stack's page in Dockge. Updating apps through Seraphina is v2's
  Upgrades & Enhancements section, after the executor.
- **No reverse proxy, login gateway or DNS of its own** (§5). An owner's
  existing gateway is read for its routes and never changed.
- **No Tailscale serve changes**, no sidecar edits, no compose file edits, no
  Dockge settings, no gateway config.
- **No Docker engine, kernel, GPU driver, Tailscale or SSH server upgrades**
  through a button (§7.6).
- **No terminal of its own** (§9).
- **No history graphs**: Beszel keeps them (§4).

### 1.5 What a read-only capture of a real server changed

The capture of 2026-10-25 (observed; Status) found these, each of which a
home server may also show, and each of which changed this design in place:

| A server may ... | So this design ... |
| ---------------- | ------------------ |
| put its own login gateway (nginx, Dex, oauth2-proxy) on the ports a removed proxy used | reads its routes as addresses (§2.3, §6.3 `ingress`), probes upstreams (§2.5), keeps out of its path (§5 N11, D25), lists it on the root page (§8) |
| run sidecars that set no `TS_*` variables at all, with state in a bind or a named volume at `/var/lib/tailscale` | finds state by mount destination and names by the state's profile (§6.5) |
| run sudo-rs as `sudo` with classic sudo installed beside it under another name | decides the implementation from the resolved `sudo` binary, lists both setuid programs as doors, and checks every grant with both (§8.1 row 3, §6.11, L7) |
| put the owner's login account in `docker` and `lxd` | never runs the harness as that account, from P1a on (§0 item 8, §6.1, D1) |
| run root services whose program is an interpreter and whose script sits in a person's home folder | reads the script argument after the interpreter, not only the program (§8.1 row 10) |
| run almost every container from a local copy of its image, referenced by id, with dated local tags only | shows "pinned to a copy kept on this server" and treats those tags as the owner's keep tags (§2.6) |
| leave label-less, network-less job containers that share an app's image | lists them as job runs, never as old copies (§2.1) |
| keep a Compose recipe as `compose.json` outside Dockge's folder, and Dockge's own recipe in the owner's home folder | takes the recipe from the `config_files` label (§2.7, §6.3 `compose`) |
| run a web file manager and a Docker manager on every address, one with a page that opens without a password (a remote-desktop password stands behind it) | lists both with their exposure and who they let change what (§2.10, §8.1) |
| swap NVMe device names and GPU index order between boots | keys disks by filesystem UUID and GPUs by UUID, never by `/dev` name or index (§3) |
| run both systemd-networkd and NetworkManager | warns that Cockpit's Networking page may edit a stack that does not own the interface (§9) |

---

## 2. The Apps launcher

### 2.1 What an application is

An application is what the owner thinks of as one thing he opens: a photo
library with its database and its Tailscale connection, or Cockpit. v2 defines
it as a group of containers, native units, images, networks, endpoints, data
folders, secrets, schedules, backup sets and an owner (v2 §2.3, L2). Without
Cosmos, nothing on the server records that grouping, so the cockpit computes it
from evidence, strongest first, and the owner corrects it once (judgement):

| # | Evidence | Becomes | Basis |
| - | -------- | ------- | ----- |
| 1 | The owner's own grouping (merge, split, move, rename, hide, retire) | exactly what he chose; it outranks everything below | judgement |
| 2 | The Compose labels `com.docker.compose.project.working_dir` and `com.docker.compose.project`, taken together | one app per folder and project name. When the folder is a direct child of Dockge's stacks folder and the project is the name Compose gives that folder now, the app is that Dockge stack. A second project made from the same folder under another name (an older `name:` or `COMPOSE_PROJECT_NAME`, or a copy started by hand with `-p`) is its own card, "Made from the same folder under another name. Dockge does not show it.", and its stopped containers are proposed as old copies | verified: Compose writes `project`, `service`, `container-number`, `oneoff`, `project.working_dir`, `project.config_files`, `config-hash`, `image`, `replace`, `version` and `depends_on` labels (lab R5); the name Compose gives a folder is the top-level `name` of `config --format json`, read with no daemon (lab R23); source: Dockge lists each direct subfolder that holds a compose file as one stack, keyed by the folder's name, and matches running projects to stacks by project name through `docker compose ls` (`backend/stack.ts:265-336`) |
| 3 | A Tailscale sidecar | its serve entries are joined to their targets, and each entry is a separate address candidate (§2.3) for the app its target belongs to. A target is joined, in this order: by a shared network namespace in either direction (the app's `NetworkMode` is `container:<sidecar>`, Tailscale's usual pattern, or the sidecar's is `container:<app member>`); by a container name, alias or address on a Docker network both containers join (`NetworkSettings.Networks` in the state files); or by a host port (a container's `PortBindings`, a socket unit, or the process `ss -p` names; §6.3 `ports`). The sidecar becomes a member of the app its first confirmed address belongs to, and every other app it serves shows "Shares its Tailscale connection with <app>". A sidecar none of whose targets joins stays with its own Compose project; a project that holds only sidecars becomes an app of its own only then. A host-port join that lands on the login gateway (a host-network nginx owns every route port; §2.3 source 4) never makes the gateway the owner of another app's address: the target is followed through the gateway's route to its upstream, and only the upstream's app gets the address | verified: `NetworkMode` and `PortBindings` are in `hostconfig.json` (lab R6); source: each network endpoint's `Aliases`, `IPAddress` and `DNSNames` (Moby `api/types/network/endpoint.go:10-40`; the field names in a recorded `config.v2.json` are pinned by T4); Tailscale `ipn/serve.go:53-83` (proxy targets) |
| 4 | A container with no Compose labels | its own app, "started by hand" | judgement |
| 5 | A native systemd unit that listens beyond loopback, itself or through its socket unit, or that a serve entry proxies to | its own app, "system service" (Cockpit, a host tailscaled, Seraphina itself). Other units are not apps; they appear under Check 4 and on the root page | judgement; socket units as §6.3 `ports` maps them |
| 6 | A one-off container (`com.docker.compose.oneoff` true) | never a card | verified: the label exists (lab R5) |
| 7 | A **job run**: a container with no Compose labels, restart policy `no`, network mode `none`, and a name of one family with a generated suffix (`<family>-<uuid>`), started by a job broker | listed on the app's page under "Job runs" when it shares the exact image of an app's member, else under "Not shown"; never proposed as an old copy, never part of the light, the counts or the RAM sum | observed: four such stopped containers, three sharing one app's exact image; judgement: the rule |

**Why the folder and the project name together.** Compose names a project after
its folder, normalised (`My.Stack` becomes `mystack`), unless the compose file
has a top-level `name:` or the `.env` sets `COMPOSE_PROJECT_NAME` (verified: lab
R9). Dockge matches its stacks to running projects by that name, so an override
breaks Dockge's own status display, but the working-directory label still names
the folder (source: Dockge `backend/stack.ts:265-336`). Keyed by the folder
alone, two projects made from one folder would merge into one card, for example
a predecessor started under an older name that still holds the same ports and
GPUs; keyed by the name alone, a stack whose name was overridden would lose its
folder. The pair survives both, and "Managed by Dockge" goes only to the project
Dockge itself matches (§2.7; judgement).

**Names.** The owner's name; else a recognised product (Dockge and the Beszel
hub by image repository, Cockpit by its unit); else, for a Dockge stack, the
folder's name; for any other Compose project, its main service's name when the
project has one service that is not a sidecar, else the project name; for a
container started by hand, its name; for a unit, its description. A folder's own
name is never used for a project outside the stacks folder, because a compose
file kept directly in a home folder would make a card named after that home
folder (judgement).

**Where the stacks folder is.** Dockge takes `--stacksDir`, else
`DOCKGE_STACKS_DIR`, else `/opt/stacks`, and its shipped compose file requires
the host and container paths to be identical (source: Dockge
`backend/dockge-server.ts:101-108`, `compose.yaml`). The helper finds the Dockge
container by its image repository (`louislam/dockge`) or by the owner's mark,
and reads that one environment key (§6.4); the owner confirms the folder once.

**Members keep their identity across updates.** Dockge's Update runs `compose
pull` and then `compose up -d`, which recreates every changed container under a
new id (source: Dockge `backend/stack.ts:446-465`). So a member is known by a
stable identity, never by its container id: for a Compose container, its folder,
project, service and container-number labels; else its container name; for a
unit, its unit name (judgement). A recreate is an update of the same member,
logged in the app change log (§2.8), and the owner's hides, moves and splits
stay on it. When two containers share one identity (a predecessor renamed by
hand keeps all its Compose labels), the running one, or else the one whose name
follows Compose's own naming, is the member, and the other is a kept old copy.

**Kept old copies.** A stopped container is proposed as an old copy of an app
when it has the same Compose service as a running member, or when it has no
Compose labels (or a project of its own that holds nothing else) and the same
image repository as a running member, and it is not a job run (rule 7); the owner confirms it in the review list
of §2.3, because two stopped copies of a common image can be two real apps. A
confirmed old copy is shown under the app as "Old copy, stopped since <date>".
It is left out of the light, the Restart button, the counts and the RAM sum. The owner can mark it **retired** (it then moves to
the "Not shown" line) or **part of this app** (it then counts as a member). A
retired copy whose restart policy starts it at boot is a finding, "An old copy
of Photos tries to start whenever the server starts", with a copy block for
Cockpit's terminal, `docker update --restart=no <container>`, because the helper
never changes a container (§6.6). An old copy is often the only copy of a
previous version, so Seraphina never removes one (judgement; §7.3 M5).

**The owner's corrections.** Merge two apps (an app and its database kept in
another stack), split a member out (a stack with two web apps), move a member,
rename, hide, retire, reorder, pick an icon. Each is a change to the cockpit's
own record (`<data_dir>/yourserver/apps.json`), undone with one click, and logged
in the app change log (§2.8). A new container that appears in a stack joins that
stack's app automatically, unless the owner split that Compose service out
before. A member that disappears (Dockge's Delete runs `compose down` and then
removes the whole stack folder; source: Dockge `backend/stack.ts:218-232`) is
shown as "Removed from the server on <date>" for 30 days, then moved to an
archive list, never deleted from the record (judgement).

**App ids** have the shape `^a-[a-z0-9][a-z0-9-]{0,30}$`, made once from the
first name and never changed by a rename, so links, tracker items and settings
keep working (judgement).

### 2.2 The card, field by field

A card on a phone, for example:

```
Photos                                         ● Working
photos.<tailnet>.ts.net                        [Open]  [QR]
RAM 1.2 GB · CPU 3% · GPU 0.4 GB
Version 1.132 · A newer version is available
Managed by Dockge (stack photos)
Updates for this app: not done by Seraphina yet
Backups: no versioned backup yet · previous version kept on the server
2 open items
```

| Field | Comes from | Root needed? | Refreshed |
| ----- | ---------- | ------------ | --------- |
| Name, icon | the naming rule of §2.1. The icon from a short fixed list matched on the image name, or the owner's pick | no | on change |
| **Open** and the address | the confirmed address (§2.3) | the helper reads sidecar state | on change; asked again when it changes |
| QR code, Add to Home Screen | the confirmed address (§2.4) | no | — |
| The light and its reason | §2.5 | partly | every 60 s |
| Version | the image's `org.opencontainers.image.version` label as the container's labels carry it, else the tag, else the first 12 characters of the digest; for a native unit, the version of the package that owns its `ExecStart` program (`dpkg-query -S`, trying both the `/usr` path and the legacy `/bin`, `/sbin`, `/lib` alias, because of usrmerge; verified: lab R17) | the helper (labels) | on change |
| Update available | §2.6 | the helper | hourly for local checks; daily for the registry, if allowed |
| Managed by | §2.7 | the helper (labels) | on change |
| RAM now | the sum over running members (old copies left out) of `memory.current` minus `memory.stat` `inactive_file`, the number `docker stats` shows on cgroup v2 (source: docker/cli `cli/command/container/stats_helpers.go:183-200, 254-264`; docs: Linux `cgroup-v2.rst:1299-1305, 1523-1560`). Raw `memory.current` includes page cache and would make media and photo apps look several times larger | no: cgroup files with a read handler are mode 0444 (source: `kernel/cgroup/cgroup.c:1610-1625`; verified for `cpu.stat` and v1 memory files, lab R1) | every 15 s while someone is looking, else every 5 min |
| CPU now | two reads of `cpu.stat` `usage_usec`: (Δ usage ÷ Δ wall-clock µs) × 100 is percent of one core; the card divides by the number of online CPUs and says "3% of the server" (verified: 99.8% of one core read as `nobody` against 99.51% from `docker stats`, lab R2) | no | as RAM |
| GPU memory now | `nvidia-smi --query-compute-apps=pid,used_gpu_memory,gpu_uuid --format=csv,noheader,nounits`, each PID joined through `/proc/<pid>/cgroup` to its `docker-<id>.scope` or unit (verified: `/proc/<pid>/cgroup` is readable by an unprivileged user, lab R3; docs: nvidia-smi 595 manual). Cards are named by UUID, never by index (docs: the manual says enumeration order can change between reboots) | no, unless the driver hides other users' processes (UNVERIFIED; Q4), then the helper's `gpu` verb | as RAM |
| Open items | tracker items linked to the app (`links.app`, a new link key; Appendix A9) | no | on `items.updated` |
| Backups | §2.9 | no | on the Backups area's events |
| Maintenance | `services.<app id>.maintenance`: the card shows only a value that does something today (`pinned`, `frozen`), else "Updates for this app: not done by Seraphina yet"; the toggle itself is on the app's page (§2.9) | no | on settings change |
| Restore available | v2 §2.1's three words, computed from what exists: kept images (§7.3) and restore points (none until the Backups work of v2 phase B) | no | on change |
| What changed | the app change log (§2.8) | no | on change |

**Containers are found by PID, not by a fixed path.** On a cgroup v2 host booted
with systemd, Docker uses the systemd cgroup driver and puts each container in
`/sys/fs/cgroup/system.slice/docker-<64 hex>.scope` (source: Moby
`daemon/daemon_unix.go:589-600`, `daemon/oci_linux.go:783-807`;
opencontainers/cgroups `systemd/common.go:100-106`; needrestart's Docker plugin
matches the same pattern, `perl/lib/NeedRestart/CONT/docker.pm:59-61`). A
compose file with its own `cgroup_parent` puts it elsewhere, so the cockpit
scans for `docker-*.scope` under every slice and cross-checks with the
container's main PID from the helper (judgement). The lab could show these files
only under its v1 controllers, so the cgroup v2 memory and PID files are source
and docs here, and L1 checks them on a 26.04 host with one read as the harness
user before the cards rely on them.

**Running or not, without the socket.** A container's cgroup directory exists
only while it runs: it vanished at `docker stop` (verified: lab R7). So "a
`docker-<id>.scope` with PIDs in `cgroup.procs`" is the cheap running check,
read every 15 s with no helper call. A stopped container leaves nothing there;
why it stopped comes from the helper (§2.5).

### 2.3 The Open address: found automatically, confirmed once

Tailscale serve gives each app a name like `https://<name>.<tailnet>.ts.net`,
and that is what the owner taps. The cockpit finds candidates, in this order;
each serve entry is a candidate for the app its target belongs to (§2.1 rule
3), so one sidecar can give addresses to several apps:

| # | Source | What it gives | Basis |
| - | ------ | ------------- | ----- |
| 1 | Each sidecar's applied serve config, decoded by the helper from the sidecar's state file (§6.5) | every `Web` entry `<fqdn>:<port>` with its mount paths and the local target each proxies to; every `TCP` forward; whether Funnel is on (`AllowFunnel`) | source: Tailscale `ipn/serve.go:28-31, 53-83, 125-177`, `ipn/store/stores.go:145-226`, `cmd/containerboot/serve.go:176-194` |
| 2 | A host tailscaled, when there is one: `tailscale serve status --json` | the same config for the host node; needs no root, because the LocalAPI socket is mode 0666 and serve status is a read | source: Tailscale `safesocket/unixsocket.go:57, 87`, `ipn/localapi/serve.go:26-46`, `ipn/ipnserver/server.go:205-224, 336-355` |
| 3 | A sidecar's serve-config file (`TS_SERVE_CONFIG`), when its state holds no applied config | the same entries, but with `${TS_CERT_DOMAIN}` in place of the name, which containerboot fills in at run time; the name then comes from the state's profile, or from the owner | source: `cmd/containerboot/serve.go:30-35` |
| 4 | The owner's login gateway: its route table, read by the helper's `ingress` verb (§6.3) | every route as listen address and port, `server_name`, `location`, upstream (`proxy_pass` or an `upstream` block) and whether it asks for the sign-in (`auth_request`); each route is a candidate for the app its upstream reaches (a container address or name, or a host port joined as in §2.1 rule 3), labelled "Through your login gateway", and "Only at home" when it listens only on a home-network address | judgement; observed: nginx with Dex and oauth2-proxy, 14 route ports on the home-network address; reported: one app per route port, from the old proxy's map; the route table itself was not read in the capture, so the shape is checked on the owner's config at L16 |
| 5 | A published port on all interfaces or a home-network address | `http://<server's home address>:<port>`, shown as "Only at home" and raised as a Tailscale-only finding (DESIGN §31.4) | verified: `PortBindings` (lab R6) |
| 6 | The owner types it | — | — |

**The owner confirms once, in one list.** The first time, the cockpit shows one
review list: each app's proposed name and best address on one row, "Photos ·
photos.<tailnet>.ts.net · Should it be running? yes", with a tick box. A row is
ticked in advance only when the node name in the sidecar's state profile (§6.5
item 3) equals the address's first label and exactly one target joins (§2.1 rule
3); every other row waits unticked. The profile is used, not `TS_HOSTNAME`,
because a sidecar may set no `TS_*` variable at all (observed: seven of eight).
A gateway route (source 4) is never ticked in advance: its upstream join is
inferred from the gateway's config, which the owner can change at any time. **These are right** confirms the ticked rows at once; any row can be
edited in place ("No, it's another one", "I only use it at home", a different
name, "This is an old copy"). Detection can be wrong, which is why it asks
(judgement, from the cases below). Later, an app that appears or a changed
address asks on its own card: "Is this the address you use for Photos?
photos.<tailnet>.ts.net [Yes] [No, it's another one] [I only use it at home]".
"No, it's another one" puts that address aside: it is not proposed again, the
next one found is, else "Type the one you use"; an address already confirmed
stays until the owner answers for it.
The record keeps `{url, source, fingerprint, confirmed_at}`; the fingerprint is
a hash of the serve key, the mount path, the proxy target and the sidecar's node
name. The card asks again when the fingerprint changes, and never changes a
confirmed address by itself.

**Why detection can be wrong** (judgement; source: `ipn/serve.go:53-83`):

- a state file can be stale, or belong to a node that was removed;
- a name clash makes the control plane give the node a suffixed name;
- one app can have several entries (ports, paths), or none when it is reached
  only at home;
- a sidecar on the host network that proxies to `127.0.0.1:<port>` serves
  whatever owns that host port, which has to be joined through `PortBindings`,
  a socket unit or `ss -p`, while a sidecar that shares the app's network
  namespace serves the app itself;
- a sidecar can sit in a Dockge stack of its own, proxy to another container
  by name on a shared Docker network, or front a gateway that routes to several
  apps, so neither its folder nor its own name proves which app it serves.

**The tailnet itself is confirmed once.** The first sidecar profile's
`MagicDNSName` (the tailnet suffix) is shown to the owner as "Your Tailscale
network: <tailnet>.ts.net. [That's mine]" and pinned in settings
(`yourserver.tailnet.suffix`; source for the field: `ipn/prefs.go:1052-1056`).

**What may become an Open link** (judgement). Only an `https` address whose host
ends in `.<confirmed suffix>`, an `http` or `https` address on one of this
server's own home-network addresses (labelled "Only at home"), or loopback (for
testing). The cockpit refuses anything else, typed or detected: an Open button
is a link a novice taps without reading it, and the record that holds it is a
file the harness's user can write (§10.3). The record carries a hash chain
(each change line holds the previous line's hash), and the harness keeps the
chain's newest hash in memory and compares it at every read, so a change made
outside the cockpit's own API while it runs shows as "This address was changed
outside Seraphina. Is it still right?" and the Open button waits for the owner.
After a harness restart, a confirmed address that matches no detected candidate
waits the same way. The chain has no secret, so a process that rewrites it
while the harness is down is not caught by it; the suffix rule above still
limits any forged address to the owner's own tailnet or home address (judgement).

**Other dashboards, for comparison** (source unless noted). Homepage and Homarr
take the link typed by hand or from Docker labels, and get status from a ping or
an HTTP check, with Docker stats only through the socket (Homepage `v2.7.0`
`docs/configs/docker.md:30-48, 133-172`, `services.md:269-332`; Homarr `v2.9.1`
`packages/db/schema/sqlite.ts:454-455`). CasaOS, TrueNAS and Umbrel build the
address from the device's address and a published port (CasaOS AppManagement
`v0.4.16` `api/app_management/openapi.yaml:640-650`; TrueNAS apps
`library/2.3.17/portals.py:12-65`; Umbrel `umbrel-app.yml`), and Unraid from its
template's WebUI field (community forum threads, UNVERIFIED). All of these give
home-network addresses, not the Tailscale names this owner uses, and none treats
the address as something the owner confirms.

### 2.4 QR code and Add to Home Screen

- **The QR code** is made by the harness from the confirmed address only, by a
  small standard-library encoder (`swfactory/qr.py`: byte mode, error correction
  level M, versions 1 to 10, which hold an address of up to 213 bytes, the
  eight masks scored by the standard's penalty rules; judgement). `GET
  /api/yourserver/apps/<id>/qr` returns `{size, rows}`, one string of 0s and 1s
  per row, and the view draws the squares with `SF.h` as a CSS grid with a
  four-square quiet border. This needs no new `SF` helper (app.js's `svgEl` is
  not on `SF`, and a new key would change the `SF_API` test; source:
  `swfactory/web/app.js:8665-8705`, `tests/test_gui_static.py:151-166`) and no
  image route. T17 checks the encoder against stored reference codes made once
  with a well-known encoder at development time, and decodes them back.
- **It works only on the tailnet.** The card says: "Scan this with your phone's
  camera. It opens Photos only while Tailscale is on on that phone."
- **Only for tailnet addresses.** The QR code and the Add to Home Screen steps
  are offered only for a confirmed tailnet address. A home-network gateway route
  uses the gateway's own private certificate, which a phone does not trust, so a
  shortcut to it opens on a certificate warning; for such an address the card
  says "This address uses your login gateway's own certificate, so your phone
  will warn before opening it. Use the Tailscale address on your phone."
  (judgement; observed: the routes listen on the home-network address with a
  certificate the gateway holds).
- **Add to Home Screen** is the phone's own feature; Seraphina cannot install
  another site's app (judgement). The card shows the steps for the phone the
  owner picks: on an iPhone or iPad, open the address in Safari, tap Share, then
  "Add to Home Screen"; on Android, open it in Chrome, tap the menu, then "Add to
  Home screen" or "Install app" (judgement, UNVERIFIED wording per browser
  version). Serve's certificates are publicly trusted, so the shortcut opens
  without warnings (source: Tailscale `ipn/ipnserver/server.go:219`, the
  certificate permission behind serve's HTTPS; judgement for "no warning"). Each
  app keeps whatever sign-in it has: its own, or the owner's login gateway's.
- **It never leaves the screen.** Not in a notice, a tracker item, an event or a
  model's context (§10).

### 2.5 The health light

**What is checked, in order** (judgement unless a citation says otherwise):

1. **Running.** For a container, its cgroup has PIDs (§2.2). For a unit,
   `systemctl show -p ActiveState,SubState,Result,NRestarts <unit>`, which needs
   no root. A service started on demand through its socket (Cockpit behind
   `cockpit.socket`) is inactive between visits: while its socket listens it is
   green, "Ready: it starts when you open it", never "Stopped".
2. **How it stopped.** From the helper's `containers` verb: running or not,
   exit code, finish time, restart count and restart policy (verified: written
   to `config.v2.json` on stop, lab R7), and Moby's "manually stopped" flag.
   Moby sets that flag for every stop or kill made through its API while the
   daemon is not shutting down, and clears it at the next start (source: Moby
   `daemon/kill.go:95-96`, `daemon/start.go:184`). So it means "stopped through
   Docker": by a person, by Dockge's Stop, by a backup or GPU script, or by a
   stop that ran out of time and ended in a kill (exit code 137). It never means
   "stopped on purpose" (T5 pins it on a recorded file, L4 on the owner's Docker
   version).
3. **Answering.** A probe of the local target behind the confirmed address:
   the serve entry's proxy target on the host for a host-network sidecar; the
   container's own address and port for a sidecar that shares its namespace; a
   published host port otherwise. The probe is one `GET` of the entry's path
   through `plainhttp.request`, which follows no redirect, uses no proxy, caps
   the body and sends exactly the headers given (source:
   `swfactory/plainhttp.py:1-18, 73-171`), with the confirmed name as `Host`
   and no cookie or credential; or a TCP connect for an app that is not a web
   page. Any HTTP answer counts as answering, a login page included; a 5xx is
   "answering with errors". When the target cannot be reached from the host (a
   shared namespace where the app listens only on its own loopback), the light
   says "can't be checked from the server" and uses the running check only.
   **For an app reached through the login gateway, the probe goes to the
   upstream** the route forwards to (the container's own address and port, or
   its published port), never to the route: oauth2-proxy answers 200, 302 or
   401 with its own sign-in before it touches the app, so a probe of the route
   would show a dead app as green (judgement, from how oauth2-proxy works;
   observed: 13 of the 14 route ports answered 200, 302, 307 or 401 to a
   visitor who had not signed in, and one was not probed; those codes do not
   show whether the app or the sign-in answered). An app whose upstream cannot be reached from
   the host says "can't be checked from the server", as above. When the owner
   asks for a probe of a home-network `https` route itself
   (`services.<app>.probe.target: "route"`), it needs `ca_file` set to the
   gateway's public certificate, never verification turned off. Python 3.13 and
   later build their default TLS context with `VERIFY_X509_STRICT` (docs: the
   Python 3.13 changelog for `ssl`; the probe uses
   `ssl.create_default_context(cafile=…)`, source: `swfactory/plainhttp.py:146`),
   so a self-signed certificate without the extensions strict mode requires
   (basicConstraints, keyUsage, subjectKeyIdentifier, authorityKeyIdentifier)
   fails on 3.13 and later while 3.10-3.12 accept it; the setup card reads the
   certificate's extensions and offers two ways out, a compliant certificate the
   owner issues once, or pinning the leaf certificate by its SHA-256
   (`probe.pin_sha256`), never `verify=false` (judgement; UNVERIFIED on the
   owner's certificate until it is read).
4. **Docker's own health check**, when the helper may make read-only Docker API
   calls (D3): `State.Health.Status`. Never from `config.v2.json`: a container
   whose check had failed 17 times in 35 seconds was "unhealthy" in the API
   while the file still said "starting" with a failing streak of 0 (verified:
   lab R7).
5. **Never waking anything.** An app that holds GPU memory, or that the owner
   marks as a model server, gets a TCP connect only, unless the owner names an
   HTTP path that does not load a model (v2 L76). No probe reads a file.
6. **Its Tailscale connection.** The sidecar running and its serve entry present
   give "Reachable from your Tailscale devices", labelled inferred: the server
   cannot test the phone's own path.

**The login gateway's own card.** The gateway is an app of its own (its stack),
with a light built from its members running: nginx, the sign-in service, its
database and each oauth2-proxy, because none of them has a Docker health check
(observed; judgement). One member down is red, "Your login gateway is down:
every app behind it is unreachable through it, though each still runs"; the
apps behind it keep their own lights from their upstream probes, so a gateway
outage never turns them red. Its card lists its routes, each with the app it
reaches and whether it asks for the sign-in, and nothing of its config beyond
the `ingress` verb's fields (§6.3).

**Expected state.** Each app has an expected state, `run` or `off`, kept in
`services.<app id>.expect`. It is proposed from the main member's restart policy
(`always`, `unless-stopped` and `on-failure` propose `run`; `no` asks) and
confirmed by the owner once, in the review list of §2.3; **It should run** and
**Keep it off** change it later (judgement). What stopped an app does not decide
its colour: an app that should run and does not is never shown as quietly off.

**The main member** is the member the confirmed address reaches; else the member
no other member depends on (the top of the `depends_on` graph); when several
qualify, the owner picks one on the app's page. Old copies and retired members
are left out of the light (§2.1), and so is a one-shot member that finished with
code 0 (restart policy `no`).

| Light | When | Reason the card shows |
| ----- | ---- | --------------------- |
| **Red** | expected to run, and the main member exited with a non-zero code without the "manually stopped" flag (under any restart policy, `no` included), keeps restarting (at least 3 times in 10 minutes), or is a `failed` unit | "Down: it stopped with an error at 14:02." / "Keeps restarting: 5 times in 10 minutes." |
| **Amber** | expected to run, and the main member has been stopped through Docker or with code 0 for longer than `expect.grace_minutes` (default 15), or was made and never started | "Stopped at 03:12, not from Seraphina. Should it be running? [Yes: start it in Dockge] [Keep it off]" / "Made but never started." |
| **Amber** | running, but 3 probes in a row failed or answered 5xx; Docker says unhealthy; or another member that should be running is down | "Not answering." / "Answering with errors." / "Works at home, but its Tailscale connection is down." |
| **Green** | running, and the last probe answered (or, where it can't be checked, running) | "Working." / "Running (can't be checked from the server)." |
| **Grey** | no fresh facts for `stale_minutes` (default 10), or the helper is unreachable and no cgroup answers | "Last checked 25 minutes ago." Green is never shown on stale facts |
| **Off** (no colour) | expected off, whatever its state; or the app is only a one-shot that finished with code 0 | "Off, as you chose." / "Finished (it runs once)." |

Probes run every 60 seconds per app with a little jitter, one at a time per
app, with a 5-second timeout, backing off to every 5 minutes after 10 failures
in a row. A red light on three checks in a row, or the amber "Stopped at …"
light once its grace has passed, opens one `service.down` incident for that app,
whatever stopped it; the first green after it sends "Working again" (source: the
notifier's incident pairing, `swfactory/notify.py:89-97, 2156-2320`); phone text
in §10.2. A stop inside a power hold that names the app (a backup job that stops
containers and starts them again under its hold, §6.2) counts from the hold's
end, so a backup that finishes on time opens nothing and one that leaves the app
stopped does (judgement).

### 2.6 Version and update available

Read-only, in three kinds (judgement for the wording):

| Kind | How | Card |
| ---- | --- | ---- |
| **Waiting in its folder** | the stack's compose file now names a different image, or its service config hash differs from the container's `com.docker.compose.config-hash` label. The helper runs Compose from its fixed plugin path, `<compose> --project-directory <dir> -f <file> config --hash '*'` and `config --images`, and returns service names, hashes and image names only, each checked against a strict shape; never the rendered config, which holds environment values, and never Compose's standard error, which can quote a line of a `.env` file (§6.3; source: Compose `v5.6.1` `cmd/compose/config.go:633-680` for `--hash`, `712-735` for `--images`; verified: both ran with no Docker daemon, and a malformed env file's line, value included, appeared on standard error, lab R23) | "Changed in its folder since it was started. Dockge's Deploy applies it." |
| **Downloaded, not in use** | the tag the container was made from now points to a different local image (someone pulled and did not recreate): the helper's read-only image list | "A newer copy is downloaded. Dockge's Deploy starts it." |
| **Pinned to a local copy** | the container references its image by id, and every tag on that image is a local one under the owner's keep-tag families (`yourserver.keep_tag_prefixes`, proposed from tags under `local/` such as `local/<family>/<app>:<date>`), so there is no upstream name to compare | "Pinned to a copy kept on this server on <date>; where it came from isn't recorded." with the image's `org.opencontainers.image.version` label when there is one. Upstream checks are off for it. These tags count as the owner's own keep tags: never trimmed by `docker.clean`, and shown as "previous version kept" (§7.3 M5; observed: about 32 containers ran such copies after a migration; judgement for the rule) |
| **Published upstream** | only when the owner allows network checks (D4): the harness asks the image's registry for the tag's current manifest digest, at most daily, through `plainhttp`, and compares it with the running image's repository digest. Off by default, because it tells each registry which images this server runs (judgement); registry rate limits for such reads are UNVERIFIED | "A newer version of <tag> is available. Update it in Dockge. The version you have now stays on the server; going back is an edit in Dockge. [How to go back]" |

An image pinned by digest never shows "update available" by tag; the card says
"Pinned to an exact version." A floating tag (`latest`, a major version) says
"Follows <tag>: the next update may change more than you expect" (v2 §6.5,
L54). The cockpit never pulls or recreates; "Update it in Dockge" links to the
stack's page (§2.7), and, when the `keep` class is on (D9), the current image
already carries a keep tag (§7.3), so the version before the update stays on
the server.

**How to go back** (judgement). Seraphina does not edit compose files (§1.4) and
Dockge has no rollback button, so going back is three steps the panel spells
out for each service, with a copy button and a link to the stack in Dockge:

1. "In Dockge, open the stack <stack> and press **Edit**."
2. "In the service <service>, replace the line `image: <current image line>`
   with `image: seraphina-keep/<project>-<service>:<yyyymmdd>-<12 hex>`."
3. "Press **Deploy**. While this line is in place, Dockge's **Update** shows an
   error, because the kept name exists only on this server. When you want
   updates again, put the first line back the same way." (Deploy is `compose up
   -d`, which pulls only an image that is missing; Update runs `compose pull`
   first; source: Dockge
   `backend/stack.ts:209-216, 446-465`.)

The panel also says what this does not do: "This puts back the app's program,
not its data. If the update changed the app's data (for example a database
upgrade), the old version may not be able to read it, and the data has no copy
until versioned backups exist." Without a keep tag the panel says "The previous
version is not kept" and offers none of this.

### 2.7 What manages it, and deep links

The badge answers v2 §3.2's question (who may write this app) from the evidence
of §2.1:

| Evidence | Badge (v2 state) | Deep link |
| -------- | ---------------- | --------- |
| a Compose project whose folder is a direct child of Dockge's stacks folder and still holds its compose file, and whose name is the one Compose gives that folder now (§2.1 rule 2) | "Managed by Dockge (stack <folder>)" (`owned:dockge`) | `<Dockge>/compose/<folder>` (source: Dockge `frontend/src/router.ts:30-63`; stacks are keyed by folder name and matched to projects by name, `backend/stack.ts:265-336`) |
| another project made from such a folder under a different name | "Made from the same folder as the Dockge stack <folder>, under another name. Dockge does not show it." (`unknown`) | Cockpit's terminal in that folder |
| a Compose folder elsewhere, still holding its file | "Compose folder outside Dockge. Dockge cannot see it." (`owned:compose`) | Cockpit's terminal opened in that folder, `<Cockpit>/system/terminal#/?path=<dir>` (source: Cockpit `pkg/systemd/terminal.jsx:32-42, 91-154`) |
| Compose labels whose folder or file is gone | "Made by Compose; its folder is gone." (`unknown`) | none |
| no Compose labels | "Started by hand." (`unknown`) | none |
| a systemd unit | "System service." (`owned:systemd`) | `<Cockpit>/system/services#/<unit>`; its logs `<Cockpit>/system/logs#/?service=<unit>` (source: Cockpit `pkg/systemd/services.jsx:661-673`, `service-details.jsx:467, 613`, `logsHelpers.js:32-73`) |
| two of these at once | "Two tools manage this app. Pick one before Seraphina changes it." (`contested`) | each |

Beszel adds "History in Beszel" on every card, linking to the host's page or
its containers page (§4.3).

**Where the recipe is.** Compose records the files a project was made from in
the `com.docker.compose.project.config_files` label, and that is the file the
helper reads, not a guessed `compose.yaml`: a recipe may be `compose.json`, and
two running projects on the reference server keep root-owned `compose.json`
recipes outside Dockge's folder (observed). Dockge's own project may live in
the owner's home folder, named after his account, on a floating tag; it is
recognised by its image repository (§2.1) and named "Dockge", never after the
folder (observed; judgement).

**Where the link bases come from.** `yourserver.links.dockge`, `.cockpit` and
`.beszel`, each proposed from the app the cockpit recognises as that tool
(Dockge and the Beszel hub by image repository, Cockpit by its `cockpit.socket`
unit and the address that socket listens on, because `cockpit-ws` itself runs
only while someone uses it; §6.3 `ports`) with that app's confirmed address, and
confirmed by the owner. View modules may hold no URL at all (source:
`tests/test_gui_static.py:24, 140`), so every link arrives as API data and
passes `SF.safeHref`, which accepts a single `http(s)` URL with no whitespace,
quotes, angle brackets, backticks or backslashes (source:
`swfactory/web/app.js:313-317`). Every link opens in a new tab with `rel="noopener
noreferrer"`. Dockge's page needs Dockge's own login (source: Dockge
`main-socket-handler.ts:30, 64, 122, 167`), and Cockpit's needs Cockpit's
(§9).

### 2.8 Ask about this app

The app's page answers three questions from facts first, then offers an AI
answer:

- **What is this?** Its name, image and version, what manages it, its parts,
  addresses and ports, the data folders it mounts, the networks it joins, its
  privileges in words ("can control Docker", "uses the server's network"), and
  the owner's own note; plus the matching Blueprint row when a capture exists
  (source: `indexes.containers`, `swfactory/blueprint/collect.py:1038-1081`).
- **Is it healthy?** The light's reason, restarts in the last day, the last exit
  code, Docker's health, the last probe answers, GPU memory, and the link into
  Beszel.
- **What changed?** The **app change log**, `<data_dir>/yourserver/changes.jsonl`
  (append-only, one line per observed change of image, config hash, mounts,
  ports, restart policy, privileges, members or address, never an environment
  value), with the tracker items linked to the app and the helper's receipts
  that name it. When two Blueprint captures exist, their diff for the app is
  shown too. This log is the launcher's own short memory until v2's Blueprint
  store exists (v2 §9.4), and then moves there (Appendix A6).
- **Ask the AI.** A question box. The context is the three answers above as
  typed fields: no logs, no environment, no file contents, and no free-text
  image labels (an image's description label is text its publisher wrote, so it
  is shown as data, never sent as context). It goes to the local model by
  default (D19), and the reply is labelled "AI, from the facts above".

### 2.9 The maintenance toggle and backup status

**The toggle** is `services.<app id>.maintenance.mode`: `inherit`, `manual`,
`scheduled`, `automatic`, `pinned` or `frozen`, with v2 §6.2's plain words. It
sits on the app's page, which shows the effective value and its source
("Scheduled (from the main setting)", "Frozen (set on this app)"). This design
updates no app, so a value reaches the card only when it does something today
(judgement):

| Value | What it changes in this design | On the card |
| ----- | ------------------------------ | ----------- |
| `pinned` | its keep tags are never trimmed (§7.3), and "update available" reads "Pinned: you chose to keep this version" | "Pinned" |
| `frozen` | from P2, Fix buttons for this app need your code ("only with me present", v2 §6.2) | "Frozen: Seraphina asks for your code first" |
| `manual`, `scheduled`, `automatic`, `inherit` | nothing yet: stored for when Seraphina updates apps, and labelled "Planned" on the app's page | "Updates for this app: not done by Seraphina yet" |

The same `services` section holds the server's own maintenance jobs under fixed
ids: `os-packages`, `os-cleanup`, `docker-cleanup` and `server-restart`, so the
global toggle and per-service toggles of DESIGN §31.3 cover the Maintain and Fix
buttons too (§7.5). The SnapRAID design's per-job overrides move here as
`snapraid-sync` and `snapraid-scrub` (Appendix A7).

**Backup status** says three things honestly (judgement):

- **Versioned backup:** "None yet" until v2's Backups work (restic behind an
  append-only server, v2 §5.2) exists; then the app's set, its last good copy and
  its last restore test.
- **Disk protection:** when the SnapRAID design is installed and one of the
  app's data folders lies on a protected data disk: "Disk protection: protected
  up to 08:42" (counts and times from the SnapRAID panel; the folder match is
  made by the harness from mount sources and the parsed disk list, and never
  shown outside the cockpit). It is never called a backup (BACKUPS_SNAPRAID §1.1).
- **Previous version:** "kept on the server (from 3 October). Going back is an
  edit in Dockge. [How to go back]" when a keep tag exists (§2.6, §7.3).

"Restore available" (v2 §2.1) is "partly (the previous version is kept on the
server, and going back is an edit in Dockge; its data has no copy)" or "no"
until versioned backups exist. Disk protection alone never turns the backup line
green.

### 2.10 Empty, stale and error states

| State | What the page shows |
| ----- | ------------------- |
| First visit, helper not installed | the host strip, then one summary card instead of app cards: "12 containers are running (postgres, node, nginx and 9 more), using 18 GB of memory. To show them as apps with names, addresses and who manages them, Seraphina needs its system access. [Set it up]" (§6.11). The process names come from `/proc/<pid>/comm` of each container's first PID, which holds no arguments and is readable without root unless `/proc` uses `hidepid` (Q16); container ids are never shown |
| Helper installed, nothing confirmed yet | the review list of §2.3: "Seraphina found 23 apps. Check the names and addresses below; the ticked ones look certain. [These are right]" |
| No apps found | "No apps found. Seraphina looks for Dockge stacks, other compose folders, containers and system services that you open in a browser." |
| Docker engine stopped | cards from the last facts, all grey: "Docker is stopped. Every container is off, and so is every Tailscale connection that runs in one." The helper does not start it (§6.4) |
| The helper refuses (`unsafe_install`) | the whole page grey with the file at fault: "Seraphina's system access is switched off because a file has the wrong owner." |
| The helper is not allowed to read (`allow` lacks `read`) | as "not installed", with the reason |
| An app with no address | "No address found. [Type the one you use]" |
| An app that answers on its own port, beside or instead of the gateway, with a page that opens without a password (a web file manager's remote-desktop page, with its own password behind it; a disk-protection app's web page) | the card names its exposure, any password behind the page, and what it can change: "Its page opens without a password from any address; a remote-desktop password stands behind it, and it can write your backup and parity disks." with a link to Check 6 and the root page's row (judgement; observed: such a file manager on every address, IPv4 and IPv6, its page answering without credentials with a remote-desktop password set, and a disk-protection web page on the home network while it existed) |
| An address changed outside Seraphina | the Open button waits: "This address was changed outside Seraphina. Is it still right?" |
| Stale numbers | the number in grey with its age; never a green light on stale facts |
| GPU driver not loaded | "GPU numbers unavailable: the driver is not loaded" (`nvidia-smi` exit 9; verified: lab R20; docs: exit codes 0, 2, 3, 4, 6, 9, 12, 15, 255 in the manual) |
| Hostile names | container names, labels and stack folders are shown as text, never as markup (the view modules' contract, §11.5); the mock seeds them (§11.6) |

---

## 3. The host strip

One line of server-wide numbers above the cards, the owner's "how much RAM was
left", read by Seraphina's own user without the helper unless noted:

| Item | Comes from | Refreshed | Shown as |
| ---- | ---------- | --------- | -------- |
| Free memory | `/proc/meminfo` `MemAvailable` ("an estimate of how much memory is available for starting new applications, without swapping") of `MemTotal` (docs: Linux `Documentation/filesystems/proc.rst:1106-1110`; verified readable as `nobody`, `LAB/logs/hostnums-nobody.json`) | 15 s while shown, else 5 min | "31 GB of 64 GB free" |
| Swap | `SwapTotal`, `SwapFree` | as above | only while swap is in use: "Using 2 GB of swap" |
| CPU | `/proc/stat` over two reads; `/proc/loadavg` | as above | "CPU 12%" |
| Up since | `/proc/uptime`, and the boot cause when the power schedule records it (v2 §6.3) | 5 min | "Up 9 hours (started by the morning wake)" |
| Free space per disk | `/proc/self/mounts`, one entry per device (one filesystem can be mounted under two names), real filesystems only (ext4, xfs, btrfs, zfs, vfat, fuse pools; never overlay, tmpfs, squashfs, proc, sysfs or cgroup); `os.statvfs` `f_bavail × f_frsize`, the space a normal user can use. `statvfs` needs search permission on every parent folder but none on the mount point itself, so a disk mounted under a private folder goes through the helper's `disks` verb (verified: as `nobody`, `statvfs("/var/lib/docker")` worked and `statvfs("/var/lib/docker/volumes")` failed with `EACCES`, lab R8). A mount point the cockpit's own unit covers (`InaccessiblePaths=`, `ProtectHome=` or `TemporaryFileSystem=` put an empty folder from `/run` over it; `/proc/self/mountinfo` shows the cover stacked on it) is never stat'ed, since the cover would answer: the helper's numbers when its namespace sees the disk (the PID 1 hand-off), else "cannot be seen from Seraphina's service" (judgement) | 60 s | "System disk 71 GB free", using the owner's label, else the mount point. A disk is keyed by its filesystem UUID, never by its `/dev` name, because NVMe device names swapped order between two boots (observed) |
| GPU per card | `nvidia-smi --query-gpu=index,uuid,name,memory.used,memory.total,temperature.gpu,utilization.gpu --format=csv,noheader,nounits`, which needs no root (docs: nvidia-smi 595 manual; only setters say "Requires root") | 15 s while shown | "GPU 1: 18 of 24 GB, 61 °C" |
| Temperatures | `/sys/class/hwmon/hwmon*/temp*_input` in millidegrees with a `name` per chip: `coretemp` or `k10temp` for the CPU, `nvme` for NVMe disks (docs: Linux `Documentation/hwmon/sysfs-interface.rst:251-262`; the lab had no sensors) | 60 s | "CPU 54 °C · NVMe 41 °C" |
| SATA disk temperatures | from the daily SMART read only, with its time (§7.2 Check 5), never from `drivetemp` | daily | "Disk 2: 34 °C this morning" |
| Beszel | §4 | 5 min | "History: Beszel, recording" |
| Restart needed | `/run/reboot-required`, world-readable, written by update-notifier's hook (source: `/usr/share/update-notifier/notify-reboot-required`) | 5 min | "Restart needed for updates" (§7.2 Check 2) |
| Power | the next power-off from `wakes.power` and active power holds (source: `swfactory/clock.py:190-258`; holds from the helper's `info`, §6.3) | 5 min | "Off at 23:00 · 1 job keeping it on" |

**Not waking disks.** Reading a SATA disk's temperature through the kernel's
`drivetemp` driver, `smartd` or `hddtemp` "may reset the spin down timer on some
drives" (docs: Linux `Documentation/hwmon/drivetemp.rst:36-48`), so the strip
never reads one. SMART runs once a day with `smartctl -n standby,3`, which exits
with code 3 before touching a sleeping disk (docs: smartmontools `smartctl.8.in:883-930`),
the same rule as the SnapRAID design's (BACKUPS_SNAPRAID §2.4).

**Tones.** A percentage alone would keep a large data disk that is full on
purpose amber for good: 5% of a 16 TB disk is still 800 GB. So a disk is amber
when its free space is below 15% of its size or 100 GB, whichever is smaller,
and red below 5% or 20 GB, whichever is smaller; a 465 GB system disk turns
amber below 70 GB, a 16 TB disk below 100 GB (judgement). Each disk can have its
own two numbers, or be marked **full on purpose**, which keeps only the red
floor. Disks in the SnapRAID array are marked full on purpose by default, and
their line links to that panel, which shows each disk's free space and the
parity's room (BACKUPS_SNAPRAID §2.1). Free memory below 10% is amber; a CPU above
90 °C, an NVMe disk above 70 °C or a GPU above 85 °C is amber (judgement; the
defaults live in `yourserver.host`, and the owner can change them). A red disk
opens a `disk.low` incident (an existing kind; source: `swfactory/notify.py:89`),
with the subject only when the owner allows disk names on his phone.

**No history here.** The strip keeps the previous reading of each number only as
long as the next one needs it for a rate (CPU). Graphs live in Beszel (§4).

---

## 4. Beszel: linked for history, watched for health, never duplicated

### 4.1 What stays in Beszel, and why the cockpit does not read its numbers

Beszel keeps the history: graphs, per-container numbers over time, its SMART
pages and its own alerts. The cockpit keeps no time series of any number
(T26 checks there is none in `<data_dir>/yourserver/`). It does not take its
"numbers now" from Beszel either, for two reasons (judgement): the cockpit's
numbers would go dark exactly when Beszel is down, which is the failure this
section exists for, and Beszel's agent reads container numbers through the
Docker socket (source: Beszel `agent/docker.go:657-690, 870`), which the cockpit
avoids. The cost is one more reader of cgroup files, which is cheap.

### 4.2 Watching it

Three checks, because each catches what the others cannot:

| Check | How | What it proves | Basis |
| ----- | --- | -------------- | ----- |
| The hub answers | an unauthenticated `GET <hub>/api/health`, which returns 200, every 5 minutes, to the hub's upstream: its container address and port (from the `containers` verb) when its confirmed address is a gateway route, else the local target behind that address. Never through the gateway, whose sign-in would answer for a dead hub (§2.5; judgement). The record reads of the third check go to the same upstream | the hub process serves HTTP | source: Beszel `internal/cmd/hub/hub.go:68-102`; `beszel health --url` does the same with a 3-second timeout |
| The agent runs | its container's cgroup has PIDs, or its unit is active, every 15 s with the other cards | the agent process exists. Its own `health` command proves no more: it only checks that a file the main loop touches every 90 seconds is fresh, whether or not the hub is connected | source: `agent/health/health.go`, `agent/connection_manager.go:195-220` |
| Data still arrives | with a read-only Beszel login (§4.4): the `systems` records' `status` (`up`, `down`, `paused`, `pending`) and `updated` time, every 5 minutes | the pipeline from agent to hub is recording | source: `internal/hub/systems/system_manager.go:27-32`; the records' list rules require a signed-in user, `internal/migrations/0_collections_snapshot_0_20_0.go` (list rules at lines 173, 567, 1305, 1940) |

The light: green when all three pass; amber "Beszel is running but has stopped
recording" when the hub answers and a system has not been `up` with a fresh
`updated` time for `stale_minutes` (default 15); red "Beszel is down" when the
hub has not answered twice in a row or the agent is not running. Without a
login, the third check is skipped and the card says: "Seraphina can see Beszel
running, but not whether it is still recording. [Give Seraphina a read-only
Beszel login]" (judgement).

**The 21-hour case.** The hub and agent both exited (code 137) and nothing said
so (judgement, from the private notes). Here the agent's cgroup disappears at
once (verified: a stopped container's cgroup vanishes, lab R7), the next
15-second read turns the Beszel card red, two failed hub checks open one
`service.down` incident with the subject `beszel` within 10 minutes, and the
first good check sends "Working again" (source: the notifier's incident pairing,
`swfactory/notify.py:89-97, 2272-2320`). T25 and U9 replay it. A kill that ends
in 137 usually sets Docker's "manually stopped" flag (§2.5), so a light that
trusted the flag would have shown "Off"; the Beszel card's own checks do not
read it, and for every other app the expected state of §2.5 gives the same
result: an app that should run and has been stopped for 15 minutes is amber and
opens an incident, whatever stopped it.

### 4.3 Links into Beszel

A system's page is `<hub>/system/<record id>`, where the id is the PocketBase
record id, not the system's name, with Beszel's `BASE_PATH` in front when it is
set; the containers page is `<hub>/containers` (source: Beszel
`internal/site/src/components/router.tsx:3-31`, `command-palette.tsx:71`,
`internal/hub/utils/utils.go:11-16`). With a login the cockpit reads the id
from the `systems` list and links each card's "History in Beszel" there; without
one it links to the hub's front page. Links keep the hub's confirmed address,
a gateway route included (`yourserver.links.beszel`), because they are opened
in the owner's browser, where the gateway's sign-in belongs; only the cockpit's
own checks go to the upstream (judgement).

### 4.4 The read-only Beszel login

Beszel has a `readonly` role, and its universal-token endpoints refuse that role
(source: `internal/hub/api.go:165-205`). So the owner creates one Beszel user for
Seraphina with the read-only role and gives it the server's systems; Seraphina
signs in with it through PocketBase's password sign-in and keeps the password in
the harness's secret store, bound to the hub's origin (v2 §9.5), never in
settings and never in a model's context (D7; judgement). What it can read is
metrics, so a leak of it reveals usage, not control.

**The steps the card shows** (the menu entries and fields are from source: Beszel
`internal/site/src/components/navbar.tsx:256-272`, which links **Users** and
**Systems** to PocketBase's own pages, and the `role` field with the values
`user`, `admin` and `readonly` and the systems' `users` field,
`internal/migrations/0_collections_snapshot_0_20_0.go:737, 867-876`; the
wording of PocketBase's buttons is UNVERIFIED, and P1a's novice check tries these steps on the owner's Beszel version):

1. "Seraphina made a password for this login. [Copy it]" (the harness makes a
   long random one and keeps it in its secret store at once).
2. "Open Beszel [link], open the menu at the top right and choose **Users**. A
   new tab opens; sign in there with Beszel's admin login if it asks."
3. "Press **New record**. Email: anything you like, for example
   `seraphina@localhost`. Password: paste the one you copied, twice. Role:
   **readonly**. Press **Create**."
4. "In the same menu choose **Systems**, open your server's row, add the new
   login under **Users** (leave the others there), and press **Save**."
5. "Back here, type the email you chose and press **Check**." Seraphina signs in
   and says "Seraphina can read Beszel: 1 system, recording." or the one thing
   that failed ("Beszel did not accept the password", "The login sees no
   systems: do step 4"). After a failed Check the password stays on the page to
   paste again; it goes once the login works.

### 4.5 Beszel's heartbeat does not fit the harness's hooks

Beszel v0.21 can send an outbound heartbeat (`HEARTBEAT_URL`, every
`HEARTBEAT_INTERVAL` seconds, default 60, `POST` by default) with a JSON body of
system counts and the names and hosts of systems that are down (source:
`internal/hub/heartbeat/heartbeat.go:22, 65-106, 139-230`). It sets only
`Content-Type` and `User-Agent` headers (source: the same file, `send`), while
the harness's event-source routes accept a token only as an `Authorization:
Bearer` header and refuse one in the URL on purpose (source:
`swfactory/server.py:819-846`). So Beszel cannot authenticate to the harness,
and this design does not weaken that rule for it: the cockpit polls (§4.2). The
heartbeat stays useful pointed at a check service off the machine, chosen with
the owner's consent, which is the only thing that notices the whole server
going down (v2 §6.6 heartbeat; D8).

---

## 5. Seraphina is never in the request path

Cosmos put its own reverse proxy and login in front of every app; Umbrel puts an
`app_proxy` container in front of each app that checks Umbrel's own auth service
by default (source: Umbrel `v1.0.4`
`packages/umbreld/source/modules/apps/legacy-compat/docker-compose.app_proxy.yml`;
Cosmos `v0.23.4` per v2 §3.1). Both make the dashboard a single point of
failure. This design keeps Seraphina beside the request path, never in it
(judgement), as requirements a test can check.

The owner may run his own login gateway in front of his apps (§1.2), and that
is his choice, not Seraphina's: the gateway runs in Docker, holds the sign-in
service's secrets and injects each app's tokens, and it dies with the Docker
engine. So the rule is restated next to it: Seraphina reads the gateway's
routes (§6.3 `ingress`), never writes its files, never reads them in full, is
never one of its upstreams, and is never a client of its sign-in service (N2,
N11). If the gateway changes or breaks, Seraphina keeps working and says that
its Open links through the gateway may fail (§10.4).

| # | Requirement | Checked by |
| - | ----------- | ---------- |
| N1 | The harness opens no listening socket for any app, and no port other than its own (`server.port`, and in v2 its own Unix sockets) | T27: the server's sockets after start, with the Your server area on |
| N2 | The harness never writes a Tailscale serve config, a sidecar's files, a compose file, a `.env`, Dockge's data, a DNS record or any file of the owner's login gateway (its nginx config, certificate and key, the sign-in service's config and database, oauth2-proxy's configs); no helper verb can (§6.6). The gateway's files are never read in full either: `ingress` returns only the fields §6.3 names | T6: the helper's verb table and argv builder; T27: a file-system watch on a fixture stacks folder during a full refresh |
| N3 | Every Open link points at the app's own address, never at the harness's host name, port or path | T15; U3 |
| N4 | The harness adds no login, cookie or header to an app's traffic; Open is a plain link with `rel="noopener noreferrer"` | U3 |
| N5 | With the harness stopped, every app answers on its address exactly as before | L3: stop the harness, probe a fixture app through its sidecar-style local target and its published port |
| N6 | With the helper removed, every app answers as before | L3, second half |
| N7 | Probes are read-only: one `GET` or a TCP connect, no credentials, no redirect followed, at most one a minute per app | T16 |
| N8 | Nothing the cockpit does depends on an app being reachable through Seraphina; a card for an app the cockpit cannot probe still opens it | U4 |
| N9 | This design adds no new way in to the harness itself: no new port, host name or serve entry for it | T27 |
| N10 | An app never shares the harness's host name on another port, because the `sf_token` cookie holds the master token and cookies are not isolated by port (v2 §7.2); when it does, that is a high finding on the card and in Check 6 (source: `swfactory/server.py` cookie, v2 §7.2). So the harness is never served on, and never allowed on (`server.host`, `server.allowed_hosts`), a home-network address that other web apps listen on, on any port: on the reference server that one address carries the gateway's routes and several apps' direct ports (observed). The doctor reports an error when it does (§11.7) | T15, T30 |
| N11 | Seraphina is never an upstream of the owner's login gateway, never a client of its sign-in service, and never behind its oauth2-proxy unless the owner decides so (D25). Even then the harness keeps its own session gate, and the identity headers oauth2-proxy sets are claims, never trusted as a login | T27; a doctor check that no gateway route's upstream is the harness's port (§11.7) |

---

## 6. `sf-system`, the system helper

### 6.1 Why a helper, which user, which unit

**What needs root.** Seraphina's own user can read cgroup numbers, `/proc`,
`statvfs`, `nvidia-smi` queries, `systemctl` lists and `apt-get -s` with no help
(§2.2, §3). These need root: Docker's state files, whose folders are mode 0710
and files 0600 under root (verified: lab R4); the owning process of a listening
socket (`ss -p` shows it only to root; verified: lab R18); SMART (devices are
`brw------- root`, and `smartctl` as `nobody` reports "Permission denied" with
exit status 2; verified: lab R19); `needrestart`'s service list (as non-root it
sees only its own processes; source: needrestart `v3.11` `needrestart:95-115,
1112`); `journalctl --disk-usage` (without read access it reports a silent 0B;
verified: lab R22); sidecar Tailscale state files (written 0600; source:
Tailscale `ipn/store/stores.go:145-226`); users' crontabs (verified: the folder
is `drwx-wx--T root:crontab`, lab R16); `sudo -l -U` for other users and `sshd
-T` (verified: run as root, lab R13, R15). The harness has no root path today:
`install.sh` never uses sudo, the service is a systemd user unit, and the only
sudo is the Blueprint's `sudo -n -- <allow-listed argv>`, which now stops at the
first refusal (source: `install.sh:3, 224-232`; `collect.py:449-506`).

| Option | For | Against | Chosen |
| ------ | --- | ------- | ------ |
| Put the harness's user in the `docker` group | no new code for containers | that is root-equivalent, and every agent running as that user would get the Docker API too: a read-only bind does not stop `connect()` on a Unix socket, and the sandbox masks do not cover the socket (source: `swfactory/config.py:398-446`; judgement) | **no** |
| A narrow root helper behind one sudoers line, shaped like `sf-snapraid` | small; no root process listening; each call short; one pattern for both helpers | sudo must be reachable (no `NoNewPrivileges`); long jobs must leave the caller's process tree | **yes** |
| A socket-activated root unit that checks `SO_PEERCRED` (v2's executor shape) | works under `NoNewPrivileges`; the v2 target | a root listener before the executor's review exists | later: the helper retires into the executor (Appendix A3) |
| The operator persona acts with her own root | — | she has no sudo grant now; other root paths may remain (§8.1 row 10), and none of them is a design for Seraphina | no |

**Which user calls it (D1).** Today the harness runs as the owner's login user
from a user unit (source: `systemd/factory.service`, `install.sh`). **On a
server where that account is in a root-equivalent group (`docker`, `lxd`,
`incus-admin`, `disk`, `sudo`), this route is not allowed at all**, before any
helper is installed: `bwrap` does not contain such an account. For `docker`,
`lxd` and `incus-admin`, a read-only bind of `/` does not stop a program from
connecting to the engine's socket, and that socket is root. For `sudo` and
`disk`, bwrap itself stops setuid sudo (no new privileges) and shows no disks
in its `/dev`, but the harness and whatever it starts outside the sandbox (a
CLI driver, or every agent under `harness.sandbox: "none"`) keep the account's
rights (judgement; DESIGN §17). On the reference server the owner's account is in
`docker` and `lxd` and does not linger, so a user unit would not even start at
boot (observed). There the system user `seraphina`, outside those groups, is a
precondition of P1a, the helper's `setup` refuses a `callers` uid in any of
those groups, and the doctor reports an error while the harness runs as such an
account (§11.7). The rest of this paragraph applies only to a server where the
owner's account holds none of them. The
sudoers line of §6.11 gives that account the helper with no password, while the
owner's own sudo asks for his. So every process of that account, the agents
included while their sandbox is off (`harness.sandbox: "none"` is the shipped
default; source: `swfactory/config.py:110`), and anything that ever runs under
it, can ask the helper as root without anyone typing a password (judgement).
That is a real widening, and this design limits it in three ways:

- **Only the server's layout for a login account.** The `read` class returns
  structure: container names, images, mounts and privileges, ports with process
  names, tailnet names and serve targets, disks, SMART, update lists, sizes.
  Most of it is what the cockpit's own record holds anyway (§2.3). The helper
  answers it to the harness's current account in P1a and P1b.
- **Private reads and every change only for Seraphina's own user.** The
  `private` class (one member's logs, the root-access enumeration and its
  details, the receipts and tickets of access changes) and the `maintain`,
  `fix` and `access` classes are refused by the helper itself to any caller
  that is a person's login account: a uid at or above `UID_MIN` in
  `/etc/login.defs`, or an account with a login shell (§6.7 item 1). This is
  enforced root-side, so a harness running as the owner cannot switch it off.
  `refresh` (package lists) and `keep` (adding keep tags, never removing one)
  stay open to it, because neither reveals or removes anything.
- **Log pages redacted inside the helper**, before they leave root (§7.2 Check
  8), so even Seraphina's own user never receives a raw token from a log.

So before P1c, the harness moves to its own system user `seraphina` (v2 §9.2),
the sudoers line names that user only, and the helper's config pins it by uid
(`callers`, §6.11). The reasons go beyond agents: the owner asked for root
access "separate to mine or Kate's"; receipts must say who acted, and a helper
cannot tell the owner's own shell from the harness when both are one user. The
owner's account and the operator persona's account get nothing from this helper
by default (D2). Until the move, the owner can still see the root page in his
own terminal: `sudo /usr/local/libexec/seraphina/sf-system explain access`
runs the enumeration (a few minutes) and prints the page in the helper's own words, through his own sudo and password. Moving
the harness is v2 §9.2's migration (its data folder, its repositories, the CLI's
login); this design needs only its first step, and lists it as a P1c
precondition (§11.10).

**Which unit.** sudo refuses to run under `no_new_privs` ("The "no new
privileges" flag is set, which prevents sudo from running as root."; verified:
lab R14, which settles the SnapRAID design's UNVERIFIED claim). So the harness's
unit must not set `NoNewPrivileges=yes` while it uses a sudo helper, as
BACKUPS_SNAPRAID A15 says. A second constraint is new here (judgement; docs:
systemd.exec, UNVERIFIED): a program started through sudo stays in the caller's
mount namespace, so a hardened harness unit (`ProtectSystem=strict`,
`TemporaryFileSystem=` over the data roots, v2 §9.2) would make the helper's
writes fail and hide what it reads. So every verb that writes runs in a
transient unit started through PID 1, which runs in the host's namespace (§6.8),
and once the harness runs in a hardened system unit, the read verbs run that way
too (`systemd-run --wait --pipe --collect --quiet`), not as direct children. L8
checks both in a systemd-booted lab.

### 6.2 Exactly like `sf-snapraid`, except where it must differ

| Part | `sf-snapraid` (BACKUPS_SNAPRAID §3) | `sf-system` |
| ---- | ----------------------------------- | ----------- |
| File | `bin/sf-snapraid`, installed at `/usr/local/libexec/seraphina/sf-snapraid`, `#!/usr/bin/python3 -I`, never imported by the harness | `bin/sf-system` at `/usr/local/libexec/seraphina/sf-system`, the same |
| Sudoers | `Defaults!<path> !use_pty` and `<harness user> ALL=(root) NOPASSWD: <path> ""` in `/etc/sudoers.d/seraphina-snapraid` | the same two lines in `/etc/sudoers.d/seraphina-system` (D17 asks whether both helpers share one file) |
| Request | one JSON line on standard input, at most 16 KiB, `v: 1`, a known verb, only that verb's fields, unknown fields refused, ids of fixed shapes opened with `dir_fd` and `O_NOFOLLOW` | the same (§6.7) |
| Refusals | `{"ok": false, "error": <code>, "words": <plain sentence>}`; exit 0 answered, 2 refused, 1 own failure | the same |
| Argument modes | `run <job id>` only inside its own unit (checked through `/proc/self/cgroup`); `explain helper-config\|snapraid-config` through the owner's sudo | `run <job id>` the same; `explain helper-config\|approval\|access\|ingress\|ticket <id>`; plus `setup`, `remove`, `enrol`, `approve <ticket>` and `confirm <ticket>`, which need a terminal and an owner's sudo (§6.9, §6.11) |
| Installation check | on every call, the helper, its folder and every parent, the tool binaries and its config are root-owned and not group- or world-writable, else `unsafe_install` | the same, over its own binaries list, Compose's plugin file included (§6.6) |
| Config | `/etc/seraphina/snapraid-helper.json`, `allow` classes, installed through install, explain, move | `/etc/seraphina/system-helper.json`, the same; adds `callers` and `owners` (§6.11) |
| Install | copy blocks, one per file: install to `.new`, check or explain, move | the same steps, run by the helper's own `setup` mode from one self-checking paste, which writes the sudoers file from its own template; the separate blocks stay under Details (§6.11). Proposed for `sf-snapraid` too, so the two install alike (A17) |
| State | `/var/lib/seraphina-snapraid/` (root, 0700), no secrets | `/var/lib/seraphina-system/` (root, 0700); holds one secret, the approval key (§6.9) |
| Long jobs | transient units, `--collect`, `KillSignal=SIGINT`, `SendSIGKILL=no`, output to 0600 files, never the journal | the same; package jobs are never interrupted once `dpkg` starts (§7.3) |
| Power holds | `/run/seraphina/holds/<holder>-<job id>.json`, `{id, holder, why, until, max_delay_minutes, resources}`, plus `systemd-inhibit` | the same folder and shape, holder `system`; the restart waiter reads every holder's records (§7.4) |
| Never | SIGKILL; a shell; a caller-chosen path, binary or config | the same, and §6.6 |
| Removal | `sudo rm` of three files, plus the state folder if wanted | the same three, plus P3's boot unit; one of them, the sudoers file, alone ends all its root access (§6.11) |
| Callers | does not tell the harness user from the owner's login user | knows its caller by uid (`SUDO_UID`, which sudo sets), refuses a uid the config does not name, and refuses the `private`, `maintain`, `fix` and `access` classes to a login account, because only Seraphina's own user may have them (§6.1, §6.7) |
| Receipts | job records | a receipt per change with its way back, and `rid` idempotency (§6.8, §6.10) |

The harness side gets one shared client for both, `swfactory/helpers.py`
`HelperClient`, which calls `sudo -n <helper>` through an injectable runner like
the Blueprint collector's (source: `collect.py:365-405`), writes the request,
reads one JSON line, maps exit codes, and never retries a changing request
(§6.8).

### 6.3 The verbs

`Runs` says where the work happens: *direct* means as the helper's own child
under sudo, with a timeout (30 s for most reads, 120 s for SMART and the
compose checks, 10 minutes for the root enumeration); *job* means in a transient
unit (§6.8). Every argument list is built by the helper from this table and the
request's typed fields.

| Verb | Phase | Allow class | What the helper does | Runs | Class (v2 §5.8) |
| ---- | ----- | ----------- | -------------------- | ---- | --------------- |
| `info` | P1a | read | its version, config verdict, installation check, `allow` and `callers`; whether `docker.service` is active; active power holds; the boot id and running kernel (recorded in `boots.jsonl`); pending jobs, tickets and undo timers; whether an approval key is enrolled; the last 20 receipt ids | direct | read |
| `containers` | P1a | read | the container map from Docker's state files (§6.4) | direct | read |
| `docker` | P1b | read | one of a fixed set of read-only Docker API calls, only while `docker.service` is active and `docker_api` is on (§6.4, D3) | direct | read |
| `compose` | P1a | read | for one stack folder, Compose run standalone from its fixed plugin path (`/usr/libexec/docker/cli-plugins/docker-compose`, with `DOCKER_CONFIG` set to an empty root-owned folder): `--project-directory <working_dir> -f <file> config --hash '*'`, where `<file>` is each file the container's `com.docker.compose.project.config_files` label names (a `compose.json` included, §2.7), walked without following links, `config --images`, and `config --format json`, of which only the top-level `name` is kept and the rest is dropped in memory. Standard error goes to nothing: it is never returned, logged, stored or shown. Every output line must match its shape (a service name and 64 hex; an image reference; a project name) or the whole answer is `compose_parse_failed`, a fixed error with no tool text, and a non-zero exit gives the same (§2.6; verified: standalone use with no daemon, and what standard error can quote, lab R23) | direct | read |
| `ports` | P1a | read | `ss -Htlnp` and `ss -Hulnp`, parsed by columns (iproute2's `ss` in the lab has no JSON output; verified: lab R18), each PID joined to its container or unit through `/proc/<pid>/cgroup`; and `systemctl list-sockets --all --output=json`, which needs no root and maps each listening address to its socket unit and the unit that socket starts, so a socket-activated service (Cockpit, SSH) is named even while only PID 1 holds the socket. When `ss -p` names several processes for one socket, one other than PID 1 wins (source: systemd `v255` `src/systemctl/systemctl-list-units.c:410-452`, the `listen`, `type`, `unit` and `activates` columns; not run in the lab, which had no systemd; T18 fixtures, L15) | direct | read |
| `ingress` | P1a | read | the owner's login gateway's route table, from the nginx config the gateway container binds (found through the `containers` verb, by image repository or the owner's mark), parsed by a strict-shape reader like the `compose` verb's: per `server` block only `listen` (address and port), `server_name`, each `location` path, its `proxy_pass` target or named `upstream` servers, and whether it has `auth_request`. Never `proxy_set_header` values, `include`d secret files, certificates or keys, the sign-in service's config (`dex.json` and the like), oauth2-proxy's configs or the database; a config it cannot parse in that shape is `ingress_parse_failed`, a fixed error with no file text. Answers are routes `<address:port><path>` to an upstream, with `auth: sign-in | none` | direct | read |
| `gpu` | P1a | read | `nvidia-smi --query-compute-apps=pid,process_name,used_gpu_memory,gpu_uuid --format=csv,noheader,nounits`, only if the harness's user cannot see other users' processes (Q4) | direct | read |
| `tailscale` | P1a | read | each sidecar's name, tailnet, serve entries, TCP forwards and Funnel state, decoded from its state file (§6.5); for a host tailscaled, `tailscale status --json` and `serve status --json` | direct | read |
| `disks` | P1a | read | `lsblk -J -b` and `statvfs` for filesystems under private folders (a mount point covered in its namespace is left out) | direct | read |
| `smart` | P1b | read | per disk, `smartctl -H -A -j -n standby,3 [-d <type>] <device>`, the type only from a fixed list (§7.2 Check 5) | direct | read |
| `needrestart` | P1b | read | `needrestart -b -r l`, parsed (§7.2 Check 2) | direct | read |
| `journal` | P1b | read | `journalctl --disk-usage`, and the names, sizes and times of archived journal files | direct | read |
| `logs` | P1b | private | one page of one member's logs, redacted by the helper itself before it answers (§7.2 Check 8) | direct | read |
| `du` | P1b | read | the biggest folders on one filesystem, two levels deep: `du -x -d 2 -B1`, under `nice` and `ionice -c3`, with a 120-second timeout | direct | read |
| `access` | P1c | private | the who-can-act-as-root enumeration (§8.1), its result kept root-side | job (minutes) | read |
| `job`, `log`, `receipts`, `tickets` | P1b | read; `private` for the receipts and tickets of access changes | one job record or its progress; one page of a job's output; receipts by id, by `rid` or newest first; pending tickets | direct | read |
| `apt.refresh` | P1b | refresh | `apt-get update`, which writes the package lists and runs apt's own hooks as the daily timer does (§7.2 Check 1) | job | read: it changes only the package lists |
| `keep.images` | P1b | keep | tag each app's current image `seraphina-keep/<project>-<service>:<yyyymmdd>-<12 hex>`; it never removes a tag (trimming happens only inside `docker.clean`, §7.3 M5) | direct | `undo_instantly` (a tag; its undo is removing it) |
| `apt.upgrade` | P2 | maintain | install the approved `<package>=<version>` pairs that are still upgradable at exactly those versions, security only or all, after taking a packages restore point and passing the way-back check in the same job (§7.3 M1, M2) | job | `undo_by_restore` |
| `apt.remove` | P2 | maintain | remove exactly the listed auto-removable packages (no purge), after keeping their `.deb` files and passing the way-back check (§7.3 M3) | job | `undo_by_restore` |
| `apt.restore` | P2 | maintain | the way back of either: reinstall the kept `.deb` files of one receipt (`--allow-downgrades`), after its own simulation shows exactly those downgrades | job | `undo_by_restore` |
| `apt.restore_check` | P2 | maintain | the way-back check of §7.3 for one receipt's kept files, installing nothing | job | read: it writes only in its own state folder |
| `journal.vacuum` | P2 | maintain | `journalctl --vacuum-time=<N>d`, which removes archived files only (§7.3 M4) | job | `cannot_undo` |
| `docker.clean` | P2 | maintain | a keep pass, the list from `GET /images/json` and `GET /system/df`, then `POST /images/prune` (dangling only) and `POST /build/prune` (default, not `--all`) (§7.3 M5) | job | `cannot_undo` |
| `restart` | P2 | fix | restart one running unit (`systemctl restart -- <unit>`) or one running container (`POST /containers/<id>/restart?t=30`), after checking inside the job that it is still running; never a stopped member, because Docker's restart starts a stopped container (§7.4 F1) | job | `undo_instantly` |
| `reboot`, `reboot.cancel` | P2 | fix | arm, or cancel, a restart that waits for every power hold and long job (§7.4 F2) | job | `undo_instantly` until it starts; the `management_plane` flag |
| `access.ticket` | P3 | access | record one root-access change for the owner to approve in his terminal (§8.4) | direct | `undo_instantly` with the `management_plane` flag |
| `job.cancel` | P2 | the job's class | cancel a job that has not reached its point of no return (§6.8) | direct | — |

`keep.images` is protective: it only adds names. When the kept versions pass the
root-side limit, the card says so ("Kept versions use 52 GB, over your 50 GB
limit"); only `docker.clean` trims them, with its own confirmation. D9 asks whether to
switch the `keep` class on in P1b, so that Dockge's updates never leave a
previous version unprotected.

### 6.4 Reading containers without the Docker socket

1. **Running containers** come from the cgroup scan of §2.2, with no helper.
2. **The map** (id to name, image, Compose labels, privileges, mounts, ports,
   restart policy, last exit) comes from the helper's `containers` verb, which
   reads `<data root>/containers/<id>/config.v2.json` and `hostconfig.json` for
   every id folder (verified: the fields `Name`, `Config.Image`, `Image`,
   `Config.User`, `Config.Labels`, `State.Running/Pid/StartedAt/ExitCode/FinishedAt`,
   `MountPoints`, `Privileged`, `CapAdd`, `CapDrop`, `SecurityOpt`, `Binds`,
   `PortBindings`, `NetworkMode`, `PidMode`, `Devices`, `RestartPolicy`; lab R4,
   R6). The data root is `data-root` in `/etc/docker/daemon.json`, else
   `/var/lib/docker`, and must pass the installation check. The harness calls it
   when the set of running ids changes and every 5 minutes.
3. **What it returns** (judgement): the fields above, labels only from an allow
   list (`com.docker.compose.*`, `org.opencontainers.image.{version,title,source,revision,created}`
   and `seraphina.*`), mounts as type, source, destination and read-only flag,
   networks with their addresses, and `Config.User`, where `""` or `"0"` means
   root inside the container (verified: the image's `USER` is copied there when
   `-u` is not given, lab R6). Never `Config.Env` (except the keys of §6.5 and
   Dockge's `DOCKGE_STACKS_DIR` and `DOCKGE_ENABLE_CONSOLE`), never `Cmd` or
   `Entrypoint` (arguments can carry secrets), never the log.
4. **These files are Docker's private checkpoint, not an API**, and they lag for
   health (§2.5). A contract test per Docker version pins the fields read (T4,
   recorded from the lab's 29.9.1 files with ids and paths replaced), and the
   helper reports `docker_format_unknown` rather than guessing when a file does
   not parse.
5. **Read-only Docker API calls, when allowed (D3).** The helper is already
   root, so a fixed read-only call through the socket gives it nothing new; the
   rule that matters is that Seraphina's own user never holds the socket
   (judgement). The allowed calls: `GET /containers/json?all=1`, `GET
   /containers/<id>/json` (health only is returned), `GET /images/json`, `GET
   /system/df` (Check 3), `GET /info`; and for the `keep`, `maintain` and `fix`
   classes only, `POST /images/<id>/tag` (keep), `DELETE /images/seraphina-keep/<name>:<tag>`
   (maintain, for trimming), `POST /images/prune` with no filters (dangling only), `POST /build/prune`
   without `all`, and `POST /containers/<id>/restart` only for a container that
   `GET /containers/<id>/json` shows running or restarting just before, in the
   same job, because a restart starts a stopped container (source: Moby
   `daemon/restart.go:58-69`, which stops only a running container and then
   always starts it). The helper speaks HTTP/1.1 over the fixed path
   `/run/docker.sock` itself, caps answers at 8 MiB, and never calls anything
   else; the build-cache list comes from `GET /system/df`, whose records carry
   `Type`, `InUse` and `Shared` (source: Moby `api/types/build/cache.go:8-20`),
   not from `docker buildx du`.
6. **Never starting Docker.** Connecting to the socket while `docker.service` is
   stopped would start it through socket activation, so the helper asks
   `systemctl is-active docker.service` first and answers from the state files
   alone when it is not active, the same rule the Blueprint already follows
   (source: `collect.py:725-740`).

### 6.5 Reading Tailscale sidecars

1. **Finding them.** A container whose processes include `tailscaled` or
   `containerboot` (from `/proc/<pid>/comm` for the PIDs in its cgroup, readable
   without root), or whose image repository is `tailscale/tailscale` or
   `ghcr.io/tailscale/tailscale` (judgement).
2. **Five environment keys, nothing else**: `TS_HOSTNAME`, `TS_STATE_DIR`,
   `TS_SERVE_CONFIG`, `TS_USERSPACE` and `TS_SOCKET` (source: containerboot's
   environment block, Tailscale `cmd/containerboot/main.go`, `settings.go:112,
   125`). Never `TS_AUTHKEY`, `TS_AUTH_KEY` or `TS_EXTRA_ARGS`, which can carry
   an auth key. A sidecar may set none of them: on the reference server seven of
   eight sidecars had only `PATH` in their environment (observed), so nothing
   below depends on them; they only add a name or a serve file when present.
3. **The state file.** The state folder is the destination of the container's
   mount at `/var/lib/tailscale` (a bind source, or a named volume's mount
   point), else the folder `TS_STATE_DIR` names inside the container, mapped to
   a host path through the container's mounts (observed: all eight sidecars
   kept their state at `/var/lib/tailscale`, in binds or named volumes). The
   node's name is read from the state's current profile, never from
   `TS_HOSTNAME`, which may be absent or stale. The helper walks to `<dir>/tailscaled.state` one component at a time
   with `dir_fd` and `O_NOFOLLOW`, as `sf-snapraid` does on the array, because a
   container can write that folder (judgement). The file is a JSON map of keys to
   base64 bytes (source: `ipn/store/stores.go:145-226`); the helper decodes only
   `_current-profile`, `_profiles` (each login profile's `NetworkProfile.MagicDNSName`
   and `DomainName`) and `_serve/<current profile id>` (the applied serve config)
   (source: `ipn/store.go:53, 57`, `ipn/prefs.go:1052-1056`,
   `ipn/ipnlocal/local.go:1992`). Every other key, including the node's private
   keys, is never decoded, copied, logged or returned; T9 checks it with key
   material planted in a fixture.
4. **Without a state file** (state kept in memory, or a volume the helper cannot
   map; none on the reference server, where every sidecar's state was persistent,
   Q2), it reads the `TS_SERVE_CONFIG` file instead, which holds no secrets but
   often holds `${TS_CERT_DOMAIN}` in place of the name (source:
   `cmd/containerboot/serve.go:30-35`), and the owner confirms or types the name
   (Q2).
5. **A host tailscaled**, when one exists, answers `tailscale status --json` and
   `serve status --json` to any local user (§2.3 source 2); the harness reads it
   directly.

### 6.6 What the helper never does

- **No shell and no free-form command.** Every argument list comes from §6.3.
- **No caller-chosen binary, path or config.** Binaries come from a fixed list
  written in its code, one root-owned `/usr/bin` or `/usr/sbin` path per tool it
  uses: `systemctl`, `systemd-run`, `systemd-inhibit`, `loginctl`, `apt-get`,
  `apt-cache`, `apt-mark`, `dpkg`, `dpkg-query`, `dpkg-deb`, `dpkg-repack`,
  `journalctl`, `smartctl`, `needrestart`, `ss`, `nvidia-smi`, `du`, `nice`,
  `ionice`, `lsblk`, `find`, `getcap`, `getent`, `passwd`, `sshd`, `sudo`,
  `cvtsudoers`, `visudo`, `tar` and `tailscale`, plus one plugin file, Compose
  at `/usr/libexec/docker/cli-plugins/docker-compose` (the path Docker's
  packages install it to; verified: lab R23), run by that path and never through
  the `docker` command, whose plugin search reads several folders and root's own
  Docker config. Each is checked like the helper's own file, and a tool that is
  missing is reported, never searched for. Account files are edited by the
  helper itself, one line at a time (§8.4), not through `usermod`, `gpasswd` or
  `chage`, whose "undo" is not exact (verified: lab R24).
- **No Docker writes beyond §6.4 item 5.** Never `run`, `create`, `exec`,
  `start`, a restart of a container that is not running, `stop`, `rm`, `pull`, a container, network or
  volume prune, `system prune`, `image prune -a`, or removing an image by id
  taken from a dangling list: after a pull the dangling list can show the id of
  a tagged rollback copy, and `docker rmi -f $(docker images -f dangling=true -q)`
  would delete it (verified: lab R11).
- **No reading beyond the fields of §6.4 and §6.5.** No container environment
  beyond the named keys, no `Cmd`, no application data, volumes or databases;
  compose and `.env` files only through Compose's `config`, of which only the
  hashes, image names and project name leave the helper, and whose standard
  error is discarded unread, because it can quote a line of an env file, value
  included (verified: lab R23; §6.3 `compose`); no file contents under `/home` except the `authorized_keys` files
  it parses for fingerprints and options (§8.1), and never the whole of a
  Tailscale state file in an answer.
- **No tool's own words in an answer** where a tool reads files that hold
  secrets: a failure of Compose, `sshd -T` or `cvtsudoers` is a fixed typed
  error, and its standard error is not kept (judgement).
- **No change to how anyone reaches anything.** No Tailscale, serve, firewall,
  network, DNS, compose, Dockge or sidecar change (§5 N2).
- **No kernel, boot, GPU driver, Docker engine, Tailscale, SSH server or
  Seraphina package** in `apt.upgrade` or `apt.remove` unless §7.6 allows it.
- **No purge, no `apt-get clean`, no `autoclean`.** Both cleans delete cached
  `.deb` files, the second exactly the old versions the archive no longer offers
  (judgement from apt-get(8)); kept files live outside the apt cache (§6.12).
- **No root password, no root account lock, no change to the owner's own
  access, no removal of the last admin** (§8.5).
- **No change to its own sudoers line or config on a request**: those change
  only through `setup` and `remove`, which the owner runs with his own sudo in a
  terminal and which the harness's no-argument sudoers line can never reach
  (§6.11).
- **No SIGKILL**, and no interrupting `dpkg` (§7.3).
- **No second changing job** at a time (`flock` on `/run/seraphina-system/job.lock`).

### 6.7 How requests are checked

The helper refuses, before starting anything, with a typed error (judgement for
every shape):

1. **The caller.** The sudoers line allows no arguments, and the helper accepts
   only its argument modes (§6.2). For a request on standard input, `SUDO_UID`
   must be a uid in the config's `callers`, and the verb's allow class must be in
   both `allow` and that caller's list. The `private`, `maintain`, `fix` and
   `access` classes are refused, with `caller_is_login_account` ("Seraphina's
   own account must ask for this; it is still running as a person's account."),
   when the caller's uid is at or above `UID_MIN` in `/etc/login.defs` or its
   `getent passwd` shell is not `/usr/sbin/nologin`, `/sbin/nologin` or
   `/bin/false`, whatever the config says (§6.1). Every class is refused, with
   `caller_holds_root` ("Seraphina's account is in a group that already
   controls the server; take it out first."), when the caller is in `docker`,
   `lxd`, `incus-admin`, `disk` or `sudo`: such a caller needs no helper, and
   its agents would hold root anyway (§6.1; judgement).
2. **The request.** One line, at most 16 KiB, JSON, `v: 1`, a known verb, only
   that verb's fields, each of the right type; unknown fields refused.
3. **Shapes.** A container id `^[0-9a-f]{64}$` that exists in the state files; a
   unit name `^[A-Za-z0-9:_.@][A-Za-z0-9:_.@-]{0,239}\.(service|socket|timer)$`
   (no leading `-`) that systemd has loaded, and that always goes into an
   argument list after a literal `--` (`systemctl restart -- <unit>`) or as one
   `--unit=<unit>` argument (`journalctl`), so it can never be read as an
   option; a user name `^[a-z_][a-z0-9_-]{0,31}$` that `getent passwd`
   knows; a group from a fixed list; a key fingerprint `^SHA256:[A-Za-z0-9+/]{43}$`;
   a stack folder that is a direct child of the confirmed stacks folder and
   matches Dockge's own rule `^[a-z0-9_-]+$` (source: Dockge
   `common/util-common.ts:110-115`), or a working directory some container's
   label names exactly; package names `^[a-z0-9][a-z0-9+.-]+$` and versions
   `^[A-Za-z0-9.+~:-]+$`; a list hash, 64 hex; a `rid`, 32 hex; ids `J-`, `R-`,
   `T-` followed by 1-9 digits, opened inside their own folder with `dir_fd` and
   `O_NOFOLLOW`; a code, 6 digits; offsets and line counts within fixed bounds.
4. **The installation**, on every call (§6.2).
5. **The plan still matches.** For a press of the button now, the helper
   recomputes the list (`apt-get -s`) and refuses with `list_changed` when its
   hash differs from the `list_sha256` the owner saw. For an approval ahead of a
   maintenance time, which is a set of exact `<package>=<version>` pairs, it
   installs only the pairs still upgradable at exactly those versions, skips
   those already installed, adds nothing, and refuses only when the simulation
   of that subset pulls in anything else (§7.3, §7.5).

### 6.8 Answers, jobs, outcomes and the shared outcome rule

- **Direct verbs** answer with one JSON line; **job verbs** answer at once with
  `{"ok": true, "job": "J-0042"}` and are followed with `job`.
- **Jobs run in transient units**, started through PID 1 so that a harness
  restart, crash or upgrade touches neither (judgement; source for the pattern:
  BACKUPS_SNAPRAID §3.6):

  ```
  systemd-run --unit=seraphina-system-<verb>-<job id> --collect
    --property=Nice=10 --property=IOSchedulingClass=idle
    --property=ProtectHome=read-only --property=PrivateTmp=yes --property=UMask=0077
    --property=KillSignal=<SIGINT, or SIGCONT for package jobs> --property=SendSIGKILL=no
    --property=TimeoutStopSec=<per verb>
    --property=StandardOutput=file:/var/lib/seraphina-system/runs/<job id>/out
    --property=StandardError=file:/var/lib/seraphina-system/runs/<job id>/out
    [--property=PrivateNetwork=yes  for every verb that needs no network]
    /usr/local/libexec/seraphina/sf-system run <job id>
  ```

  `apt.refresh` and `apt.upgrade` need the network; nothing else does. A job
  takes a power hold for as long as it runs (§6.2). Whether `systemd-run` works
  from a hardened caller and `file:` output lands as written is checked in L8
  and L9 before P2 ships (docs: systemd-run, systemd.exec; UNVERIFIED here).
- **Outcomes**: `ok`, `failed` (with the first error line), `refused`, `busy`,
  `interrupted` (stopped before its point of no return), `unknown`.
- **Every changing request carries a `rid`**, 32 random hex characters the
  harness makes per press of a button. The helper creates `rids/<rid>.json`
  exclusively and with `fsync` before it does anything else, and holds a lock on
  it while it works. A second request with the same `rid` returns the first
  record and runs nothing (judgement).
- **The shared outcome rule** (DESIGN §37 rule 5): an outcome that is unknown is
  never replayed. When the harness's call to sudo times out, or the connection
  is lost after the request was written, the harness marks the action `unknown`
  (the same `sent` meaning as `plainhttp` and the custody client; source:
  `swfactory/plainhttp.py:15-17`, `swfactory/custody.py:243-250`) and asks
  `receipts {rid}`. A record found gives the real outcome. No record, while no
  helper holds that `rid`'s lock, proves the request never started, and the
  card then offers the button again as a person's choice. Nothing is ever
  retried automatically.

### 6.9 Approvals before the executor

v2 puts every approval on the executor's own safety page (v2 §9.3), which does
not exist yet. Until it does, this design uses four kinds, and says what each
proves (judgement):

| Kind | Used for | How | What it proves | What it does not |
| ---- | -------- | --- | -------------- | ---------------- |
| none | Check | — | — | — |
| a click | Fix restart of a running app that is not Frozen; keep tags | the button | that the harness asked | that a person pressed it: while agents run unsandboxed they can ask too (§10.3) |
| **your code** | every Maintain button; the server restart; Fix for a Frozen app | a 6-digit TOTP code (RFC 6238, HMAC-SHA1, 30-second steps, one step of drift either way) from the owner's authenticator app, typed in the cockpit and sent in the request. The helper checks it against a secret only root can read (`/var/lib/seraphina-system/approval.key`, 0600) and accepts each code once | that someone holding the owner's phone typed a code within the last minute | what it approves: a compromised harness could show one list and send another with the code, which is v2 §9.3's own reason for the safety page. The receipt and the phone notice after the job are built from the helper's own counts ("26 packages updated"), so a mismatch is visible |
| **your own sudo** | root-access changes (§8) | `sudo /usr/local/libexec/seraphina/sf-system approve <ticket>` in the owner's own terminal (Cockpit's, logged in as himself), where the helper prints the change in its own words and asks him to type `yes`; then `confirm <ticket>` from a login that started after the change (§8.6) | that the owner, with his own password, read the helper's words and agreed, and could still sign in afresh and use sudo afterwards | — the harness's sudoers line allows no arguments, so it can never run `approve` or `confirm` |

**Enrolment** is a copy block the owner runs in his terminal: `sudo
/usr/local/libexec/seraphina/sf-system enrol` creates the key and prints an
`otpauth://` address both as text and as a QR code drawn in the terminal, for
his authenticator app. The cockpit never sees the secret. Running it again
replaces the key. When the executor exists, codes move to its safety page and
this key is retired (Appendix A3).

### 6.10 Receipts: every change with its way back

Each change the helper makes writes one receipt,
`/var/lib/seraphina-system/receipts/<id>.json` (root, 0600), for example:

```json
{"schema": "seraphina-system-receipt/1", "id": "R-0042", "rid": "<32 hex>",
 "started": "<time>", "ended": "<time>",
 "caller": {"uid": 991, "user": "seraphina"}, "verb": "apt.upgrade",
 "class": "undo_by_restore", "flags": [], "approval": {"kind": "code", "at": "<time>"},
 "request": {"set": "security", "list_sha256": "<64 hex>", "count": 26},
 "before": {"packages": [{"name": "<package>", "version": "<old>"}], "etc_sha256": "<64 hex>"},
 "steps": [{"argv": ["apt-get", "install", "--only-upgrade", "-y", "<package>=<new>"], "exit": 0}],
 "after": {"packages": [{"name": "<package>", "version": "<new>"}], "restart_needed": ["<unit>"]},
 "way_back": {"kind": "reinstall_kept", "verb": "apt.restore", "how": "Put back the 26 previous versions from the kept files.",
              "kept": ["kept/R-0042/"], "expires": "<time>"},
 "outcome": "ok",
 "verified": ["every listed package is at its new version", "no package is half-installed (dpkg --audit)"],
 "not_verified": ["the apps that use these libraries were not restarted"]}
```

**One viewer for two kinds of record.** The operator persona's receipts have the
fields `schema, id, time, actor, intent, undo_class, jobs, before, after,
rollback, verified, not_verified` (source: `swfactory/receipts.py:1-40`). The
mapping is direct: `caller` is `actor`, `verb` and `request` are `intent`,
`class` is `undo_class`, `steps` are `jobs`, `way_back` is `rollback`. The
receipts page shows both in one list, each labelled by where it came from: "Done
by Seraphina's system access (recorded on the server)" or "Reported by the
operator" (source for the label: `swfactory/operator.py:28`), and each with its
outcome in words (Done, Partly done, Failed, Refused, Nothing to do, or the
operator's reported outcome), never only as a colour; the changes whose outcome
is still unknown are named above the list (one the helper's record later settled as
done or never started is not). A record that did not do its change (any
outcome but Done or Partly done) is shown by the change's name, never by words
written as done; a failed keep pass added no name, so it says that none were
kept and there is nothing to undo instead of a way back and checks. The helper's
receipts are not "reported": the helper wrote them as it acted. The harness
reads them through `receipts` and keeps only ids, verbs, classes, outcomes and
counts in `<data_dir>`; names stay root-side and reach the cockpit a page at a
time (§10.1).

### 6.11 Install, configure and remove

The harness never runs sudo to install anything. It stages files in
`<data_dir>/yourserver/setup/` (0700, files 0600) and shows copy blocks in the
`{id, title, commands, files?, note?, expect, if_not}` shape of
`Notifier.prepare_ntfy` (source: `swfactory/notify.py:3428, 3595-3625`). The
rule of BACKUPS_SNAPRAID §3.10 holds unchanged: a staged file is installed to a
root-owned `.new` path, checked or explained there, then moved into place. Here
it takes one paste that checks itself, so a novice pastes once and reads one
line (DESIGN §31.1; v2 §10's novice check; judgement):

```
sudo install -D -m 0755 -o root -g root <staged>/sf-system /usr/local/libexec/seraphina/sf-system.new && sha256sum /usr/local/libexec/seraphina/sf-system.new && sudo /usr/local/libexec/seraphina/sf-system.new setup <staged>/system-helper.json
```

`setup` is an argument mode reached only through the owner's own sudo (the
harness's sudoers line allows no arguments), and it needs a terminal. It:

1. runs its installation check (§6.2) on its own `.new` path and parents;
2. reads the staged config without following links (one regular file, at most
   16 KiB) and checks every field itself: known classes only; `callers` uids
   that exist; `owners` that are login accounts with full sudo by a rule it can
   see (§8.5); a stacks folder that exists; keep limits within bounds; and no
   `private`, `maintain`, `fix` or `access` class for a login account (§6.7);
3. writes the sudoers file from its own template, never from a staged file:
   the two lines below, naming the user whose uid the config's `callers` holds
   (looked up with `getent passwd`), to `/etc/sudoers.d/seraphina-system.new`
   (0440), checked with `visudo -cf`; the config goes to
   `/etc/seraphina/system-helper.json.new` the same way;
4. prints, in its own words, what will be allowed and what changed ("Seraphina
   may: look at your server's layout"; "Callers: the user <harness user> (uid
   1000)"; "Your accounts: <owner account>"; "To switch it all off: sudo rm
   /etc/sudoers.d/seraphina-system"), and asks him to type `yes`;
5. moves the helper, then its config, then the sudoers file into place, in that
   order, so sudo reaches nothing until the other two are right; from P3, when
   the `access` class is added, it also writes, enables and checks the boot unit
   of §8.6 step 8 from its own template;
6. prints exactly one line: "Done. Seraphina can now look at your server (read
   only). Go back to Seraphina; the page updates by itself." The harness then
   calls `info`, and the setup card turns green.

**Anything else printed means nothing changed**: `setup` removes its `.new` files
on every refusal, and the card's `if_not` says "Something did not match, so
nothing was changed. Paste what the terminal printed here, or ask someone to
help you." **Extra care** is optional: compare the `sha256sum` line with the
release notes opened on another device (what a hash proves is BACKUPS_SNAPRAID
§3.10's paragraph, unchanged). The sudoers lines `setup` writes are:

```
Defaults!/usr/local/libexec/seraphina/sf-system !use_pty
<harness user> ALL=(root) NOPASSWD: /usr/local/libexec/seraphina/sf-system ""
```

Until the move sudo ignores the file, because `@includedir` skips names that
contain a dot (verified: with a lab `@includedir`, only the dot-free file was
parsed by `cvtsudoers` and `visudo -c`, lab R13). The `""` rule parses
(verified: lab R13); that it allows the command only with no arguments is docs
(sudoers(5); sudo-rs `docs/man/sudoers.5.md:143-151`), and L7 checks it as a
non-root caller before P1a ships.

**Under sudo-rs, and with two sudo programs.** A server may run sudo-rs as
`sudo` with classic sudo installed beside it under another name; both are
setuid root and both read `/etc/sudoers*` (observed: sudo-rs 0.2.13, older
than the v0.2.15 source read for this design, with classic 1.9.17p2 as
`sudo.ws`). Every sudoers line above was checked only against classic sudo. So
L7 runs under both on Ubuntu 26.04 before P1a ships: the `Defaults!<path>
!use_pty` line, the `""` rule, `@includedir` skipping names with a dot,
`visudo -cf` and which `visudo` the PATH finds, and a piped request with no
terminal. If sudo-rs 0.2.13 does not accept the `Defaults!` line, `setup`
leaves it out: the harness never has a terminal, so no pty is used under either
implementation (judgement). A line sudo-rs cannot parse could fail closed for
the helper, or break the owner's own sudo, so `setup` decides the
implementation from `readlink -f /usr/bin/sudo` (never from whether
`cvtsudoers` exists), checks the file with that implementation's `visudo`, and
refuses when the check fails (judgement; L7). With sudo-rs, a refused `sudo -n`
says "sudo: interactive authentication is required" where classic sudo says "a
password is required" (observed); the harness's matchers take both. The config `setup` installs, for P1a:
`{"v": 1, "allow": ["read"], "callers": {"<uid>": ["read"]}, "owners": [<uid>],
"stacks_dir": "<path>", "docker_api": true,
"keep": {"images_per_app": 2, "max_gib": 50}, "smart_types": {}}`. `owners` are
the accounts the owner confirms as his own; they matter for `approve`, `confirm`
and the lock-out rules (§8.5).

**Details** shows the same steps one command at a time (install, `explain
helper-config`, `visudo -cf`, the three moves, a check through `info`) for
anyone who prefers them, exactly as BACKUPS_SNAPRAID §3.10 writes them.

**More classes** are the same paste with a newly staged config, one change at a
time, so a compromised harness user cannot widen what it may do: `setup` prints
the old and new values in the helper's own words, not the cockpit's. **From P2,
enrol your code** is one more paste, `sudo
/usr/local/libexec/seraphina/sf-system enrol` (§6.9), after the harness has
moved to its own user (§6.1).

**Switch it off with one file.** `sudo rm /etc/sudoers.d/seraphina-system` ends
all of the helper's root access at once: the harness can no longer reach it, and
the cockpit says "Seraphina's system access is switched off" (judgement). A job
already running in its unit finishes, and a pending timed undo still runs,
because both are root units that need no call from the harness; the block shows
both first, from `info`. **Removing it completely** is one paste, `sudo
/usr/local/libexec/seraphina/sf-system remove`, which removes the sudoers file
first, then `/etc/seraphina/system-helper.json`, the boot unit if there is one,
and the helper itself, and asks separately whether to remove
`/var/lib/seraphina-system/`, which holds the receipts and the kept files that
are the way back of earlier changes; it says so in plain words. Keep tags stay
in Docker until a separate block removes them.

### 6.12 What the helper keeps

`/var/lib/seraphina-system/` (root, 0700): `rids/`, `jobs/`, `runs/<job>/`
(output, 0600, 90 days), `receipts/` (kept for good; small), `kept/<receipt>/`
(the previous `.deb` files, a tar of `/etc` taken before a package change, the
old copies of the files and account-file lines an access change replaced; kept until the receipt's
`way_back.expires`, by default 30 days after the change was verified),
`tickets/`, `access/` (the last enumeration, §8), `boots.jsonl` (boot id,
running kernel and time, which tells which kernel last ran a whole day; §7.3
M3), `approval.key` and `codes.used`. Everything in it is private (§10.1); the
harness keeps no copy of any of it in `<data_dir>`.

---

## 7. The buttons

### 7.1 How every button behaves

Every button follows the same six steps (judgement), so a novice learns them
once:

1. **It says what it will do before it does it**: one sentence, the class badge
   in v2 §2.4's words, any flag badge, and each precondition with a tick or the
   reason it blocks. The exact commands are under **Details**.
2. **It shows the list first** when it changes things: every package, file or
   image it would touch, with the size where it matters. The list carries a
   hash; the helper refuses with "The list changed since you looked. Look again."
   when the list it computes differs (§6.7 item 5).
3. **It asks for what its class needs** (§6.9): nothing, a click, your code, or
   your own sudo in a terminal. A precondition that is a safety rule a person may
   override (busy, a hold, the power schedule) offers **Do it now anyway** after
   a warning that names what would be interrupted (v2 §6.1 rule 2); a missing
   restore point or restore test can never be overridden.
4. **It shows progress in steps**, never a fake percentage: "Keeping the
   previous versions (26 files)", "Downloading", "Installing". **Stop** is
   offered only before the point of no return, which each button names.
5. **It says what happened**, in one sentence, with the way back as a button
   ("Put back the previous versions, until 24 November") and what still needs
   the owner ("4 services still use the old libraries. [Show them]").
6. **It leaves a record**: a receipt (§6.10), an activity entry (§11.8), and a
   tracker item when something waits for the owner (a failure, a restart
   needed, an unknown outcome). An unknown outcome is shown as unknown, with
   **Check what happened**, and is never replayed (§6.8).

### 7.2 Check: read-only, any time (P1b)

| # | Button | Exact commands | Runs as | What the owner sees |
| - | ------ | -------------- | ------- | ------------------- |
| 1 | **Updates available** | `apt-get -s upgrade` and `apt-get -s dist-upgrade`, parsed from the `Inst <package> [<old>] (<new> <origin>:<version>/<suite>[, …] [<arch>])` lines and the "N upgraded, N newly installed, N to remove and N not upgraded" summary, because `apt list --upgradable` warns that apt "does not have a stable CLI interface" (verified: lab R21, `apt-s-upgrade.out`, `apt-list.err`); a line is a security fix when one of its suites ends in `-security`, which gave exactly the 26 packages `unattended-upgrade --dry-run` would install (verified: lab R21); `apt-mark showhold`; the list's age from `/var/lib/apt/periodic/update-success-stamp`, which apt's post-update hook touches (verified: lab R21); `snap refresh --list` (docs; the lab had no snapd). **Refresh the list** is the helper's `apt.refresh` (`apt-get update`, which needs root, writes `/var/lib/apt/lists` and runs apt's hooks; verified: lab R21) | harness user; refresh through the helper | "26 updates, 9 of them security fixes. 21 can be installed from here. 5 must be done at home: 2 are the kernel, which needs a restart; 3 are systemd and its libraries, which must go together. [Show how] Ubuntu is still rolling 1 out. List checked 3 hours ago. [Refresh the list]" The count "can be installed from here" is the list of §7.3 M1 after its dependency step, simulated here, so the number the owner sees is one the button can install. [Show how] opens the copy block of §7.6 for each family left out. Running `apt-get -s` without root is docs (apt-get(8)), UNVERIFIED as a non-root run here (L2) |
| 2 | **Restart needed?** | `/run/reboot-required` and `/run/reboot-required.pkgs`, world-readable and gone after a restart (source: update-notifier's `notify-reboot-required`, called from the `postinst` of `libc6`, `dbus`, `libpam0g` and others and from `/etc/kernel/postinst.d/update-notifier`); the helper's `needrestart -b -r l`, whose `NEEDRESTART-KCUR`, `-KEXP`, `-KSTA` (0 unknown, 1 current, 2 ABI-compatible upgrade pending, 3 version upgrade pending), `-UCSTA` and one `NEEDRESTART-SVC: <unit>` per service are parsed (source: needrestart `v3.11` `needrestart:165-171, 265-282, 919-921`, `perl/lib/NeedRestart/Kernel.pm:33-38`) | harness user, and the helper for the service list | "Restart needed: a new kernel is installed but not running yet. 4 services use old libraries." Then one row per service, each with its own **Restart** (P2) and its own before text; never one button for all. A service the refusal list of §7.4 F1 covers (needrestart's own never-restart list, and the units that reach the server) has no button and says "This one is restarted only by restarting the server. [Schedule a server restart] (P2)". Ubuntu's own config never restarts dbus, display managers, networking, gettys, `user@`, Docker, `systemd-logind`, unattended-upgrades, the apt timers' services or cloud-init (verified: the `override_rc` table in the lab's `/etc/needrestart/needrestart.conf`, Ubuntu 24.04's package; upstream `ex/needrestart.conf:125`) |
| 3 | **Disk use** | the host strip's numbers; Docker's share from `GET /system/df` (images with how many containers use each, containers, volumes with how many containers link each, build cache); the journal's size (`journal`); the apt cache's size; the kept images and kept `.deb` files; on request, the biggest folders on one disk (`du`, two levels) | harness user and the helper | "System disk: 71 GB free of 465 GB. Docker uses 177 GB: 143 GB of images (12 GB that no app uses), 35 GB of leftover build files. System logs: 3.1 GB. Previous versions kept: 9 GB." Folder names only on the cockpit (§10.1) |
| 4 | **Failed services** | `systemctl list-units --failed --output=json` and `systemctl list-timers --all --output=json` (source: systemd `v255` `src/systemctl/systemctl-util.c:895-907`, `systemctl.c:810-826`; not run in the lab, which had no systemd), each timer's service `Result`; containers that exited with an error under a restart policy, and restart loops, from §2.5 | harness user | "2 things failed: the nightly backup timer (failed at 03:00) and the notes app (keeps restarting). [Open in Cockpit] [Open its logs]" |
| 5 | **Disk health** | the helper's `smart`: per disk `smartctl -H -A -j -n standby,3 [-d <type>] <device>`, reading `smartctl.exit_status`, `smart_status.passed` and the attributes; the exit status is a bitmask and is decoded bit by bit (bit 3 "DISK FAILING", bit 4 a pre-failure attribute at or below its threshold now, bit 5 in the past, bit 6 errors in the error log, bit 7 errors in the self-test log; bit 1 can mean "could not open" or "asleep"), so a non-zero exit is never read as "failing" (source: smartmontools `smartctl.8.in:2486-2520`; verified: `nobody` gets exit status 2, root reads the device, lab R19). The `-d` type for a USB bridge comes from the same fixed list `sf-snapraid` accepts (BACKUPS_SNAPRAID §3.4 item 7). Disks in SnapRAID's array show that panel's daily reading instead of being read twice (judgement). Only a disk read as healthy counts as healthy: while one was asleep or could not be read (smartctl timed out, which a failing disk may do) the result is grey and counts it apart, never "All N disks healthy" | the helper | "All 5 disks healthy." / "4 of 5 disks healthy. Disk 3 was asleep." / "2 of 3 disks healthy. 1 could not be read." / "Disk 2 reports a failure. Replace it soon." |
| 6 | **What is listening** | the helper's `ports`, each listener joined to its container or unit (a socket-activated one to its socket unit and the service that socket starts, so Cockpit and SSH are named even while idle), given a scope with the Blueprint's own rules (loopback, all interfaces, tailnet range `100.64.0.0/10` and `fd7a:115c:a1e0::/48`, private, link-local, public; source: `collect.py:641-655, 916-955`) plus "home-network address only" for a listener bound to one private address of this server (observed: the gateway's routes and several apps listen that way), and Docker's published ports mapped through `PortBindings`. The login gateway's ports are shown as one row with its routes under it (§6.3 `ingress`), not as a listener per port. Sockets inside a container's own network namespace do not appear on the host (judgement; lab R18 lists host listeners only) | the helper | "14 things listen. 3 can be reached from your home network without Tailscale: Dockge (which can control the whole server), Cockpit and the media app. Your login gateway: 15 ports (443 and 14 routes) on your home network only, and port 80 on every address." Each is a finding under DESIGN §31.4; a direct port that bypasses the gateway for an app the gateway also serves is named as such ("The file manager also answers on its own port, without your gateway's sign-in") |
| 7 | **Tailscale** | the helper's `tailscale` (§6.5): each node, its serve entries, whether Funnel is on, whether its container runs, its state file's age; a host tailscaled if there is one | the helper | "8 Tailscale connections, all running. Funnel is off everywhere. 1 old connection's state is still on disk but its container is gone. Who may reach what is set in Tailscale's admin console and cannot be read from here." Funnel on is a high finding |
| 8 | **Logs for one app** | the helper's `logs` (class `private`, so only once the harness runs as its own user; until then the button says "Open its logs in Dockge" or "in Cockpit" and links there): for Docker's `json-file` driver, the container's log file and its rotated copies read directly (mode 0640 root; verified: lab R4), one JSON object per line; for the `journald` driver, `journalctl CONTAINER_ID=<12 hex> -n <lines> -o json`; for a unit, `journalctl --unit=<unit> -n <lines> -o json`; the `local` driver's binary files are refused ("Open its logs in Dockge"). At most 500 lines or 256 KiB a page. The helper redacts each page itself before it answers, with a frozen copy of the Blueprint redactor's patterns kept in its own file, because it never imports harness code; T35 checks both give the same output on one corpus (source: `swfactory/blueprint/redact.py:37-74`) | the helper | the newest lines, with times; the harness runs each page through its redactor again and keeps no copy; never sent to a model unless the owner sends a page himself (§10.3) |

`factory capture --container` reads logs with `docker logs`, which needs the
Docker socket (source: `swfactory/capture.py:226-246`), so Check 8 does not
reuse it; capture can use the helper's `logs` verb later (Appendix A12).

### 7.3 Maintain: only in a maintenance time, only after a backup check (P2)

**M1 and M2: Install updates, security fixes only, or all.**

- **The list.** The `Inst` lines of `apt-get -s upgrade` (an upgrade installs
  nothing new and removes nothing), security only for M1, minus the families of
  §7.6, then a **dependency step**: any listed package whose new version depends
  on an exact version (`=`) of a package left out, or on which a left-out
  package's installed or new version depends with `=`, is taken out too and
  shown as "waits with systemd" (or the family's name). This is needed because
  families travel together: systemd's package depends on `libsystemd0` and
  `libsystemd-shared` at exactly its own version, and `openssh-server` on
  `openssh-client` at exactly its own (verified: `apt-cache show` in the lab,
  Ubuntu 24.04), so installing `libsystemd0` alone would make apt want systemd
  too, and the whole job would be refused. The step reads `Depends`,
  `Pre-Depends` and `Breaks` of the candidate and installed versions with
  `apt-cache show <package>=<version>`, repeats until nothing more moves, then
  simulates the result; if the simulation still pulls in anything outside the
  list, the packages it names are moved out the same way, at most five times,
  and then the rest is shown as "These need to go together. Do them at home."
  with their copy block (§7.6). Check 1 runs the same steps, so the number it
  shows is one the button can install (judgement). "Held back" packages, which
  need new packages or removals, are listed apart: "These need more than an
  update. Do them in Cockpit's terminal." Each line: the package, old and new
  version, security or not, and "needs a restart" when the package is one whose
  install asks for a restart (§7.2 Check 2).
- **Preconditions** (§7.5): a maintenance time is open, or the owner starts one
  now with his code; the packages restore point below is taken and passes the
  way-back check in this same job, before anything is installed; free space for
  the downloads and the kept files; the job fits before the quiet period before
  the power-off; no other change runs.
- **The restore point.** For each package to be replaced, its installed version's
  `.deb`: from `/var/cache/apt/archives` when there, else `apt-get download
  <package>=<installed version>` while the archive still offers it, else
  `dpkg-repack <package>` when that tool is installed (D12), copied to
  `kept/<receipt>/`; plus `dpkg --get-selections`, `apt-mark showmanual`,
  `apt-mark showhold` and a tar of `/etc`. A package whose old version cannot be
  kept is marked "can't be put back"; the job refuses it unless the owner takes
  it off the list or accepts that line in words. Ubuntu's mirrors drop
  superseded versions, so `apt-get download` cannot be relied on later (v2 §5.2
  Packages row).
- **The way-back check** (D24; judgement), run by the helper itself on this
  job's own restore point, before `dpkg` starts: (1) each kept file's sha256 is
  recorded, and `dpkg-deb --info` shows the right package, version and
  architecture; (2) each is unpacked with `dpkg-deb --extract` into a scratch
  folder under `/var/lib/seraphina-system/scratch/<job>/`, and every file is
  checked against the package's own `md5sums` list from `dpkg-deb --control`,
  so the archive is complete and intact; (3) `apt-get -s install
  --allow-downgrades <the kept files>` is simulated against the system as it
  will be after the install, and must show exactly one downgrade `Inst` line
  per kept package and no `Remv`. Any failure stops the job before the point of
  no return: "Seraphina could not check that it can put these back, so nothing
  was installed." What it proves: the previous versions are complete, intact
  and accepted by apt as a way back. What it does not prove: that their
  maintainer scripts run cleanly when going back, or that the apps work
  afterwards; only v2's package-layer test in a sandbox (v2 §5.5 kind 3P)
  proves that, and the card says "Way back checked, not rehearsed".
- **The install.** The helper simulates its exact argument list first and
  refuses if the simulation shows any `Remv` line or any `Inst` line outside the
  list; then, with `DEBIAN_FRONTEND=noninteractive` and `NEEDRESTART_SUSPEND=1`
  (honoured by needrestart's apt hook; v2 §6.5), runs `apt-get install
  --only-upgrade -y -o Dpkg::Options::=--force-confdef -o
  Dpkg::Options::=--force-confold <package>=<version> …`, so what is installed
  is exactly what the owner saw. Afterwards: `dpkg --audit` must print nothing,
  every version must match, and the restart-needed facts are read again.
- **The point of no return** is `dpkg` starting. Before it, **Stop** cancels.
  After it, the unit's stop signal is `SIGCONT`, which does nothing, with
  `SendSIGKILL=no` and a 30-minute stop timeout, so a power-off waits for `dpkg`
  instead of cutting it off (judgement; docs: systemd.kill, UNVERIFIED; L10).
- **After.** "26 packages updated (9 security fixes). They are used after a
  restart. [Schedule a restart] Undo: [Put back the previous versions] (kept
  until 24 November)." The way back is `apt.restore`, itself `undo_by_restore`
  with the same code, which simulates first and runs only the downgrades the
  way-back check saw. A Blueprint capture follows (§11.9).
- **Class** `undo_by_restore`: "You can undo this from the kept previous
  versions, in minutes. Seraphina checked them before installing."

**M3: Clean up old packages.**

- **The list.** The `Remv` lines of `apt-get -s autoremove`, shown before
  anything runs. Old kernels are listed apart.
- **Kernels are protected.** apt itself keeps the running kernel and fills up
  to `APT::NeverAutoRemove::KernelCount` (default 2) with the newest others
  (source: apt `3.1.12` `apt-pkg/algorithms.cc:1475-1600`; Ubuntu 24.04's
  `01autoremove` and the absence of `/etc/kernel/postinst.d/apt-auto-removal`,
  read in the lab). Booted on a newer kernel with an older one known to work,
  apt still protects both; but booted on a trial kernel with a third, newer one
  installed, the known-good kernel becomes removable. So the helper refuses to
  remove the kernel that last ran for a whole day (from `boots.jsonl`) and the
  running one, whatever apt says, and D10 asks whether to also raise
  `KernelCount` to 3.
- **The removal.** The `.deb` of each package to be removed is kept first, as in
  M1, and passes the same way-back check, whose simulation here is a reinstall
  of the kept files after the removal; then `apt-get remove -y <exact list>` (never `purge`, so configuration
  files stay; never `autoremove`, which takes no list), after its simulation
  shows exactly those `Remv` lines.
- **Class** `undo_by_restore` (the way back reinstalls the kept files); the
  same preconditions as M1.

**M4: Clean up old logs.**

- **The list.** `journalctl --disk-usage` ("Archived and active journals take up
  X in the file system.") and the archived journal files older than the owner's
  choice (default 30 days, D14), with their total size. Run as root, because a
  caller who cannot read the files gets a silent 0B (verified: lab R22).
- **The step.** `journalctl --vacuum-time=<days>d`, which removes archived files
  only, oldest first, and never active ones unless `--rotate` is given in the
  same call (docs: systemd `v258` `man/journalctl.xml`). Rotated text logs under
  `/var/log` stay with logrotate.
- **Class** `cannot_undo`: the logs are not backed up. The screen says so ("These
  logs are not backed up. Type `delete 2.1 GB` to confirm."), and v2 §5.8's rule
  applies: a typed confirmation plus your code, never automatic, never in a
  batch.

**M5: Clean up Docker leftovers.** Only unnamed old copies and leftover build
files, never anything else:

| Step | What | Basis |
| ---- | ---- | ----- |
| 1. Keep | every app's current image, and its previous one when still present (known from the app change log, or, the first time, from a dangling image whose repository digest names the app's image; L6), carry a keep tag `seraphina-keep/<project>-<service>:<yyyymmdd>-<12 hex>`, up to the root-side limit per app (default 2) and in total (default 50 GiB). Older keep tags beyond the limit are removed here, and a version whose last name was that tag is then deleted with the rest; a `pinned` app's tags are never trimmed. An image carrying a tag under the owner's `keep_tag_prefixes` (§2.6) is never trimmed either, does not count toward Seraphina's per-app or total limit, and shows as "previous version kept" (judgement) | verified: on a pull that moves a tag, the containerd image store keeps the old image as a dangling image, and after `up -d` that is the only copy of the previous version; plain `docker image prune` deleted exactly that image (lab `prune-image`; source: Moby `daemon/containerd/image_pull.go:123-138`). An explicit tag protected a copy from plain prune (lab `prune-image`) but not from `prune -a` (lab `prune-image-a`) |
| 2. List | dangling images used by no container, stopped ones included, and carrying no other name; build-cache records from `GET /system/df` that are not in use, not shared, and not of type internal or frontend | verified: these listings matched what the prunes removed (lab `prune-*/predict.txt` against `removed.txt`); the dangling filter also lists the id of an image that still has a real tag after a pull, so the helper drops such ids from the list (verified: lab R11); source: BuildKit `cache/manager.go:1212-1217` |
| 3. Check | the list is computed again inside the job and must match what the owner saw; no `docker compose` process is running (a Dockge update in progress) | judgement |
| 4. Remove | `POST /images/prune` with no filter (dangling only) and `POST /build/prune` without `all` | verified: plain image prune removed only the untagged previous version and kept tagged copies, unused tagged base images and images of stopped containers (lab `prune-image`); default builder prune removed 6 of 10 records, the unshared ones (lab `prune-builder`) |
| 5. Compare | what the prune reports it deleted against the list; anything extra is reported ("2 more images were removed than you saw") and opens a tracker finding | judgement |

Never, in any phase: `docker container prune` (it removed every stopped
container, including one kept on purpose, a one-shot init container and a
never-started one, with their writable layers; lab `prune-container`), `docker
network prune` (after it, a stopped stack's network was gone, and both `docker
start` and Dockge's Start and Deploy command, `compose up -d --remove-orphans`,
failed with "network … not found" until `up -d --force-recreate`; lab R10,
`prune-network`), `docker volume prune` (with `-a` it deleted the named volume of
a stack that had been brought down; lab `prune-volume-a`), `docker image prune
-a` (it deleted the tagged rollback copy; lab `prune-image-a`) or `docker system
prune`, which combines container, network, image and build-cache prunes (lab
`prune-system`). Cosmos did the equivalent of `prune -a` after network events
unless told not to (source: Cosmos `v0.23.4` `src/docker/network.go:445-505`),
and Dockge prunes nothing at all, so every Dockge update leaves the previous
version dangling and nothing ever cleans it (source: Dockge `backend/stack.ts`,
no `prune`, `rmi` or `image rm` anywhere in backend, common or frontend).

**Class** `cannot_undo` for steps 4 and 5 (an unnamed image or a build record
that is removed is gone; some can be downloaded or rebuilt again, not all), with
v2 §5.8's typed confirmation and code (D14). Step 1 alone is `undo_instantly`
and runs in P1b when the `keep` class is on (D9).

### 7.4 Fix (P2)

**F1: Restart a service.**

- **What.** One running container, one running unit, or the running members of
  a whole app, in the order their `com.docker.compose.depends_on` labels give. A
  unit through `systemctl restart -- <unit>`; a container through `POST
  /containers/<id>/restart?t=30`. Only members that are running (or restarting)
  are touched, and the helper checks that again inside the job, just before each
  one, because Docker's restart starts a container that is stopped (source:
  Moby `daemon/restart.go:58-69`). Never touched: a stopped member, an old copy
  or a retired member (§2.1), and a one-shot service that finished (restart
  policy `no`, exit code 0), which a restart would run again. This differs from
  Dockge's Restart, which is `docker compose restart` (source: Dockge
  `backend/stack.ts:428-435`) and restarts "all stopped and running services"
  (docs: Compose `v5.6.1` `docs/reference/compose_restart.md:4`).
- **No Start button** (judgement). A stopped app is where Seraphina knows least:
  why it stopped, whether an old copy holds its ports or GPUs, whether a
  one-shot job would run again. So the card says "Start it in Dockge" with a
  link to its stack, and shows beside the link what Seraphina can see
  read-only: "Another copy of this app is running", "Its port 8080 is in use by
  <app>", "Its GPU is in use by <app>".
- **Refused, with a reason** (the `management_plane` flag, v2 §7.7): the SSH
  server, `tailscaled`, Docker, containerd, Cockpit's units, the harness itself,
  any sidecar that carries the confirmed address of Cockpit or of Seraphina, and
  every unit that needrestart's own never-restart list covers (`dbus`,
  networking, gettys, `user@`, `systemd-logind`, the apt timers' services and
  the others of §7.2 Check 2): "This is how you reach your server. Restart it
  from Cockpit's terminal, or when you are at home." or "This one is restarted
  only by restarting the server." A unit is matched by every name `systemctl
  show -p Id,Names` gives, so `sshd.service`, the alias, and `ssh.service`, the
  unit Ubuntu ships with `ssh.socket`, are both caught. needrestart's list is
  the helper's own copy of Ubuntu's shipped `override_rc` patterns, plus any
  entry of the simple form `qr(^…) => 0` it finds in
  `/etc/needrestart/needrestart.conf` and `conf.d/`; when either file holds an
  entry it cannot read, Check 2 offers no restart buttons and says so
  (judgement).
- **Busy probes** warn and offer **Do it now anyway**: GPU memory in use by the
  app with utilisation above 10%, a power hold that names it, a running backup
  (v2 §6.4).
- **After.** Running again and answering within 2 minutes: "Restarted. Working."
  Otherwise: "It did not come back. [Open its logs] [Open in Dockge]", and a
  tracker item.
- **Class** `undo_instantly` with nothing to put back; a click, or your code for a
  Frozen app; at most 3 restarts of one app an hour from Seraphina, so a loop (an
  agent's or a bug's) cannot keep an app down (judgement).

**F2: Restart the server when nothing important is running.**

- **What it waits for**, checked every minute: every power-hold record in
  `/run/seraphina/holds/` (SnapRAID jobs and this helper's own jobs); a running
  backup, by process name (the Blueprint's `BACKUP_TOOLS` minus `snapraid`;
  source: `collect.py:82`); `dpkg` or `apt` running; logind inhibitor locks
  (`systemd-inhibit --list`); a pending timed undo of a root-access change (§8.6);
  and the harness's own long jobs (factory jobs on local models, wakes, GPU
  custody leases), which the harness writes as hold records in
  `<data_dir>/holds/` (v2 §6.3's location) and the helper reads as advice only,
  since a forged record can only delay a restart (judgement).
- **Never kills.** It waits up to the owner's limit (default 120 minutes, D16),
  then does not restart and says so: "The restart did not happen: a backup was
  still running after 2 hours. [Try again tonight] [Restart now anyway]".
  **Cancel** works until it starts.
- **Refused or warned.** Inside the quiet period before the nightly power-off it
  offers "The server turns off at 23:00 anyway. [Use tonight's power-off]". When
  a kernel is installed that has never booted, it says "This restart will start
  a new kernel for the first time. If it does not start, someone must be at the
  machine to pick the previous one." and needs the owner to tick "I am here"
  (v2 §5.8, §6.3).
- **How.** The job unit runs `systemctl reboot` when everything is clear. The
  helper records the boot id before; after the restart, `info` sees a new boot id
  and closes the receipt: "Restarted at 21:14 and back 3 minutes later." A job
  that ended without a new boot id is `unknown` and is never re-armed by itself.
- **Class** `undo_instantly` until it starts (Cancel), with the
  `management_plane` flag, because the server is out of reach while it restarts
  and stays out of reach if it does not come back: your code is needed.

### 7.5 Preconditions in one place

| Precondition | For | How it is checked | Can a person override it? |
| ------------ | --- | ----------------- | ------------------------- |
| A maintenance time is open | M1-M5 | `maintenance.windows` in v2 §6.6's shape and the effective mode of the job's service (`os-packages`, `os-cleanup`, `docker-cleanup`); in manual mode, pressing the button opens one now; in scheduled mode it offers **Now (needs your code)** and **Next maintenance time: Sunday 19:00**, as v2 §6.1 rule 2 offers for a higher class | it is the owner's own trigger |
| A packages restore point that passes the way-back check, in the same job, before anything is installed | M1-M3, and `apt.restore` before it reinstalls | the helper, §7.3 (D24) | no |
| Typed acceptance of what is lost | M4, M5 | the server compares the typed words with the size the helper reported | no |
| Your code | M1-M5, F2, F1 for a Frozen app | §6.9 | no |
| The quiet periods and the power-off | every changing button | `clock.PowerSchedule` (source: `swfactory/clock.py:190-258`): no start in the quiet period after boot or before the power-off, and the expected length must fit before it | **Do it now anyway**, except in the last 30 minutes before the power-off |
| One change at a time | every changing button | the helper's job lock and the harness's queue | no |
| Busy and holds | F1 (busy), F2 (holds), M1-M5 (no backup running) | §7.4 | F1 and F2 only, with a warning that names what would be interrupted (v2 §6.4), and F2 with your code; never M1-M5 |
| Paused | scheduled starts only | `maintenance.paused` ("Pause all maintenance"; BACKUPS_SNAPRAID §5.2) and v2's "Updates paused" after a failure | a person's press still works |
| Agents cannot reach the helper | the `private` class (P1c) and every P2 and P3 class | the doctor (§11.7): `harness.sandbox` is `bwrap` and no agent route uses a CLI driver, or agents run as their own user (v2 §9.2); otherwise the cockpit hides the changing buttons and says why | no |

**Approving ahead of a maintenance time.** A TOTP code lasts 30 seconds, so a
change approved for the next maintenance time is recorded by the helper as an
approval of a set of exact `<package>=<version>` pairs, valid until that time
ends, never of a list hash: between the approval and the time, apt's daily
timers refresh the lists, and unattended-upgrades may install the security part
of the set by itself, so a hash would almost never match again (judgement). When
the time opens, the harness asks the helper to run it, and the helper installs
only the pairs that are still upgradable at exactly the approved versions,
skips pairs already installed, never adds a package or a newer version, and
refuses only when the simulation of that subset pulls in anything else. The
result says what happened to each: "21 installed; 3 already installed by
Ubuntu's automatic updates; 2 have a newer version now, waiting for your next
approval." One code approves everything waiting for the same time (v2 §6.2,
scheduled mode). Automatic mode, where Seraphina approves routine changes
itself, needs v2's standing grants and the executor, so these buttons never run
automatically in this design (v2 §6.1 rule 10).

### 7.6 Kernel, GPU, engine, remote-access and Seraphina packages

v2's never-automatic list (v2 §6.1 rule 11) names the packages whose failure
takes away the way back. In this design they are never in M1 or M2, even on a
person's press (judgement; D13), and neither is any package the dependency step
of §7.3 ties to them. Each family left out gets its own card with one plain
sentence and an exact copy block, built from Check 1's simulation (with
`dist-upgrade` for the kernel's new packages): `sudo apt-get install
--only-upgrade <package>=<version> …` for exactly the family and its tied
packages, never a bare `apt upgrade`:

| Family | Packages | Why | What the card says, above its copy block |
| ------ | -------- | --- | ---------------------------------------- |
| Kernel and boot | `^linux-`, `^grub`, `^shim` | v2 classes these `undo_by_reboot`, which needs the boot-once fallback, a protected known-good kernel and the owner present (v2 §5.8); none of that exists yet, and the nightly power-off would boot a new kernel unwatched the next morning | "These change what the server starts with. Run this when you can be at the machine, then restart it and watch it come back. If it does not start, choose the previous entry in the boot menu." The restart-needed badge shows on them |
| GPU stack | `^nvidia-`, `^libnvidia-`, `^cuda-`, `^nvidia-container-toolkit` | its own restore layer (v2 §5.8) | "These change the graphics driver. Run this at home; the AI apps stop working until the server restarts." |
| Docker engine | `^docker-ce`, `^containerd\.io$`, `^docker-compose-plugin$`, `^docker-buildx-plugin$` | upgrading restarts every container, every Tailscale sidecar included, so every remote route goes down at once; Docker's repository is outside Ubuntu's unattended-upgrades origins, so only a plain upgrade moves it (verified: lab, `/etc/apt/apt.conf.d/50unattended-upgrades` and the dry run, R21) | first a block for `sudo apt-mark hold docker-ce docker-ce-cli containerd.io docker-compose-plugin docker-buildx-plugin`, so a plain upgrade in Cockpit or a terminal cannot restart everything mid-day (v2 §6.1 rule 11); then, for when he chooses: "Every app stops for a minute or two, and you lose remote access while it happens. Run this at home." with `sudo apt-mark unhold …`, the exact install line and `sudo apt-mark hold …` again |
| Remote access and the system manager | `^tailscale$`, `^openssh-server$` (and `openssh-client`, tied to it), `^netplan\.io$`, `^network-manager$`, `^systemd` (and `libsystemd0`, `libsystemd-shared`, tied to it) | the `management_plane` (v2 §7.7) | "These change how you reach the server. Run this at home, from Cockpit's terminal on your home network or at the machine itself." |
| Seraphina's own | `^seraphina` | Seraphina is never updated by itself mid-change (v2 §6.5) | its own update path |

**Ubuntu's automatic updates.** Unattended-upgrades may still install security
fixes every morning by itself, and its default origins and empty blacklist let
it install kernels, systemd and OpenSSH from the security pocket; only Docker's
packages, from Docker's own repository, are outside it (verified: the lab's
`50unattended-upgrades`, Ubuntu 24.04, where the `linux-` blacklist line is
commented out). The Check page says whether it is on (from
`/etc/apt/apt.conf.d/20auto-upgrades`, a file the Blueprint already reads;
source: `collect.py:84-94`). When it is on, every family card adds: "Ubuntu's
automatic updates are on, so the security fixes among these may install by
themselves some morning. A kernel installed that way starts at the next
power-on, with nobody watching." Becoming the single scheduler of updates (v2
§6.5) is outside this design; D13 asks the owner to choose with this in view.

### 7.7 What the owner sees before and after, and changes made elsewhere

**Before** (fixed templates, judgement):

> **Install 9 security fixes** · You can undo this from the kept previous
> versions, in minutes.
> ✓ Maintenance time: now, until 20:30 · ✓ Seraphina will check it can put these back before installing ·
> ✓ 1.4 GB free needed, 71 GB free · ✗ A backup is running (until about 19:40)
> [Next maintenance time] [Show the 9 packages] [Details: the exact commands]

**After:**

> **9 security fixes installed** at 19:44, in 3 minutes. They are used after a
> restart. [Schedule a restart] · Undo: [Put back the previous versions] (kept
> until 24 November) · [See the record]

**Changes made elsewhere.** A package change in `/var/log/dpkg.log` with no
helper receipt behind it (Cockpit's Software Updates, unattended-upgrades, a
terminal), or a container that was recreated with no Seraphina action, is shown
in the receipts list as "Changed outside Seraphina" (v2 §2.4) with its time and
what changed, and a tracker note links it (v2 §3.8, §6.5). It needs no receipt
of its own: the point is that the owner sees two kinds of change in one list.

---

## 8. Who can act as root

### 8.1 What is enumerated

The helper's `access` verb runs these as a job and keeps the result root-side;
the cockpit reads it a page at a time. Each row says why it can act as root.

| # | Kind | How | Why it counts | Basis |
| - | ---- | --- | ------------- | ----- |
| 1 | Accounts with uid 0 | `getent passwd`, third field 0 | they are root | judgement |
| 2 | The root account's password | `passwd -S root`: `L` locked, `P` usable, `NP` none | a usable root password works at the console and wherever root may sign in | verified: lab R16 |
| 3 | Sudo rules | which sudo is installed, from `readlink -f /usr/bin/sudo` and that program's `--version` (classic sudo or sudo-rs; Q1), and whether a second setuid sudo program sits beside it (classic sudo as `sudo.ws` next to sudo-rs); both are doors, and every step below runs with each of them (`sudo -l -U` and `sudo.ws -l -U`), flagging any rule one honours and the other ignores; for every account `getent passwd` lists, service accounts with no login shell included, because a `NOPASSWD` rule for one of them is as strong as any: `sudo -l -U <user>` (exit 0 even when it prints "is not allowed", so its text is parsed) and `sudo -ll -U <user>`, which names the granting file; `sudo -n -l -U <user> <command>` to test one command (exit 0 and the path if allowed, 1 if not); `cvtsudoers -f json /etc/sudoers` for the whole parsed policy where it is installed (tested, never assumed from the implementation), and any rule there for a user or group that the per-account loop did not reach is listed too; `visudo -c`; which files in `/etc/sudoers.d` sudo actually reads (names with a `.` or ending in `~` are skipped, so a naive listing misleads) | a rule for `(ALL) ALL` is full control; `NOPASSWD` gives it to anything running as that account. A rule for named commands counts as full when a command on a fixed list can start others (shells, editors, pagers, `find`, `tar`, `systemctl`, `docker`), and as "can't tell" otherwise | verified: lab R13; sudo-rs differs: no `cvtsudoers`, and an `@socket` rule reads rules from a Unix socket that no file reveals (source: sudo-rs `v0.2.15` `docs/man/sudoers.5.md:326-350`, `README.md:260-290`); observed: sudo-rs 0.2.13 as `sudo` and classic 1.9.17p2 as `sudo.ws` on one server; `-ll`, `-l -U <user> <cmd>` and the `@socket` claim are re-checked against 0.2.13 in L7 |
| 4 | Groups | `getent group` for `sudo`, `admin`, `wheel`, `docker`, `lxd`, `incus-admin`, `libvirt`, `disk` (root-equivalent when present), and `shadow` (can read password hashes) | each is a known path to root, or to secrets | verified: lab R13, R16 |
| 5 | Polkit | the `.rules` files in `/etc/polkit-1/rules.d` and `/usr/share/polkit-1/rules.d`; Ubuntu's `49-ubuntu-admin.rules` makes the `sudo` and `admin` groups polkit administrators | administrators can approve privileged actions, Cockpit's included | verified: lab R17. Other rules files are JavaScript and are listed, never decided (§8.3) |
| 6 | SSH | `sshd -T` (the effective settings; with `-C user=<u>,host=<h>,addr=<a>` for `Match` blocks); for every account with a home: both key files that `AuthorizedKeysFile` names (Ubuntu's default includes `.ssh/authorized_keys2`, easy to forget), each line parsed by the helper itself: key type, fingerprint (SHA-256 of the key's bytes, computed with `hashlib` and checked against `ssh-keygen -lf` in T21), options (`restrict`, `command=`, `from=`, `principals=`, `cert-authority`) and comment; `AuthorizedKeysCommand` and `TrustedUserCAKeys` when set | a key for root, where root may sign in with keys, is root; a key for an account with full `NOPASSWD` sudo is root; with password sudo, it is half the way | verified: `sshd -T` showed `permitrootlogin without-password`, `authorizedkeysfile .ssh/authorized_keys .ssh/authorized_keys2`, `passwordauthentication yes`; `ssh-keygen -lf` printed one fingerprint per key, hid every option, and skipped comments and garbage lines silently, which is why the helper parses lines itself (lab R15) |
| 7 | The Docker socket | its owner, group, mode and ACL entries; the members of its group (row 4); every container that mounts it, by path or by the socket's inode; every process connected to it now (`ss -Hxp`, as root) | whoever controls the daemon can start "a container where the /host directory is the / directory on your host" (Docker's security page, cited in v2 §5.8). A `:ro` bind stops only replacing the socket file: the API behind it is not limited | verified: a `:ro` socket bind showed `RW=false` in the mount table (lab R6); source: Beszel's agent reads the socket (`agent/docker.go:657-690`) |
| 8 | Dockge | the container recognised as Dockge (§2.1); whether it listens beyond loopback (port 5001 on every interface unless `DOCKGE_HOSTNAME` is set); `DOCKGE_ENABLE_CONSOLE`; and who can write the stacks folder, each stack folder, its compose file and its `.env` (every path component walked: not root-owned, or writable by a group or others) | Dockge holds the socket read-write and deploys whatever a stack says, as root; so does anyone who can sign in to it or write a stack, and, with the console on, anyone signed in to it gets a shell in Dockge's container, which holds the socket | source: Dockge `backend/dockge-server.ts:150-158`, `frontend/src/router.ts:30-63, 99` (`/console`), `main-socket-handler.ts:269-270` (`disableAuth`, a setting in Dockge's own data, which the helper does not read; Q9); observed: Dockge on every address with its sign-in page over plain HTTP, the socket and the stacks folder bound read-write, and its own recipe group-writable in the owner's home folder |
| 9 | Powerful containers | from the state files. **Full control now**: `Privileged`; `CapAdd` of `SYS_ADMIN`, `SYS_MODULE`, `SYS_PTRACE` with the host's PID namespace, `DAC_READ_SEARCH` or `SYS_RAWIO`; a bind of `/`, `/etc`, `/root`, `/home`, `/boot`, the Docker data root or the socket; root in the container (`Config.User` `""` or `0`) with a bind of the host's D-Bus system bus (`/run/dbus/system_bus_socket` or its folder), because root on that bus can drive systemd; a device that is a host disk, `/dev/mem`, `/dev/kmem` or `/dev/port`, or the whole `/dev`. **Could matter if something escapes**: a writable mount of a backup, parity or array data disk, or of an operator persona's memory store (named on the row: "can write your backup disk"); root inside the container (`Config.User` `""` or `0`); other added capabilities (`NET_ADMIN` and the rest of a fixed list); `SecurityOpt` that turns off AppArmor, seccomp or labels; the host's network, PID or IPC namespace alone; other devices, such as GPUs (`/dev/nvidia*`, `/dev/dri`) | the first group reaches the host directly; the second does not by itself, so it is shown apart and never counted as a way to take full control | verified: every field read from the lab's files (lab R6); the split is judgement; observed: a monitoring agent's container binds the system bus, a web file manager mounts the backup, parity and data disks read-write, and a chat app runs as root with a writable mount of an operator persona's memory store |
| 10 | Root services and timers that run files others can change | for every service that runs as root (no `User=`, or `User=root`): `systemctl show -p ExecStart,ExecStartPre,ExecStartPost,WorkingDirectory,EnvironmentFile,User,FragmentPath,DropInPaths`; every path component from `/` to each program, working folder, environment file, the unit file and its drop-ins is walked, and any one that is not root-owned or is writable by a group or others is flagged. The arguments are read too, not only the program: when the program is an interpreter (`python3`, `bash`, `sh`, `perl`, `node`), its options (`-B`, `-I`, `-c` and the like) are skipped and the script it runs is walked like a program, and a script under a home folder is flagged on its own ("runs a script from <account>'s home folder"). Runtime drop-ins under `/run/systemd/system`, which a root timer can write, are read with the rest. Units created while another account held root are shown with their creation time, so the owner can review them. Timers run their service's command | whoever can change such a file runs code as root at the next start | judgement; observed: root units that run `python3` with a script in an operator persona's home folder, and a root timer that rewrites a runtime drop-in every 30 seconds, so a walk of the program path alone would mark them clean; a script that reads other files is listed as "runs a script; what it reads was not checked" |
| 11 | Cron and at | `/etc/crontab`, `/etc/cron.d/*`, the `cron.hourly/daily/weekly/monthly` folders, `/var/spool/cron/crontabs/*`, `/etc/anacrontab`, `/var/spool/cron/atjobs`; the first word of each command that is a path is walked as in row 10 | the same | verified: user crontabs need root to read (lab R16) |
| 12 | Setuid and setgid files | `find <each local filesystem> -xdev -perm -4000 -o -perm -2000`; each file checked with `dpkg -S` on both its `/usr` path and its legacy alias, and `dpkg -V <package>`, whose `5` column means the checksum changed (it exits 0 even then) | a setuid-root file that no package installed, or that was changed, can be a planted way back to root | verified: 29 files in 11 seconds; `dpkg -S` failed for the `/usr` path of two files and found them at the legacy path; `dpkg -V` exit 0 with changes (lab R17) |
| 13 | File capabilities | `getcap -r /`; flagged when a dangerous capability (`cap_setuid`, `cap_setgid`, `cap_sys_admin`, `cap_dac_override`, `cap_dac_read_search`, `cap_sys_ptrace`, `cap_sys_module`, `cap_chown`, `cap_fowner`) sits on a file no package installed | the same | verified: 3 seconds; `cap_net_raw` on `ping` and the `snap-confine` and `gst-ptp-helper` capabilities found (lab R17) |
| 14 | Tools that act as root on request | Cockpit (root through the signed-in user's own sudo or polkit; source: Cockpit `src/cockpit/superuser.py:33-60`); Dockge (row 8); a root service whose command line carries a known "run commands" switch from a fixed table (for example a backup server started with its actions enabled); Seraphina's own helpers, shown as "Seraphina's system access: only these actions", with their verbs; a disk-protection daemon whose web page or API can start a sync or change its own safety thresholds (BACKUPS_SNAPRAID), and a web file manager that runs as root with the data disks mounted | each runs commands as root for whoever can use it | judgement |
| 15 | Tailscale SSH | a host tailscaled's settings, when there is one | Tailscale SSH can give a shell without any key on the box | UNVERIFIED how to read it; sidecars' `TS_EXTRA_ARGS` is never read (§6.5), so for them this is a "can't check" line |
| 16 | The owner's login gateway | the gateway's stack (§2.1), its config files' owners and modes (walked as in row 10, never read beyond `ingress`), and whether its sign-in service's admin page is reachable | whoever can change its config or its stack, or administer its sign-in service, decides who reaches every app behind it; not root, but every app's front door | judgement; observed: one gateway with 15 home-network ports (443 and 14 routes) and port 80 on every address; reported: about 14 apps behind it, one per route port |

### 8.2 How it is shown

**The page title is a question the owner asked:** "Who can take full control of
your server?" Under it, one sentence, always in this shape: "Seraphina found 11
ways. 2 are new since you last looked. Some kinds can't be checked from here.
[What Seraphina can't see]" (judgement). A **way** is a row that gives full
control now: uid 0, a usable root password, full sudo (or a command list with
an escape, row 3), a root-equivalent group, a holder of the Docker socket, a
container in row 9's first group, Dockge's write paths, and root services,
timers, cron jobs, setuid files or capabilities others can change. Root inside
a container, ordinary devices such as GPUs, and host networking are listed
under their own heading, "Could matter if something escapes", and never
counted, because with a few dozen containers most of them run as root inside
and a count that includes them teaches the owner to stop reading (judgement).
The login gateway (row 16) is shown under Programs and never counted as a way
to take full control: it decides who reaches the apps, not who is root.

| Group | One row, for example |
| ----- | -------------------- |
| **People** (accounts) | "<owner account>: can act as root with its password (a sudo rule in /etc/sudoers, and the sudo group). 2 SSH keys: a laptop key (no limits) and a phone key (only from your home network)." |
| | "<an operator account>: no root. It can run these commands as root: none." |
| **Programs** (services and containers) | "Dockge: can control Docker, which can control the whole server. Anyone who can sign in to Dockge can too, and Dockge can be reached from your home network." |
| | "The monitoring agent: can control Docker (its socket is mounted read-only, which does not limit what it can ask)." |
| | "Seraphina's system access: only these actions: look, refresh package lists, keep previous versions." |
| | "Your login gateway: decides who can reach about 14 apps. Anyone who can change its files or its Dockge stack, or sign in to its sign-in service as an administrator, can let anyone in." |
| | Could matter if something escapes: "The file manager can write your backup and parity disks, and its page opens without a password from any address; a remote-desktop password stands behind it." |
| **Doors** | "Password sign-in over SSH is on." · "The root password is set." · "Cockpit gives root to anyone who signs in and has sudo." · "Two sudo programs are installed (sudo-rs as `sudo`, and classic sudo as `sudo.ws`). A rule that one ignores, the other may honour." |
| **Files** | "A root service runs <folder>/start.sh, which the account <name> can change." · "1 setuid file was not installed by any package." |

Each row has **Why** (the evidence, plain then exact), **Since** (first seen),
and **I know about this** with an optional note. A row is identified by its
kind, its subject and its target: an app id (§2.1) or an account, a unit or a
path, and the thing granted, never a container id. So when Dockge recreates the
Beszel agent or Dockge itself at an update, its socket row stays the same row,
keeps its mark and raises nothing (T23). After the first review, a new row that
is a way and is not marked raises a warning in Needs you ("A new way to take
full control of your server appeared"); new rows under "Could matter if
something escapes" are listed, not raised. A row that disappears is shown as
"gone since <date>" (judgement). The marks live in the harness's own record;
they change nothing on the server.

### 8.3 What cannot be known from here

The page always lists these, because a read-only audit cannot prove it found
everything (judgement):

- polkit rules are JavaScript, and can depend on things that happen at run
  time: they are listed, not decided;
- sudo rules can come from SSSD or LDAP (the `sudoers:` line of
  `/etc/nsswitch.conf`) or from sudo-rs's `@socket`;
- under sudo-rs, which has no `cvtsudoers`, the page can test only the
  accounts `getent passwd` lists, so a rule for a name it does not list (a
  directory account, a user not yet created) is not seen; this line shows only
  when sudo-rs is the installed `sudo` (Q1);
- PAM modules (`pam_rootok`, `pam_wheel`, custom ones) decide outside these
  files;
- anyone who knows the root password, and keys or passwords kept on other
  machines;
- programs running as root that run commands when asked: backup tools with
  actions, Dockge's console, web interfaces with plugins or hooks;
- the tailnet policy (who can reach what) and any Tailscale SSH rules live in
  Tailscale's admin console, not on the server;
- the login gateway's sign-in service keeps its users and their second factors
  in its own database, which the helper never reads, so who may sign in to the
  gateway, and whether they need a second factor, cannot be checked from here;
- with two sudo programs installed, a rule that the two parse differently is
  flagged when one test allows and the other refuses, but a difference no
  `-l` test reaches cannot be seen;
- a bug that lets a program escape a container or the kernel's checks.

### 8.4 Changes (P3)

Five changes, each reversible, each approved by the owner with his own sudo, each
with a timed undo (§8.6). Every one keeps exactly what it replaces and puts
exactly that back:

| Change | What the helper does | Way back |
| ------ | -------------------- | -------- |
| **Remove a sudo rule** that is a whole drop-in file | renames `/etc/sudoers.d/<name>` to `/etc/sudoers.d/<name>.seraphina-off-<ticket>`, which sudo then ignores because of the dot (verified: lab R13), then `visudo -c` | rename it back, then `visudo -c` |
| **Remove one rule** from `/etc/sudoers` or from a drop-in holding several | writes a copy with that one line commented as `# seraphina-off <ticket>: <line>` to a `.new` file in the same folder, checks it with `visudo -cf`, keeps the old file in `kept/<receipt>/`, renames the copy over the old file, then `visudo -c` | put the kept file back, then `visudo -c` |
| **Take an account out of a group** (`sudo`, `admin`, `wheel`, `docker`, `lxd`, `incus-admin`, `libvirt`, `disk`) | keeps that group's exact line from `/etc/group` and from `/etc/gshadow`, then writes each file with only that line changed (the account taken out of the member list, and out of the `gshadow` member and administrator lists) | put both saved lines back, byte for byte |
| **Lock an account** | keeps the account's exact `/etc/shadow` line, then writes it with the password field prefixed by `!` (unless it already starts with one) and the expiry field set to `1`, so the account has expired and SSH keys stop working too, not just the password (docs: shadow(5); UNVERIFIED that Ubuntu's sshd refuses an expired account that uses a key; L12) | put the saved line back, byte for byte |
| **Remove an SSH key** | rewrites that account's key file without that one line, matched by line number and fingerprint, keeping its owner and mode, with the old file kept; the folder is walked one component at a time with `dir_fd` and `O_NOFOLLOW`, because the account can write it | put the old file back through the same walk |

**Account files are edited one line at a time by the helper itself**, not with
`usermod`, `gpasswd` or `chage` (judgement). Their pairs are not inverses
(verified: lab R24, on a scratch copy of the account files): `usermod -U` after
`usermod -L -e 1` refused to unlock an account whose password field had been
empty or `!` and left it marked locked, while still exiting 0; it turned a
password that had been disabled on purpose (`!` before a hash) into a usable
one; and it left the expiry in place. `gpasswd -a` after `gpasswd -d` put the
member back at the end of the list and added it to `gshadow` where it had not
been. So the helper writes each file the way the shadow tools do, under the
locks they take (the `<file>.lock` link and `lckpwdf(3)`, called through
`ctypes`; source: shadow-utils `lib/commonio.c`, not read here, UNVERIFIED; L12
checks that a `passwd` run at the same moment waits or fails cleanly): a copy
with the one line changed, the same owner, group and mode, `fsync`, then
`rename` over the old file. The undo puts the saved line back only when the
current line is still exactly the one the change wrote; if someone changed it
since, the undo touches nothing, the receipt says "changed since; not undone",
and the owner gets a high notice with the saved line's way back as a new ticket.
When `nscd` or SSSD serves accounts (the `passwd:` line of
`/etc/nsswitch.conf`, or a running `nscd`), the change is refused by rule 4 of
§8.5, because the files would not be the whole story.

Each change is `undo_instantly` (the old state is kept and put back exactly) with
the `management_plane` flag, because it changes who can get in (v2 §5.8, §7.7;
v2 §9.3 lists `/etc/sudoers*` and every `authorized_keys` as access-controlling
targets): never automatic, never in a batch, approved by a person. Adding a way
in (a key, a rule, a group) is `opens_access` and is not offered; nor is editing
`sshd_config`, such as turning off password sign-in over SSH, which the page
explains with a copy block for the owner to run at home (judgement).

### 8.5 Lock-out protection

The helper refuses a change, with a reason, when (judgement):

1. it would take an account in `owners` out of an admin group, or remove a
   sudo rule that `sudo -ll -U <owner>` names as one of that account's grants,
   unless another grant of full `(ALL) ALL` for that same account remains;
2. after it, no account would be in `owners`, not locked, not expired, and
   have full sudo by a rule the helper can see. This rule alone decides a
   lock: a locked account can use none of its grants, however many it has, so
   locking an owner account is allowed only while another owner account keeps
   full, usable sudo;
3. it touches root's password or root's lock, or Seraphina's own sudoers files
   (those change only through `setup`, §6.11);
4. the enumeration is older than 10 minutes (it runs again first), any sudo
   rule comes from somewhere the helper cannot read (SSSD, LDAP, `@socket`), or
   accounts come through `nscd` or SSSD (§8.4): "Seraphina can't be sure. Do
   this in Cockpit's terminal.";
5. another access change is pending or its timed undo is still running;
6. it is within 30 minutes of the nightly power-off, inside a quiet period, or a
   server restart is armed (§8.6 step 8).

After applying, and before telling the owner it is done, the helper checks
again: `visudo -c` passes; `sudo -l -U` still shows full sudo for an owner
account that is not locked; an edited key file still parses; and a changed
account-file line reads back exactly as written (`getent shadow <user>`,
`getent group <group>`, `getent gshadow <group>`) with every other line of the
file unchanged. If any check fails, it puts the saved lines and files back at
once and compares them byte for byte.

### 8.6 The approval and the timed undo

1. The owner presses **Remove this key** (for example). The harness sends
   `access.ticket {kind, target}`; the helper checks the request and §8.5,
   records ticket `T-0007` (valid for one hour), and returns its own plain-words
   text and the command.
2. The cockpit shows: "To make this change, open Cockpit's terminal [Open
   terminal] and paste: `sudo /usr/local/libexec/seraphina/sf-system approve
   T-0007`" with a copy button.
3. In that terminal, signed in as himself and giving his own password to sudo,
   the owner sees the helper's own words: what will change, for whom, how it is
   undone, and the 10-minute rule; and types `yes`. The `approve` mode needs a
   terminal on standard input and output and a `SUDO_UID` in `owners`.
4. The helper arms the undo first, `systemd-run --on-active=10min
   --unit=seraphina-system-undo-T-0007 /usr/local/libexec/seraphina/sf-system
   run undo T-0007` (the `run` mode is accepted only inside its own unit), the
   same shape as `netplan try`'s automatic revert (docs: netplan
   `doc/netplan-try.md:16-55`); then applies the change; then checks it (§8.5).
5. It prints: "Done. Now prove you can still get in. Leave this window open.
   In a private browser window, or on another device, sign in to Cockpit again,
   open its Terminal and run `sudo /usr/local/libexec/seraphina/sf-system
   confirm T-0007`. A new SSH login works too. A second terminal in this same
   Cockpit window does not count. If you don't confirm within 10 minutes, the
   change undoes itself." The harness, which follows the ticket through
   `tickets`, sends the `access.pending` notice (§10.2).
6. `confirm` needs an owner's `SUDO_UID`, a terminal, and a login that started
   after the change was applied. Every terminal of one Cockpit sign-in runs
   inside that sign-in's one login session: `cockpit-session` opens one PAM
   session and starts the bridge in it, and each terminal is a channel the
   bridge spawns (source: Cockpit `doc/authentication.md:215-247`,
   `pkg/systemd/terminal.jsx`), so a new terminal is not a new login. And an SSH
   session opened before the change still holds its old credentials, because a
   lock, a removed key or a removed group affects only new logins. So the
   helper takes the logind session of its caller from the `session-<n>.scope` in
   `/proc/self/cgroup`, reads that session's start time and user from logind
   (`loginctl show-session <n> -p Timestamp -p Name -p Service`), and accepts
   only a session of an owner account that differs from the approving one and
   started after the receipt's apply time, in the same boot (docs:
   org.freedesktop.login1(5); UNVERIFIED here; L11). It cancels the timer and
   closes the receipt as verified: "You confirmed from a new login at 14:12." A
   login made after the change that can still use sudo is the proof that
   matters: the owner is not locked out.
7. With no confirmation, the timer's unit puts the kept files and account-file
   lines back, compares them byte for byte with the saved copies, closes the
   receipt as "undone by the timer"; the harness sends `access.undone` when it
   next reads the ticket.
8. **Surviving a restart.** Transient timers do not survive a reboot (docs:
   systemd-run; judgement), so a pending undo could be lost to a power cut. So
   F2 waits while an undo is pending, a ticket cannot be approved within 30
   minutes of the power-off, and from P3 the helper's install adds one small
   root unit, `seraphina-system-boot.service`, which runs `sf-system run boot`
   at every boot and performs any undo whose time has passed, before anyone signs
   in (D18). It is one more file on the removal list (§6.11).

v2 §7.7 confirms management-plane changes on the executor's safety page through
the new path, and requires two independent remote paths. Before the executor,
the confirmation is the owner's own sudo from a login made after the change, and the two-paths
rule is not checked; this is stated on the page (Appendix A10).

---

## 9. The terminal

**Open terminal** opens Cockpit's terminal, `<Cockpit>/system/terminal`, in a new
tab (source: Cockpit `pkg/systemd/manifest.json`, `pkg/systemd/terminal.jsx`).

- **The owner signs in as himself.** Cockpit's web service starts a PAM session
  for the typed user and password and forwards further prompts, such as a
  second factor or an expired password; the terminal runs that user's own shell
  in his home folder; "Administrative access" re-launches Cockpit's bridge
  through sudo or polkit, so root in Cockpit still depends on the owner's own
  sudo (source: Cockpit `doc/authentication.md:24, 215-247`,
  `src/cockpit/superuser.py:33-60`, `pkg/systemd/terminal.jsx:32-42`).
- **Deep links.** From an app with a Compose folder, "Open a terminal in its
  folder" adds `#/?path=<folder>`, which errors if the folder does not exist
  (source: `terminal.jsx:32-42, 91-154`). From a system service: its page and
  its logs (§2.7). From an account row on the root page: `<Cockpit>/users#/<name>`.
  From Check 1: `<Cockpit>/updates`, with the warning that updates installed
  there happen outside any maintenance time (v2 §6.5) (source for the URLs:
  Cockpit `doc/urls.md`, `pkg/systemd/manifest.json`).
- **In its own tab, never embedded.** Every Cockpit response carries
  `X-Frame-Options: sameorigin` (source: Cockpit
  `src/ws/cockpitwebresponse.c:748, 861`), and the cockpit's own pages send
  `frame-ancestors 'none'` (source: `swfactory/server.py:110-112`).
- **Behind Tailscale serve**, the TLS ends in the sidecar, so Cockpit must run
  with `--for-tls-proxy` or list the `https` origin in `cockpit.conf`
  `[WebService] Origins`, or the terminal's WebSocket is refused (source:
  `src/ws/cockpitwebservice.c:1297-1328`, `src/ws/main.c:162, 195-196`). The
  doctor reads `/etc/cockpit/cockpit.conf` (already on the Blueprint's list;
  source: `collect.py:84-94`) and Cockpit's unit drop-ins and warns when
  neither is set while Cockpit's confirmed address is a tailnet name (§11.7).

**Why there is no built-in terminal** (judgement):

1. A web shell in the cockpit would run as the harness's user, and anyone holding
   the master token would hold a shell; the `sf_token` cookie holds the master
   token itself (source: `swfactory/server.py`, DESIGN §14).
2. The harness's own agents run as that same user; a terminal there is one more
   thing they could drive.
3. Cockpit already does sign-in, second factors, sessions and privilege
   properly, and its logs show the owner's own name, not Seraphina's.
4. "Seraphina's own root access" is the helper's fixed list (§6), which can be
   recorded, undone and removed with one file; a shell can be none of those.

**When Cockpit cannot be reached.** If Cockpit's tailnet route runs in a sidecar
container, stopping the Docker engine removes it with every other route (§1.2).
The terminal button then says: "Cockpit can't be reached from here: its
Tailscale connection runs in Docker, which is stopped. At home, use SSH on your
home network." D22 recommends giving Cockpit, and the cockpit, a path that does
not depend on Docker: a host-native tailscaled, as v2 §7.2 designs, which is a
separate change under §7.7's rules. Cockpit's Networking page talks to
NetworkManager, which may not be what manages the server's network (netplan with
networkd is Ubuntu Server's default), and a server may run both
systemd-networkd and NetworkManager at once (observed), so that page may edit a
stack that does not own the network interface; the terminal card says "Change
networking only with Seraphina's help or at home" (judgement; v2 §2.3 Network,
L14). A tailnet-proxy drop-in for Cockpit's web service and a plain-HTTP port
that redirects to the home-network address were seen (observed); which scheme
it redirects to was not recorded, so the doctor's `Origins` check of §11.7 still
reads the drop-in before it says anything (Q10).

---

## 10. Privacy and security

### 10.1 What is private, and where it stays

| Data | Where it lives | Shown | Never |
| ---- | -------------- | ----- | ----- |
| App, stack, container and unit names | the harness's record; the helper's answers | the cockpit | a phone, unless the owner allows that app's name (§10.2); a model by default; this repository |
| Tailnet names, addresses, QR codes | the harness's record (`apps.json`) | the cockpit | a phone, a tracker item's text, an event, a model by default |
| Mount sources, data folders, folder names from `du`, stack paths | the helper's answers, passed through | the cockpit, a page at a time | `<data_dir>` (the record keeps member identities, §2.1, not paths), events, a phone, a model by default |
| Logs | the containers' log files and the journal, read through the helper (class `private`), a page at a time, redacted inside the helper and again by the harness | the cockpit | `<data_dir>`, a model unless the owner sends a page, a second copy anywhere |
| Processes behind listeners | the helper's `ports`: process name and PID only, never the command line | the cockpit | — |
| Account names, SSH key comments and options | the helper's `access` result (class `private`), root-side | the cockpit | a phone, a model, a caller that is a person's login account |
| Receipts | root-side | the cockpit, a page at a time | `<data_dir>` beyond ids, verbs, classes, outcomes and counts |
| Tailscale state | never leaves the helper's memory beyond the three decoded keys (§6.5) | the decoded names and entries | anywhere else |
| The Beszel password | the harness's secret store, bound to the hub's origin | — | settings, logs, a model |
| The approval key | `/var/lib/seraphina-system/approval.key`, root, 0600 | — | the harness |

`<data_dir>/yourserver` (0700, files 0600) is added to `config.sandbox_masks`
(source: `swfactory/config.py:398-446`; today the list does not include it).

### 10.2 What reaches a phone

Phone text is built by code from typed fields, and `filter_text` replaces
anything that looks like a link, a path, a token or a long number with the
short notice (source: `swfactory/notify.py:8-20, 117-127`). Kinds (judgement for
the new ones):

| Kind | Priority | Title | Body |
| ---- | -------- | ----- | ---- |
| `service.down` for an app (existing) | high | "A service is down" | "The {subject} service is down." with the app's short name (its id without the leading `a-`, which fits the notifier's subject rule) as subject only when the owner allowed that app's name on his phone, else the subject-free text (source: `notify.py:459-462`). A third-party channel gets it only with `operational: true` (source: `notify.py:8-20`) |
| `service.up` (existing) | default | "Working again" | as today |
| `service.down` for Beszel | high | "A service is down" | "The beszel service is down." (a product's name, allowed by default) |
| `disk.low` (existing) | high | "Disk almost full" | as today, the subject only when allowed |
| `system.failed` (new) | high | "A maintenance task failed" | "It did not finish. Open Seraphina for details." |
| `system.done` (new) | low | "Maintenance done" | "{count} packages updated." and, when true, "A restart is needed." The count is the helper's own (§6.9) |
| `access.pending` (new) | high | "Confirm your change" | "Open a new login and confirm within 10 minutes, or it undoes itself." |
| `access.undone` (new) | high | "A change was undone" | "A change to who can control your server was undone because it was not confirmed." |
| `access.new` (new) | default | "New full-control access found" | "Seraphina found {count} new way(s) to take full control of your server. Open Seraphina." |

The internal source of these is `yourserver`, a new case in `_incident`, which
today allows a subject only for the owner or a hook's allowlist (source:
`notify.py:2272-2282`); here it is allowed per app by `services.<app
id>.phone_name`. New kinds touch `RULE_IDS`, `RULE_ROUTES`, `operational_text`,
the `rule_priority` defaults, settings validation and the DESIGN §34 table
(source: `notify.py:89-97`; the same list as BACKUPS_SNAPRAID §8.4).

### 10.3 What agents can and cannot see or do

- **While the agents' sandbox is off** (`harness.sandbox: "none"`, the shipped
  default; source: `swfactory/config.py:110`), every agent process runs as the
  harness's user and can call the helper exactly as the server does, with no
  password: whatever classes that user has. While the harness runs as the
  owner's login account (P1a, P1b), that is `read`, `refresh` and `keep`: the
  server's layout, a package-list refresh and new keep tags; the helper itself
  refuses logs, the root-access details and every other change to a login
  account (§6.1, §6.7). Once the harness runs as its own user, its agents would
  get the `private` class (logs, the root-access details) and the click-class
  verbs (restart an app) too; your code and your own sudo still block everything
  else (§6.9). So the doctor reports an **error** while `private` or any of
  `maintain`, `fix` and `access` is allowed and the sandbox is off or an agent
  route uses a CLI driver, which runs outside `bwrap`, the same pair it already
  checks for phone messages (source: `swfactory/doctor.py:984-991`), and a
  **warning** for `read`, `refresh` and `keep` alone, which names what they
  reveal (D20).
- **Under `bwrap`**, sudo fails for agents, because sudo refuses to run under
  `no_new_privs` (verified: lab R14) and bubblewrap sets it for what it runs
  (docs: bubblewrap's README, UNVERIFIED here; L7 checks it), and the masks hide
  `<data_dir>/yourserver`.
- **An agent that can write files as the harness's user** could change an app's
  address in `apps.json`; the hash chain makes the cockpit stop and ask (§2.3).
- **The AI panel** sends typed facts only (§2.8).
- **Names, labels, log lines and image descriptions are data**, written by image
  publishers and apps, never instructions; when the owner sends any of them to a
  model, the turn is tainted as WAKE_AND_MESSAGES's rules say (judgement).
- **No agent route.** None of this design's routes is in `AGENT_TOKEN_ROUTES`,
  and no persona or agent tool list contains a changing route (T29).

### 10.4 Threats, closed and left open

| Threat | This design | Left open, and why |
| ------ | ----------- | ------------------ |
| Seraphina breaks and takes the apps with it | it is never in the request path (§5) | — |
| Seraphina's user gains root through the Docker socket | it is never in the `docker` group, and the harness never runs as an account in `docker`, `lxd`, `incus-admin`, `disk` or `sudo` (§6.1; the doctor errors, the helper refuses with `caller_holds_root`); the helper uses a fixed list of calls (§6.4) | anyone already in the `docker` group, which the root page shows |
| The owner's login gateway changes or breaks | Seraphina is never in its path (N11), probes upstreams rather than routes (§2.5), and reads only the `ingress` fields (§6.3), so it keeps working and its app lights stay true; the gateway's own card says it is down | Open links through the gateway fail until it is back; tailnet addresses still work; a gateway change that sends a route to another upstream is detected at the next `ingress` read and asks the owner again (§2.3) |
| A compromised harness widens what the helper may do | `allow` and `callers` live in a root-owned file changed only through the owner's copy blocks (§6.11) | — |
| A compromised harness runs a package change the owner did not approve | your code; the list hash; the helper's own counts on the phone afterwards | the code is not bound to the list (§6.9) until the executor's safety page |
| A root-access change locks the owner out | §8.5 refusals, the post-check, the timed undo, the boot unit and the confirmation from a login made after the change | a server that does not boot at all; the console is then the way in |
| Unsandboxed agents read private data through the helper | logs and root-access details only for Seraphina's own user, enforced by the helper (§6.7); log pages redacted inside the helper; the doctor's error and warning; the masks under `bwrap` | while the harness runs as the owner's account, its agents get the server's layout with no password; once it has its own user and while the sandbox stays off, they get what that user gets (D20) |
| A phishing link in place of an app's address | addresses only on the confirmed tailnet or this server's home address; the hash chain (§2.3) | a forged address on the owner's own tailnet, written while the harness was stopped; an attacker who controls the tailnet itself |
| A sidecar's state file planted with a link to a root-only file | the no-follow walk; only JSON is parsed; only three keys decoded (§6.5) | — |
| Clean-up deletes the way back | keep tags first; never `prune -a`, container, network, volume or system prune (§7.3) | an owner or tool running those by hand; the root page cannot stop that |

---

## 11. Fit with the harness

### 11.1 Files

| File | New or changed | What |
| ---- | -------------- | ---- |
| `bin/sf-system` | new | the helper (§6), one standard-library file, never imported by the harness at run time; it carries its own frozen copy of the redactor's patterns (§7.2 Check 8) and its own sudoers and boot-unit templates (§6.11); the owner installs a copy (§6.11); tests load it by path |
| `swfactory/helpers.py` | new | `HelperClient`, shared with the SnapRAID design's harness side: `sudo -n <helper>` through an injectable runner, one JSON line each way, exit codes mapped, the `rid` and the outcome rule of §6.8, never a retry |
| `swfactory/yourserver.py` | new | the area: the cgroup and `/proc` readers, grouping, the app record with its hash chain, the address candidates, the probes and lights, the host strip, the Beszel watcher, the Checks, the receipts mirror (ids and counts), the app change log, the Home status, notices and tracker items |
| `swfactory/qr.py` | new | the QR encoder (§2.4) |
| `swfactory/config.py` | changed | DEFAULTS for `yourserver` and `services`, `windows` added to the SnapRAID design's `maintenance`; `<data_dir>/yourserver` in `sandbox_masks` |
| `swfactory/settings.py` | changed | `yourserver` and `services` in `UPDATABLE_SECTIONS` (source: `settings.py:44-46`), with `_check_yourserver` and `_check_services` |
| `swfactory/server.py` | changed | the routes of §11.3 in `_ROUTES`, path rules in `_PARAM_RULES`; none in `AGENT_TOKEN_ROUTES` |
| `swfactory/__main__.py` | changed | `factory apps` and `factory system` (§11.4) |
| `swfactory/orchestrator.py` | changed | `Factory.start` and `stop` start and stop the area's thread; `Factory.yourserver_*` methods for the routes |
| `swfactory/notify.py`, `swfactory/rules.py` | changed | the kinds of §10.2 and the internal source `yourserver` |
| `swfactory/items.py` | changed | `app` in `LINK_KEYS` (source: `items.py:68`) |
| `swfactory/activity.py` | changed | the source `system`, appended last, after the SnapRAID design's `snapraid` (source: `activity.py:51`) |
| `swfactory/doctor.py` | changed | one `_safely("your server", …)` line (§11.7) |
| `swfactory/channels.py` | changed | `tailnet_status` falls back to the confirmed tailnet suffix when the host has no `tailscale` CLI (Appendix A11) |
| `swfactory/blueprint/collect.py` | changed | a helper mode for the root probes (§11.9) |
| `swfactory/web/views/yourserver.js` | new | the area (§11.5) |
| `swfactory/web/index.html` | changed | one script tag; the phone bar (D21) |
| `systemd/seraphina.service` | new, before P2 | a system unit for the harness's own user, with v2's hardening except `NoNewPrivileges` (§6.1) |
| `tests/gui_mock/yourserver.py`, `tests/gui/yourserver.mjs` | new | the mock backend and its smoke suite (§11.6) |
| `tests/test_gui_static.py`, `tests/gui_mock/__init__.py`, `tests/gui/smoke.mjs` | changed | `yourserver` added to `AREAS` and the smoke imports (source: `tests/test_gui_static.py:27`, `tests/gui_mock/__init__.py`) |
| `tests/fixtures/sfsystem/` | new | recorded outputs with every id, name and path replaced by a placeholder: Docker 29.9.1 state files, `ss`, `smartctl -j`, `needrestart -b`, `apt-get -s`, `sudo -l`, `cvtsudoers`, `sshd -T`, key files, a Tailscale state file with planted key material, serve configs, `systemctl --output=json` (`list-sockets` included, with a socket-activated service idle and running), `apt-cache show` pairs tied by exact versions (systemd with `libsystemd0`, `openssh-server` with `openssh-client`), Compose standard error that quotes an env file, scratch account files (an empty password field, a `!` field, a `!` before a hash, a hash, group lines in a non-alphabetical order) |
| `tests/test_sf_system.py`, `tests/test_yourserver.py`, `tests/test_qr.py`, `tests/test_yourserver_lab.py` | new | §12.2 |
| `DESIGN.md` | changed | §2 layout (one line, with this design), then §11, §14, §16, §34 and a new section when built |

`index.html`, the two `AREAS` lists, `smoke.mjs` and DESIGN.md §2's list are the
lines the other GUI and design branches also change, so merging them conflicts
in those lines only (judgement).

### 11.2 Settings

Harness config, GUI-editable (judgement for every default):

```json
{
  "yourserver": {
    "enabled": false,
    "helper": "/usr/local/libexec/seraphina/sf-system",
    "refresh": {"live_seconds": 15, "idle_minutes": 5, "probe_seconds": 60, "map_minutes": 5},
    "stale_minutes": 10,
    "tailnet": {"suffix": null, "confirmed_at": null},
    "links": {"dockge": null, "cockpit": null, "beszel": null},
    "beszel": {"watch": true, "login_secret": null, "stale_minutes": 15},
    "updates": {"registry_checks": false},
    "keep_tag_prefixes": [],
    "host": {"disk_warn": {"free_percent": 15, "free_gb_cap": 100},
             "disk_err": {"free_percent": 5, "free_gb_cap": 20},
             "disks": {"<disk label>": {"warn_gb": null, "err_gb": null, "full_on_purpose": false}},
             "ram_warn_percent": 90,
             "temp_warn_c": {"cpu": 90, "nvme": 70, "gpu": 85}},
    "restart": {"max_wait_minutes": 120},
    "logs": {"vacuum_days": 30}
  },
  "maintenance": {"mode": "scheduled", "paused": false,
                  "windows": {"sun-evening": {"days": ["sun"], "start": "19:00", "minutes": 120, "owner_present": true}}},
  "services": {
    "<app id>": {"maintenance": {"mode": "inherit"}, "phone_name": false,
                 "probe": {"kind": "auto", "path": "/", "target": "upstream", "ca_file": null, "pin_sha256": null},
                 "expect": {"state": "run", "grace_minutes": 15, "confirmed_at": null}},
    "os-packages": {"maintenance": {"mode": "inherit"}},
    "os-cleanup": {"maintenance": {"mode": "inherit"}},
    "docker-cleanup": {"maintenance": {"mode": "inherit"}},
    "server-restart": {"maintenance": {"mode": "inherit"}}
  }
}
```

`maintenance.mode` and `maintenance.paused` are the SnapRAID design's interim
keys (BACKUPS_SNAPRAID §9.2); `windows` takes v2 §6.6's shape. Links and the
tailnet suffix are validated like `notify.cockpit_url` (an `https` name under
the confirmed suffix, or loopback; source: `swfactory/settings.py:1013-1015,
1169-1185`) or a home-network address, labelled. The confirmed app addresses are
not settings: they live in `<data_dir>/yourserver/apps.json`, the cockpit's own
record, with its hash chain (§2.3). What the helper may do (`allow`, `callers`,
`owners`, `keep` limits) lives only in its root-owned config (§6.11); the GUI
shows it and stages changes as copy blocks. The server section's
`allowed_hosts` stays a config-file edit (source: `swfactory/settings.py:44-46`;
`config.py:65`), which matters when the owner reaches the cockpit through a
sidecar under a tailnet name (Q14). Neither `server.host` nor
`server.allowed_hosts` may hold a home-network address that other web apps
listen on, on any port (N10; the doctor's error, §11.7).
`services.<app>.probe.target` is `upstream` (the default, §2.5) or `route`,
which probes the gateway route itself and needs `ca_file` (the gateway's public
certificate) or `pin_sha256` (the leaf certificate's SHA-256) for an `https`
route; there is no setting that turns verification off.
`keep_tag_prefixes` lists the owner's own local tag families (§2.6), proposed
from the tags found and confirmed once.

### 11.3 Routes

All under the master token, documented in the DESIGN §14 table, answered with the
same shapes by the GUI mock:

| Method and path | Does |
| --------------- | ---- |
| `GET /api/yourserver` | `{helper {installed, allow, unsafe, words}, host {…, observed_at}, apps_summary {count, by_tone}, beszel {tone, reason}, needs, stale}` |
| `GET /api/yourserver/apps` | the cards (§2.2), each with `light {tone, reason}`, `address {url, state: confirmed\|proposed\|changed_outside\|none}`, numbers with `observed_at` |
| `GET /api/yourserver/apps/{app}` | one app: card, members, candidates, privileges in words, backup lines, the last 50 change-log lines |
| `POST /api/yourserver/apps/{app}` `{name?, icon?, hidden?, note?, phone_name?, probe?, expect?, main_member?}` | the owner's corrections, the expected state and the main member (§2.5) |
| `GET /api/yourserver/review`, `POST /api/yourserver/review` `{rows: [{app, name?, url?, home_only?, expect?, old_copy?}]}` | the first-run review list and its **These are right** (§2.3) |
| `POST /api/yourserver/apps/{app}/address` `{action: confirm\|reject\|set\|home_only, candidate?, url?}` | §2.3 |
| `GET /api/yourserver/apps/{app}/qr` | `{size, rows}` (§2.4) |
| `POST /api/yourserver/groups` `{action: merge\|split\|move\|retire\|keep\|undo, …}` | regrouping, and marking an old copy retired or part of the app (§2.1) |
| `GET /api/yourserver/apps/{app}/logs` `{member, lines?, before?}` | one redacted page from the helper (Check 8) |
| `POST /api/yourserver/apps/{app}/ask` `{question}` | the facts of §2.8 and the model's answer |
| `GET /api/yourserver/checks`, `POST /api/yourserver/checks/{check}` | the last results; run one Check now (`apt.refresh` from Check 1's button) |
| `GET /api/yourserver/root`, `POST /api/yourserver/root/{row}/mark` `{known, note?}` | §8.2 |
| `POST /api/yourserver/root/changes` `{kind, target}` | P3: a ticket and the command to paste (§8.6) |
| `GET /api/yourserver/tickets/{ticket}` | its state, for the countdown |
| `POST /api/yourserver/maintain/{action:updates\|packages\|logs\|docker}` `{set?, list_sha256, typed?, code?, when: now\|window, rid}` | P2: §7.3; 202 `{job}`, or 409 `{error, words, blocked_by}` |
| `POST /api/yourserver/fix/restart` `{app?, member?, code?, anyway?, rid}`, `POST /api/yourserver/fix/reboot` `{max_wait_minutes?, code, here?, rid}` | P2: §7.4 |
| `POST /api/yourserver/jobs/{job}/cancel`, `GET /api/yourserver/jobs`, `GET /api/yourserver/jobs/{job}` | jobs |
| `GET /api/yourserver/receipts` `{cursor?}`, `GET /api/yourserver/receipts/{receipt}`, `POST /api/yourserver/receipts/{receipt}/undo` `{code?, rid}` | §6.10; undo is the receipt's `way_back` verb |
| `GET /api/yourserver/setup`, `POST /api/yourserver/setup/{action:stage\|check}` | §6.11, returning copy blocks |

Errors follow `server.py`'s mapping (ActionError 409, KeyError 404, ValueError
400; source: `swfactory/server.py:1810-1953`). The helper's `words` are shown as
they are. `GET` routes never call a changing verb.

### 11.4 CLI

`factory apps` lists the cards in plain text (data on stdout, hints on stderr,
`--json` for scripts; DESIGN §16): `factory apps [show <app>] [qr <app>]`, the QR
drawn with block characters. `factory system` prints the host strip and the
helper's state: `host`, `check <name>`, `root`, `receipts [<id>]`, `helper
stage|check|remove` (copy blocks). P2 adds `factory system restart <app>
[--code N]`. There is no CLI for Maintain or for root-access changes: the first
must follow reading the list in the cockpit, and the second is approved in the
owner's own terminal anyway (judgement).

### 11.5 The GUI: a "Your server" area

`swfactory/web/views/yourserver.js` follows the view-module contract (one IIFE
taking `window.SF`, `SF.registerView`, no URL literal, no `innerHTML`, `fetch`,
`EventSource`, `localStorage` or `eval`; source: `tests/test_gui_static.py:122-149`).
Area id `yourserver`, routes `apps` and `server` (both free: neither is in
`CORE_ROUTES`; source: `swfactory/web/app.js:8162`), nav label "Your server",
icon `server` (an existing symbol in `index.html`), nav order 5, between Home (0)
and Factory (10) (source: the order list in `app.js:8321-8330`; the area views
use 25, 40, 45 for the SnapRAID design's `backups`, 70 and 75).

- **Phone bar.** The bar holds six entries and already has Home, Factory, Plans,
  Chat, Settings and Messages (source: `index.html:126-137`, `app.js:8328`,
  `views/messages.js:23`). The launcher is what a phone is for, so D21 proposes
  that Your server takes Plans' place on the phone bar, Plans staying one tap
  away on Home.
- **Home card.** Title "Your server", order 5, text "Every app with its address,
  how your server is doing now, and who can take full control of it." Its status
  is the server sentence: "All 23 apps working. 1 needs you." with tone `err`
  when an app is down or the helper refuses, `human` when something waits for the
  owner, `warn` for Beszel or a new root row, `ok` otherwise (the tones Home
  takes; source: `app.js:8163`). Its `needs`: an app down (`err`), "Check 23
  app names and addresses" (`human`, one item, the review list of §2.3), an
  app that should be running and is stopped (`warn`), a pending root-access
  confirmation with its countdown (`human`), Beszel down or not recording
  (`warn`), a new way to take full control (`warn`). Needs you holds only what
  the owner can act on from the item itself: "restart needed" and "updates
  waiting" join it as `todo` items from P2, when their buttons exist; before
  that they are lines on the Check page and in the host strip, never items
  (judgement).
- **Planned cards.** `HOME_PLANNED` keeps its six cards (source: `app.js:8462-8478`);
  System Maintenance and Systems Audit stay planned, because this design ships
  their buttons and views without their lines (v2 §8.3, §8.4).
- **Novice words.** U8 is this design's own word check, in
  `tests/test_yourserver.py`, until v2's `tests/test_gui_words.py` exists: no
  novice template in `swfactory/yourserver.py` or `yourserver.js` contains
  "cgroup", "sidecar", "verb", "allow class", "receipt", "keep tag",
  "dangling", "build cache", "ticket", "probe", "member", "helper",
  "sf-system", "prune", "namespace" or "PID".

### 11.6 The mock backend

`tests/gui_mock/yourserver.py` (`AREA = "yourserver"`, a docstring naming this
design) seeds the states the cockpit must draw: helper absent (the summary
card with process names); helper read-only; nothing confirmed (the review list
of 23 apps, some rows ticked in advance); a mix of green, amber, red, grey and
off; an app down; an app that should run, stopped through Docker with exit code
137; an app with an old copy, a retired copy that fails at every boot, and a
one-shot service that finished; the layouts of T10 (a sidecar in its own stack,
two projects from one folder, one sidecar serving two apps, a sidecar reaching
its app by name on a shared network, the app sharing the sidecar's network
namespace, a compose file in a home folder); a "How to go back" panel; an
address changed outside Seraphina; Docker stopped; `unsafe_install`;
Beszel down, Beszel running but not recording, Beszel without a login; the GPU
driver not loaded; stale numbers; a root page with 11 rows, 2 new and 3 marked
known, with sudo-rs's "can't list" line; a pending access ticket with its
countdown; an access change undone by the timer; a root page before the move
to Seraphina's own user (the `explain access` copy block); and, for P2, an
updates list with one package that cannot be kept, a family left out with its
copy block and "waits with systemd" lines, a way-back check that failed, "The
list changed since you looked", an approval ahead of a maintenance time partly
installed by Ubuntu's automatic updates, a running package job past its point of
no return, an unknown outcome, and a server restart waiting for a backup. It
also seeds the shapes a real server showed (observed, as placeholders): a login
gateway app with 15 home-network ports (443 and 14 routes) on one host name and port 80 on every address, one of its members
down; sidecars with only `PATH` in their environment; Dockge as a project in a
home folder on a floating tag; two `compose.json` projects; four job runs
sharing an app's image; images referenced by id with local tags only; a web file
manager running with read-write mounts of the backup and parity disks; and a
root page with two sudo programs and a root service running a script from a
home folder. Names are hostile
wherever names are shown: markup, a right-to-left override, a 600-character
stack name, a `javascript:` link in a label, an address on another tailnet.
`mock(…)` switches between them at `/__mock/yourserver/<state>`, and
`contract(test)` checks the mock's shapes against `swfactory/yourserver.py`.
`tests/gui/yourserver.mjs` drives each state.

### 11.7 Doctor checks

One `_safely("your server", …)` line in `run_checks`, read-only, calling only the
helper's `info` (source: `swfactory/doctor.py:74-138`):

| Check | Level when it fails |
| ----- | ------------------- |
| the helper installed, and its hash the release's (the doctor says this catches damage, and a swap only if the owner checks the release notes himself) | warn |
| `sudo -n` reaches the helper; when it does not and `/proc/self/status` shows `NoNewPrivs: 1`, the doctor names that as the cause (verified: sudo's refusal under `no_new_privs`, lab R14) | error |
| the helper's `unsafe_install` | error |
| the harness's user is in the `docker` group | error: "Seraphina's own user can control Docker, and so can its agents" |
| the harness's user is in `lxd`, `incus-admin`, `disk` or `sudo` (the owner's own account, on a server where it holds such a group; §6.1) | error: "Seraphina runs as an account that already controls the server, so its agents do too. Give Seraphina its own account first." |
| `server.host` or `server.allowed_hosts` names a home-network address that other listening web apps share (from `ports`) | error: "Seraphina shares its address with other apps, so they could read its sign-in cookie." (N10) |
| a login-gateway route's upstream is the harness's own port (from `ingress`) | warn, until the owner decides D25: "Your login gateway forwards to Seraphina. Seraphina keeps its own sign-in either way." (N11) |
| a probe of an `https` route whose certificate Python's strict check rejects, with no `ca_file` or `pin_sha256` that accepts it | warn, naming the missing certificate extensions (§2.5) |
| `private`, `maintain`, `fix` or `access` is allowed while `harness.sandbox` is `none` or an agent route uses a CLI driver; only `read`, `refresh` and `keep` in that case | error; warn, naming what those classes reveal (§6.1) |
| `private`, `maintain`, `fix` or `access` is in the config for a login account (the helper refuses it anyway, §6.7) | error |
| `maintain` or `fix` allowed with no approval key enrolled; `access` allowed without the boot unit | error |
| Cockpit's confirmed address is a tailnet name and neither `Origins` nor `--for-tls-proxy` is set | warn |
| Beszel is watched without a login | info |
| a confirmed address no longer matches any detected candidate | warn |
| `<data_dir>/yourserver` is in the masks (checked under `bwrap`) | warn |

### 11.8 Events, activity and the tracker

- **Events** on the EventBus (DESIGN §10), with ids, tones and counts only, never
  names or addresses: `yourserver.updated` (host or app facts changed),
  `yourserver.app` (one app's light changed), `system.job` (started, step,
  ended with its outcome), `system.receipt` (a new receipt id), `access.ticket`
  (pending, applied, confirmed, undone).
- **Activity**: the source `system`, appended last (source: `swfactory/activity.py:51`,
  "new sources go last"), with entries `{at, source: "system", kind: <verb>,
  agent: <caller user>, summary: <fixed template with counts>, evidence: {file:
  "receipts/<id>.json"}}`, and `kind: "outside"` for changes made elsewhere
  (§7.7).
- **The tracker**: system items opened by the factory under stable keys through
  `Factory.system_item` with a dedupe key (source: `swfactory/orchestrator.py:2037-2041`):
  `yourserver:down:<app>`, `yourserver:stopped:<app>`, `yourserver:address:<app>`, `yourserver:root:<hash of kind, subject and target>` (§8.2, never a container id), `yourserver:old-copy-boots:<app>`,
  `yourserver:beszel`, `system:failed:<receipt>`, `system:unknown:<rid>`,
  `system:restart-needed`. Each carries `links.app` when it is about one app. An
  item closes only on a fresh, readable observation that no longer shows the
  finding, the drift sources' rule (source: `swfactory/driftsources.py:28-36`),
  or on a receipt (`receipt` evidence, which code may give; source:
  `swfactory/items.py:58-63`). An unreadable or stale check closes nothing.

### 11.9 The Blueprint

The Blueprint stays read-only, but its root probes stop working without general
sudo (§1.2). This design adds a helper mode, `factory snapshot --helper`, in
which the collector asks `sf-system`'s read verbs for what it used to run under
`sudo -n` (judgement):

- the `docker` section and `indexes.containers` from `containers` (and `docker`
  when allowed), keeping the index's shape (source: `collect.py:1038-1081`), with
  the fields that need the API marked unknown when it is off;
- `ss -p` from `ports`, SMART from `smart`, and, from P1c (class `private`), user crontabs from `access`;
- the compose files Dockge holds from the stacks folder through `compose`
  (hashes and images, not contents);
- and, from P1c, a new capture of users, groups and sudoers from `access`, names kept and
  no secrets, which scope_limits says v0 does not capture (source:
  `collect.py:84-94, 1270-1271`).

Captures run daily and before and after every P2 change (DESIGN §28; v2 §9.4),
and the app change log of §2.8 moves into the Blueprint store when v2's store
exists.

### 11.10 Phases

Each phase is small enough to ship on its own, and each ends with v2's novice
check (v2 §10): someone who has not read this design does the phase's journey,
named below, from the GUI on a phone; the time and wrong turns are recorded; the
pass bar is no help and no terminal beyond the pastes the row names (Appendix
A22 says why some remain).

| Phase | Ships | Preconditions | Done when |
| ----- | ----- | ------------- | --------- |
| **P1a: the launcher and the host strip** | first the host strip, which needs no helper, and the summary card of §2.10; then, with the helper's `read` class and the verbs `info`, `containers`, `compose` (for the project name only), `ports`, `tailscale`, `ingress`, `disks` and `gpu`: the cards, grouping, old copies, the review list with addresses and expected states, QR, health, versions, badges and deep links, the Beszel watch, "Ask about this app", the maintenance toggle and backup lines; the one-paste `setup` and `remove`; doctor; mock and GUI | none for the host strip; for the rest, the helper installed by the owner (one paste) and L7 passed under both sudo programs the server has; the harness may still run as today's user (D1) only where that account holds no root-equivalent group, else it first moves to the system user `seraphina` (§0 item 8, §6.1) | T1-T7, T9-T12, T15-T17, T25-T29, T36-T38; U1-U5, U9, U10, U12; L1 (one read-only read on the owner's host, Q3), L3, L7, L15, L16; **novice check**: set up Seraphina's system access with the one paste, confirm the review list, open three apps from the phone and add one to its home screen |
| **P1b: the Check buttons** | Checks 1-8 with the verbs `docker` (D3), `compose` (hashes and images), `smart`, `needrestart`, `journal`, `du`, `job`, `log`, `receipts` and `apt.refresh`; update kinds and Docker's health on the cards; keep tags if D9 says so, and the receipts list for them; the Blueprint helper mode. Check 8 reads logs itself only once P1c's preconditions hold, and links to Dockge and Cockpit until then | P1a | T8, T13, T14, T18, T20 (the keep pass), T30, T34, T35; U6, U8; L2, L4, L5; **novice check**: find out whether updates are waiting and whether a restart is needed, which disk is fullest, and one app's latest log lines |
| **P1c: who can act as root** | the root page (view only) with the `access` verb, and logs read through the helper (both class `private`) | the harness runs as its own system user (D1; the first step of v2 §9.2's migration); agents cannot reach sudo (D20). Until then `explain access` shows the same page in the owner's own terminal (§6.1) | T21-T23; U7; **novice check**: answer "who can take full control of my server?" and mark one row as known |
| **P2: Maintain and Fix** | `apt.upgrade`, `apt.remove`, `apt.restore`, `apt.restore_check`, `journal.vacuum`, `docker.clean`, `restart`, `reboot`; your code; approval ahead of a maintenance time; the restart waiter; "restart needed" and "updates waiting" in Needs you | P1c's preconditions; the approval key enrolled (one paste); the way-back check of §7.3 in every package job, which the helper runs itself, so the order Forge's point 7 and the owner set (changing buttons after backup and restore work) holds for the package layer without waiting for v2 phase B (D24); L6, L8, L9 and L10 pass | T14, T19, T20, T31-T33; U11; L6, L8-L10, L14; **novice check**: install the waiting security fixes in a maintenance time, put them back with the Undo button, and restart one app |
| **P3: Root-access changes** | `access.ticket`, `approve`, `confirm`, the timed undo, the boot unit | P1c's preconditions; the boot unit installed by `setup`; L11-L13 pass | T24; U7 (changes part); L11-L13; **novice check**: remove one old SSH key and confirm it from a new login, with the two terminal pastes the page gives |

P1a, P1b and P1c are built (DESIGN.md §42; Status above lists what is not); P2
and P3 are not.

The v2 roadmap's phases still apply around these: P1a to P1c are v2's A1 ("see
everything, change nothing") for this area; P2's buttons are v2's phase C and D
kinds of change, brought forward for the package layer only, through a narrow
helper instead of the executor, with the helper's own way-back check in place of
v2 phase B's sandbox restore test (D24, Appendix A23), and they retire into the
executor when it exists (Appendix A3).

---

## 12. Decisions, tests and questions

### 12.1 Decisions for the owner

| # | Question | Options | Recommended |
| - | -------- | ------- | ----------- |
| D1 | When does Seraphina get its own Unix user? | (a) P1a and P1b run as today's harness user with reads of the server's layout only; the move to the system user `seraphina` comes before logs, the root page and any change (P1c); (b) move first, before P1a; (c) never (the helper then refuses logs, the root page and every change to a login account, so they never ship) | (b) wherever your account is in `docker`, `lxd`, `incus-admin`, `disk` or `sudo` (on the reference server it is in `docker` and `lxd`), because there (a) and (c) are not allowed: every agent would already hold root (§6.1). Elsewhere (a), knowing the trade-off: the sudoers line lets every process of your account, agents included while their sandbox is off, read the server's layout as root without your password (names, addresses, ports, mounts, disks), though never logs or root-access details (§6.1) |
| D2 | Who else may call Seraphina's system access? | (a) nobody but Seraphina; (b) also Kate's operator account, read-only; (c) Kate, read and Fix | (a); add (b) later if Kate needs the same facts |
| D3 | May the helper make read-only Docker API calls while Docker runs? | (a) yes, a fixed list, never starting Docker; (b) no, state files only (health then comes from Seraphina's own checks only) | (a): the helper is already root, and Docker's own health and image list are not in the state files |
| D4 | Look for newer versions upstream? | (a) off; (b) daily for every app; (c) per app | (a) at first: it tells each image registry which apps your server runs |
| D5 | Confirming app addresses | (a) one review list, with rows ticked in advance only where the evidence agrees (§2.3), and you press **These are right**; (b) accept automatically when exactly one address is found | (a): detection can be wrong in five ways (§2.3) |
| D6 | How apps are checked | (a) a web request for web apps, a connection only for GPU and model apps; (b) a connection only, for every app; (c) running state only | (a) |
| D7 | A read-only Beszel login for Seraphina | (a) yes, kept in Seraphina's secret store; (b) no: Seraphina can then tell that Beszel runs, not that it records | (a) |
| D8 | Beszel's heartbeat | (a) point it at an outside check service you choose, so a dead server is noticed; (b) leave it off. It cannot point at Seraphina (§4.5) | (a), with a service you pick |
| D9 | Keep previous versions of every app from P1b | (a) yes: 2 per app, at most 50 GB in all; (b) only when cleaning up (P2); (c) off | (a): every Dockge update leaves the old version unnamed and unprotected. On a server whose apps already run pinned local copies (§2.6), those copies count as kept and are never trimmed |
| D10 | Protecting kernels in package clean-up | (a) Seraphina's own rule (never the running kernel or the last one that ran a whole day); (b) also tell apt to keep 3 kernels; (c) apt's default only | (a), and (b) once the boot safety net exists |
| D11 | Approvals before the executor exists | (a) your code for Maintain and the server restart; your own sudo in a terminal for root-access changes; (b) your own sudo for everything; (c) a click for everything | (a) |
| D12 | A package whose old version cannot be downloaded again | (a) rebuild it from the installed files (`dpkg-repack`) when that tool is installed, else leave that package out unless you accept; (b) always leave it out; (c) install without a way back | (a) |
| D13 | Kernel, GPU driver, Docker, Tailscale, SSH and systemd packages in "Install updates" | (a) never through these buttons until the boot safety net exists, each with an exact copy block for doing it at home; (b) the kernel too, when you tick "I am here". Either way, Ubuntu's automatic updates, if on, may install the security fixes among them by themselves (§7.6) | (a) |
| D14 | Clean-ups that cannot be undone (old logs, unnamed old copies, build files), and how old logs must be | (a) typed confirmation and your code; (b) typed confirmation only; logs older than 30, 60 or 90 days | (a), 30 days |
| D15 | When "Restart a service" ships | (a) with P2; (b) right after P1b, since it can be undone instantly | (a), as your order says; (b) is safe if you want it sooner |
| D16 | How long a server restart may wait for running jobs | 60, 120 or 240 minutes, then it does not restart and tells you | 120 |
| D17 | Sudoers files | (a) one per helper (`seraphina-system`, `seraphina-snapraid`), each removable alone, with one "Switch off all" block that removes both; (b) one shared `/etc/sudoers.d/seraphina` | (a) |
| D18 | A small boot-time unit that finishes any root-access undo that a restart interrupted | (a) install it with P3; (b) no, and make root-access changes only at home | (a) |
| D19 | "Ask about this app" | (a) the local model, from the facts on the page; (b) also cloud models, per question | (a) |
| D20 | The agents' sandbox while Seraphina's system access is installed | (a) turn it on (`bwrap`) before P1c, and accept a warning in P1a and P1b, while agents can read the server's layout; (b) accept that agents can call the helper with whatever Seraphina's user may do | (a) |
| D21 | The phone's bottom bar | (a) Your server takes Plans' place; (b) Your server only from Home | (a) |
| D22 | A way to reach Cockpit and Seraphina that does not depend on Docker | (a) a host-native tailscaled, v2 §7.2's design, as its own careful change; (b) keep the sidecars; at home, SSH on your network | (a) |
| D23 | App names on your phone | (a) per app, off until you allow it; (b) on for every app | (a) |
| D24 | When the Maintain buttons may install packages | (a) from P2, with the helper's own way-back check in every job: the previous versions are kept, verified, unpacked and checked against their own file lists, and apt's simulation accepts them as the way back, all before anything is installed; it does not rehearse going back (§7.3); (b) only after v2 phase B's restore tests in a sandbox, which come with the executor; until then the Maintain buttons do not exist | (a): it keeps your order (a way back first) for the package layer, and the card says "Way back checked, not rehearsed" |
| D25 | Put Seraphina behind your login gateway? | (a) no: Seraphina keeps its own host-native tailnet path (D22) and its own sign-in, and stays out of the gateway entirely (N11); (b) a home-network-only route through the gateway's home-network oauth2-proxy, keeping Seraphina's own session gate, with the gateway's identity headers taken as claims only | (a): the gateway runs in Docker and dies with it, and Seraphina's way in must not |

### 12.2 Acceptance tests

"Unit" (T) means `unittest` against fixtures (recorded outputs with every name,
id and path replaced by a placeholder) and fakes; "GUI" (U) means the mock
backend and the smoke suite; "lab" (L) means a virtual machine gated by
`SF_YOURSERVER_LAB=1`. L8-L11, L13 and L14 need a **systemd-booted Ubuntu 26.04
lab with cgroup v2**, because the research machine used so far had none of these
(Appendix C); L1, and L15 if no such lab exists yet, are instead one read-only
read on the owner's own host (Q3), and L7 needs only a machine with classic sudo
and sudo-rs, so P1a waits for no new lab. A test passes only on what code records (exit codes, files,
hashes, records), never on the words a tool prints.

| # | Kind | Passes when |
| - | ---- | ----------- |
| T1 | unit | the cgroup readers: RAM is `memory.current` minus `inactive_file`; CPU from two `usage_usec` reads; a missing `memory.current` is "unknown", never 0; a `docker-<id>.scope` under a custom slice is found; a scope with no PIDs is not running |
| T2 | unit | `/proc/meminfo`, `/proc/uptime` and `/proc/stat` parse; mounts de-duplicated by device; pseudo-filesystems skipped; an `EACCES` from `statvfs` routes to the helper |
| T3 | unit | GPU: fixture `nvidia-smi` output joined through fixture `/proc/<pid>/cgroup` files to containers and units; cards by UUID; exit codes 9, 4 and 6 give their states |
| T4 | unit | the Docker 29.9.1 state files parse into the map, each network's aliases, address and DNS names included; a file of an unknown shape gives `docker_format_unknown`; only allow-listed labels are returned; planted environment values, `Cmd` arguments and log lines appear in no answer |
| T5 | unit | exit codes, restart counts, restart policy, the expected state and the manually-stopped flag give the lights of §2.5: a 137 exit with the flag set on an app expected to run is amber and, after its grace, an incident; a non-zero exit under restart policy `no` is red; a container made and never started is amber when expected to run; an expected-off app is off whatever its state; a one-shot that finished with 0, an old copy and a retired copy change no light |
| T6 | unit | the helper refuses an unknown verb, an unknown field, a request over 16 KiB, every bad id shape (a unit name with a leading `-` included), an id with no record, a caller uid not in `callers`, a class not in `allow`, and `private`, `maintain`, `fix` or `access` for a login-account caller (`caller_is_login_account`) whatever the config says; every unit name in a built argv follows a literal `--` or sits in one `--unit=` argument; no argv it builds contains `prune -a`, a container, network, volume or system prune, `run`, `create`, `exec`, `rm`, `pull`, `purge`, `clean` or `autoclean`; every binary comes from the fixed list; no shell is used |
| T7 | unit | `unsafe_install` for a group- or world-writable or non-root file anywhere in the chain (in a temporary tree with fake owners); `explain` prints old and new values of `allow`, `callers`, `owners` and the keep limits |
| T8 | unit | the Docker API client sends only the allow-listed method and path pairs; with a fake `systemctl` saying `docker.service` is inactive it never connects and answers from the state files |
| T9 | unit | the Tailscale decoder returns only the three keys' contents; key material planted in a fixture state file appears in no answer, log, receipt or event; a link in the state path is refused; `${TS_CERT_DOMAIN}` is handled; `TS_AUTHKEY` and `TS_EXTRA_ARGS` are never read; a sidecar with only `PATH` in its environment and its state in a named volume at `/var/lib/tailscale`, and one with a bind there, are both found, and the node name comes from the state's profile; one with `TS_*` set and a `${TS_CERT_DOMAIN}` serve file still names the node from its profile |
| T10 | unit | grouping: one app per Compose folder and project name; Dockge stacks recognised, and only the project Dockge matches gets the Dockge badge; a stack whose name is overridden by `name:` or `COMPOSE_PROJECT_NAME` keeps its folder; two projects from one folder make two cards; a sidecar in a Dockge stack of its own joins the app it proxies to and leaves no sidecar-only card; one sidecar with entries for two apps gives each app its own candidate; a sidecar that reaches its app by container name or alias on a shared network joins it; the namespace join works both ways (`container:` on the app or on the sidecar); a host-network sidecar joins by host port, a socket unit included; a compose file directly in a home folder is named by its service, not the folder; one-off containers are left out; a recreate under a new id is the same member, with its hide or split kept and one change-log line; a renamed predecessor with the same labels is an old copy; the owner's merge, split and retire survive refreshes; a new container joins its stack; a removed one shows "Removed" for 30 days |
| T11 | unit | address candidates in the order of §2.3; a fingerprint change asks again; a confirmed address never changes by itself; an `https` address outside the confirmed tailnet, another tailnet's, or a `javascript:` link is refused; an edit of `apps.json` outside the API breaks the hash chain and the Open link waits |
| T12 | unit | each row of the light table; the main member chosen as §2.5 says; green never on stale facts; GPU and model apps get a connection only; a stop inside a power hold that names the app counts from the hold's end |
| T13 | unit | a differing config hash gives "waiting in its folder"; a tag pointing to another local image gives "downloaded"; registry checks only with D4 on; a digest-pinned image never shows an update |
| T14 | unit | receipts carry the schema of §6.10; the operator viewer shows both kinds with their labels; a repeated `rid` returns the first record and runs nothing; a timed-out call is `unknown` and is never retried; no `rid` record with no lock held is "not started" |
| T15 | unit | every Open link and deep link passes `safeHref`; none points at the harness's own host name (N3); an app on the harness's host name with another port raises the high finding (N10) |
| T16 | unit | a probe is one `GET` or a TCP connect, with no cookie, credential or redirect, at most once a minute per app, backing off after 10 failures (N7) |
| T17 | unit | for 20 addresses across versions 1-10 at level M, the encoder's matrix with the mask forced to the one the reference used equals the stored reference matrix (made once at development time with a well-known encoder; that tool is not needed to run the test); penalty scores and the mask choice are tested apart, against hand-worked cases; and a small standard-library reader in the test (format bits, unmasking, codewords, Reed-Solomon check) reads every encoder output back to its address |
| T18 | unit | the Check parsers: `apt-get -s` `Inst` lines and summary, the security set; `needrestart -b`; `smartctl -j` exit bits (exit status 2 as `nobody` is "can't read", never "failing"; exit 3 with `-n standby,3` is "asleep"); `ss` with and without the process column; `systemctl --output=json`; `list-sockets` with a socket-activated service idle (only PID 1 holds the socket) and running, both naming the service, and a socket where `ss -p` lists PID 1 and the service, which picks the service; "No journal files were found." is "unknown", never 0 |
| T19 | unit | M1-M3 plans: a list hash that differs refuses with `list_changed`; a simulation with an extra `Inst` or any `Remv` refuses; the dependency step moves `libsystemd0` and `libsystemd-shared` out with `systemd`, and `openssh-client` with `openssh-server` (the fixture pairs), so the rest of the list still installs, and Check 1's count equals the button's list; every kept `.deb` passes the way-back check, and a truncated one, a wrong version, a file that fails its `md5sums` line or a restore simulation with a `Remv` stops the job before `dpkg` starts; a package without an old copy is refused unless accepted; the families of §7.6 never appear, and each has its copy block; the running kernel and the last whole-day kernel are refused |
| T20 | unit | M4 and M5: `journalctl --vacuum-time` is the only log argv; the keep pass tags current and previous images within the limits and never trims a pinned app's; the dangling list drops ids that carry another tag (the lab R11 fixture); the build-cache list comes from `GET /system/df` records, never from `docker buildx`; the job refuses when its list differs; extra deletions reported by the prune are flagged |
| T21 | unit | SSH: fingerprints equal `ssh-keygen -lf`'s for each key type in the fixtures; options are parsed; comment and garbage lines are reported, not skipped; both key files are read; an `AuthorizedKeysCommand` gives a "can't list" row |
| T22 | unit | sudo: `sudo -l -U` texts (allowed and "not allowed"), `sudo -ll` granting files, `cvtsudoers` JSON; the implementation decided from the resolved path of `sudo`, not from `cvtsudoers`; with sudo-rs as `sudo` and classic sudo as `sudo.ws`, both are listed as doors and a rule one allows and the other refuses is flagged; sudo-rs's refusal text ("interactive authentication is required") read as a refusal; the loop covers every `getent passwd` account, so a `NOPASSWD` rule for a service account with `/usr/sbin/nologin` and no admin group is found under both implementations; a dot-named drop-in reported as "not read by sudo"; under sudo-rs, the per-user path, the `@socket` line and the "names not in getent" line, shown only when `sudo --version` says sudo-rs; a command-list rule with an escape command counts as full |
| T22b | unit | root services (row 10): a fixture unit modelled on a root service that runs `python3 -B <home>/<persona>/script.py` flags the script under a home folder, though the program `/usr/bin/python3` is root-owned; `bash <script>` the same; a runtime drop-in under `/run/systemd/system` is read; a root container binding the system bus is a way to take full control (row 9) |
| T23 | unit | root-page rows, their sentences, marks, "new since" findings and the "can't be checked from here" list, which is always present; the headline counts only ways to take full control now, with root-inside-container, GPU devices and host networking listed under "Could matter if something escapes"; a socket holder recreated under a new container id is the same row, keeps its mark and raises nothing |
| T24 | unit | each access change applied and undone restores byte-identical files, on scratch account files: a lock and its undo give back the exact `/etc/shadow` line for a password field that was empty, `!`, `*`, `!` before a hash, or a hash, aging fields included; a group change and its undo give back the exact `/etc/group` and `/etc/gshadow` lines, member order included, for an account in only one of the two; an undo whose line was changed since touches nothing and says so; each rule of §8.5 refuses its case, and locking the only unlocked owner account is refused even when it has several full sudo rules; a failing post-check undoes at once; `approve` and `confirm` need a terminal and an owner uid; `confirm` refuses the approving session, a second terminal of the same Cockpit sign-in, and an SSH session that started before the apply time, and accepts a session that started after it; the harness's no-argument sudoers line can never reach either |
| T25 | unit | the Beszel watcher: the hub check, the agent check and the records check; the 21-hour replay (hub and agent exit 137 with the manually-stopped flag set) turns the card red at the next agent read and opens one `service.down` incident within 10 minutes; "Working again" on recovery; without a login the third check is skipped and the card says so |
| T26 | unit | `<data_dir>/yourserver` holds no time series, no log line, no mount path, no address outside `apps.json`, and no Tailscale or Beszel secret; its folders are 0700 and files 0600; it is in `sandbox_masks` |
| T27 | unit | never in the request path: the harness opens no new listening socket (N1, N9); a full refresh writes nothing in a fixture stacks folder, serve config or `.env` (N2) |
| T28 | unit | phone texts: each kind of §10.2 at its priority with its fixed text; an app's name only when its `phone_name` is on; no address, path or name in any payload by default |
| T29 | unit | every route needs the master token; none is in `AGENT_TOKEN_ROUTES`; no persona or agent tool list contains a changing route |
| T30 | unit | each doctor check of §11.7 at its level, including `NoNewPrivs: 1` named as the cause, the `docker` group error and the "classes beyond read while the sandbox is off" error |
| T31 | unit | codes: RFC 6238 test vectors pass; one step of drift either way is accepted; a code is accepted once; an approval ahead of a maintenance time is a set of `<package>=<version>` pairs and expires with the time; at the time, a list refreshed in between (one pair now has a newer version) installs the rest and reports that pair as waiting; a list partly installed by unattended-upgrades skips those pairs; nothing outside the pairs is ever installed; a subset whose simulation pulls in another package is refused |
| T32 | unit | the server restart: it waits for a hold record, a backup by process name, `dpkg` running and a pending undo; it treats the harness's own holds as advice; at the cap it does not restart and reports; a new boot id closes the receipt; no new boot id is `unknown` |
| T33 | unit | Restart a service: only running members are restarted, checked again inside the job, so a stopped member, a one-shot that finished, an old copy and a retired copy are left alone and a whole-app restart touches only the running ones; the management-plane units, sidecars and needrestart's never-restart units are refused with their reason, matched by every name `systemctl show -p Id,Names` gives (`sshd.service` and `ssh.service` both); a needrestart config entry the helper cannot read hides Check 2's buttons; at most 3 restarts of one app an hour; busy apps warn and need **Do it now anyway** |
| T34 | unit | the `compose` verb: with an env file holding a secret-looking value, a malformed env file whose line Compose quotes on standard error, and an unset variable it names, no file content or variable name appears in the answer, a receipt, an event, a job log or the doctor's output; a non-zero exit gives `compose_parse_failed` with no tool text; an output line of the wrong shape fails the whole answer |
| T35 | unit | the helper's frozen redactor and `swfactory/blueprint/redact.py` give the same output on one corpus of log lines with tokens, keys and passwords; a `logs` page from the helper never holds a planted token |
| T36 | unit | `setup`: refuses without a terminal or through the harness's sudoers line; refuses a staged config with an unknown class, a missing caller, a `private` class for a login account, or a link; writes the sudoers file only from its own template; moves the helper, the config and the sudoers file in that order; on any refusal removes its `.new` files and leaves the old ones untouched; prints the one success line only after its own check passes |
| T37 | unit | the `ingress` verb: from fixture nginx configs it returns only listen, `server_name`, `location`, upstream and `auth_request` per route; a secret planted in a `proxy_set_header` value, an included file and a key file appears in no answer, log or event; a config outside the shape is `ingress_parse_failed` with no file text; each route becomes a candidate for its upstream's app, never for the gateway, and a host-port join that lands on a host-network nginx follows the route (§2.1 rule 3) |
| T38 | unit | probes behind the gateway go to the upstream; an oauth2-proxy sign-in answer (200, 302 or 401) from a route never counts as the app answering; the gateway's card is red when a member is down while the apps behind it keep their own lights; on Python 3.13 and later a self-signed certificate without the strict extensions is rejected with a named reason, and accepted with `ca_file` of a compliant certificate or with `pin_sha256`; no code path disables verification |
| U1 | GUI | each mock state draws the right cards, tones and reasons; green never on stale numbers |
| U2 | GUI | confirming an address; the outside-edit wait; the refusal of an address outside the tailnet |
| U3 | GUI | Open opens a new tab with `noopener noreferrer`, never on the harness's host (N3, N4) |
| U4 | GUI | an app that cannot be checked still opens (N8) |
| U5 | GUI | the QR code drawn from its rows; the Add to Home Screen steps per phone |
| U6 | GUI | each Check's result; the logs page shows hostile log lines as text |
| U7 | GUI | the root page: groups, why, since, marks and the can't-check list; P3 buttons disabled with their reasons until P3 |
| U8 | GUI | the word check of §11.5 over every novice template |
| U9 | GUI | the Home card and Needs you: an app down is `err`, confirmations `human`, Beszel `warn`; the 21-hour replay in the mock |
| U10 | GUI | every page works at phone width with no horizontal scroll |
| U11 | GUI | P2 screens: the before and after templates, class badges, the code field, **Do it now anyway** only where §7.5 allows it, no Stop after the point of no return, and an unknown outcome offers **Check what happened** and no retry until checked |
| U12 | GUI | the first-run review list (ticked and unticked rows, **These are right**, edit in place, "This is an old copy"); old copies and retired copies on an app's page; the "How to go back" panel's three steps; "Start it in Dockge" with its read-only warnings; the summary card before the helper is installed, with no container id shown |
| L1 | the owner's host, one read-only read (Q3) | as the harness user: `memory.current`, `memory.stat`, `cpu.stat`, `io.stat` and `pids.current` of one container's `docker-<id>.scope` are readable, and RAM matches the owner's own `docker stats --no-stream` within 5%; in the lab later: a container with a custom `cgroup_parent` is found |
| L2 | lab (26.04) | `apt-get -s upgrade` as a non-root user: apt 3.x's `Inst` format and phasing messages match the parser |
| L3 | lab | a fixture app behind a local proxy and a published port answers with the harness stopped, and again with the helper removed (N5, N6) |
| L4 | lab | the owner's Docker version: the contract test's fields exist; the manually-stopped flag after `docker stop`; whether container labels carry the image's version label |
| L5 | lab | on the owner's Compose version, run from its plugin path: `config --hash '*'`, `--images` and `--format json`'s `name` with the daemon stopped (lab R23 showed they need none on `v5.6.0`); the hashes equal the containers' `config-hash` labels after `up -d`, and differ after the file changes |
| L6 | lab | the prune lab run again on the owner's Docker version with the keep pass first: M5 removes exactly its list; a keep-tagged previous version survives a Dockge-style update (pull, then `up -d`) followed by M5; whether a dangling image's repository digests name its repository, for the first-run keep pass |
| L7 | lab (Ubuntu 26.04 with sudo-rs as `sudo` and classic sudo as `sudo.ws`; no systemd needed) | as a non-root harness user with §6.11's sudoers line: a piped request works, any argument is refused, nothing else runs through sudo; inside `bwrap`, `sudo -n` fails; under each of the two programs: `Defaults!<path> !use_pty` accepted or left out, the `""` rule, `@includedir` skipping dotted names, `visudo -cf` of each implementation, `-ll`, `-l -U <user> <cmd>`, and the `@socket` claim, against sudo-rs 0.2.13 |
| L8 | lab (systemd-booted) | from a unit with `ProtectSystem=strict` and `TemporaryFileSystem=`: a direct verb fails as §6.1 predicts; `systemd-run --wait --pipe --collect` from it works; job units start from it |
| L9 | lab (systemd-booted) | job units: `file:` output lands 0600 in the run folder; nothing reaches the journal; a harness restart does not stop a job; the power hold record exists while it runs |
| L10 | lab (systemd-booted) | a package with a slow `postinst` installing in its job unit, then `systemctl poweroff`: shutdown waits for `dpkg` (`KillSignal=SIGCONT`, `SendSIGKILL=no`, 30-minute timeout) and no package is left half-configured |
| L11 | lab (systemd-booted, with Cockpit) | `confirm`'s session check: two terminals of one Cockpit sign-in have the same logind session and are refused; a new Cockpit sign-in in a private window has a different session, started after the change, and is accepted; an SSH session opened before the change is refused; a new SSH login is accepted; `loginctl show-session` gives each session's start time and user as §8.6 reads them |
| L12 | lab | an account locked as §8.4 writes it (`!` and expiry `1`) cannot sign in with an SSH key or through Cockpit; the undo gives back the exact line and both sign-ins work again; a `passwd` or `usermod` run during the helper's write waits or fails cleanly under the shadow tools' locks |
| L13 | lab (systemd-booted) | a root-access change approved, then a reboot before confirming: the boot unit undoes it before anyone signs in |
| L14 | lab (systemd-booted) | the restart waiter with a hold file, a fake backup process and a pending undo: it waits; at the cap it withholds; the new boot id closes the receipt |
| L15 | lab (systemd-booted), or the owner's host as part of Q3's one read | `systemctl list-sockets --all --output=json` as the harness user, with `cockpit.socket` and `ssh.socket` idle and in use: each address maps to its socket unit and the service it starts, and `ports` names Cockpit and SSH in both states |
| L16 | the owner's host, one read-only read as root by the owner (with `explain ingress`) | the gateway's real nginx config parses in the `ingress` shape, with no secret in the answer; each route's upstream matches the app the owner opens there |

### 12.3 Open questions

| # | Question |
| - | -------- |
| Q1 | Answered (observed): sudo-rs 0.2.13 as `sudo`, older than the v0.2.15 source this design cites, with classic sudo 1.9.17p2 installed as `sudo.ws`. So the helper decides the implementation from `readlink -f /usr/bin/sudo`, tests whether `cvtsudoers` exists rather than assuming, lists both setuid programs as doors and checks grants with both (§8.1 row 3); the `""` rule and `Defaults!<cmd> !use_pty` were never run as a non-root caller under either (L7). |
| Q2 | Answered (observed): every sidecar keeps its state persistently at `/var/lib/tailscale`, in a bind or a named volume, and seven of eight set no `TS_*` variable (§6.5). |
| Q3 | The cgroup v2 memory, io and pids files and the `system.slice/docker-<id>.scope` path were checked from source and docs only; one read-only read as the harness user on the real host settles it (L1): `cat` of one container scope's `memory.current`, `memory.stat`, `cpu.stat`, `io.stat` and `pids.current`, next to the owner's own `docker stats --no-stream`, and `systemctl list-sockets --all --output=json` (L15). Will the owner run it once, or let the operator run it? |
| Q4 | Does `nvidia-smi --query-compute-apps` run as the harness user list other users' and containers' processes with their memory on the owner's driver? Is MIG or any graphics context in use? |
| Q5 | Which Docker versions will the state-file contract pin, and is the read-only API acceptable (D3)? |
| Q6 | Will the owner create a read-only Beszel user (D7), and which outside check service, if any, should receive Beszel's heartbeat (D8)? |
| Q7 | How many previous versions per app, and how much disk may they use (D9)? |
| Q8 | Partly answered (observed): Dockge's console setting is absent, so the console is off by default; every project name under the stacks folder matches its folder. Two running projects keep `compose.json` recipes outside it (§2.7). |
| Q9 | Dockge's "disable auth" setting lives in Dockge's own data, which the helper does not read. Can the owner confirm it is off, once? |
| Q10 | Partly answered (observed): a tailnet-proxy drop-in exists for Cockpit's web service (its contents unread), and plain HTTP on Cockpit's port answers 301, redirecting to the home-network address; the target scheme was not recorded and the HTTPS policy was not checked. Does its "Administrative access" still work for the owner under sudo-rs? |
| Q11 | Does the server use SSSD or LDAP for sudo, custom PAM modules, or a host tailscaled with Tailscale SSH? Each needs its own line on the root page. |
| Q12 | Ubuntu 26.04's versions of apt (the `Inst` line format, phasing text), systemd's `--output=json`, needrestart and its restart mode, and unattended-upgrades' origins: a read-only probe should confirm each before the parsers are written (L2). |
| Q13 | Answered (observed): `json-file`. |
| Q14 | How does the owner reach the cockpit itself from his phone today, and is that tailnet name in `server.allowed_hosts`? The launcher on a phone depends on it, and it disappears with Docker if it runs through a sidecar (D22). |
| Q15 | Answered (observed): the owner's account does not linger, so a user unit installed as him would not start at boot. The harness runs from its own system unit as `seraphina` there (§6.1, D1), and the watchers run only while the harness runs. |
| Q16 | Is `/proc` mounted with `hidepid` on the owner's server? The lab's was not (lab R3); `ports` reads process names as root either way, but other users' processes are visible to the harness user without it, and the summary card shown before the helper is installed (§2.10) needs that. |
| Q17 | Partly answered (observed): four stopped containers are job runs of a job broker (no Compose labels, restart `no`, network `none`, three sharing one app's exact image), now rule 7 of §2.1, never old copies. Which other stopped containers are old copies kept on purpose? |
| Q18 | Does Beszel's read-only user need its address verified before PocketBase lets it sign in with a password, on the owner's Beszel version (§4.4)? |

---

## Appendix A. Differences from SERAPHINA_OS_HARNESS.md, DESIGN.md and BACKUPS_SNAPRAID.md

| # | Where | What this design does differently, and why |
| - | ----- | ------------------------------------------ |
| A1 | v2 §2.3 Apps (Cosmos's market, installs through the Cosmos API with "its own network, a name and the Cosmos login", "Open in Cosmos") | On a server without Cosmos the launcher follows this design: no installs, no market, no login layer; addresses come from Tailscale sidecars and are confirmed by the owner; "Open in Dockge" and Cockpit's pages replace "Open in Cosmos". A note at the top of v2 §2.3 Apps says so |
| A2 | v2 §3.2 (who owns what on a Cosmos machine) | Without Cosmos: Dockge stacks are `owned:dockge`, other Compose folders `owned:compose`, units `owned:systemd`, the rest `unknown`; the Cosmos rows do not apply; SnapRAID's row is BACKUPS_SNAPRAID A1's |
| A3 | v2 §9.2 ("only the executor holds root") and §9.3 | A further narrow root program ahead of the executor, beside `sf-snapraid` and the planned `sf-codex-launch`. With the executor, its read verbs become `inspect.<probe>` verbs, `restart` becomes `systemd.restart` and a container restart action, `apt.*` become `apt.install\|upgrade` in a transient unit, keep tags become `image.keep`, and root-access changes become `file.edit` on access-controlling targets with the `management_plane` flag; the approval key retires to the safety page |
| A4 | v2 §3.4 (the native backend reads the Docker Engine API) | The core never holds the socket; the helper reads Docker's state files and makes a fixed list of read-only calls; container starts are inferred from cgroups until v2's engine event recorder (§9.4) exists |
| A5 | v2 §5.8 and §9.3 (codes typed only on the executor's safety page) | Before it exists, codes are typed in the cockpit and checked by the helper, and root-access changes use the owner's own sudo; the gap (a code is not bound to what it approves) is stated (§6.9) |
| A6 | v2 §9.4 (the Blueprint store and schedule) | The app change log is the launcher's interim memory and moves into the store |
| A7 | v2 §6.6 (`services.<name>.maintenance`) and BACKUPS_SNAPRAID A7 | This design defines `services`, keyed by app ids and fixed ids (`os-packages`, `os-cleanup`, `docker-cleanup`, `server-restart`), and asks the SnapRAID design to move `snapraid.schedule.*.mode` to `services.snapraid-sync` and `services.snapraid-scrub` |
| A8 | v2 §6.3 (holds at `<data_dir>/holds/`) and BACKUPS_SNAPRAID A6 (`/run/seraphina/holds/`) | Both are read: root jobs' holds in `/run/seraphina/holds/`, the harness's own long jobs as advice in `<data_dir>/holds/` |
| A9 | DESIGN §32 (`LINK_KEYS`) | Adds `app`, so tracker items can name an app |
| A10 | v2 §7.7 (confirmation on the safety page through the new path; two independent remote paths) | Before the executor, confirmation is the owner's own sudo from a login made after the change; the two-paths rule is not checked, and the page says so |
| A11 | `channels.tailnet_status` and `Notifier._link_base` | On a server whose Tailscale runs only in sidecars, `tailnet_status` returns `None`, so no notice ever carries a link; it falls back to the owner-confirmed tailnet suffix |
| A12 | DESIGN §39 (the capture bundle uses `docker logs` as the harness's user) | Capture can use the helper's `logs` verb instead of the socket |
| A13 | v2 §2.1 (Home opens with a server-health sentence) | The Your server card supplies it; making it Home's first line changes `homeHealth` in the core (source: `app.js:8614-8630`) |
| A14 | `swfactory/operator.py:1-13`, DESIGN §36, v2 §0 item 9 and §9.2 ("the operator persona holds root") | Stale on this server: her unrestricted sudo is gone. She may become a read-only caller (D2); her receipts and the helper's appear in one list with different labels |
| A15 | v2 §2.1 point 4 (eight modules under Your server) | This design ships Apps, the numbers-now part of Storage and GPU & AI, and the who-can-become-root half of Users and Security; the rest stays planned |
| A16 | v2 §2.4 (the word list) | New words should join it: app, part, its Tailscale connection, record, how to undo it, previous version kept, unnamed old copies, leftover build files, maintenance time, your code, a change waiting for you, full control |
| A17 | BACKUPS_SNAPRAID §3 and §9 | A shared `HelperClient` (`swfactory/helpers.py`); one sudoers file per helper (D17); the server restart reads its hold records; Check 5 reuses its daily SMART for array disks; its `maintenance` keys gain `windows`. Two changes proposed back to it, so the helpers stay alike: the one-paste `setup` and `remove` modes (§6.11), and the root-side refusal of private reads to a login-account caller (§6.7 item 1), which would cover its file lists the way it covers logs here |
| A18 | v2 §5.8 (`cannot_undo`) | Applied strictly to clean-ups: old logs, unnamed old copies and build files are deleted without a backup, so they need a typed confirmation and a code (D14); v2 did not name these cases |
| A19 | v2 §6.1 rule 11 (never automatic) | Applied to buttons a person presses too, because the boot-once fallback (v2 §5.8, phase E) does not exist yet (D13) |
| A20 | the Blueprint collector's `--sudo` path (`collect.py:449-506`) | A helper mode, and a new capture of users, groups and sudoers (§11.9) |
| A21 | `systemd/factory@.service` and v2 §9.2's hardening (`NoNewPrivileges=yes`) | As BACKUPS_SNAPRAID A15, plus: under a hardened unit the helper's verbs run through `systemd-run`, because sudo keeps the caller's mount namespace (§6.1) |
| A22 | v2 §10's novice check ("no terminal (beyond the install line) and no help") | Every phase has the novice check (§11.10), but some steps stay in a terminal: the one setup paste (P1a), the enrolment paste (P2), and `approve` and `confirm` for root-access changes (P3). Before the executor's safety page exists, a terminal running as the owner, behind his own password, is the only channel the harness cannot forge; the pastes are single lines with one expected answer and a plain fallback |
| A23 | v2 §5.5 kind 3P and phase B (package-layer restore tests in a sandbox, before any changing button) and v2 §2.4's badge text for `undo_by_restore` ("from a tested backup") | P2's package buttons rest on the helper's own way-back check of each job's restore point (§7.3, D24), which proves the previous versions are complete and accepted by apt, not that going back has been rehearsed; the badge says "from the kept previous versions" and the card "Way back checked, not rehearsed". v2's sandbox test replaces it when the executor exists |
| A24 | v2 §2.1's service card, which does not spell out its light's rules | This design adds an expected state the owner confirms per app, so an app stopped by a script or through Docker's API is never shown as quietly off (§2.5); v2's card should take the same rule |
| A25 | v2 §3 and §7.4 (endpoint chains through Cosmos) | Without Cosmos the chains include the owner's login gateway where he has one: client, nginx (its own certificate), oauth2-proxy (home network or tailnet) with the sign-in service, then the app; direct ports are listed apart. Seraphina stays outside the chain (N11, D25), and probes go to upstreams (§2.5) |
| A26 | v2 §9.2 and DESIGN §17 (the harness may run as the owner's account with `bwrap` on) | Not on a server where that account is in `docker`, `lxd`, `incus-admin`, `disk` or `sudo`: `bwrap` does not contain such an account, so the system user `seraphina` comes first there, before P1a (§0 item 8, §6.1, D1) |

Unchanged on purpose: no agent ever holds root (v2 §9.2); phone text is built by
code from typed fields (DESIGN §34); names, labels and logs are data, never
instructions; Cockpit is never served under the harness's own host name (v2
§3.8, §7.2).

---

## Appendix B. Sources

**Upstream, read at pinned tags on 2026-10-25** (cloned into the lab):
Moby `docker-v29.9.2` (`daemon/daemon_unix.go`, `daemon/oci_linux.go`,
`daemon/prune.go`, `daemon/containerd/image_prune.go`,
`daemon/containerd/image_pull.go`, `daemon/volume/service/*.go`,
`daemon/server/router/volume/volume_routes.go`, and in the critic round
`daemon/kill.go`, `daemon/start.go`, `daemon/restart.go`,
`daemon/container/container.go`, `api/types/network/endpoint.go`,
`api/types/build/cache.go`); docker/cli `v29.9.2`
(`cli/command/container/stats_helpers.go`); BuildKit `v0.34.0`
(`cache/manager.go`); opencontainers/cgroups `v0.1.0` (`systemd/common.go`);
Compose `v5.6.1` (`cmd/compose/config.go`, `pkg/compose/restart.go`,
`docs/reference/compose_restart.md`); Linux `v6.18`
(`Documentation/admin-guide/cgroup-v2.rst`, `kernel/cgroup/cgroup.c`,
`mm/memcontrol.c`, `Documentation/filesystems/proc.rst`,
`Documentation/hwmon/sysfs-interface.rst`, `Documentation/hwmon/drivetemp.rst`);
Dockge `1.5.0` at `bac498f` (`backend/stack.ts`, `backend/dockge-server.ts`,
`backend/routers/main-router.ts`, `backend/socket-handlers/main-socket-handler.ts`,
`common/util-common.ts`, `frontend/src/router.ts`, `compose.yaml`,
`extra/healthcheck.go`); Tailscale `v1.104.2` (`ipn/serve.go`, `ipn/store.go`,
`ipn/store/stores.go`, `ipn/prefs.go`, `ipn/ipnlocal/local.go`,
`ipn/ipnserver/server.go`, `ipn/localapi/serve.go`, `ipn/ipnstate/ipnstate.go`,
`safesocket/unixsocket.go`, `cmd/containerboot/main.go`, `settings.go`,
`serve.go`); Cockpit `370` at `fd21ab5` (`pkg/systemd/*`, `src/ws/*`,
`src/cockpit/superuser.py`, `doc/authentication.md`, `doc/urls.md`); Beszel
`v0.21.1` at `e2ed147` (`internal/cmd/hub/hub.go`, `agent/health/health.go`,
`agent/connection_manager.go`, `agent/docker.go`, `internal/hub/api.go`,
`internal/hub/heartbeat/heartbeat.go`, `internal/hub/systems/system_manager.go`,
`internal/migrations/0_collections_snapshot_0_20_0.go`,
`internal/site/src/components/router.tsx`, `internal/site/src/components/navbar.tsx`,
`internal/hub/utils/utils.go`);
needrestart `v3.11`; apt `3.1.12` (`apt-pkg/algorithms.cc`); systemd `v255`
(`src/systemctl/*`, `systemctl-list-units.c` for `list-sockets`) and `v258`
(`man/journalctl.xml`); smartmontools
`RELEASE_7_4` (`smartctl.8.in`); sudo-rs `v0.2.15`
(`docs/man/sudoers.5.md`, `README.md`; the reference server runs 0.2.13, so
claims from this source are re-checked in L7); netplan `main`
(`doc/netplan-try.md`). Dashboards: Homepage `v2.7.0`, Homarr `v2.9.1`, CasaOS
AppManagement `v0.4.16`, TrueNAS apps at `c675b3a` (library `2.3.17`),
umbrel-apps at `00c981c`, Umbrel `v1.0.4`, Cosmos-Server `v0.23.4`. Unraid's
WebUI field rests on community forum threads only (UNVERIFIED).

**The read-only capture of the owner's server** (2026-10-25, by his own
assistant; the facts labelled observed). Its probe names, values, paths and
addresses are kept in his private notes and are not published.

**Installed in the research machine and run there**: Ubuntu 24.04.5's sudo
`1.9.15p5`, OpenSSH `9.6p1`, iproute2's `ss`, smartmontools 7.4, apt and
unattended-upgrades `2.9.1` (its `50unattended-upgrades` read), needrestart `3.6`
(Ubuntu's package, its `needrestart.conf` read), the shadow tools' `usermod`
and `gpasswd` on a scratch prefix (R24), the Compose plugin `v5.6.0` (R23), and the
nvidia-utils 595 package's `nvidia-smi` and manual page (`595.91.07`), with no
driver loaded.

**The harness** at `776c539`: `swfactory/blueprint/collect.py`, `redact.py`,
`web/app.js`, `web/index.html`, `web/views/*.js`, `server.py`, `plainhttp.py`,
`notify.py`, `channels.py`, `items.py`, `receipts.py`, `operator.py`,
`activity.py`, `custody.py`, `doctor.py`, `settings.py`, `config.py`,
`orchestrator.py`, `driftsources.py`, `capture.py`, `clock.py`, `install.sh`,
`systemd/factory.service`, `systemd/factory@.service`,
`tests/test_gui_static.py`, `tests/gui_mock/__init__.py`. DESIGN.md §10, §11,
§14-§17, §28, §30-§32, §34, §36-§39. SERAPHINA_OS_HARNESS.md §0, §2.1-§2.5,
§3.1-§3.4, §3.8, §5.2, §5.5, §5.7, §5.8, §6, §7.2, §7.6, §7.7, §9.2, §9.3, §10.3.
BACKUPS_SNAPRAID.md at `7b7cd36` §1.1, §2.4, §3, §5.2-§5.4, §8.3, §8.4, §9,
Appendix A.

**Documentation not fetched here (UNVERIFIED in this design):** sudoers(5) on
the `""` rule; systemd.exec, systemd.kill and systemd-run on namespaces,
`file:` output, stop signals and transient timers across reboots; bubblewrap's
README on `no_new_privs`; shadow(5) on account expiry and SSH; shadow-utils'
`lib/commonio.c` on its locks; org.freedesktop.login1(5) on a session's
`Timestamp`; PocketBase's button wording; the phones' "Add to Home Screen" wording; image registries'
rate limits for manifest reads.

---

## Appendix C. The research lab

**The machine.** Ubuntu 24.04.5 with kernel 6.18, a hybrid cgroup layout (v1
controllers plus a cgroup2 hierarchy without the memory, io and pids
controllers), and no systemd as PID 1. A private Docker Engine 29.9.1 (containerd
2.4.2 image store, as on the owner's server) ran with its own data root inside
the lab folder, no iptables and no bridge, and is now stopped and unmounted.
"`nobody`" means a process run with `setpriv --reuid=65534 --regid=65534
--clear-groups`. Side effects outside the lab folder, all harmless: apt's and
unattended-upgrades' hooks touched their stamp files under `/var/lib/apt/periodic`
and `/var/lib/update-notifier`, and a short symbolic link needed for a Unix
socket path was made and removed.

| Run | What it showed |
| --- | -------------- |
| R1 | cgroup file modes: 0444 for `cpu.stat` and the other read-only files, 0644 for `cgroup.procs` and pressure files, 0200 for `cgroup.kill`; `nobody` read `cpu.stat` and `cgroup.procs` |
| R2 | CPU by two `usage_usec` reads as `nobody`: 99.8% of one core, `docker stats` 99.51% |
| R3 | `/proc/<pid>/cgroup` readable by `nobody`, `/proc/<pid>/environ` not; `/proc` without `hidepid` |
| R4 | Docker's state files: data root, `containers/` and each id folder 0710 root; `config.v2.json` and `hostconfig.json` 0600; the JSON log 0640 |
| R5 | the Compose labels on a container |
| R6 | privilege fields, mounts, `PortBindings`, network modes and `Config.User` in the state files; a `:ro` socket bind shows `RW=false` |
| R7 | health in the state files lags the API; run state and exit code are written at stop; the cgroup folder disappears at stop |
| R8 | `statvfs` as `nobody` works on a mount point and fails under an unsearchable parent |
| R9 | Compose project names from a folder, from `name:` and from `COMPOSE_PROJECT_NAME` |
| R10 | network prune breaks a stopped stack until `up -d --force-recreate` |
| R11 | after a pull, the dangling filter also lists the id of a tagged rollback copy |
| R12 | no prune has a dry run; answering N exits 0 |
| R13 | `cvtsudoers` JSON; `@includedir` skips names with a dot or a tilde; `visudo -c -f`; `sudo -l -U` exit codes; `sudo -ll` names the file; the `""` rule parses |
| R14 | sudo refuses under `no_new_privs` |
| R15 | `ssh-keygen -lf` hides options and skips bad lines; `sshd -T` defaults |
| R16 | `passwd -S`; the user crontab folder's mode |
| R17 | setuid scan, `dpkg -S` and usrmerge, `dpkg -V`, `getcap -r /`, Ubuntu's polkit admin rule |
| R18 | `ss -p` shows owners only to root; no JSON output |
| R19 | `smartctl -j` as `nobody`: permission denied, exit status 2 |
| R20 | `nvidia-smi` with no driver: exit 9 |
| R21 | `apt-get update` and `unattended-upgrade --dry-run` run hooks even when isolated; the security set matches the `-security` `Inst` lines |
| R22 | `journalctl --disk-usage` reports 0B with "No journal files were found." |
| R23 | critic round: Compose `v5.6.0`, run directly from `/usr/libexec/docker/cli-plugins/docker-compose` with `DOCKER_HOST` pointing at a socket that does not exist and an empty `DOCKER_CONFIG`, on a scratch stack: `config --hash '*'` printed one `<service> <64 hex>` line per service, `config --images` one image per line, and `config --format json`'s top-level `name` was the folder's name, all with exit 0 and no daemon; with a malformed env file, exit 1, nothing on standard output, and standard error quoting the bad line with its value; with an unset variable inside an env file's value, exit 0 and a warning on standard error naming the variable |
| R24 | critic round, on scratch copies of `/etc/passwd`, `shadow`, `group` and `gshadow` under a prefix (`usermod -P`, `gpasswd -Q`), nothing outside the scratch folder touched: after `usermod -L -e 1`, `usermod -U` refused to unlock accounts whose password field had been empty or `!` ("would result in a passwordless account"), left the field as `!` and exited 0; it turned `!` before a hash into a usable hash; the expiry `1` stayed. `gpasswd -d` then `gpasswd -a` moved the member to the end of the list and added it to `gshadow`, where it had not been |
| `prune-*` | eleven prune runs from one fixed state (a Dockge-style stack updated twice, an explicit rollback tag, a one-shot service, a stopped stack, a kept-on-purpose container, a never-started one, named, anonymous and option-carrying volumes, build cache), each recording what it removed; `predict.txt` beside each is the listing that matched |

**What the lab could not run**: systemd units and timers, `systemctl` JSON,
`needrestart`'s service list, a GPU, SATA disks and their temperatures, SMART on
real disks, Tailscale, Cockpit, Beszel and Dockge themselves (read at source
only). §12.2 marks the lab tests that need a systemd-booted 26.04 machine.

---

## Review log

Two passes on 2026-10-25, before the system audit: one by the author (V1-V30),
then one by critics (C1-C28). The auditor's findings should be added below as
further lines. "Fixed" or "Accepted" means the text above already carries the
change.

| # | Issue | Decision |
| - | ----- | -------- |
| V1 | Beszel's heartbeat was first planned to point at a new harness hook, but Beszel sets no `Authorization` header and the harness's hooks refuse a token in the URL on purpose | Fixed: the cockpit polls Beszel (§4.2); the heartbeat goes to an outside check service, if any (§4.5, D8) |
| V2 | A TOTP code lasts 30 seconds, so it cannot approve a change that runs in next Sunday's maintenance time | Fixed: the helper records an approval valid until that time ends (§7.5); superseded by C16, which binds it to exact package versions instead of a list hash |
| V3 | A program started through sudo keeps the caller's mount namespace, so a hardened harness unit would make the helper's writes fail and hide what it reads | Fixed: writing verbs run in transient units through PID 1, and read verbs too once the harness is hardened (§6.1); L8 |
| V4 | A transient undo timer does not survive a reboot, so a power cut inside the 10 minutes could leave an unconfirmed root-access change in place | Fixed: the server restart waits for pending undos, no approval near the power-off, and a boot unit finishes overdue undos (§8.6, D18); L13 |
| V5 | `apt-get clean` and `autoclean` delete cached `.deb` files, the second exactly the old versions the archive no longer offers, which are the way back | Fixed: never run; kept files live outside the apt cache (§6.6, §6.12) |
| V6 | `apt-get autoremove` takes no package list, so the owner's list could not be enforced | Fixed: `apt-get remove <exact list>` after a simulation shows exactly those lines (§7.3 M3) |
| V7 | Keep-tagging an app's previous image only after noticing an update races a prune run in between | Fixed: the current image is tagged at first sight, so the previous version is already kept when Dockge updates (§7.3 M5 step 1) |
| V8 | `TS_EXTRA_ARGS` was on the first list of sidecar keys to read, and it can carry an auth key | Fixed: never read (§6.5) |
| V9 | Container labels can carry secrets and text written by an image's publisher | Fixed: labels only from an allow list; free-text labels never sent to a model (§6.4, §2.8) |
| V10 | The Open link comes from a file the harness's user (and an unsandboxed agent) can write, so it could be swapped for a lookalike | Fixed: only addresses on the confirmed tailnet or this server's home address, with a hash chain that stops the link when the file changes outside the API (§2.3) |
| V11 | The words "cockpit" (Seraphina's page) and "Cockpit" (the web console) were used without telling them apart | Fixed: the words table |
| V12 | `passwd -l` was first proposed for locking an account, which leaves SSH keys working | Fixed: a locked password and an expiry, whose effect on key logins is checked in L12 (§8.4); superseded by C1 in how it is done: the helper edits the exact line itself instead of `usermod` |
| V13 | A power-off during `dpkg` could leave packages half-configured | Fixed: `SIGCONT` as the stop signal, no SIGKILL, a 30-minute stop timeout, and the job must fit before the quiet period (§7.3); L10 |
| V14 | The phone's bottom bar is already full (six entries) | Fixed: D21 |
| V15 | Restarting a sidecar that carries Cockpit's or Seraphina's address would cut off the owner's own way in | Fixed: refused with a reason (§7.4 F1) |
| V16 | `confirm` could have been run from the same terminal session that approved the change, proving nothing about a fresh login | Fixed: a different session is required (§8.6 step 6); L11; tightened by C9: the session must have started after the change |
| V17 | The dangling-image list can include an id that still carries a real tag (lab R11) | Fixed: such ids are dropped, and nothing is ever removed by id from that list (§6.6, §7.3) |
| V18 | `docker compose config` prints environment values | Fixed: only service names, hashes and image names are returned (§2.6); tightened by C2: standard error is discarded too |
| V19 | Probing a model server can make it load a model | Fixed: a connection only for GPU and model apps by default (§2.5, D6) |
| V20 | The notifier can never put a link in a notice on a sidecar-only server, because it needs the host's `tailscale` CLI | Recorded as Appendix A11: fall back to the confirmed suffix |
| V21 | The access-change table said "four changes" and listed five | Fixed |
| V22 | A threshold sentence in §3 read "D-free defaults" | Fixed: plain wording |
| V23 | `keep.images` was first allowed to trim old keep tags, which can delete an image, inside P1, the read-only phase | Fixed: it only adds tags; trimming happens only in `docker.clean` with its confirmation (§6.3, §7.3 M5) |
| V24 | The helper's fixed binary list named only half the tools its verbs use | Fixed: the full list (§6.6) |
| V25 | The boot unit of §8.6 had no install block and was missing from the removal list | Fixed: §6.11 and the removal paragraph; since C19, `setup` writes the unit and `remove` removes it |
| V26 | "Never reads `.env` files" contradicted `docker compose config`, which reads them | Fixed: compose and `.env` files are read only through Compose, whose output the helper filters (§6.6); tightened by C2: its standard error is discarded unread |
| V27 | Package jobs were given `KillSignal=SIGINT` in the unit template but `SIGCONT` in §7.3 | Fixed: the template names both (§6.8) |
| V28 | The text promised "three" root-access changes in one place and listed five in another | Fixed: five everywhere (§1.1, §0 item 11) |
| V29 | The hash chain on the app record was presented as if it caught any outside edit, but it has no secret, so a rewrite made while the harness is stopped passes it | Fixed: the harness keeps the chain head in memory, re-checks confirmed addresses against detected ones after a restart, and the text says what is left (§2.3, §10.4) |
| V30 | Restarting the server at its cap offered "Restart now anyway" while the preconditions table said only F1 could be overridden | Fixed: F1 and F2 may be overridden with a warning, F2 with your code (§7.5) |

Totals for the author's pass: 30 issues, 29 fixed in the text, 1 recorded as a
change for the harness (V20, Appendix A11).
**Critic round** (28 findings, in the order the critics gave them; one line each):

| # | Finding | Decision |
| - | ------- | -------- |
| C1 | high: `usermod -U` is not the inverse of `usermod -L -e 1`; a key-only account stays locked, or a password disabled on purpose comes back | Accepted, and extended to groups: account files are changed and undone by the helper's own one-line edits under the shadow tools' locks, putting back the exact saved line; an undo whose line changed since touches nothing and says so; `usermod`, `gpasswd` and `chage` leave the binary list. Lab R24 reproduced both faults and found the same for `gpasswd -a` after `-d` (member order, `gshadow`); T24 cases for empty, `!`, `*`, `!` before a hash and a hash; L12 (§6.6, §6.12, §8.4, §8.5) |
| C2 | medium: the `compose` verb can leak `.env` contents on standard error | Accepted: standard error is discarded unread, only shape-checked standard-output fields leave the helper, a failure is the fixed `compose_parse_failed`, and a general rule keeps any tool's own words out of answers; lab R23 reproduced the leak; T34 (§2.6, §6.3, §6.6) |
| C3 | medium: under sudo-rs the per-user loop misses a grant to a service account | Accepted: the loop covers every `getent passwd` account under both implementations, rules `cvtsudoers` shows for names the loop missed are listed, and a sudo-rs-only "names not in getent" line is gated on `sudo --version` (§8.1 row 3, §8.3, Q1, T22) |
| C4 | medium: with the shipped `sandbox: "none"`, same-uid agents reach the helper's private reads with no password; §6.1's "adds no power" is wrong | Accepted with C14: §6.1 and D1 state the widening; `read` is split into `read` (the server's layout) and `private` (logs, root-access details, access receipts and tickets); the helper itself refuses `private`, `maintain`, `fix` and `access` to any login-account caller (§6.7 item 1); the doctor's error covers those classes while the sandbox is off; D20 asks for `bwrap` before P1c. Rejected in part: an error for `read` alone, because after the split it returns the layout, much of which the cockpit's own user-readable record holds anyway, and an error would block P1a under the shipped default; the warning names what is exposed, and D1 (b) is offered (§6.1, §6.2, §10.3, §10.4, §11.7) |
| C5 | low: rule 1's "unless another full grant remains" makes no sense for a lock | Accepted: rule 1 covers group and sudo-rule removals only; a lock is decided by rule 2 alone; T24 case for the only unlocked owner with several rules (§8.5) |
| C6 | low: `confirm` assumes two Cockpit terminals have different session ids | Accepted, and resolved by C9's change: a second terminal of one Cockpit sign-in is refused by design, and the text sends the owner to a new sign-in or SSH login (§8.6, L11) |
| C7 | low: unit names are not passed after `--` | Accepted: the unit shape forbids a leading `-`, and every unit goes after a literal `--` or into one `--unit=` argument; T6 (§6.3, §6.7 item 3) |
| C8 | high: F1's Restart starts stopped members, old copies and finished one-shots | Accepted in part: only running (or restarting) members are restarted, checked again inside the job (Moby `daemon/restart.go:58-69` confirms a restart starts a stopped container); stopped members, one-shots that finished, old and retired copies are never touched; the text says how Dockge's own Restart differs; T33 (§6.3, §6.4, §6.6, §7.4). Rejected: a separate Start button, because a start is where Seraphina knows least (why it stopped, an old copy on the same ports or GPUs, a one-shot that would run again) and the helper's never-start rule keeps its writing surface small; the card links to Dockge with the read-only warnings the critic listed |
| C9 | high: the fresh-login check fails the Cockpit path the novice is told to use, and accepts an SSH session opened before the change | Accepted: `confirm` needs a logind session of an owner account, other than the approving one, that started after the receipt's apply time; the text says "sign in to Cockpit again in a private window, or open a new SSH login; a second terminal here does not count"; L11 rewritten; T24 cases (§6.9, §8.6) |
| C10 | high: grouping rules break on real sidecar and stack layouts | Accepted: apps keyed by folder and project name, the Dockge badge only on the project Dockge matches; each serve entry joined to its target by namespace in both directions, by container name, alias or address on a shared network, or by host port, and offered as a candidate to that app; a sidecar-only project is an app only when nothing joins; names never from a non-stack folder's basename; T10 and the mock carry each layout. Two Dockge citations were corrected on the way (Delete is `stack.ts:218-232`; project matching `265-336`) (§2.1, §2.3, §2.7) |
| C11 | high: the light hides outages caused by automation; cases missing; "main member" undefined | Accepted: an expected state per app, proposed from the restart policy and confirmed once; Moby's flag read as "stopped through Docker" (`daemon/kill.go:95-96`, `start.go:184`); an app expected to run and stopped past its grace is amber, asks "Should it be running?" and opens an incident whatever stopped it, counted from the end of a power hold that names it; the main member defined; old copies, retired copies and finished one-shots out of the light; rows for a non-zero exit under `no`, made but never started, and expected off; T5, T12, T25 (§2.5, §4.2, A24) |
| C12 | medium: members are keyed by container id, which changes at every Dockge update; old copies not modelled | Accepted: members keyed by Compose folder, project, service and number, else name, else unit; a recreate is the same member with one change-log line; a "kept old copy" state the owner can retire or keep, and a finding with a copy block for a retired copy that starts at boot; T10 (§2.1, words table, §10.1) |
| C13 | medium: "Restart them" and F1 can restart what needrestart never restarts; `sshd` is not Ubuntu's unit name | Accepted: one button per service, never a bulk restart; F1 refuses needrestart's never-restart list (the helper's copy of Ubuntu's `override_rc`, read in the lab, plus simple config entries; buttons hidden when an entry cannot be read), matching every name `systemctl show -p Id,Names` gives; those units route to a server restart; T33 (§7.2 Check 2, §7.4) |
| C14 | medium: D1's argument is wrong, and `logs` returns raw lines | Accepted with C4: log pages are redacted inside the helper with a frozen copy of the redactor's patterns (T35) and again by the harness; logs are class `private`, for Seraphina's own user only; D1 states the real trade-off (§6.1, §7.2 Check 8, D1) |
| C15 | medium: excluding families makes apt refuse whole jobs, and the novice gets no way to install them | Accepted: a dependency step moves every package tied by an exact version to a left-out family, both ways, then simulates and repeats; Check 1 shows the number the button can install; each family has an exact copy block and a plain sentence; the Docker "own time" text is replaced; the unattended-upgrades interaction is stated (the lab's config allows kernels); T19 fixture pairs (§7.2 Check 1, §7.3, §7.6, D13) |
| C16 | medium: approvals ahead of a maintenance time would almost always be refused | Accepted: an approval is a set of exact `<package>=<version>` pairs; at the time, only pairs still upgradable at those versions install, installed ones are skipped, nothing is added, and only a subset whose simulation pulls in more is refused; the rest is reported as waiting; T31 cases (§6.7 item 5, §7.5); V2's list-hash binding is superseded |
| C17 | medium: P2 and P3 waited for v2 phase B, which brings the executor that replaces them; Needs you held items the novice could not act on | Accepted, route (a): every package job runs the helper's own way-back check (kept files verified, unpacked against their own `md5sums`, and accepted by apt's simulation as the way back) before anything is installed, which replaces phase B's restore tests as P2's precondition; what it does and does not prove is on the card and in A23; D24 offers route (b); P3 no longer inherits a backup precondition; "restart needed" and "updates waiting" join Needs you only from P2 (§7.3, §7.5, §11.5, §11.10, D24) |
| C18 | medium: P1 is too large, and its done-when needs a lab that does not exist | Accepted: P1a (host strip first, then the launcher with `info`, `containers`, `compose` for project names, `ports`, `tailscale`, `disks`, `gpu`), P1b (the Checks), P1c (the root page, after the move to Seraphina's own user), each with its own done-when; L1 is one read-only read on the owner's host (Q3); L6 moved to P2; L7 needs no systemd (§11.10, §12.2) |
| C19 | medium: no novice check in any phase; terminal steps not stated as a difference from v2 | Accepted: every phase names its novice journey; setup is one self-checking paste through the new `setup` mode, which writes the sudoers file from its own template, moves files in a safe order and prints one line, with a plain fallback; `remove` is one paste; the Beszel login has steps taken from Beszel's source; A22 records the remaining terminal steps and why; T36 (§4.4, §6.2, §6.11, §11.10) |
| C20 | medium: the "full control" headline and its "new" alerts will be noise | Accepted: the headline counts only ways to take full control now; root inside a container, ordinary devices such as GPUs, host networking and other capabilities go under "Could matter if something escapes", never counted or raised; rows keyed by kind, subject and target, never a container id; T23 recreate case (§8.1 row 9, §8.2, §11.8) |
| C21 | medium: the card promises "you can go back" with no way to do it | Accepted: a "How to go back" panel with the exact `image:` line, the stack link, Deploy, what Update then shows, and what it does not restore (the data); without a keep tag it says so; U12 (§2.6, §2.9) |
| C22 | medium: socket-activated listeners such as Cockpit and SSH are joined to PID 1 | Accepted: `ports` also reads `systemctl list-sockets --all --output=json` (systemd `v255` `systemctl-list-units.c:410-452`) and prefers a process other than PID 1; Cockpit's link base comes from its socket; T18 fixtures, L15 (§2.1, §2.7, §6.3, §7.2 Check 6) |
| C23 | low: the maintenance toggle shows values that do nothing as if active | Accepted: the card shows only `pinned` and `frozen`; other values read "Updates for this app: not done by Seraphina yet", and the toggle sits on the app's page, labelled Planned (§2.2, §2.9) |
| C24 | low: first run asks for every address one at a time | Accepted: one review list, rows ticked in advance only when `TS_HOSTNAME` equals the address's first label and exactly one target joins, **These are right**, edit in place; it also proposes expected states and old copies; D5; U12 (§2.3, §2.10, §11.3) |
| C25 | low: percentage tones stay amber on big disks full on purpose | Accepted: amber below 15% or 100 GB, whichever is smaller, red below 5% or 20 GB; per-disk numbers or "full on purpose"; SnapRAID array disks full on purpose by default, linked to that panel (§3, §11.2) |
| C26 | low: T17's comparison is brittle and its decoder needs a non-stdlib tool | Accepted: matrices compared with the reference's mask forced; penalty scoring and mask choice tested apart; a small standard-library reader in the test reads each output back; the reference encoder is needed only once, at development time (T17) |
| C27 | low: Compose and `buildx` run as Docker CLI plugins outside the binary check, and `buildx du` contradicted "never calls anything else" | Accepted: Compose runs standalone from its fixed plugin path, checked like every binary, with an empty `DOCKER_CONFIG` (verified, lab R23); the build-cache list comes from `GET /system/df` (Moby `api/types/build/cache.go:8-20`), so `buildx` is not used (§6.3, §6.4, §6.6, §7.3 M5) |
| C28 | low: before the helper is installed, cards are named by container id | Accepted: the page shows the host strip and one summary card with process names from `/proc/<pid>/comm` and no container id (§2.10, Q16) |

Totals for the critic round: 28 findings, 28 accepted, 26 of them in full and 2
in part (C4 and C8, each with the rejected part and its reason in its line); 0
rejected outright. Two pairs were the same issue and got one change each (C4 and
C14, C6 and C9). Six lines of the author's pass are superseded or tightened by
this round, as their own lines now say (V2, V12, V16, V18, V25, V26).

**Capture round** (2026-10-25, from a read-only capture of the owner's server;
one line per change; the facts are labelled observed in the text):

| # | Finding | Decision |
| - | ------- | -------- |
| K1 | high: a login gateway (nginx, Dex, two oauth2-proxy) took over the old proxy's ports; the design assumed "no gateway, per-app logins" | Accepted: Status, §1.2 row, §1.5, the words table |
| K2 | high: "never in the request path" must hold next to the gateway | Accepted: §0 item 4 and §1.4 drop "each app keeps its own login"; N2 covers the gateway's files; new N11; §10.4 row "the gateway changes or breaks" |
| K3 | high: addresses through the gateway are invisible to the serve-based discovery, and a host-port join lands on nginx | Accepted: §2.3 source 4 and the `ingress` verb (§6.3, shape only, never secrets; T37, L16); §2.1 rule 3 follows the route to its upstream |
| K4 | high: 7 of 8 sidecars set no `TS_*` variable | Accepted: state found by the mount at `/var/lib/tailscale`, node name from the profile, pre-ticking by the profile's name (§2.3, §6.5); Q2 answered; T9 fixtures |
| K5 | high: oauth2-proxy answers before the app, so route probes show dead apps green; the gateway has no health check; Python 3.13+ verifies certificates strictly | Accepted: probes go to upstreams; the gateway's own card; `probe.target`, `ca_file`, `pin_sha256`, never verification off (§2.5, §11.2, §11.7; T38) |
| K6 | medium: almost every image runs from a local copy referenced by id | Accepted: the "pinned to a local copy" kind and the owner's keep-tag families, never trimmed and outside Seraphina's limits (§2.6, §7.3 M5, D9, `keep_tag_prefixes`) |
| K7 | low: label-less job containers would be proposed as old copies | Accepted: §2.1 rule 7, job runs; Q17 partly answered |
| K8 | medium: one home-network address carries the gateway and several apps | Accepted: N10 extended; the doctor errors on a shared address in `server.host` or `allowed_hosts` (§11.2, §11.7) |
| K9 | medium: the gateway, and containers with writable backup or parity mounts, were missing from the root page | Accepted: §8.1 row 16, row 9's "could matter" list, §8.2 rows, §8.3 (the sign-in service's users and second factors) |
| K10 | high: a root container on the system bus, root units running scripts from a home folder, runtime drop-ins | Accepted: row 9 counts a system-bus bind as full control; row 10 reads interpreter arguments and runtime drop-ins; T22b; §6.1's operator row reworded to "no sudo grant; other root paths may remain" |
| K11 | medium: sudo-rs 0.2.13 as `sudo`, classic sudo as `sudo.ws` | Accepted: implementation from the resolved path; both programs as doors and both checked (§8.1 row 3, §8.2 Doors, §6.11, L7, T22); Q1 answered; Appendix B notes the older version |
| K12 | medium: Beszel's hub checked through its gateway route would always look up | Accepted: checks go to the hub's upstream, links keep the route (§4.2, §4.3) |
| K13 | medium: should Seraphina sit behind the gateway? | Accepted as D25, recommending no; Appendix A25 for the endpoint chains |
| K14 | low: gateway routes use a private certificate | Accepted: QR and Add to Home Screen only for tailnet addresses, with a warning otherwise (§2.4) |
| K15 | low: recipes named `compose.json`, and Dockge's own project in a home folder | Accepted: the `config_files` label (§2.7, §6.3 `compose`); Q8 partly answered |
| K16 | low: systemd-networkd and NetworkManager both active; Cockpit's proxy drop-in | Accepted: §9; Q10 partly answered; Q13 answered |
| K17 | low: Check 6 had no "home network only" scope | Accepted: the scope, and the gateway as one row with its routes (§7.2 Check 6) |
| K18 | medium: a web file manager and the Docker manager are open on every address; NVMe names change between boots | Accepted: §8.1 rows 8 and 14, §8.2; disks keyed by UUID (§3); the mock's new states (§11.6) |
| K19 | high: P1a and P1b could run as the owner's account, which on this server is in `docker` and `lxd` | Accepted: not allowed where the account holds a root-equivalent group; the system user first; `caller_holds_root`; the doctor's error; D1's recommendation; Appendix A26 (§0 item 8, §6.1, §6.7, §11.7, §11.10); Q15 answered: the owner's account does not linger |

Totals for the capture round: 19 findings, 19 accepted. Nothing in this round
was run on the owner's server by this design's author. A review of the round's
text then corrected it without adding findings: what is observed and what is
reported or judgement (the route answers, one app per route port, the file
manager's second password and its exposure on every address), one count of the
gateway's ports everywhere (15 on the home network, port 80 on every address,
about 14 apps), each root-equivalent group's own reason (§0 item 8, §6.1), and
M5 and D9 for the owner's keep tags (K6).
