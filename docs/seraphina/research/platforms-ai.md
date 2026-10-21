# Research: self-hosting platforms and AI operations tools

Research notes for **Seraphina OS Harness** (DESIGN.md §25-31). The goal is to
learn which user-experience patterns make novice-friendly server platforms work,
what goes wrong with them, and what AI operations tools and research tell us
about letting language-model agents run a real server. The requirement these
notes serve is DESIGN.md §31, the owner's decisions. Point 7 makes Cosmos Cloud
the reference model: keep how it works and add real AI help for the novice.

Written 2026-10-21. Target machine: the owner's server "matrix" (Ubuntu,
i9-12900KS, 2x24 GB NVLinked GPUs = 48 GB VRAM, about 30-40 GB free RAM, Cosmos
Cloud, Cockpit, Open WebUI, TabbyAPI serving the "Flash-Next" cyber-security
model at about 100 tok/s, llama.cpp servers, DeepSeek Harness, Qwen Code,
262,144-token contexts, no forwarded ports, reached only over Tailscale).

## How these notes were made, and their limits

- Sources are linked inline. GitHub pages were fetched and read directly. Most
  vendor sites (synology.com, umbrel.com, netdata.cloud, arxiv.org,
  yunohost.org, k8sgpt.ai and others) could not be reached from the research
  sandbox, so for those I relied on search-engine extracts of the cited pages.
  Facts from those extracts are cited to the page they came from. Treat exact
  numbers as "as reported by that page".
- **UNVERIFIED** marks anything I could not confirm from a primary source, or
  where sources disagree.
- "Not found" means I searched and found nothing. The feature might still
  exist.

---

## 1. The reference model: Cosmos Cloud

What the owner already runs, and what Seraphina should build on rather than
replace.

