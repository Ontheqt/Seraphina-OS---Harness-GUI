# Cosmos Cloud as Seraphina's reference platform (research notes)

Written 2026-10-21 for DESIGN.md §31.7: "Reference model: Cosmos Cloud ... take how it
works ... and integrate real AI functionality for the novice on top of it."

**Method.** I read the Cosmos-Server source at the current stable tag **v0.23.4**
(commit `69c5303`, 2026-08-30) and compared it with the `unstable` branch
(**v0.24.0-unstable029**, 2026-10-09). I also read the official app market repo
(`azukaar/cosmos-servapps-official`, last commit 2026-10-13). The research
environment could not reach cosmos-cloud.io, its docs, or most third-party review
sites (DNS failures and proxy refusals). Wherever these notes depend on those
pages, the claim is marked **UNVERIFIED**. Every other statement comes from source
code, so it describes what the software *does*, which may differ from what the
docs say.

Short links used below:
- `SRC/` = https://github.com/azukaar/Cosmos-Server/blob/v0.23.4/
- `MKT/` = https://github.com/azukaar/cosmos-servapps-official/blob/master/

---

## 0. The answer in one screen

- Cosmos already handles the "plumbing" a novice needs: reverse proxy with
  automatic HTTPS, login with 2FA in front of every app, an app store with install
  forms, container management, disks with SnapRAID/mergerFS, monitoring with
  alerts, cron, and (paid) Restic backups and a VPN. It has **no AI features at all**.
- It has a **complete, token-secured REST API** (169 operations, an OpenAPI spec in
  the repo, JS and Go SDKs, a Terraform provider). Another program can read and
  change routes, containers, config, backups, cron, alerts, users and storage.
  Events are **poll-only**: alert actions "webhook" and "script" exist in the UI
  but are empty stubs in the code.
- Its weak spots match the owner's failure list in §31.5. Updates happen with no
  backup first, no health check after, and no rollback once the new image starts.
  The update path deletes old images. There are no restore drills. Container
  definitions are overwritten with no history. Nothing records cross-service
  facts (which app talks to which model server, with which profile).
- **Recommendation: both.** Seraphina defines a pluggable *platform backend*. The
  first backend is a **Cosmos adapter**, used on the owner's server and anywhere
  Cosmos is already installed. A **native backend** (plain Docker Engine API +
  restic + smartctl + Tailscale) follows for novices who don't have Cosmos. On a
  Cosmos machine, Cosmos stays the only writer for routes, TLS, auth and container
  lifecycle. Seraphina adds the safety layer (before/after Blueprint, snapshot,
  health check, rollback), the AI planning and review line, the housekeeping
  tracker, GPU/model awareness and restore drills. Seraphina does not fork Cosmos
  or bundle it: the licence forbids selling it, and an anti-tampering clause covers
  its paid features (§12).

---

## 1. Release status (current as of 2026-10-21)

| Line | Version | Date | Notes |
| ---- | ------- | ---- | ----- |
| Stable (`master`) | v0.23.4 | 2026-08-30 | `SRC/changelog.md`. The 0.23.0 release replaced MongoDB/"Lungo" with SQLite (or Postgres) and moved multi-node sync to an op-log over NATS. |
| Beta (`unstable`) | v0.24.0-unstable029 | 2026-10-09 | https://github.com/azukaar/Cosmos-Server/blob/unstable/changelog.md. Adds lazy loading (start containers on first request), better TCP proxying, editing of stopped containers, a SmartShield ban dashboard and whitelisting. The diff also adds DNS "zones", DNS record reconciliation and more Pro stubs. |