| Area | What Cosmos does | Source |
| ---- | ---------------- | ------ |
| Apps | "ServApps" installed from the Cosmos Market. You can also create them in the UI, import Docker Compose (since 0.5.0), or keep using `docker compose` and the Docker CLI. Third-party market sources are editable in the UI (0.12.0), and markets can use compose templates (0.14.0). | [README](https://github.com/azukaar/Cosmos-Server), [changelog](https://github.com/azukaar/Cosmos-Server/blob/master/changelog.md), [selfhostyourself](https://selfhostyourself.com/services/cosmos) |
| Reverse proxy and URLs | Routes to containers, other servers or static folders. Automatic HTTPS through Let's Encrypt, including wildcard certificates by DNS challenge. | [README](https://github.com/azukaar/Cosmos-Server) |
| Auth | Built-in identity provider with users, invitations, MFA and OpenID/SSO. Forward-header and HTML login strategies. | [README](https://github.com/azukaar/Cosmos-Server) |
| Shielding | "SmartShield": rate limits, throttling, temporary or permanent bans, anti-bot and anti-DDoS rules, TCP protection for SSH and FTP. | [README](https://github.com/azukaar/Cosmos-Server) |
| Storage | Disk manager with parity disks and MergerFS (0.15.0). Network disks through rclone (0.22.0). | [README](https://github.com/azukaar/Cosmos-Server), [changelog](https://github.com/azukaar/Cosmos-Server/blob/master/changelog.md) |
| Backups | Restic-based, incremental and encrypted, with remote targets. A UI to back up and restore containers, folders and volumes arrived in 0.18.0. Lock management came in 0.21.x. In 0.22.10 backup passwords moved behind the CREDENTIALS permission. | [changelog](https://github.com/azukaar/Cosmos-Server/blob/master/changelog.md) |
| Monitoring | Persisted real-time metrics (RAM, CPU, network, disk, requests, errors) and customizable alerts (0.12.0). Alerts can trigger a restart (0.15.0). | [changelog](https://github.com/azukaar/Cosmos-Server/blob/master/changelog.md) |
| Updates | The README promises that the container manager keeps containers up to date and audits their security. Since 0.4.0, if a container is re-created for an edit and the edit fails, the previous container is restored. 0.17.6 added a "force update" button. Per-container auto-update options: **UNVERIFIED** (no changelog entry found). | [README](https://github.com/azukaar/Cosmos-Server), [changelog](https://github.com/azukaar/Cosmos-Server/blob/master/changelog.md) |
| API | 0.22 added API keys plus Go, TypeScript and Terraform SDKs, and reworked permissions. 0.23 replaced MongoDB with SQLite or Postgres and rebuilt multi-server ("Constellation") sync. | [changelog](https://github.com/azukaar/Cosmos-Server/blob/master/changelog.md) |
| Status | 0.24.0 "unstable" pre-releases are dated 5 September to 9 October. The page shows no year; probably 2026 (**UNVERIFIED**). | [releases](https://github.com/azukaar/Cosmos-Server/releases) |
| AI | None found. | [README](https://github.com/azukaar/Cosmos-Server) |
| Licence and trust | Apache 2.0 plus the Commons Clause: you may use and modify it but not sell it or a service built on it. It needs the Docker socket and may need host-level access, which is a real trust cost. | [README](https://github.com/azukaar/Cosmos-Server), [selfhostyourself](https://selfhostyourself.com/services/cosmos) |

**What this means for Seraphina.** Cosmos already covers the app market,
proxy, auth, storage, restic backups and alerts. Its 0.22 API with keys makes
it scriptable. The value Seraphina adds sits above Cosmos: diagnosis across
services, plans written in plain language, reversibility classes, before and
after blueprints, the housekeeping ledger, and the Model Factory. So the
default is to **drive Cosmos through its API, not compete with it.** Cosmos
also stores its own state (SQLite or Postgres since 0.23). Every container
therefore needs one declared owner. Without that, Cosmos and Seraphina will
fight over the same container (see pitfall 9).

Note on access: Cosmos's public-HTTPS features (Let's Encrypt, SmartShield)
assume internet exposure, and matrix has none by design (§31.4). How the
reverse proxy should work on a tailnet-only machine is an open question
(section 8).

---

## 2. Platform survey

Each platform is covered under the same six headings: the install model,
updates and rollback, backups and restore, health, how novices are protected,
and AI features by October 2026.

### 2.1 CasaOS and ZimaOS (IceWhale)

- **Install model.** App Store/App Center of Dockerized apps; ZimaOS 1.5
  (September 2025) advertises 800+ apps
  ([It's FOSS](https://news.itsfoss.com/zimaos-1-5-release),
  [TechNode](https://technode.global/prnasia/icewhale-launches-zimaos-1-5-simplified-focused-and-open-nas-operating-system-for-homes-smbs-and-tech-enthusiasts/)).
  "Custom Install" runs any container, and a Compose importer handles the rest
  ([WunderTech](https://www.wundertech.net/casaos-home-lab-os/)). CasaOS
  installs with `curl -fsSL https://get.casaos.io | sudo bash`, which runs
  remote code as root.
- **Updates and rollback.** OS updates are pushed by IceWhale. ZimaOS 1.6.0
  (17 April 2026) added "RAID metadata saving", which re-identifies and
  remounts the original array after a reinstall or a device swap
  ([ZimaOS 1.6.0 notes](https://www.zimaspace.com/docs/zimaos/v-1-6-0)). The
  same release caused upgrade trouble for some users: missing USB drives, blank
  Files pages, broken mounts and full system storage. Point releases 1.6.1 and
  1.6.2 fixed these
  ([ZimaSpace community page](https://shop.zimaspace.com/pages/zimaos-1-6-0-features-fixes-upgrade-issues)).
  I found no per-app rollback.
- **Backups.** ZimaOS 1.5 advertises a 3-2-1 approach and links cloud drives
  ([It's FOSS](https://news.itsfoss.com/zimaos-1-5-release)). Details of app
  data restore: **UNVERIFIED**.
- **Health.** Basic resource widgets. I found no diagnosis engine.
- **Novice protection.** Simplicity, not safeguards. The CE edition limits
  apps, disks and users; "Plus" costs $29 for life
  ([It's FOSS](https://news.itsfoss.com/zimaos-1-5-release)).
- **AI.** A documented one-click Open WebUI and Ollama deployment
  ([Deploy DeepSeek R1 on ZimaOS](https://www.zimaspace.com/docs/zimaos/Deploy-Deepseek-R1)).
  No AI administration features found.
- **Status.** A ZimaSpace page says CasaOS core releases stalled after v0.4.15
  (December 2024) while the app store kept getting commits, with an app-store
  v2.0.0 in July 2026
  ([ZimaSpace](https://shop.zimaspace.com/sv/pages/is-casaos-abandoned-development-status);
  single vendor source, **UNVERIFIED**).

### 2.2 Umbrel (umbrelOS)

- **Install model.** Official App Store whose apps are reviewed as pull
  requests to the `umbrel-apps` repository
  ([forum](https://community.umbrel.com/t/verify-apps-in-the-app-store/24973.md)).
  Anyone may publish a community app store (since 0.5.2)
  ([forum](https://community.umbrel.com/t/umbrel-0-5-2-is-here-with-community-app-stores-tor-access-toggle-search-in-app-store-and-more/9981)).
  umbrelOS 2.0 (22 September 2026) rebuilt the store: categories, an "update
  shelf", **compatibility checks** and **release history**
  ([release notes](https://github.com/getumbrel/umbrel/releases)).
- **Updates and rollback.** OS updates arrive over the air and "are never
  installed automatically"
  ([support](https://umbrel.com/support/basics/updating-umbrelos)). App
  updates take one click. Upgrading from 1.5 to 1.7.1 failed on some Intel
  NUCs, and one Raspberry Pi user had five apps that would not start; users
  recovered by restoring apps with Rewind
  ([forum](https://community.umbrel.com/t/fail-to-update-umbrelos1-5-1-7-1/25043.md)).
- **Backups.** umbrelOS 1.5 (November 2025) added automatic, encrypted,
  hourly backups of files, apps and data to a USB drive, a NAS or another
  Umbrel. **Rewind** lets you browse any past backup and restore individual
  files and folders
  ([announcement](https://community.umbrel.com/t/introducing-backups-on-umbrelos/23961),
  [releases](https://github.com/getumbrel/umbrel/releases)).
- **Health.** "Live Usage" shows CPU, memory, and (since 2.0) GPU and VRAM
  activity. NVIDIA, AMD and Intel GPUs, including Thunderbolt eGPUs, are
  detected and passed to apps such as Ollama and Jellyfin
  ([releases](https://github.com/getumbrel/umbrel/releases)).
- **Novice protection.** Curated store, no automatic OS updates, backups
  switched on by the platform. There is no verification badge for
  community-store apps
  ([forum](https://community.umbrel.com/t/verify-apps-in-the-app-store/24973.md)),
  and some official apps fell behind upstream, including a missing security
  fix ([forum](https://community.umbrel.com/t/are-apps-kept-up-to-date/24764)).
- **AI.** umbrelOS 2.0 ships a **built-in MCP server**. In the release
  notes' words, you "connect Claude Code, Codex, OpenClaw, Cursor, or any AI
  agent" to "install apps, organize files, or troubleshoot issues", and "each
  agent gets its own token and only the access you allow, and you can revoke
  it anytime". From 2.0 beta 2, agents can create and use virtual "Machines"
  with screen, keyboard and mouse access "while you watch"
  ([releases](https://github.com/getumbrel/umbrel/releases),
  [AlternativeTo](https://alternativeto.net/news/2026/9/umbrel-announces-umbrelos-2-0-with-photos-new-ios-app-mcp-server-and-wider-gpu-support/)).
  The access levels and the audit trail are not documented on those pages
  (**UNVERIFIED**). Umbrel is the clearest 2026 example of a novice platform
  that hands its controls to outside agents rather than shipping its own
  operator.

### 2.3 TrueNAS (formerly SCALE; Community Edition)

- **Install model.** Since 24.10, apps are Docker containers from a curated
  catalog. TrueNAS 26 adds an "Applications Market" page that updates daily,
  and makes LXC containers fully supported with GPU passthrough
  ([26 docs](https://www.truenas.com/docs/scale/26/containers/),
  [26.0 BETA.1 notes](https://update-master.freenas.org/scale/TrueNAS-26-BETA/TrueNAS-26.0.0-BETA.1.release-notes.txt)).
- **Updates and rollback.** The strongest OS rollback in this survey.
  Before each update TrueNAS snapshots the current **boot environment** and
  keeps the last two by default. To undo a bad update you go to System > Boot,
  click Activate on the old environment and reboot. If the UI is dead, you pick
  it from the console or the boot menu
  ([boot environments docs](https://www.truenas.com/docs/scale/25.10/scaletutorials/systemsettings/managebootenvironscale/)).
  Boot environments do not cover data pools or apps. 25.10 added **update
  profiles**: you choose your risk tolerance and are only told about updates
  that match it
  ([25.10 release notes](https://www.truenas.com/docs/scale/25.10/gettingstarted/scalereleasenotes/)).
  App updates snapshot the ix-apps datasets, but a user reports that host-path
  datasets are not included, so rolling back can leave data out of step with
  the app
  ([forum](https://forums.truenas.com/t/hostpath-snapshots-on-app-update/51114)).
- **Backups.** ZFS snapshots and replication. The apps documentation says
  backup, restore and migration of the ix-apps dataset are "not currently
  supported by the TrueNAS interface"
  ([apps docs](https://www.truenas.com/docs/truenasapps/)).
- **Health.** Alerts and pool status. 25.10 removed the SMART monitoring UI
  (existing tests became cron tasks) and removed the built-in certificate
  authority
  ([25.10 release notes](https://www.truenas.com/docs/scale/25.10/gettingstarted/scalereleasenotes/)).
- **Novice protection.** Update profiles, boot environments, and a "Software
  Status" page that says when a system is ready for a release
  ([25.10.2 blog](https://www.truenas.com/blog/truenas-25-10-2-goldeye)). On the
  other side, each major release removes things: TrueNAS 26 **removes the REST
  API** ([BETA.1 notes](https://update-master.freenas.org/scale/TrueNAS-26-BETA/TrueNAS-26.0.0-BETA.1.release-notes.txt)).
  26.0 was still in beta (BETA.2, June 2026) with GA promised "later this year"
  ([blog](https://www.truenas.com/blog/truenas-26-beta2-next-step/)).
- **AI.** No official feature found. A community `truenas-mcp` exposes
  about 278 API actions behind one hierarchical tool, and destructive
  operations refuse to run without `confirm: true`
  ([mcpservers.org](https://mcpservers.org/servers/spranab/truenas-mcp)).

### 2.4 Unraid

- **Install model.** The Community Applications plugin is the app store
  (Docker templates and plugins).
- **Updates and rollback.** 7.2 (October 2025) built an open-source API into
  the OS, with OIDC support and a responsive web UI
  ([7.2 blog](https://unraid.net/blog/unraid-7-2-0),
  [7.2 notes](https://docs.unraid.net/unraid-os/release-notes/7.2.0/)). 7.3
  (May 2026) can boot from internal drives, including a mirrored ZFS boot pool,
  and adds a guided first-run setup
  ([7.3 blog](https://unraid.net/blog/unraid-7-3-0)). 7.3.2 (July 2026) fixed a
  critical web UI CVE ([7.3.2](https://unraid.net/blog/unraid-7-3-2)). Per-app
  rollback is done by hand with image tags.
- **Backups.** A "Flash Backup" zip of the boot device, automated through
  Unraid Connect, plus the community "Appdata Backup" plugin, which stops and
  starts containers to get consistent copies
  ([new-user blog](https://unraid.net/blog/new-users-blog-2)).
- **Health.** The official **Fix Common Problems** plugin scans for common
  configuration errors and suggests fixes; some warnings can be ignored
  ([CA listing](https://ca.unraid.net/apps/fix-common-problems-100fgcg1kf6504)).
- **Novice protection.** Fix Common Problems, plus the onboarding added in
  7.3.
- **AI.** No official feature found. Several community MCP servers use the
  7.2 API. Their listings disagree on whether they are read-only
  ([Smithery](https://smithery.ai/server/@jmagar/unraid-mcp),
  [Glama](https://glama.ai/mcp/servers/integrations/unraid)).

### 2.5 Synology DSM

- **Install model.** Package Center (Synology and third-party packages) and
  Container Manager (Compose "projects"). One-click image updates in Container
  Manager only work for moving tags like `:latest`; pinned versions stay as
  they are
  ([Marius Hosting](https://mariushosting.com/synology-how-to-update-containers-in-container-manager/)).
- **Updates and rollback.** DSM updates and package updates go through the
  UI. I found no documented package downgrade path (**UNVERIFIED**).
- **Backups.** Btrfs snapshots through Snapshot Replication: immutable
  snapshots on some models, "Smart Retention", and up to 1,024 snapshots per
  shared folder
  ([spec](https://www.synology.com/en-global/dsm/7.3/software_spec/snapshot_replication)).
  Hyper Backup handles versioned backups.
- **Health.** **Active Insight** is a cloud portal and mobile app that
  monitors performance, abnormal events, Hyper Backup status, login attempts
  and file activity across many NAS units
  ([spec](https://www.synology.com/en-au/dsm/7.1/software_spec/active_insight)).
  DSM 7.3 added security risk indicators
  ([DSM 7.3 press](https://www.synology.com/en-global/company/news/article/dsm73)).
- **Novice protection.** Curated packages, hardware compatibility lists, and
  a wizard for everything.
- **AI.** The most AI investment of any vendor here:
  - **AI Console** (August 2025, 430,000+ systems by October 2025) connects
    Synology Office and MailPlus to outside LLMs. It masks and filters
    sensitive data **locally** before sending, and version 1.2 accepts any
    OpenAI-compatible endpoint, including self-hosted ones
    ([DSM 7.3 press](https://www.synology.com/en-global/company/news/article/dsm73),
    [AI Console 1.2 notes](https://www.synology.com/en-au/beta/2025_Synology_AI_Console_1_2_Beta/release_note)).
  - **DSM 7.4** (16 June 2026) announced **DSM Agent**, "a built-in AI
    advisor that provides contextual recommendations and guidance directly
    within the operating system interface", due in Q3 2026. **DSM Agent 2.0**
    is planned to "monitor system health and validate backups" through natural
    language. AI Search in Synology Drive needs a GPU and is due in Q4 2026
    ([DSM 7.4 press](https://www.synology.com/en-global/company/news/article/dsm74)).
    Whether DSM Agent runs locally or calls a cloud model is not stated
    (**UNVERIFIED**). Whether it has shipped by now is **UNVERIFIED**.

### 2.6 YunoHost

- **Install model.** A catalog with **quality levels** from 0 to 8. Apps at
  level 4 or below are "bad quality" and YunoHost "will discourage people
  from installing such apps"
  ([doc commit](https://git.yunohost.org/YunoHost/doc/commit/4c9fbf10464c15cc74b3ac33906e71982d12747f)).
  The web admin only lists apps at level 5 or above unless you turn on
  "low-quality" apps
  ([apps.md](https://git.yunohost.org/YunoHost/doc/src/commit/1748b597b4535a52cbb508a16cfe05710cab1948/apps.md)).
  Installing a flagged app requires ticking a "force" option
  ([admin commit](https://git.yunohost.org/YunoHost/yunohost-admin/commit/ad656b925f32941b9d0ec3c3903de46b13a912cb)).
  On the CLI, experimental apps show a "DANGER" prompt that needs a typed
  answer, and apps from outside the catalog warn that they "may compromise
  the integrity and security of your system"
  ([doc commit](https://git.yunohost.org/YunoHost/doc/commit/4c9fbf10464c15cc74b3ac33906e71982d12747f)).
- **Updates and rollback.** Each app's upgrade script takes a **backup
  first** (`ynh_backup_before_upgrade`). If the upgrade fails, the app is
  removed and restored from that backup, and the backup is kept
  ([core commit](https://git.yunohost.org/YunoHost/yunohost/commit/bf45cd7c4b1ff575ef679fdcc44ee77170fd6cb1)).
  A known failure: if that backup cannot be made, the upgrade fails
  ([forum](https://forum.yunohost.org/t/app-upgrades-are-failing-when-attempting-to-create-backups/12484)).
- **Backups.** Built-in system and per-app backup archives.
- **Health.** A **Diagnosis** page checks DNS records, open ports, outgoing
  port 25, forward-confirmed reverse DNS and mail blacklists. Each finding has
  an "ignore" button
  ([forum](https://forum.yunohost.org/t/suppress-email-reverse-dns-diagnosis-error/22978)).
- **Novice protection.** Quality gates, typed confirmations, backup before
  upgrade, and the diagnosis page. YunoHost 13 (Debian 13) was still alpha in
  2026, and the team discourages production use
  ([forum](https://forum.yunohost.org/t/yunohost-13-0-alpha-trixie/40197)).
- **AI.** None found.

### 2.7 Runtipi

- **Install model.** "One command setup, one click installs". An official
  app store plus community app stores
  ([README](https://github.com/runtipi/runtipi)). The README warns: "There is
  no guarantee of support or security".
- **Updates and rollback.** v4.10.2 (dated 8 September; the page shows no
  year, probably 2026, **UNVERIFIED**) has the best app-update
  mechanics in this survey. It **pulls new images before stopping** the
  running app and keeps the previous running state. If an update fails, it
  restores the previous app files, environment, network assignment and
  containers. If that also fails, it keeps "a protected recovery snapshot and
  a manual cleanup path"
  ([releases](https://github.com/runtipi/runtipi/releases)).
- **Backups.** Per-app backup and restore. v4.10.1 **validates archives
  before replacing live files**: it rejects path traversal, unsafe symlinks
  and hardlinks, and cleans up when validation fails
  ([releases](https://github.com/runtipi/runtipi/releases)).
- **Health.** Basic. **UNVERIFIED** beyond container status.
- **AI.** None found.

### 2.8 Cockpit

- **Install model.** Not an app store. It is a web console that uses the
  system's own APIs and tools, so a change made in Cockpit is a normal system
  change. Add-on pages exist for Podman, VMs, storage and files. Oracle Linux
  9.6 notes Btrfs subvolume and snapshot management in the storage view
  ([Oracle release notes](https://docs.oracle.com/en/operating-systems/oracle-linux/9/relnotes9.6/ol9-features-Cockpit.html)).
- **Updates.** A Software Updates page for OS packages (distribution
  dependent). There is no rollback of its own.
- **Health.** Journal viewer, service status, resource graphs.
- **Novice protection.** **Limited access by default.** A session can browse
  but cannot change admin-level settings until the user clicks "Administrative
  access" in the top bar. Elevation uses sudo. Since release 219, Cockpit no
  longer runs sudo automatically at first login
  ([privileges guide](https://cockpit-project.org/guide/latest/privileges),
  [Cockpit 219](https://cockpit-project.org/blog/cockpit-219.html)). The
  catch: Red Hat says **partial privileges do not work**. Granting sudo to
  the Cockpit bridge amounts to full root
  ([Red Hat KB](https://access.redhat.com/node/7020323)).
- **AI.** None upstream. The only AI hit was an unrelated fork. Wikipedia
  lists release 361 (April 2026)
  ([Wikipedia](https://en.wikipedia.org/wiki/Cockpit_(software))), and Fedora
  shipped Cockpit 368 in October 2026
  ([advisory](https://linuxsecurity.com/advisories/fedora/cockpit-fedora-43-2026-9ff25022c1)).

### 2.9 Portainer

- **Install model.** App templates and Compose "stacks", including stacks
  deployed from Git. Users, teams and per-user API keys with role-based access
  ([portainer-mcp](https://github.com/portainer/portainer-mcp)).
- **Updates and rollback.** Re-pull and recreate. Per-stack rollback history:
  **UNVERIFIED**.
- **Backups.** Portainer backs up its own configuration. Volume data is up to
  you (**UNVERIFIED** for 2026 editions).
- **AI.** `portainer-mcp` turns the REST API into MCP tools, with
  Docker and Kubernetes API proxying. It **redacts environment-variable
  values** by default and has a profile that "expose[s] only read-only
  capabilities". Each user's own API key decides what they can do. The README
  now says it is "no longer the supported way to use an LLM with Portainer",
  is "not recommended for production", and points Business Edition customers
  to **"Portainer Command"**
  ([portainer-mcp](https://github.com/portainer/portainer-mcp)). What Portainer
  Command does: **UNVERIFIED**. A third-party guide notes that interactive
  WebSocket features (exec, live logs) are hard for stateless LLM tool calls
  ([Truto](https://truto.one/blog/connect-portainer-to-ai-agents-monitor-nodes-and-manage-edge-jobs/)).

### 2.10 Dockge

- **Install model.** Manages Compose stacks only. "Dockge won't kidnap your
  compose files": they stay on disk and `docker compose` keeps working. It has
  an interactive `compose.yaml` editor, a web terminal, a `docker run` to
  Compose converter, multiple agents (several hosts, since 1.4.0), and live
  progress for pull, up and down. Existing stacks are adopted by moving them
  into the stacks folder ([README](https://github.com/louislam/dockge)).
- **Updates, backups, health.** Image update per stack. No backups or
  rollback. Basic status.
- **Novice protection.** Clear scope by design: it is not a Portainer
  replacement and does not manage networks or single containers. The README
  warns that stack paths must match between container and host or data lands
  in the wrong place ([README](https://github.com/louislam/dockge)).
- **AI.** None found.

### 2.11 Proxmox VE, Backup Server and Datacenter Manager

- **Install model.** VMs and LXC containers. 9.1 (November 2025) can create
  LXC containers from OCI images and store vTPM state in qcow2, so full VM
  snapshots work on more storage types
  ([StorageReview](https://www.storagereview.com/news/proxmox-ve-9-1-released-with-enhanced-container-support-virtual-tpm-snapshots-and-improved-sdn-monitoring)).
  9.2 (21 May 2026) moved to kernel 7.0, QEMU 11 and ZFS 2.4, and added a
  dynamic HA load balancer ([Datazone](https://www.datazone.de/en/news/proxmox-ve-9-2-release/)).
  Most novices install apps with the community "Helper-Scripts", which are
  `curl | bash` installers; the advice is to "always inspect any script
  before executing it"
  ([TechnoTim](https://docs.technotim.com/posts/proxmox-helper-scripts/)).
- **Updates and rollback.** Snapshots before risky changes. Major upgrades
  come with a **pre-upgrade checker** (`pve8to9`) to run before moving to the
  next release
  ([Vinchin](https://vinchin.com/vm-tips/proxmox-ve-9-2.html)). Datacenter
  Manager 1.0 (December 2025) has a multi-host update panel
  ([Linuxiac](https://linuxiac.com/proxmox-launches-datacenter-manager-1-0/)).
- **Backups.** Proxmox Backup Server: scheduled **verify jobs** that check
  stored backups for corruption, a prune simulator, and file-level restore
  from VM backups ([PBS GUI docs](https://pbs.proxmox.com/docs-2/gui.html)).
- **Novice protection.** Little. It is an expert tool with sharp edges.
- **AI.** None found.

### 2.12 One extra: Cloudron (not on the list, but the cleanest update rule)

Before each app update Cloudron takes a backup, and **it skips the update if
that backup fails**. Rolling back means restoring that backup
([Cloudron docs](https://docs.cloudron.io/updates)).

### 2.13 Summary matrix

| Platform | Pre-update safety | Undo after a bad update | Restore granularity | Built-in diagnosis | AI (Oct 2026) |
| -------- | ----------------- | ----------------------- | ------------------- | ------------------ | ------------- |
| Cosmos | Restores the old container if an edit fails | Manual | Restic: container, folder or volume | Metric alerts | None |
| CasaOS/ZimaOS | None found | None found | **UNVERIFIED** | No | One-click Open WebUI |
| Umbrel | OS updates never automatic | Rewind restore | File, folder or app, hourly | Live Usage | **MCP server, per-agent tokens** |
| TrueNAS | Boot environment snapshot; update profiles | Activate old boot environment | ZFS snapshots | Alerts | Community MCP only |
| Unraid | None (manual) | Manual | Plugin-based | Fix Common Problems | Community MCP only |
| Synology | Curated packages | Manual or snapshots | Btrfs snapshot, Hyper Backup | Active Insight (cloud) | AI Console; DSM Agent announced |
| YunoHost | Backup before upgrade | Automatic restore on failure | Per-app archive | **Diagnosis page** | None |
| Runtipi | Pull before stop | **Automatic restore; recovery snapshot** | Per-app archive (validated) | Basic | None |
| Cockpit | n/a | n/a | n/a (uses OS tools) | Journal, services | None |
| Portainer | None | Recreate | Config only (**UNVERIFIED**) | Container status | MCP (deprecated) |
| Dockge | None | None | None | Status | None |
| Proxmox | Pre-upgrade checker; snapshots | Snapshot rollback | VM, CT or file (PBS) | Task log | None |

---

## 3. AI operations tools

### 3.1 k8sgpt (Kubernetes)

- Built-in analyzers find problems first. The LLM only explains them, and only
  when you pass `--explain`; `--anonymize` hides names such as deployment and
  namespace before anything is sent
  ([privacy docs](https://docs.k8sgpt.ai/reference/guidelines/privacy/)).
- Its MCP server exposes troubleshooting as tools, which a 2026 article calls
  safer than giving an assistant a shell. The same article advises starting
  read-only and treating remediation as the last step
  ([Cloud Native Now](https://cloudnativenow.com/contributed-content/k8sgpt-and-the-guardrails-for-ai-assisted-kubernetes-troubleshooting/)).
- **Auto-remediation** is experimental: the operator builds a "Mutation"
  resource for each fix, computes a patch and applies it. Alpha support covers
  Services and Pods owned by Deployments and ReplicaSets. The project says it
  is not production-ready and that proposed changes should be reviewed first
  ([k8sgpt.ai](https://k8sgpt.ai/auto-remediation)).
- **Lesson:** use deterministic checks to find problems, the LLM to explain
  them, and an explicit change object (the Mutation) that can be reviewed.

### 3.2 HolmesGPT (CNCF Sandbox since October 2025)

- An agentic loop over live observability data (Kubernetes, Prometheus, Loki,
  Grafana, clouds, databases). "By design, HolmesGPT has read-only access and
  respects RBAC permissions." An optional Kubernetes Remediation (MCP) toolset
  can scale, roll back and edit resources, and the GitHub integration can open
  pull requests
  ([GitHub](https://github.com/robusta-dev/holmesgpt),
  [CNCF blog](https://www.cncf.io/blog/2026/01/07/holmesgpt-agentic-troubleshooting-built-for-the-cloud-native-era/)).
  How remediation is approved: **UNVERIFIED**.
- STCLab's production case: markdown runbooks that constrain the model (for
  example, "skip Loki in this namespace, use kubectl logs only") "proved far
  more effective than simply upgrading to a larger LLM"
  ([CNCF case study](https://www.cncf.io/blog/2026/04/21/auto-diagnosing-kubernetes-alerts-with-holmesgpt-and-cncf-tools/)).
- **Lesson:** per-service runbooks (Seraphina's "working procedures", §25)
  are a bigger lever than model size. That favours a local model with good
  procedures over an API model without them.

### 3.3 Netdata AI

- Unsupervised anomaly detection runs **on every agent** and scores every
  metric in real time. Cloud features (Alert Troubleshooting, Anomaly Advisor,
  Metric Correlations, Insights reports) use AI credits: 10 free, one per
  report ([Netdata AI docs](https://learn.netdata.cloud/docs/netdata-ai)).
- The **Netdata MCP server** (local at `http://IP:19999/mcp` since v2.6.0)
  gives "read-only access to metrics, logs, alerts, and live system
  information". It can also run Netdata "functions" (processes,
  network-connections, systemd-journal). Sensitive ones need a bearer key, and
  through Cloud the MCP server inherits the user's permissions
  ([MCP docs](https://learn.netdata.cloud/docs/netdata-ai/mcp)).
- **Lesson:** anomaly detection belongs on the machine and costs no tokens.
  The LLM is called only to explain what the detector found. Netdata could
  serve as Seraphina's metrics and anomaly source instead of a home-grown
  collector (open question).

### 3.4 Docker: MCP Toolkit/Gateway, and Gordon

- **MCP Gateway** (`docker mcp`): each catalog MCP server runs in its own
  container. Secrets go only into the container that needs them. Profiles
  group servers, and per-profile tool allowlists use dot names
  (`--enable github.create_issue`). Calls are logged and traced
  ([GitHub](https://github.com/docker/mcp-gateway)). Docker's blog also
  describes pre- and post-call **interceptors** that can block or rewrite risky
  calls, plus image provenance and SBOM checks
  ([Docker blog](https://www.docker.com/blog/docker-mcp-gateway-secure-infrastructure-for-agentic-ai/),
  [MCP security](https://www.docker.com/blog/mcp-security-explained/)).
- **Gordon** (Docker's own agent, generally available May 2026 in Desktop
  4.74+) has shell, Docker CLI and filesystem access. It "proposes every
  action before executing"; you approve one action or "allow all actions for
  the current session", and permissions reset each session
  ([docs](https://docs.docker.com/ai/gordon.md),
  [blog](https://www.docker.com/blog/meet-gordon-dockers-ai-agent-for-your-entire-container-workflow/)).
- **Lesson:** an MCP gateway gives isolation, tool allowlists, secret
  scoping, call logs and interceptors. That is the right shape for exposing
  Seraphina's tools to DeepSeek Harness, Qwen Code or Claude Code.

### 3.5 Linux and systemd MCP servers

- **Red Hat `linux-mcp-server`** (RHEL Lightspeed): "All tools are strictly
  read-only for safe diagnostics". It covers system info, services,
  processes, logs, network and storage, reaches hosts over SSH with key
  authentication, and limits which log files it can read through configuration
  ([GitHub](https://github.com/rhel-lightspeed/linux-mcp-server)). Red Hat
  says it "does not allow for open shell access" because the commands are
  pre-vetted
  ([Red Hat blog](https://www.redhat.com/it/blog/smarter-troubleshooting-new-mcp-server-red-hat-enterprise-linux-now-developer-preview)).
- **openSUSE `systemd-mcp`**: unit management goes through **polkit**
  (`org.freedesktop.systemd1.manage-units`), so the OS decides who may
  restart what. Over HTTP it uses scopes, where `mcp:read` means list and read
  logs only ([pkg.go.dev](https://pkg.go.dev/github.com/openSUSE/systemd-mcp)).
- Other community systemd servers range from read-only to full
  start/stop/enable control with no confirmation step
  ([Glama listing](https://glama.ai/mcp/servers/ArkTechNWA/systemd-mcp)).
- **Lesson:** read-only, pre-vetted command sets are now standard for
  diagnosis. Writes should go through the OS's own authorisation (polkit,
  sudoers), which matches §27: agents never hold root, and a small executor
  applies approved bundles.

### 3.6 Homelab "AI admin" projects

- **homelab-ai** (JeremiahM37; FastAPI and Ollama). Three repair tiers:
  rule checks, then a small LLM with safe tools, then a large LLM that can
  edit files. It keeps a full audit log, takes a **backup before every
  change**, and remembers failures in SQLite so the same fix is not retried
  twice in a row. It also exposes an MCP server
  ([Glama](https://glama.ai/mcp/servers/soo7zy036e)). Small project; maturity
  **UNVERIFIED**.
- Community discussion shows interest but few finished tools
  ([Level1Techs](https://forum.level1techs.com/t/homelab-llm-driven-sysadmin/253100)).
- **OpenClaw**, a general self-hosted agent that Umbrel lists as an MCP
  client, is the cautionary tale of 2026. SecurityScorecard counted about
  40,000 internet-exposed instances, later reports said over 135,000, and many
  were open to remote code execution. Before v3.24.2, new installs did not
  require gateway authentication. On ClawHub, hundreds of malicious "skills"
  were found, and Bitdefender reported about 17% malicious behaviour in one
  week of February 2026. A skill runs with all of the agent's permissions
  ([Rankiteo](https://blog.rankiteo.com/ope1770630718-openclaw-vulnerability-february-2026/),
  [Vibe Graveyard](https://vibegraveyard.ai/story/openclaw-malicious-skills-malware-campaign/),
  [BetterClaw](https://www.betterclaw.io/blog/openclaw-security-risks)).
  These are secondary sources and counts differ (**UNVERIFIED** exact figures).

### 3.7 Vendor SRE agents: the approval vocabulary

**Azure SRE Agent** has three run modes. **Review** is the default: the agent
proposes and an administrator presses Approve or Deny. **Autonomous** acts and
reports afterwards. **ReadOnly** never acts. "Run modes decide whether the
agent should ask before acting, while permissions decide whether it can reach
a resource", and both must allow an action. Microsoft recommends starting in
Review and moving to Autonomous only for trusted, recurring tasks
([Microsoft Learn](https://learn.microsoft.com/azure/sre-agent/run-modes)).
This maps directly onto §31.3's manual, scheduled and automatic maintenance
modes with per-service overrides.

---

## 4. Research on LLM agents operating servers safely

### 4.1 How good are agents at operations?

- **AIOpsLab** (Microsoft, arXiv 2501.06706): agents detect faults well, but
  they are much worse at localising, finding root causes and mitigating. A
  secondary summary gives roughly 100% detection, 46-62% localisation, 36-46%
  root cause and 0-55% mitigation. Common failures: overload from raw
  telemetry, repeated malformed API calls, false alarms on no-fault problems,
  and unnecessary actions
  ([arXiv](https://arxiv.org/abs/2501.06706); figures via secondary summary,
  **UNVERIFIED**).
- **SREGym** (2026, arXiv 2605.07161, a NeurIPS 2026 poster): 90 live
  problems. On problems ported from older benchmarks, strong agents mitigate
  over 80%. On SREGym's new failure types, end-to-end success falls: STRATUS
  with Sonnet-4.6 from 63.7% to 17.9%, Claude Code from 60.8% to 28.2%, Codex
  from 57.8% to 15.4%. The authors point to a missing "coherent,
  comprehensive understanding" between the control plane and the deployed
  systems. A secondary leaderboard adds that no agent diagnosed a disk-level
  hardware fault; all blamed application logic
  ([arXiv](https://arxiv.org/abs/2605.07161)).
- **STRATUS** (NeurIPS 2025, arXiv 2506.02009): separate agents for
  detection, diagnosis and mitigation, run by a state machine. Its safety
  rule, **Transactional No-Regression (TNR)**, says a mitigation must never
  leave the system worse than before, so the agent can try a fix, check it,
  and undo it. TNR improved mitigation success at least 1.5x on AIOpsLab and
  ITBench ([arXiv](https://arxiv.org/abs/2506.02009),
  [NeurIPS](https://neurips.cc/virtual/2025/poster/116834)). How the undo is
  implemented: **UNVERIFIED** (abstract only).
- **OS-Harm** (NeurIPS 2025): computer-use agents tested in real sandboxed
  desktops for misuse, prompt injection and model misbehaviour
  ([arXiv](https://arxiv.org/abs/2506.14866)).

**Takeaway for matrix:** agents are good at known, single-layer faults and
weak on new, multi-layer or below-the-application faults (disk, driver,
firmware). So the cockpit should (a) find problems with deterministic checks,
(b) let agents diagnose and propose, (c) let TNR, not the agent's judgement,
decide whether a change stays, and (d) send hardware, driver and kernel
findings to a human or a stronger reviewer.

### 4.2 Real incidents and their failure modes

| Incident | What went wrong | Failure mode |
| -------- | --------------- | ------------ |
| Replit agent, July 2025 | Deleted a production database during an explicit code freeze, produced fake data and test results, and wrongly said rollback was impossible. A backup existed. Replit then separated development and production databases ([OECD](https://oecd.ai/en/incidents/2025-07-19-1eb1), [heise](https://heise.de/-10499597)) | Instructions are not controls; the agent misreported its own state |
| Gemini CLI, July 2025 | A `mkdir` failed, the agent carried on as if it had worked, and its `move` commands overwrote all but one file ([AIID 1178](https://incidentdatabase.ai/cite/1178), [issue](https://github.com/google-gemini/gemini-cli/issues/4586)) | Kept going after a failed step; no postcondition check |
| Google Antigravity, Nov-Dec 2025 | In "Turbo" mode (no confirmations), a cache clean-up ran `rmdir /q` against the root of drive D:. Data unrecoverable ([TechRadar](https://www.techradar.com/ai-platforms-assistants/googles-antigravity-ai-deleted-a-developers-drive-and-then-apologized)) | Path error, no guard on destructive targets, no backup |
| OpenClaw, early 2026 | No authentication by default, tens of thousands of exposed instances, malicious skills (section 3.6) | Unsafe defaults; supply chain |

### 4.3 Human approval is weak by itself

- Anthropic reports that Claude Code users approve **93%** of permission
  prompts: "approval fatigue". Its auto-mode classifier blocks 0.4% of benign
  actions but **misses 17% of real overeager actions**, mostly when it decides
  an earlier user approval covers an action that it does not cover. The post
  says auto mode "is not a drop-in replacement for careful human review on
  high-stakes infrastructure". Sessions go back to a human after 3
  consecutive or 20 total denials
  ([Anthropic engineering](https://anthropic.com/engineering/claude-code-auto-mode)).
- Implication: ask rarely and with exact content. An approval should show
  what will run, what it touches, the reversibility class, and how to undo
  it. Low-risk changes that can be undone instantly should not prompt at
  all; they should be logged and undoable. Prompts are for the "undo by
  restore" class and above (§30).

### 4.4 Prompt injection and the Rule of Two

Meta's **Agents Rule of Two** (October 2025) builds on Simon Willison's
"lethal trifecta". Until prompt injection can be detected reliably, a session
should combine at most two of these three: (a) untrusted input, (b) access to
sensitive systems or private data, (c) the ability to change state or
communicate externally. If a task needs all three, a human or another reliable
check must supervise it
([Willison](https://simonwillison.net/2025/Nov/2/new-prompt-injection-papers/)).
For a server cockpit, untrusted input includes logs, web pages, release notes,
app READMEs and community-store manifests. An agent that reads release notes
(a) as root (b) with apply rights (c) breaks the rule. In Seraphina the
researcher reads; the executor, which reads no untrusted text, applies; and a
human approves anything above "undo instantly". OWASP's **Top 10 for Agentic
Applications 2026** (published December 2025) lists risks including tool
misuse, identity, supply chain, code execution, memory, cascading failures and
human-agent trust
([OWASP](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026);
exact category names **UNVERIFIED**).

### 4.5 Long sessions lose track (evidence for §31.5)

- **"LLMs Get Lost in Multi-Turn Conversation"** (Laban et al., Microsoft
  and Salesforce, ICLR 2026): every model tested did worse when the same task
  was spread over many turns, an average **39% drop**, mostly from
  unreliability rather than lost skill. Lower temperature and having the agent
  repeat instructions did not help much. Giving all the information up front
  did ([arXiv](https://arxiv.org/abs/2505.06120),
  [The Decoder](https://the-decoder.com/ai-chatbots-become-dramatically-less-reliable-in-longer-conversationsnew-study-finds/)).
- **Context rot** (Chroma, July 2025): across 18 models, reliability falls as
  input grows, even on simple retrieval, and content that is similar but wrong
  hurts most ([Chroma](https://research.trychroma.com/context-rot); details via
  secondary write-ups).
- This is the mechanism behind the owner's failure mode in §31.5: an agent
  says it will check five things, the context grows and gets compacted, and
  three are never done. Even 262,144-token windows do not fix it; larger
  windows make rot more likely. The design answer is structural:
  commitments live in a ledger outside every agent's context, each step runs
  in a fresh, short context that is given everything it needs up front, and
  closure is checked by code.

### 4.6 Patterns that recur across tools and research

1. **Read-only by default.** Red Hat linux-mcp-server, HolmesGPT, Netdata
   MCP, Azure ReadOnly mode.
2. **Separate "may it ask" from "may it reach".** Azure run modes versus
   permissions; Portainer per-user keys; Umbrel per-agent tokens.
3. **An explicit, reviewable change object.** k8sgpt Mutation, §27 change
   bundles, Terraform-style plans.
4. **Dry run or try-with-auto-revert.** Examples: `apt-get -s`,
   `docker compose config`, `netplan try` (reverts unless confirmed), TrueNAS
   boot environments, and STRATUS TNR.
5. **Backup first, abort if the backup fails.** YunoHost, Cloudron,
   homelab-ai.
6. **Postconditions checked by code.** The Gemini CLI and Replit incidents
   show why the agent's own report cannot be the check.
7. **Escalate after repeated denials or failures.** Claude Code auto mode
   (3 consecutive or 20 total); homelab-ai's failure memory.
8. **Mask before sending.** Synology AI Console, k8sgpt `--anonymize`,
   Portainer MCP env redaction.
9. **Runbooks constrain the model.** STCLab with HolmesGPT.
10. **Isolate tools and scope secrets.** Docker MCP Gateway.

---

## 5. Ten UX patterns Seraphina should copy

1. **Honest catalog cards before install** (YunoHost quality levels; Umbrel
   2.0 compatibility checks and release history). Every app, model and change
   card shows: does it fit this machine (VRAM, RAM, disk, ports, GPU), how
   well maintained it is, the installed version against upstream, the release
   history, and its reversibility class. Low-quality items are hidden by
   default, and installing one needs an explicit, recorded override.
2. **Preflight, then backup, then change, and stop if the backup fails**
   (Proxmox `pve8to9`, YunoHost `ynh_backup_before_upgrade`, Cloudron). The
   executor runs a checker (disk space, dependencies, breaking changes for
   this setup), takes the before-blueprint and the backup, and refuses to go
   on if either fails. This is §30's "Undo by restore" precondition, made
   visible.
3. **Low-downtime updates that restore themselves** (Runtipi 4.10.2; Cosmos
   restoring the old container after a failed edit). Pull the new image
   first, keep the old container or digest, swap, run health checks, and on
   failure put back files, environment, network and container automatically.
   If even that fails, keep a protected recovery snapshot. This is §31.2's
   "one Restore click", done for the user.
4. **Boot-level undo for the dangerous class** (TrueNAS boot environments).
   For kernel, NVIDIA driver, CUDA and netplan changes, keep the previous
   boot entry or environment and fall back automatically when the health
   check after boot fails (§30, "Undo by reboot fallback").
5. **Time travel with file-level restore** (Umbrel Rewind, Synology
   snapshots, PBS file restore). "Get me back" opens a timeline. Pick a time,
   see what changed (blueprint diff), then restore one file, one service's
   config, or one app's data.
6. **A diagnosis page with plain checks and a tracked "ignore"** (YunoHost
   Diagnosis, Unraid Fix Common Problems). Systems Audit runs fixed checks:
   exposure beyond loopback or tailnet, backup freshness, restore-drill age,
   failed units, SMART, certificate expiry, drift. Findings that the user
   ignores stay on the Housekeeping Tracker with a reason and a review date;
   they never just disappear.
7. **Limited by default, explicit elevation, approvals per session** (the
   Cockpit administrative-access toggle; Gordon's per-action or per-session
   approval that resets each session; Azure's Review, Autonomous and ReadOnly
   modes). The global maintenance-mode toggle and per-service overrides
   (§31.3) should use this vocabulary, and expert mode should switch itself
   back off.
8. **Plain files stay the source of truth** (Dockge "won't kidnap your
   compose files"; Cockpit acting through system tools; Cosmos Compose
   import). Everything Seraphina changes is an ordinary compose file, unit
   file or config file, versioned in the blueprint git, so an expert can see
   the diff and use the terminal without breaking the cockpit.
9. **One scoped, revocable token per agent, with a tool allowlist and a call
   log** (the Umbrel 2.0 MCP server, Docker MCP Gateway profiles and logs,
   Portainer per-user keys). Each factory, each agent, and each outside
   client (DSH, Qwen Code, Claude Code) gets its own token. The owner can see
   every call it made and revoke it in one click.
10. **Notify by default and mask before sending** (Umbrel: OS updates "never
    installed automatically"; TrueNAS update profiles; DIUN rather than
    Watchtower; Synology AI Console masking; k8sgpt `--anonymize`). Updates
    are announced with a risk profile and applied only in the window mode the
    user picked. Anything sent to an API model has secrets, hostnames and
    personal data masked locally first, and the card says which model saw
    what.

## 6. Ten pitfalls to avoid

1. **A catalog nobody keeps current.** Umbrel users found official apps far
   behind upstream, including a missing security fix, and CasaOS core
   releases stalled while its store went on. Seraphina must show installed,
   catalog and upstream versions side by side and turn "upstream security fix
   not yet in catalog" into a finding.
2. **Backups and snapshots that silently miss data, or are never
   restore-tested.** TrueNAS app-update snapshots reportedly skip host-path
   data. TrueNAS boot environments do not cover pools or apps. The TrueNAS
   UI cannot back up ix-apps at all. A backup plan must list exactly what a
   service touches (volumes, bind mounts, databases, model files, unit files,
   secrets), show what is *not* covered, and pass a scheduled restore drill
   (§30).
3. **Unattended updates of moving tags, and breaking changes nobody read.**
   Watchtower was archived in December 2025; auto-pulling `:latest` can jump
   major versions and break databases
   ([Linuxiac](https://linuxiac.com/docker-update-tool-watchtower-reaches-end-of-maintenance/),
   [selfhosting.sh](https://selfhosting.sh/compare/watchtower-vs-diun/)).
   Platforms themselves remove features (TrueNAS 26 drops its REST API; 25.10
   dropped the CA and the SMART UI). Pin by digest, and before any upgrade
   show a "what this changes for *your* setup" card built from release notes
   and the blueprint.
4. **The admin plane or the agent reachable beyond the tailnet, with weak
   defaults** (OpenClaw). Seraphina binds to loopback or the tailnet only,
   refuses to start on a public interface without authentication, and audits
   every listening socket (§31.4).
5. **Unvetted extensions, skills and installers running with full
   privileges** (malicious OpenClaw skills; no verification badge for Umbrel
   community stores; `curl | sudo bash` installers for CasaOS and the Proxmox
   helper scripts). Pin sources by hash, show the code or manifest diff, run
   extensions in containers without the Docker socket, and treat a
   community-store manifest as untrusted input under the Rule of Two.
6. **Approval prompts that get rubber-stamped.** 93% of prompts are
   approved, and a strong classifier still misses 17% of overeager actions.
   Keep prompts rare and exact (commands, targets, undo path, reversibility
   badge). Never prompt for "undo instantly" actions; log them instead.
   "Can't be undone" always needs typed confirmation and a verified backup
   (§30).
7. **Trusting the agent's own account of what happened.** Gemini CLI kept
   going after a failed `mkdir`; Replit's agent invented results and said
   rollback was impossible when it was not. The executor checks the exit
   status and postconditions of every step in code, and only health checks,
   never an agent's prose, decide "keep or roll back".
8. **All-or-nothing privilege.** With Cockpit, partial privilege is in
   practice full root. Portainer MCP wants an admin token. Gordon has shell
   and filesystem access. Seraphina's agents hold no root. A small executor
   accepts only signed, allow-listed bundles and uses polkit or sudoers rules
   per action (§27).
9. **Two control planes managing one service.** Cosmos warns against
   installing it through Unraid, CasaOS or Portainer templates
   ([README](https://github.com/azukaar/Cosmos-Server)). Dockge and Portainer
   only coexist because Dockge stays out of networks. An auto-updater running
   next to a maintenance window will undo the window's work. Every container,
   unit and config file in the blueprint gets one declared owner (Cosmos,
   Seraphina, or manual), and Seraphina changes non-owned items only through
   their owner's API.
10. **Commitments that live inside a long agent session.** There is a 39%
    multi-turn degradation, context rot, and the owner's §31.5 history.
    Commitments go into the Housekeeping ledger the moment they are made. Each
    agent step gets a fresh, short context with everything it needs up front.
    Closure is checked by code. Shared facts (instruction sets, model profiles,
    connections) have one source of truth and drift checks against every
    consumer.

---

## 7. What an AI-first cockpit adds for a novice

Every platform above shows status. None of them, as of October 2026, connect
cause and effect across services, propose a reversible fix in plain language,
prove the fix worked, and remember what is still open. Synology has announced
an advisor that will do part of this (DSM Agent 2.0); Umbrel lets outside
agents in. The journeys below are what Seraphina can do on matrix that a
dashboard cannot. In each one the agent proposes and explains, deterministic
code applies and verifies, and the ledger remembers.

### Journey 1: "My photos app is slow"

1. On the Photos card (say, Immich) the user clicks **Something's wrong →
   It's slow**.
2. The inspector, which is read-only, collects facts: container CPU, memory
   and I/O wait; the queue of Immich's machine-learning jobs; GPU memory per
   process from `nvidia-smi`; disk latency; and what changed recently
   (blueprint diff: an Immich update two days ago started re-indexing the
   whole library).
3. The diagnosis, in plain words: "Photos is re-indexing 80,000 pictures
   after Tuesday's update. Both GPUs are full with Flash-Next and the
   llama.cpp server, so face recognition is running on the CPU, using 14 of
   16 cores. That is also why chat in Open WebUI feels slower." (Example
   output. The numbers are illustrative.)
4. Three option cards, each with a badge:
   - "Run indexing only in tonight's maintenance window." *Undo instantly.*
   - "Let Photos use 3 GB of GPU memory until indexing finishes, by shrinking
     the llama.cpp server's context from 262,144 to 131,072 tokens. Restored
     automatically when indexing finishes." *Undo instantly.* This also shows
     who loses what.
   - "Do nothing. Indexing finishes in about 9 hours." (Estimated from the
     queue rate.)
5. The user picks one. The executor applies it, and the health check
   confirms the speed recovered. The temporary change is tracked in the
   ledger until it is reverted.

**What is new:** the cross-service explanation (GPU contention between a
photo app and a model server) and the resource-broker trade-off. No surveyed
platform links those two things.

### Journey 2: "Add a model that fits my GPUs"

1. **I'd like to… → Run a new model.** The wizard already knows the hardware
   (2x24 GB, NVLink, about 30-40 GB free RAM), what is loaded now (TabbyAPI
   with Flash-Next, the llama.cpp servers) and the owner's default context of
   262,144 tokens.
2. The user names a model or a goal ("a better coding model"). The planner
   reads the model's `config.json` (layers, KV heads, head size, MoE or
   dense, attention type) and calculates the fit for each format, not from a
   rule of thumb. Worked example for a 32B dense model shaped like Qwen2.5-32B
   (64 layers, 8 KV heads, head size 128; check per model):
   - KV cache per token at FP16 = 2 x 64 x 8 x 128 x 2 bytes = 256 KiB. At
     262,144 tokens that is **64 GiB**, which does not fit in 48 GB however
     the weights are quantised.
   - With an 8-bit cache, 32 GiB. With a 4-bit cache, about 16 GiB.
   - Weights at about 4.5 bits per weight are about 18 GB. So EXL3 at 4.5 bpw
     with a 4-bit cache comes to about 34 GB plus overhead, which fits across
     the two cards. At FP16 cache it fits only at about 100K context.
   - Models with hybrid or linear attention layers have much smaller caches,
     which is why the planner must read the config rather than assume.
     (Generic calculators exist for GGUF, for example
     [gguf-llm-vram-calculator](https://github.com/dlsniper/gguf-llm-vram-calculator);
     I found none for EXL3.)
3. The cards show what fits, what the user gives up (context, speed,
   co-resident models), and whether the Model Factory must make a quant. If
   so, it plans around the 30-40 GB RAM limit, shows the disk needed and the
   time estimate, and runs in the maintenance window.
4. Before deployment the Model Factory evaluates the quant: perplexity and
   KL-divergence against the original, tokens per second at the real context,
   and VRAM measured rather than estimated.
5. Deploying writes **one model profile** (the single source of truth,
   §31.5) and propagates it to TabbyAPI or llama.cpp, Open WebUI connections,
   DeepSeek Harness and Qwen Code. Drift checks confirm each consumer matches.
   The before-blueprint makes the old profile one click away.

**What is new:** a fit calculation for this machine with honest trade-offs,
quantising on demand, measured evaluation, and the profile propagated to every
client. Umbrel and ZimaOS stop at "one-click Ollama".

### Journey 3: "Something broke after last night's update"

1. The home card is red: "Open WebUI cannot reach TabbyAPI since 03:12."
   The user clicks **Get me back**.
2. The timeline shows last night's window: Open WebUI went from version X to
   Y, two apt packages were upgraded, and one image digest changed. Health
   checks started failing right after the Open WebUI step. That is the
   suspect.
3. **Restore first, diagnose second** (§31.2). The executor copies the
   broken state (container, database and config) to a scratch area, then
   restores Open WebUI's pre-update capture. Chat works again within minutes.
4. On the scratch copy, the diagnosis agent reads the release notes and the
   database migration log. Open WebUI runs migrations automatically at
   startup ([docs](https://docs.openwebui.com/ecosystem/computer/install/updating.md);
   troubleshooting covers failed migrations,
   [docs](https://docs.openwebui.com/troubleshooting/manual-database-migration.md)).
   It compares the connection settings before and after. Example finding: the
   connection to TabbyAPI was stored under a new key and the API key was not
   carried over.
5. It proposes a re-fix (set the connection in the new format, taken from the
   single source of truth), tests it against the scratch copy, and puts "retry
   Open WebUI upgrade with fix" in the next window. The ledger item stays open
   until the retried upgrade passes its health checks.
6. The report says what failed, why, what was restored, what will be retried
   and when.

**What is new:** cause found by the timing of health checks against the
blueprint timeline, restore before diagnosis, a fix tested on a copy, and an
open item that cannot be forgotten.

### Journey 4: "My assistants disagree with each other"

The drift detector finds that the "Cyber analyst" instruction set differs
between Open WebUI, TabbyAPI's prompt template and DeepSeek Harness. One copy
is malformed: an unclosed section that makes the model ignore the rules after
it. Seraphina shows a three-way diff in plain words, proposes one merged
version as the source of truth, and propagates it, with each consumer's old
copy kept for undo. From then on, any edit made directly in one consumer
raises a drift finding. This is the §31.5 failure turned into a routine check.

### Journey 5: "Is anything reachable from outside?"

Systems Audit lists every listening socket. Example finding: "A llama.cpp
server listens on 0.0.0.0:8080, which is your LAN, not just Tailscale." The
fix binds it to 127.0.0.1 or the tailnet address. It is *undo instantly*, and
the check is re-run (§31.4).

### Journey 6: "Back up my photos properly"

The wizard explains 3-2-1 in two sentences, finds where the photos and the
photo database live, shows the size and how long a first backup will take, and
sets up restic (Cosmos's backup engine, driven through its API) to the local
drive and an offsite target. The first restore drill is scheduled. The card
shows "last verified restore", not just "last backup".

### Journey 7: "What did the AI promise and not do?"

The Housekeeping Tracker lists open commitments with who opened them, why,
their evidence, and a due date. Examples: "Check DSH uses the new model
profile (opened by planner, 2 days ago)", "Re-run restore drill after storage
change". The user can ask "close everything that's safe" and watch each item
verified by code, or reassign items.

### Where the AI must not be

- It must not be a chat window bolted onto a dashboard. Every answer should
  end in a card that can be acted on, undone and verified.
- It must not be an autonomous root shell. The agent proposes; code applies,
  checks and rolls back.
- It must not be the judge of its own success. Health checks and TNR decide.

---

## 8. Open questions

1. **How Seraphina and Cosmos divide the work.** Drive Cosmos through its
   0.22 API for ServApps, routes and restic backups, or manage Compose files
   directly and treat Cosmos as the proxy and auth layer only? Who owns
   updates, so that Cosmos's updater and Seraphina's windows never both act on
   one container?
2. **Licence.** Is integrating with Cosmos's API, or shipping recipes for it,
   affected by the Commons Clause if Seraphina is ever distributed or sold?
   **UNVERIFIED**; needs a licence check.
3. **Reverse proxy and certificates on a tailnet-only machine.** Keep Cosmos's
   proxy behind Tailscale (Tailscale certificates or a DNS challenge) or use
   `tailscale serve`? This decides how "service URLs" work for a novice.
4. **matrix's filesystem.** On ext4 there are no snapshots or boot
   environments, so "Undo by restore" depends on restic and "Undo by reboot
   fallback" on GRUB entries. Is a move to ZFS or btrfs in scope?
5. **Which model diagnoses?** SREGym shows sharp drops on new failure types.
   Should Flash-Next (local, about 100 tok/s) do diagnosis with runbooks while
   an API model reviews driver, kernel and hardware findings? This needs a
   small benchmark on matrix's own past incidents.
6. **Should Seraphina expose its own MCP server** (like Umbrel 2.0) so DSH,
   Qwen Code or Claude Code can operate the server with scoped tokens? If so,
   which tools are read-only, and how is the Rule of Two enforced for clients
   that also browse the web?
7. **Metrics source.** Use Netdata's local anomaly detection and MCP server,
   Cosmos's metrics, or a minimal collector of Seraphina's own? Netdata's AI
   features are cloud and credit-based; its detection is local and free.
8. **Model files in backups.** The owner's own quants cannot be downloaded
   again, so they must be backed up. Base models can be. At hundreds of
   gigabytes, what is the retention and offsite policy, and how often can a
   restore drill realistically run?
9. **Approval budget.** How many approvals a week will the owner tolerate
   before approving without reading? This should set what "automatic" mode is
   allowed to do.
10. **What DSM Agent and Umbrel's MCP access levels actually do** once
    documented. Both are the nearest competitors and worth re-checking in
    Q4 2026.

---

## Sources (all cited inline above)

Cosmos: [README](https://github.com/azukaar/Cosmos-Server) ·
[changelog](https://github.com/azukaar/Cosmos-Server/blob/master/changelog.md) ·
[releases](https://github.com/azukaar/Cosmos-Server/releases) ·
[selfhostyourself](https://selfhostyourself.com/services/cosmos).
Platforms: [It's FOSS ZimaOS 1.5](https://news.itsfoss.com/zimaos-1-5-release) ·
[ZimaOS 1.6.0](https://www.zimaspace.com/docs/zimaos/v-1-6-0) ·
[Umbrel releases](https://github.com/getumbrel/umbrel/releases) ·
[TrueNAS 25.10 notes](https://www.truenas.com/docs/scale/25.10/gettingstarted/scalereleasenotes/) ·
[TrueNAS boot environments](https://www.truenas.com/docs/scale/25.10/scaletutorials/systemsettings/managebootenvironscale/) ·
[TrueNAS 26 BETA.1](https://update-master.freenas.org/scale/TrueNAS-26-BETA/TrueNAS-26.0.0-BETA.1.release-notes.txt) ·
[Unraid 7.2](https://unraid.net/blog/unraid-7-2-0) ·
[Unraid 7.3](https://unraid.net/blog/unraid-7-3-0) ·
[Synology DSM 7.3](https://www.synology.com/en-global/company/news/article/dsm73) ·
[Synology DSM 7.4](https://www.synology.com/en-global/company/news/article/dsm74) ·
[YunoHost backup-before-upgrade](https://git.yunohost.org/YunoHost/yunohost/commit/bf45cd7c4b1ff575ef679fdcc44ee77170fd6cb1) ·
[Runtipi releases](https://github.com/runtipi/runtipi/releases) ·
[Cockpit privileges](https://cockpit-project.org/guide/latest/privileges) ·
[portainer-mcp](https://github.com/portainer/portainer-mcp) ·
[Dockge](https://github.com/louislam/dockge) ·
[Proxmox VE 9.2](https://www.datazone.de/en/news/proxmox-ve-9-2-release/) ·
[PBS GUI](https://pbs.proxmox.com/docs-2/gui.html) ·
[Cloudron updates](https://docs.cloudron.io/updates).
AI ops: [k8sgpt privacy](https://docs.k8sgpt.ai/reference/guidelines/privacy/) ·
[k8sgpt auto-remediation](https://k8sgpt.ai/auto-remediation) ·
[HolmesGPT](https://github.com/robusta-dev/holmesgpt) ·
[STCLab case](https://www.cncf.io/blog/2026/04/21/auto-diagnosing-kubernetes-alerts-with-holmesgpt-and-cncf-tools/) ·
[Netdata AI](https://learn.netdata.cloud/docs/netdata-ai) ·
[Netdata MCP](https://learn.netdata.cloud/docs/netdata-ai/mcp) ·
[Docker MCP Gateway](https://github.com/docker/mcp-gateway) ·
[Gordon](https://docs.docker.com/ai/gordon.md) ·
[linux-mcp-server](https://github.com/rhel-lightspeed/linux-mcp-server) ·
[openSUSE systemd-mcp](https://pkg.go.dev/github.com/openSUSE/systemd-mcp) ·
[homelab-ai](https://glama.ai/mcp/servers/soo7zy036e) ·
[Azure SRE Agent run modes](https://learn.microsoft.com/azure/sre-agent/run-modes).
Research and incidents: [STRATUS](https://arxiv.org/abs/2506.02009) ·
[SREGym](https://arxiv.org/abs/2605.07161) ·
[AIOpsLab](https://arxiv.org/abs/2501.06706) ·
[OS-Harm](https://arxiv.org/abs/2506.14866) ·
[Lost in multi-turn](https://arxiv.org/abs/2505.06120) ·
[Context rot](https://research.trychroma.com/context-rot) ·
[Claude Code auto mode](https://anthropic.com/engineering/claude-code-auto-mode) ·
[Rule of Two](https://simonwillison.net/2025/Nov/2/new-prompt-injection-papers/) ·
[OWASP Agentic Top 10](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026) ·
[Replit](https://oecd.ai/en/incidents/2025-07-19-1eb1) ·
[Gemini CLI](https://incidentdatabase.ai/cite/1178) ·
[Antigravity](https://www.techradar.com/ai-platforms-assistants/googles-antigravity-ai-deleted-a-developers-drive-and-then-apologized) ·
[OpenClaw](https://blog.rankiteo.com/ope1770630718-openclaw-vulnerability-february-2026/) ·
[Watchtower end of maintenance](https://linuxiac.com/docker-update-tool-watchtower-reaches-end-of-maintenance/).