- GitHub's releases page lists only the 0.24 betas, all marked pre-release
  (https://github.com/azukaar/Cosmos-Server/releases). The stable tags exist in git
  (`git ls-remote --tags`).
- The project ships very often: 568 releases, about one every 2 days
  (https://releasealert.dev/github/azukaar/Cosmos-Server). In practice there is one
  main maintainer (the licensor is Yann Stepienik, `SRC/LICENCE`). An adapter has
  to expect API drift and pin a version (§17).

## 2. Architecture

- **One Go binary** (Go 1.26 per changelog 0.23.03) serving a React/MUI web UI at
  `/cosmos-ui/` and a JSON API at `/cosmos/api/...` (`SRC/src/httpServer.go`). It
  bundles or uses: `go-acme/lego` v5 (certificates), `ory/fosite` (OAuth2/OIDC
  provider), `pquerna/otp` (TOTP), `rclone` (remote storage), the `nats-server`
  library (multi-node sync), and separate `restic` and `nebula` binaries
  (`SRC/go.mod`, `SRC/build.sh`). The Docker image is Debian 12 with `mergerfs`,
  `snapraid`, `samba`, `fuse3` and `avahi` (`SRC/dockerfile`).
- **Docker socket.** Cosmos needs `/var/run/docker.sock`. It talks to the Docker
  Engine API directly; it does **not** call `docker compose`. Any API caller that
  holds the "resources" permission can therefore create privileged containers,
  which is root on the host in practice.
- **Two install modes, with different features.** The code checks
  `IsInsideContainer` and changes what it does:

  | Feature | Docker install (`docker run ... azukaar/cosmos-server`) | Native install (binary + `cosmos service install`, systemd via kardianos/service) |
  | ------- | --------------- | -------------- |
  | Restic backups | **disabled** ("Currently running in a docker container, disabling backups", `SRC/src/backups/index.go`) | available with a licence |
  | Cosmos self-update | treated as a container update: with `AutoUpdate` on, the daily image check recreates the Cosmos container through a temporary `cosmos-self-updater-agent` container (`HasAutoUpdateOn`, `SelfRecreate` in `SRC/src/docker/docker.go`) | daily: downloads a new binary zip from `api.cosmos-cloud.io` and restarts (`SRC/src/CRON.go`) |
  | Cosmos Pro edition | refuses to start ("not supported inside a container", `SRC/src/index.go`) | supported |
  | Host folders, metrics | via `-v /:/mnt/host` | direct |

  The documented Docker command is
  `docker run -d --network host --privileged --name cosmos-server -h cosmos-server --restart=always -v /var/run/docker.sock:/var/run/docker.sock -v /:/mnt/host -v /var/lib/cosmos:/config azukaar/cosmos-server:latest`
  (https://hub.docker.com/r/azukaar/cosmos-server). `--privileged` (or `NET_ADMIN`)
  is needed for Constellation.
- **Where things live.** Config folder: `/var/lib/cosmos/` natively, `/config/`
  inside the container (bind-mounted from `/var/lib/cosmos`), or overridden by
  `COSMOS_CONFIG_FOLDER` / `CONFIG_FILE` (`SRC/src/utils/utils.go`, `SRC/src/index.go`).
  - `cosmos.config.json`: the whole configuration (routes, users' roles, backups,
    alerts, cron, storage, API token hashes, licence). One JSON file.
  - `auth.db` (SQLite): users (bcrypt hashes, TOTP keys), Constellation devices,
    a kv table (`SRC/src/utils/store_schema.go`).
  - `database.db` (SQLite, or Postgres if `Database.PostgresHost` is set):
    metrics, events, notifications (`SRC/src/utils/database_schema.go`). Events and
    notifications are deleted after 30 days (`RunDatabaseRetention`).
  - `backup.cosmos-compose.json` and `cosmos.docker-compose.yaml`: an export of
    every container, rewritten a few seconds after each Docker event. It contains
    the containers' environment variables (secrets); mode 0600
    (`SRC/src/docker/export.go`). There is no history: each export overwrites the last.
  - `cosmos-backup.zip`: a zip of the config folder, written every 2 hours
    (`SRC/src/backup.go`, `SRC/src/CRON.go`).
  - `backup-passwords.log`: restic repository passwords, kept apart from the config.
  - `rclone.conf`.
  - App data defaults to `/cosmos-storage` (`DockerConfig.DefaultDataPath`).
- **Listening.** HTTP and HTTPS bind `0.0.0.0` (hardcoded, `SRC/src/httpServer.go`
  lines 42/128/190). Constellation uses UDP 4242. Cosmos cannot be told to listen
  only on the tailnet interface; that takes a host firewall or per-route IP
  allow-lists (§6).

## 3. ServApps and the app market

- **"ServApp"** is Cosmos's name for any container it manages, including ones
  created outside Cosmos. The UI groups containers into stacks by the label
  `cosmos-stack` / `cosmos.stack` or Docker Compose's `com.docker.compose.project`
  (`SRC/client/src/pages/servapps/servapps.jsx`). Converting an existing container
  into a reachable app only takes adding a URL.
- **Compose format ("cosmos-compose").** This is JSON close to Compose, with
  `services`, `volumes` and `networks`, plus Cosmos extras. Each service can
  carry a `routes` array (reverse-proxy entries created at install time) and
  `post_install`. Top-level keys are `cosmos-installer` (an install form) and
  `minVersion`. Standard `docker-compose.yml` is also accepted; the browser
  converts it. Service fields include `runtime` (e.g. `nvidia`, since 0.16.0),
  `devices`, `mem_limit` and `cpus`. The fields `deploy`, `shm_size`, `init`,
  `logging` and `group_add` are **not honoured** by the server
  (`MKT/CREATING_A_MARKET_TEMPLATE.md` §3.3; struct in
  `SRC/src/docker/api_blueprint.go`). That matters for GPU apps written the
  Compose way (`deploy.resources.reservations.devices`): they have to use
  `runtime: nvidia` instead.
- **Templating happens in the browser.** Templates use "whiskers": `{ServiceName}`,
  `{Context.<field>}`, `{Passwords.0..4}`, `{DefaultDataPath}`, `{RootHostname}`,
  `{if ...}`. The client renders them, then POSTs the finished JSON to
  `/cosmos/api/docker-service` (`SRC/client/src/pages/servapps/containers/docker-compose.jsx`).
  **Consequence for an adapter:** to install a market app through the API, the
  adapter renders the template itself and sends the final JSON.
- **Install transaction.** The server creates networks, volumes and containers,
  then adds the routes. It streams a log back. If a step fails, it runs a recorded
  rollback list (remove created containers/volumes, revert edited containers)
  (`Rollback()` in `SRC/src/docker/api_blueprint.go`). Note that Cosmos already
  uses the word **"blueprint"** for this code. Seraphina's Blueprint (§28) is a
  different thing, so the docs should say so to avoid confusion.
- **`cosmos-*` labels** the server reads:

  | Label | Effect |
  | ----- | ------ |
  | `cosmos-force-network-secured=true` | Cosmos puts the container on its own bridge network `cosmos-<name>-default`, keeps it off `bridge`, and proxies to it (`SRC/src/docker/network.go`). |
  | `cosmos-network-name` | The isolated network in use (`auto` creates one). |
  | `cosmos-auto-update=true` | Opts the container into the daily auto-update (§11). |
  | `cosmos-icon` | Dashboard icon URL. |
  | `cosmos-stack`, `cosmos-stack-main` | Stack grouping in the UI. |
  | `cosmos-force-network-mode`, `cosmos-persistent-env`, `cosmos-chown`, `cosmos-link*` | Network mode pinning, env kept across edits, chown of binds, container links. |

- **Market sources.** The built-in source is `cosmos-cloud` →
  `https://azukaar.github.io/cosmos-servapps-official/index.json`
  (`SRC/src/market/init.go`). Extra sources go in `MarketConfig.Sources[]{Name, Url}`.
  The server re-fetches each source every 12 hours (`SRC/src/market/update.go`).
  Each source is a JSON index (`showcase`, `all[]` with `name`, `description`,
  `compose` URL, `icon`, `screenshots`), built by `MKT/index.js`.
- **What the official market holds.** 151 apps as of 2026-10-13: Jellyfin, Immich,
  Nextcloud, Home Assistant, Vaultwarden, n8n, Paperless-ngx, SearXNG, Uptime Kuma
  and so on. It has **no LLM servers or chat front ends** (no Ollama, Open WebUI,
  llama.cpp, vLLM, TabbyAPI, ComfyUI), and **no template uses `runtime: nvidia`**
  (I grepped `servapps/*`). GPU use in the market is limited to `/dev/dri`
  passthrough for transcoding (Emby, Frigate, Handbrake, Unmanic).

## 4. URLs, reverse proxy, HTTPS, Smart Shield

- **Route types** (`ProxyModeList`, `SRC/src/utils/types.go`): `SERVAPP` (a
  container), `PROXY` (any URL, including other machines), `STATIC`, `SPA`,
  `REDIRECT`. The old docs also list `AUTH`, `HIDDEN`, `LOCAL`, `STREAM`, `BLOCK`
  as template modes. Routes match **in order; the first match wins**
  (https://github.com/azukaar/cosmos-docs/blob/master/src/content/docs/guides/urls.md,
  a docs repo last updated Jan 2024, so treat its UI details as possibly dated).
- **Per-route settings** (`ProxyRouteConfig`): host and/or path prefix (with
  strip), timeout, `ThrottlePerMinute`, CORS origin, max bandwidth, `AuthEnabled`
  (Cosmos login required; Cosmos then sends `x-cosmos-user` /
  `x-cosmos-user-role` headers upstream, `SRC/src/proxy/routerGen.go`),
  `AdminOnly`, `BlockCommonBots`, `BlockAPIAbuse`, `RestrictToConstellation`,
  `WhitelistInboundIPs` (exact IPs or CIDR, `SRC/src/utils/middleware.go`
  `CheckIPAccess`), extra headers, Host header override, accept insecure HTTPS
  target, h2c, and load balancing over `AdditionalTargets`
  (`first`/`round_robin`/`load_based`).
- **HTTPS modes**: `LETSENCRYPT`, `SELFSIGNED`, `PROVIDED`, `DISABLED`. Let's
  Encrypt supports HTTP-01, and DNS-01 through lego's DNS providers
  (`DNSChallengeProvider`, `DNSChallengeConfig`), including wildcard certificates
  (`UseWildcardCertificate`). Without forwarded ports, HTTP-01 cannot succeed,
  which leaves three options. **DNS-01** works when the domain's DNS has an API.
  `SELFSIGNED` works. `PROVIDED` works too, for example with a Tailscale
  certificate from `tailscale cert`, which uses Let's Encrypt DNS-01 on
  `*.ts.net` and expires after 90 days (https://tailscale.com/kb/1153/tailscale-cert).
  Cosmos does not renew a provided certificate itself, so an outside job (e.g.
  Seraphina) would have to renew and re-upload it. That last point comes from
  reading the code paths, not from a test: **UNVERIFIED**.
- **Smart Shield** (`SRC/src/proxy/shield.go`): adaptive per-user limits. When
  enabled, the defaults are a 2 h time budget per user, 18,000 requests, 200 GB,
  100 simultaneous requests per user, 2,000 global, strictness "normal", and
  admins exempt. Over-limit users are throttled first, then given strikes and
  temporary bans. Around it sit IP abuse counters, geo-blocking by country
  (`BlockedCountries`, black- or whitelist), bot blocking, a referer check, and
  TCP/UDP shields for non-HTTP routes. The internal API itself goes through Smart
  Shield and an extra **500 requests/minute per IP** limit (`SecureAPI` in
  `SRC/src/httpServer.go`).
- **Admin API lock-down options**: `AdminConstellationOnly`, `AdminWhitelistIPs`.
  Unless `AcceptAllInsecureHostname` is on, there is also a hostname check: API
  and UI requests must use a configured hostname, or a local IP if
  `AllowHTTPLocalIPAccess` is set (`EnsureHostname`, `SRC/src/utils/middleware.go`).
  An adapter has to call Cosmos by its configured hostname or send the right
  `Host` header.

## 5. Authentication and identity

- **Users** live in `auth.db`. Cosmos sends invite links and password-reset
  emails (SMTP config), and supports self-registration with a key. **Seats:** 5
  users without a licence (UI-enforced, `SRC/src/system.go`,
  `SRC/client/src/pages/config/users/usermanagement.jsx`), and "up to 19 users"
  with Premium (sales copy in `SRC/client/src/utils/locales/en/translation.json`).
- **Roles and permissions** (`SRC/src/utils/types.go`): GUEST/USER/ADMIN plus
  custom roles built from permissions: `ADMIN_READ`(1), `ADMIN`(2),
  `USERS_READ`(10), `USERS`(11), `RESOURCES_READ`(20), `RESOURCES`(21),
  `CONFIGURATION_READ`(30), `CONFIGURATION`(31), `CREDENTIALS_READ`(40),
  `LOGIN`(100, MFA required), `LOGIN_WEAK`(101).
- **2FA**: TOTP. `RequireMFA` forces it for everyone. 0.23.2 fixed "App Gate not
  checking 2FA consistently".
- **Sudo mode**: an admin's session acts as a plain user until they re-enter their
  password (`/api/sudo`; `effectiveRole = USER` unless sudoed, `SRC/src/httpServer.go`).
  This pattern suits a novice tool: daily browsing never carries admin rights.
- **OpenID Connect**: Cosmos is an **OIDC provider** (fosite) for apps
  (`/oauth2/...`, `/.well-known/openid-configuration`, PKCE since 0.22.20,
  `OpenIDClients` in config). I found no OIDC *client* code, so Cosmos cannot
  sign users in through an outside identity provider such as Google or Authentik
  (absence in code; **UNVERIFIED** against the docs). Issue #556 (closed
  2026-07-10) was Cosmos intercepting `/.well-known/openid-configuration` on
  proxied hosts that run their own identity provider (https://github.com/azukaar/Cosmos-Server/issues/556).

## 6. Constellation (VPN) and Tailscale

- **What it is**: Nebula (Slack's overlay mesh) managed by Cosmos, plus NATS for
  syncing config between Cosmos nodes. The default range is `192.168.201.0/24`
  (`SRC/src/constellation/api_nebula_connect.go`). It runs a DNS server on port 53
  with ad/tracker blocklists and custom entries (`SRC/src/constellation/DNS.go`).
  Devices join by scanning a config file or QR code. It can also run "tunnels",
  which publish a route on one node that is served by another, and since 0.23.1
  it accepts client-only servers.
- **Paid**: Constellation is a Premium feature (`InitPremiumFeatures` starts it
  only with a valid licence, `SRC/src/index.go`; sales page "Unlock Cosmos Premium
  and use Constellation").
- **It needs a public lighthouse.** The sales text says: "The lighthouses (the
  tunnel) is selfhosted, and requires you to install it on a server that is
  reachable from the internet". Under the owner's "no ports forwarded" rule
  (§31.4), the lighthouse would have to sit on an outside VPS. Tailscale avoids
  this with its coordination server and DERP relays.
- **Coexistence with Tailscale (analysis, not tested on matrix):**
  - The address ranges do not collide: Tailscale uses `100.64.0.0/10` and
    `fd7a:115c:a1e0::/48` (https://tailscale.com/kb/1610/reserved-ip-addresses),
    Constellation uses `192.168.201.0/24`. Two overlays on one host is normal.
    Tailscale's FAQ on other VPNs exists
    (https://tailscale.com/docs/reference/faq/other-vpns.md) but was not readable
    from here: **UNVERIFIED** details.
  - **DNS is the likely conflict.** If Constellation's DNS binds :53, it can clash
    with systemd-resolved's stub listener on Ubuntu and change name resolution for
    the host. **UNVERIFIED** on Ubuntu 24.04; check on matrix before enabling.
  - Recommendation for the owner: **keep Tailscale as the only remote-access
    path and leave Constellation off.** It saves the licence, the public
    lighthouse and the second DNS. Then make Cosmos tailnet-only in one of two
    ways. (a) Set every route's `WhitelistInboundIPs` to
    `["100.64.0.0/10","fd7a:115c:a1e0::/48"]` (plus `127.0.0.1/32` if local tools
    call through Cosmos), and set `AdminWhitelistIPs` the same way. Or (b) add a
    host firewall rule that accepts 80/443 only on `tailscale0` and `lo`. Because
    Cosmos binds `0.0.0.0`, without one of these it also answers on the LAN. That
    counts as a §31.4 audit finding.
  - Docker-published ports (`ports:` in templates) bypass UFW's INPUT rules,
    because Docker DNATs them in its own chains. Filtering belongs in
    `DOCKER-USER`, or the ports should be bound to `127.0.0.1`
    (https://docs.docker.com/network/packet-filtering-firewalls/). The Seraphina
    audit should check published ports, not just UFW status.

## 7. Storage

- Disk list with **SMART** read in-process (`anatol/smart.go`, SATA and NVMe), a
  SMART definitions endpoint, temperatures as metrics, and a default "Disk Health"
  alert at >50 °C (`SRC/src/storage/disks.go`, `SRC/src/utils/utils.go`).
- Format, mount and unmount disks. Pool disks with **mergerFS**. **SnapRAID**
  arrays (`Data` map, `Parity[]`, `SyncCrontab`, `ScrubCrontab`) with sync, scrub
  and fix actions (`SRC/src/storage/snapRAID.go`). There is mdadm RAID code, but
  its API routes are commented out in 0.23.4 (`SRC/src/httpServer.go` lines 797-803).
- A folder picker and directory create (`/api/list-dir`, `/api/new-dir`).
- **Remote storage and network shares (Premium)**: RClone mounts (Dropbox,
  Google Drive, S3, SFTP and more) that can attach to containers as binds, and
  SMB/NFS/FTP shares behind Smart Shield.
- No ZFS or btrfs management and no snapshots. The storage page does not cover
  §30's "whole-OS snapshot / boot environment" layer.

## 8. Backups

| Layer | Free? | What it covers | Notes |
| ----- | ----- | -------------- | ----- |
| `cosmos-backup.zip` | yes | the config folder (config JSON, auth.db, database.db, certs) | every 2 h, overwritten; optional `BackupOutputDir` |
| `backup.cosmos-compose.json` | yes | definitions of all containers (image, env, mounts, networks, labels, routes) | rewritten on Docker events; no history; includes secrets |
| Restic backups | **Premium licence + native install only** | one `Source` path per backup job, to a local path or RClone remote | `SRC/src/backups/` |

- **Engine**: the restic binary, so repositories are standard restic. The sales
  text says "you can always restore your backups without Cosmos as it is using
  Restic under the hood", which the code confirms (`restic backup --repo <repo> <source>`,
  `SRC/src/backups/restic.go`).
- **Per job**: `Repository`, `Password`, `Source`, `Crontab`, `CrontabForget`,
  `RetentionPolicy`, and `AutoStopContainers`, which stops the containers that
  mount the source path during backup and restore (`GetContainersUsingPath`,
  `SRC/src/docker/backups.go`). The config UI offers a "Cosmos Internal Backup" of
  the config folder (backup 04:00, forget 12:00).
- **Restore UX**: list snapshots, browse a snapshot's folders, see the size of a
  subfolder, then restore a whole snapshot or selected paths to a target folder.
  This runs as a job with live logs. Snapshots can be forgotten and repositories
  unlocked (`/api/backups/{name}/restore` etc.).
- **Missing**: database-consistent dumps (no pre/post hooks), scheduled restore
  tests, "restore this app" as one unit (container definition + image digest +
  volumes + routes together), and any check that a backup is fresh before an
  update.

## 9. Monitoring, alerts, notifications

- Metrics are sampled every 30 s (`SRC/src/metrics/index.go`) and stored at raw,
  hourly and daily granularity in SQLite/Postgres. They cover host CPU/RAM/disk/
  network/temperature, per-container CPU/RAM/network (`cosmos.system.docker.*`),
  per-route request counts, errors, time, bytes and blocks (`cosmos.proxy.route.*`),
  and RClone. **No GPU metrics**: no `nvidia`/GPU code in the community build.
- **Alerts** (`SRC/src/metrics/alerts.go`): metric (wildcards allowed) +
  `Period` (latest/daily) + condition (gt/lt, value or percent) + actions:
  `notification` (in-app bell), `email` (all admins), `stop`/`restart` (the
  container), or disable the route. **`webhook` and `script` are stubs that only
  write a debug log**, in 0.23.4 and in 0.24.0-unstable029.
- **Default alerts**: "Anti Crypto-Miner" stops any container whose daily CPU is
  over 80 %, and "Anti Memory Leak" stops any container over 80 % RAM. Both are
  **disabled by default**. If a novice turns them on, they would stop llama.cpp,
  TabbyAPI or a quantisation job. "Disk Health" and "Disk Full (>95 %)" are
  enabled and only notify.
- **Events log**: about 40 event types, including `cosmos.docker.event.<type>.<action>`,
  `cosmos.proxy.shield.*`, `cosmos.backup`, `cosmos.settings`,
  `cosmos.docker.container.update`, `cosmos.api.token.*` and
  `cosmos.metrics.alert`. They are searchable in the UI and through
  `/api/events`, and kept for 30 days.
- **Notifications**: in-app plus email. No push, ntfy, Discord or Telegram
  channel. (ntfy exists only as a market app.)

## 10. Cron and jobs

- Jobs are `{Name, Crontab, Command, Container, Enabled}` (`CRONConfig`). The
  command runs `sh -c` either where Cosmos runs (on the host for a native
  install, inside the Cosmos container otherwise) or via `docker exec` in a named
  container (`SRC/src/cron/index.go`). A job tracker reports running jobs with
  logs, and a websocket streams output. Backups, restores and SnapRAID runs use
  the same scheduler.
- Internal schedule: daily at 02:00 Cosmos runs DB retention, image cleanup,
  certificate check and container update check/auto-update. Licence renewal and
  the version check run at an hour derived from the hostname. The config zip runs
  every 2 h (`SRC/src/CRON.go`). These times are hardcoded; the code itself has a
  TODO for "customizable maintenance schedules".

## 11. Updates

- **Containers.** Every day at 02:00, Cosmos pulls the image tag of each
  *running* container. If the pull brings new layers and the container has
  `cosmos-auto-update=true`, Cosmos recreates it with the same config
  (`CheckUpdatesAvailable` → `RecreateContainer`, `SRC/src/docker/docker.go`).
  Manually, `/api/servapps/{id}/manage/update` does a pull and recreate.
  - Rollback happens **only if Docker create/start fails**. If the new version
    starts but is broken (a database migration, a changed API, lost connections
    — the Open WebUI case in §31.5), Cosmos keeps it.
  - There is no backup or snapshot before the update, and no health check after it.
  - Image prune runs with `dangling=false` after network cleanup unless
    `SkipPruneImages` is set (`SRC/src/docker/network.go`). The **previous image
    is usually deleted**, so going back means re-pulling an old digest, and only
    if someone recorded it.
- **Cosmos itself.** With `AutoUpdate` on (and `BetaUpdates` for betas), a
  native install downloads a new binary daily from `api.cosmos-cloud.io`. A
  Docker install goes through the container path above: Cosmos re-pulls its own
  image and recreates itself through a temporary `cosmos-self-updater-agent`
  container. `POST /api/force-server-update` runs the same check immediately.

## 12. Licence and paid tiers

- **Licence**: "Apache 2.0 with Commons Clause and Anti Tampering Clause"
  (`SRC/LICENCE`). The Commons Clause forbids "Sell"ing: providing to third
  parties for a fee "a product or service whose value derives, entirely or
  substantially, from the functionality of the Software". The Anti Tampering
  Clause ends the licence if anyone tries to "tamper with, alter, circumvent, or
  avoid compliance with any subscription, paywall, feature restriction". It
  explicitly includes "redistributing parts or all of a feature that was intended
  to be a paid feature" without its restrictions. Docker Hub calls it "Visible
  Source", not open source.
- **Community (free)**: proxy, HTTPS, Smart Shield, auth/2FA/OIDC, market,
  containers, storage (SMART, mergerFS, SnapRAID), monitoring/alerts, cron, config
  zip, 5 users, 1 node.
- **Premium (licence key)**: Constellation, RClone remote storage and network
  shares, Restic backups, up to 19 users. Prices hardcoded in the 0.23.4 UI
  (`SRC/client/src/utils/free.jsx`): US$10.90/month, US$99/year, US$249 lifetime
  (EUR 9.90/93/229; GBP 8.90/88/199). The prices on cosmos-cloud.io itself:
  **UNVERIFIED**.
- **Cosmos Pro**: a separate closed build (`pro.IsPro()` is `false` in the
  community source, and its handlers are stubs answering `PRO001`). It covers
  groups and multi-node deployments with a placement scheduler (tags such as
  `gpu`), and in 0.24 CI, SeaweedFS and managed databases. It cannot run inside
  a container. Pricing and availability: **UNVERIFIED** (cosmos-cloud.io
  unreachable).
- **Licence check**: Cosmos calls `api.cosmos-cloud.io` daily to renew the
  licence (`SRC/src/utils/cloud.go`). That is an outbound dependency, which the
  "no ports forwarded" rule does not block.

## 13. The API: can another program read and change Cosmos?

**Yes, nearly everything the UI can do.** Details:

- **Base**: `https://<cosmos-hostname>/cosmos/api/...`. The OpenAPI 3 spec for
  0.23.4 is in the repo (`SRC/api-docs/openapi.json`: 134 paths, 169
  operations). There are SDKs for JS/TS (`SRC/sdk/README.md`, npm package
  `cosmos-cloud-sdk`, publication **UNVERIFIED**) and Go (`SRC/go-sdk/`), and a
  **Terraform provider** (`SRC/terraform-provider-cosmos/README.md`, source
  `cosmos-cloud.io/azukaar/cosmos`). The provider has resources for routes,
  docker services, volumes, networks, API tokens, users, backups, alerts, cron
  jobs, OIDC clients, Constellation, SnapRAID and storage mounts.
- **Tokens** (`SRC/src/configapi/api_tokens.go`, `tokenMiddleware` in
  `SRC/src/httpServer.go`): `Authorization: Bearer cosmos_<43 chars>` (or
  `?token=` for websockets). Only the SHA-256 hash is stored, and the raw token
  is shown once. A token carries an explicit permission list (the §5 numbers;
  `readOnly: true` gives 1/10/20/30), an `ipWhitelist` (CIDR allowed),
  `restrictToConstellation`, and `expiryDays`. A token cannot grant permissions
  its creator lacks. **Tokens skip MFA and sudo**, so they must be treated as
  root-equivalent when they hold `RESOURCES` or `ADMIN`. Every use writes a
  `cosmos.api.token.used` event, so frequent polling fills the events table
  (30-day retention).
- **Endpoint groups** (all under `/cosmos/api`, from `SRC/src/httpServer.go`):

  | Area | Read | Change |
  | ---- | ---- | ------ |
  | Status | `GET /status` (resources, `Licence`, `containerized`, cert mode, `ConfigFolder`, `needsRestart`, `newVersionAvailable`) | `/restart`, `/restart-server` (host reboot), `/force-server-update` |
  | Config | `GET /config` (secrets masked as `***` without `CREDENTIALS_READ`) | `PUT /config` (whole object), `PATCH /config` (route ops: add/replace/delete/move_up/move_down), `/config/dns` |
  | Routes | `GET /routes`, `GET /routes/{name}` | `POST /routes`, `PUT /routes/{name}`, `DELETE /routes/{name}` |
  | Containers | `GET /servapps`, `GET /servapps/{id}/`, `/logs` (limit, search, errorOnly), `/check-update`, `/export` (definition as cosmos-compose), `GET /get-backup` (all containers; needs `CREDENTIALS_READ`) | `/servapps/{id}/manage/{stop,start,restart,kill,remove,pause,unpause,recreate,update}`, `PUT /servapps/{id}/update` (edit), `/auto-update/{true,false}`, `/secure/{true,false}`, `/network/...`, `POST /docker-service` (install; streamed log), `/images/pull`, `/volumes`, `/networks` |
  | Market | `GET /markets` | sources via config |
  | Backups | `/backups-config`, `/backups/{name}/snapshots`, `/backups/{name}/{snap}/folders`, `/backups-repository/{name}/stats` | `POST /backups`, `/backups/edit`, `DELETE /backups/{name}`, `POST /backups/{name}/restore` `{snapshotId,target,include[]}`, `/forget`, `/unlock` |
  | Storage | `/disks`, `/smart-def`, `/mounts`, `/snapraid` | `/disks/format`, `/mount`, `/unmount`, `/merge`, `/snapraid/{name}/{sync,scrub,fix...}` |
  | Monitoring | `GET /metrics?metrics=a,b`, `/list-metrics`, `GET /events?from&to&logLevel&search&query&page`, `GET /notifications?from` | `/alerts` CRUD, `/notifications/read`, `/reset-metrics` |
  | Jobs | `/cron`, `/jobs`, `/jobs/running`, `/jobs/get` | `/cron` CRUD, `/jobs/run`, `/jobs/stop`, `/jobs/delete` |
  | Users/identity | `/users`, `/me`, `/openid`, `/api-tokens` | CRUD on each, `/invite` |
  | Constellation | `/constellation/devices`, `/config`, `/logs`, `/nats-status`, `/tunnels` | create/connect/reset/block/edit-device, DNS entries |
  | Terminal | — | websocket host terminal and container terminal (admin) |

- **Events**: **poll only.** `/api/events` takes a cursor (`page` = last event id)
  and a JSON `query` filter. No outbound webhooks (the alert action is a stub),
  no server-sent events, and no websocket for events (websockets exist only for
  terminals and job logs).
- **Write semantics and safety:**
  - No ETag, version number or compare-and-swap. `PUT /config` replaces the
    whole object, so **the last writer wins**. Cosmos restores fields it never
    sends (TLS keys, auth keypair, API tokens, masked `***` secrets) before
    saving (`SRC/src/configapi/set.go`). It validates routes before saving.
  - Every `PUT /config` triggers `SoftRestartServer()`, which restarts the HTTP
    server, Constellation and RClone mounts. Route changes restart the HTTP
    server. Expect a short interruption of **all** proxied apps on each change.
  - **Editing `cosmos.config.json` by hand while Cosmos runs is unsafe.** The
    writer truncates the file and then encodes into it (`O_TRUNC`, not
    write-temp-and-rename, `SaveConfigTofile` in `SRC/src/utils/utils.go`). The
    lock is in-process only (`ConfigLock`), with no file lock. There is no file
    watcher; Cosmos reads the file at startup and on its own read-modify-write
    paths, so it can overwrite outside edits. Invalid JSON is a `Fatal` at next
    start. 0.22.35 fixed a "rare race condition breaking config file". **Rule:
    change config only through the API.** For a file-level restore: stop Cosmos,
    replace the file, start Cosmos.
  - The `/cosmos` API is rate-limited to 500 req/min per IP, sits behind Smart
    Shield, and has a 45 s handler timeout. Long operations (pull, install,
    update) stream their output.

## 14. What Cosmos does well for a novice (Seraphina must match)

1. **One door for every app.** A name per app (`photos.example`), automatic
   HTTPS, and login + 2FA in front of apps that have weak or no auth. The novice
   never sees port numbers.
2. **An install form instead of a compose file.** The market's install form asks
   plain questions (paths, passwords, hostname), generates passwords, isolates
   the app on its own network, and creates the URL in one step. If an install
   fails, its leftovers are rolled back.
3. **It doesn't lock you in.** It adopts containers made by `docker run`,
   Portainer or Compose, uses vanilla Docker objects, standard restic repositories
   and plain JSON config, and lets you leave at any time.
4. **Secure by default without tuning.** Smart Shield, bot blocking, per-app
   network isolation, admin-as-user until sudo, and hostname checks.
5. **The disk-to-alert basics.** SMART, temperatures, disk-full alerts, mergerFS
   and SnapRAID without the command line, and history graphs per container and
   per URL.
6. **Everything has an API with scoped tokens.** Nothing is UI-only, which keeps
   power users able to use Terraform or scripts. (Seraphina's "everything a
   novice does is visible to an expert" goal, §28.)
7. **Translations** in 19 UI locales (one is a joke "Shakespeare" English) and a
   dashboard ("homepage") with app icons.

## 15. What Cosmos lacks that real AI (and Seraphina's design) can add

Mapped to the owner's decisions in §31:

| Gap in Cosmos (0.23.4) | Seraphina answer |
| ---------------------- | ---------------- |
| Updates have no pre-backup, no health check, no rollback once the new image starts; old images pruned (§11) | Update = executor bundle: record digest and Blueprint → restic snapshot of the app's volumes → pull → recreate → **app-level health checks** (HTTP probe, the app's own API, dependent connections) → keep, or recreate with the old digest and restore volumes. Turn off `cosmos-auto-update` on managed containers so only one updater exists (§31.2, §30). |
| No cross-service knowledge: Open WebUI's connections to TabbyAPI/llama.cpp, model profiles, instruction sets live in each app's own database | Shared-facts registry with one source of truth and drift detection across Open WebUI, TabbyAPI, DSH and Qwen Code (§31.5). Cosmos routes and container definitions are one more consumer to check (e.g. a route whose target container no longer exists). |
| Alerts can only notify/email/stop; no diagnosis; webhook/script stubs | The inspector reads Cosmos events and metrics, plus journal, `nvidia-smi` and model-server stats. It turns them into findings in plain language with evidence, and a planner proposes the fix and its rollback. |
| No GPU, VRAM, NVLink or model-server awareness; "anti crypto-miner" alert would stop inference | Resource broker (§29): GPU/VRAM owners and windows. Metrics for tokens/s and context. Alert policy that knows a model server is *supposed* to use 100 % of a GPU. |
| Market has no AI apps; no `runtime: nvidia` templates; `deploy:` GPU syntax ignored | An **AI market source** run by Seraphina (§17.4) with tested templates for Open WebUI, llama.cpp server, TabbyAPI, ComfyUI and others. The Model Factory deploys quantised models to them. |
| Restic backups need Premium + native install; no restore drills; no "restore this app" unit | Seraphina owns restore drills and app-level restore bundles. On Cosmos it uses Cosmos backups when licensed, otherwise runs restic itself on separate repositories (§16). |
| Fixed 02:00 maintenance, no per-service windows | Maintenance modes manual/scheduled/automatic with a global toggle and per-service overrides (§31.3). |
| Container export has no history and holds secrets in clear | Blueprint stores versioned exports in git with secrets replaced by fingerprints (§28). |
| No record of "what we said we would check" | Housekeeping tracker: commitments outside any agent's context, tracked to closure (§31.5). |
| Single-maintainer, fast-moving project | The adapter pins and tests against the OpenAPI spec of the installed version (§17.6). |

## 16. Replace, sit on top, or both?

**Both: a pluggable platform backend, with the Cosmos adapter first.**

Why not replace Cosmos on the owner's server:
- It already serves the owner's routes, certificates, users and app isolation.
  Re-creating that is a large live change with no benefit to the §31 goals.
- Cosmos's gateway and Smart Shield are mature and specialised; rebuilding a
  reverse proxy plus an identity provider in stdlib Python is out of scope for
  Seraphina.
- The API is complete enough that Seraphina loses nothing by driving it.

Why not *only* sit on top of Cosmos:
- §31.1 says any novice can install Seraphina. Many will have no Cosmos, and
  Seraphina cannot bundle or sell Cosmos (Commons Clause). Bundling Cosmos's paid
  features without their restrictions is explicitly "tampering" (§12).
- Key safety features are gated in Cosmos: backups need Premium *and* a native
  install. Seraphina's "Restore always works" promise (§31.2) cannot depend on a
  third party's paywall or install mode.
- The update and rollback behaviour Seraphina needs is not in Cosmos and cannot
  be added from outside without wrapping it.

So:
- **`PlatformBackend` interface** in Seraphina: inventory, apps
  (install/update/rollback/remove), routes, certificates, users/auth, storage,
  backups, metrics, events. Implementations: `CosmosBackend` (first),
  `NativeBackend` (Docker Engine API over the socket, restic, smartctl,
  `tailscale serve`/Caddy for URLs), and later others (CasaOS/Portainer:
  undecided).
- **One writer per resource.** On a Cosmos machine, Cosmos owns routes, TLS,
  auth and container lifecycle, and Seraphina changes them only through the
  Cosmos API inside an executor bundle. Seraphina owns the Blueprint, restore
  drills, the update policy, model servers' profiles and the housekeeping tracker.
- **No circumvention.** Seraphina never patches or unlocks Cosmos Premium/Pro.
  If Cosmos backups are unlicensed, Seraphina runs its own restic jobs to its own
  repositories. That is independent software doing its own job, not a bypass of
  Cosmos's gate. A licence question for a lawyer, not settled here: whether a
  *paid* Seraphina would count as a product whose value derives "substantially"
  from Cosmos. Keeping Cosmos optional (pluggable backend) keeps that risk low.

## 17. Concrete integration points (CosmosBackend)

1. **Connection and credentials**
   - The executor holds two tokens. Agents never hold them (§27: agents never hold root).
     - `seraphina-read`: permissions `[1,10,20,30]`, plus `40` only if the
       Blueprint must capture env values for fingerprinting.
     - `seraphina-exec`: `[20,21,30,31]`, and `2` only for Cosmos restarts.
       Leave out `11` (user management) unless needed.
   - `ipWhitelist: ["127.0.0.1/32","::1/128"]` if Seraphina runs on the same host,
     otherwise its tailnet IP. Set `expiryDays`, and rotate tokens through
     `/api/api-tokens`.
   - Call Cosmos by its configured hostname (the `EnsureHostname` check in §4).
     Read `GET /api/status` first to confirm `containerized`, `Licence`, `hostmode`
     and `ConfigFolder`. Those decide which backup path is possible.
2. **Blueprint capture (read-only, before and after every change)**
   - Capture `GET /config` (masked), `/routes`, `/servapps` + `/servapps/{id}/`
     + `/servapps/{id}/export`, `/get-backup`, `/volumes`, `/networks`, `/disks`,
     `/mounts`, `/snapraid`, `/backups-config`, `/cron`, `/alerts`, `/openid`,
     `/users` and `/api-tokens` (metadata only).
   - Record each container's `Image` *and* `ImageID`/repo digest.
   - Commit to `<data_dir>/blueprint/` with secrets replaced by fingerprints. The
     executor also copies `/var/lib/cosmos/cosmos.config.json`, `auth.db` and
     `database.db` (read-only, file level) so a config restore is an
     "undo instantly" action (§30).
3. **Events → findings**
   - Poll `/api/events?page=<last id>&logLevel=warning` every 60 s. Not faster,
     because each poll writes a `token.used` event.
   - Also poll `/api/notifications?from=<id>` and the metrics Seraphina charts.
   - Map `cosmos.docker.event.container.die`, `cosmos.metrics.alert`,
     `cosmos.proxy.certificate`, `cosmos.backup`, `cosmos.docker.container.update`
     and `cosmos.proxy.shield.*` into inspector findings. Treat every
     `cosmos.settings` event that Seraphina did not cause as **out-of-band
     change → capture a Blueprint and diff**.
4. **AI market source**
   - Seraphina serves an `index.json` in the official format (§3) on
     loopback/tailnet and adds it via `MarketConfig.Sources` (one config change).
     Novices then see AI apps inside Cosmos's own store.
   - Seraphina's own wizards install the same templates directly: render the
     whiskers variables in Python, then `POST /api/docker-service` and stream the
     log into the job view.
   - Templates use `runtime: nvidia` and `NVIDIA_VISIBLE_DEVICES`, not `deploy:`.
5. **Safe update bundle** (replaces Cosmos auto-update for managed apps)
   1. `/servapps/{id}/auto-update/false` and set `SkipPruneImages` so the old
      image survives. Tagging alone does **not** protect it: Cosmos prunes with
      `dangling=false`, which removes every image no container uses, tagged or
      not. Without `SkipPruneImages`, keep the old image referenced by a
      stopped holder container, or keep a `docker save` copy with its digest
      recorded.
   2. Blueprint + restic snapshot of the app's binds and volumes. Use the Cosmos
      backup API if licensed and native, otherwise Seraphina's restic. Stop the
      container first if it holds a database.
   3. `GET /servapps/{id}/check-update` → `manage/update` (streamed).
   4. Health checks defined per app in the Blueprint: HTTP probe through the
      route, the app's own API, and dependent links (Open WebUI → each
      OpenAI-compatible backend answers `/v1/models` with the expected model IDs).
   5. On failure: capture the broken state (logs, inspect, a DB copy) for
      diagnosis (§31.2). Then `PUT /servapps/{id}/update` with the old image
      digest and restore the volumes, verify, and open a re-fix job.
6. **Version pinning and contract tests**
   - Store the Cosmos version and keep the matching `openapi.json`. The version
     comes from the `meta.json` that ships next to the Cosmos binary
     (`GetCosmosVersion` in `SRC/src/CRON.go`), or from the startup log line
     "Starting Cosmos-Server version ...". `/api/status` does not return it in
     0.23.4.
   - On a Cosmos upgrade, the inspector diffs the spec. A changed endpoint
     puts the adapter into read-only mode until tests pass.
   - Treat Cosmos's own upgrade as an "undo by restore" action: config folder
     copy first.
7. **Tailscale-only audit**
   - Check every route for `WhitelistInboundIPs` (tailnet CIDRs) or a host
     firewall that blocks 80/443 off-tailnet.
   - Check `AdminWhitelistIPs`, and that no container publishes ports on
     `0.0.0.0` outside `DOCKER-USER` filtering.
   - Check that Constellation is off or deliberately on.
   - Flag certificate mode and expiry (`cosmos.proxy.certificate` event; the
     `TLSValidUntil` field in config).
8. **Alert policy sync**
   - Seraphina owns the policy and writes Cosmos alerts (`/api/alerts`) only for
     simple local reactions.
   - Make sure "Anti Crypto-Miner"/"Anti Memory Leak" stay off for GPU/model
     containers, or are scoped to other containers.
   - Use `notification` so the Cosmos bell and Seraphina agree.
9. **Grid Card mapping**: Cosmos areas map onto Seraphina's home sections.
   - URLs, containers and market → *System Maintenance* / *Upgrades & Enhancements*.
   - Storage and backups → *Backups*.
   - Events and alerts → *Systems Audit* and *Housekeeping Tracker*.
   - Each Seraphina service card links "Open in Cosmos" to
     `/cosmos-ui/servapps/containers/<name>` for experts.

## 18. Risks specific to the owner's server ("matrix")

- **Install mode unknown.** If Cosmos runs in Docker (the documented default),
  its backups are disabled whatever the licence. Seraphina must then run restic
  itself. **UNVERIFIED** until the snapshot collector (§28 "Next step") reads
  `/api/status`.
- **Open WebUI upgrade breakage (§31.5).** That incident fits §11 exactly: a
  pull-and-recreate with no health check and no rollback. A network detail is
  also worth checking in the first snapshot. Cosmos isolates "secured" containers
  on per-app networks and removes networks with no members after Docker events.
  Whether Open WebUI's path to TabbyAPI/llama.cpp ran over a shared Docker
  network, a host IP or a Cosmos URL decides whether a recreate can break it.
  This is a hypothesis to test, not a finding.
- **Model servers behind Cosmos routes.** Long streaming responses need high
  `Timeout`. Smart Shield's per-user time and byte budgets (2 h, 200 GB per
  window) can throttle heavy agent traffic from DSH/Qwen Code at 262k context.
  Agent traffic should use direct tailnet/loopback endpoints or routes with Smart
  Shield tuned for them. (Analysis; not measured.)
- **Cosmos and Cockpit side by side.** Cockpit covers what Cosmos doesn't:
  systemd, packages, journal, network, users. Seraphina's inspector should read
  both, and its executor should be the one place where changes are applied.

## 19. Open questions

1. On matrix: is Cosmos Docker or native, which version, and is there a
   Premium/Pro licence? (`GET /api/status` → `containerized`, `Licence`.)
2. Which of the owner's AI containers were created through Cosmos (labels
   `cosmos-*`), and how do Open WebUI, TabbyAPI, llama.cpp, DSH and Qwen Code
   reach each other (Docker network, host port, Cosmos URL, tailnet)?
3. Which HTTPS mode is in use, given no forwarded ports: DNS-01 with which
   provider, self-signed, or Tailscale certificates?
4. Is Constellation enabled anywhere? If so, where is its lighthouse, and does
   its DNS on :53 interact with systemd-resolved and MagicDNS on matrix?
5. Should Seraphina's Cosmos adapter be allowed to change `PUT /config` fields
   (each one triggers a soft restart of all routes), or only routes, containers,
   alerts and market sources?
6. Will Seraphina ever be sold or offered as a paid service? If yes, get a
   licence opinion on the Commons Clause before shipping the Cosmos adapter in a
   paid edition.
7. What changes in 0.24 (zones, managed routes, lazy loading) affect the
   adapter? Lazy loading in particular could start a GPU model container on a
   stray request, which conflicts with the resource broker.
8. Pricing and features of Cosmos Pro, and current prices on cosmos-cloud.io:
   unreachable from here.

## 20. Sources

- Cosmos-Server source at v0.23.4: https://github.com/azukaar/Cosmos-Server/tree/v0.23.4
  (files cited inline as `SRC/...`); OpenAPI spec `SRC/api-docs/openapi.json`;
  changelog `SRC/changelog.md`; licence `SRC/LICENCE`; readme `SRC/readme.md`.
- Unstable branch changelog: https://github.com/azukaar/Cosmos-Server/blob/unstable/changelog.md
- Releases: https://github.com/azukaar/Cosmos-Server/releases ; release cadence
  https://releasealert.dev/github/azukaar/Cosmos-Server
- Docker Hub: https://hub.docker.com/r/azukaar/cosmos-server
- Official market: https://github.com/azukaar/cosmos-servapps-official (template
  guide `MKT/CREATING_A_MARKET_TEMPLATE.md`, example `MKT/servapps/Jellyfin/cosmos-compose.json`)
- Older docs repo (Jan 2024): https://github.com/azukaar/cosmos-docs
- Issue #556: https://github.com/azukaar/Cosmos-Server/issues/556
- Tailscale reserved ranges: https://tailscale.com/kb/1610/reserved-ip-addresses ;
  HTTPS certificates: https://tailscale.com/kb/1153/tailscale-cert
- Docker and firewalls: https://docs.docker.com/network/packet-filtering-firewalls/
- Third-party reviews seen only as search snippets (pages not fetchable here, so
  **UNVERIFIED** in detail): https://www.virtualizationhowto.com/2026/01/i-tested-cosmos-server-is-this-the-best-home-server-os-yet/
  (Jan 2026: easy for beginners; "not an all inclusive hypervisor") and
  https://forum.level1techs.com/t/my-cosmos-cloud-server-and-where-to-go-next/254267
  (a user hit confusing local-access problems).
