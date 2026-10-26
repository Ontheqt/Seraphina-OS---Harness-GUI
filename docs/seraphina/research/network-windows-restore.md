# Tailscale-only access, maintenance windows, and restore-first (research notes)

Written 2026-10-21 for DESIGN.md §31.2 (restore first, then diagnose), §31.3
(maintenance windows: manual, scheduled, automatic, with per-service overrides)
and §31.4 (no forwarded ports; Tailscale only; anything exposed beyond
loopback/tailnet is an audit finding). These notes build on §27, §28 and §30,
and on the sibling notes `cosmos.md` and `platforms-ai.md` in this folder.

**Method and limits.** This environment could not reach tailscale.com,
docs.docker.com, freedesktop.org, renovatebot.com, crazymax.dev, truenas.com,
readthedocs.io or api.launchpad.net. Every fetch to those hosts failed with
DNS errors or proxy refusals. GitHub git and launchpad.net HTML did work. So I
read the **source code and documentation repositories** on GitHub at the
commits listed below, and used web search summaries for pages that exist only
on tailscale.com or synology.com. A statement taken from source code describes
what the software *does* at that commit. A statement that rests only on a
search snippet or on my own knowledge is marked **UNVERIFIED**. Nothing here
was tested on the owner's server ("matrix").

Short links used below:
- `TS/` = https://github.com/tailscale/tailscale/blob/47743a8b9ad16cf3a49504dc443208c37748072e/
  (main at 2026-10-20, `VERSION.txt` 1.105.0; newest stable tag v1.104.2)
- `DD/` = https://github.com/docker/docs/blob/7e40e5a15d9de1d61489c664e56f319005e40a96/content/manuals/engine/
  (docs for Docker Engine 29; newest engine tag `docker-v29.9.2`)
- `SF/` = the root of this repository

---

## 0. The answer in one screen

**Tailscale-only access**
- Serve every web UI (the harness, Cockpit, Cosmos, Open WebUI) through
  **`tailscale serve --bg --https=<port> http://127.0.0.1:<app port>`**, with the
  app bound to loopback only. Serve gets and renews a Let's Encrypt certificate
  for `matrix.<tailnet>.ts.net`, answers only on the Tailscale IP, and keeps the
  original `Host` header. It also strips forged identity headers and sets its
  own. Because tailscaled handles those ports itself, they do **not** show up in
  `ss -tlnp`.
- **Identity headers are not enough on matrix.** `Tailscale-User-Login` can be
  trusted only when nothing but tailscaled can reach the backend. On matrix,
  AI agents run shell commands as a local user. Any local process can send a
  request to `127.0.0.1:8765` and set that header itself. So the harness keeps
  its **token cookie** as the gate. Tailscale identity becomes a second check
  and an audit label, never the only key. §1.3 and §1.7 describe how to make
  identity strong if the owner wants that later.
- The harness's existing guards work behind serve with **one config line**:
  add `matrix.<tailnet>.ts.net` to `server.allowed_hosts`. The Origin check
  passes because serve keeps the Host header. Two fixes are needed: give each
  app its **own origin** (never `--set-path` another app under the harness's
  hostname), and mark the cookie `Secure` when it is served over HTTPS.
- **Funnel** is the one Tailscale feature that publishes a node to the internet.
  Keep the `funnel` node attribute **out of the tailnet policy** so it cannot be
  turned on by mistake. The audit then checks `AllowFunnel` in `tailscale serve
  status --json` on every run.
- The exposure audit has to look past UFW. Docker-published ports skip UFW
  entirely. Cosmos and Cockpit listen on all interfaces by default. A global
  IPv6 address means LAN NAT protects nothing. Tailscale itself asks the router
  for a UPnP/NAT-PMP mapping. Router port forwards **cannot be seen from the
  server**, so they need an outside scan (for example, a phone on mobile data)
  plus a guided look at the router page.

**Maintenance windows**
- Copy the best idea from each tool. From Renovate: a window *permits* change
  but does not *trigger* it, release age is a separate rule, and changes are
  rate-limited. From Watchtower/Cosmos labels: per-container overrides with
  explicit precedence. From Diun: "detect" is kept apart from "apply". From
  systemd timers: catch up after downtime (`Persistent=`) and spread start times
  at random. From unattended-upgrades: package blacklists, and reboots only at a
  set time. From TrueNAS: every update creates a rollback point automatically.
- **Mode** (manual / scheduled / automatic) is set once globally, and a service
  may override it. **Safety rules** (busy, blackout, one change at a time,
  reversibility class from §30) are separate from mode, and no mode can
  override them. Only a person pressing "Do it now anyway" can, after a warning
  in plain words.
- **Busy detection** is a veto with debounce. Use llama.cpp `/slots`
  (`is_processing`) or `/metrics` (`requests_processing`). TabbyAPI has no busy
  endpoint, so use open connections plus GPU activity. Look for GPU compute
  processes that belong to someone else (quantisation). Check logind
  **inhibitor locks**, which Model Factory jobs should take. Check the harness's
  own running jobs.
- Ubuntu already changes things on its own. `apt-daily-upgrade.timer` runs at
  06:00 plus up to 60 minutes. On 24.04, `needrestart` restarts services after
  library upgrades. Seraphina must take over these jobs or fence them off,
  otherwise "manual" mode is a fiction.

**Restore first, then diagnose**
- Order: **freeze** the change queue → **capture** (at most about 2 minutes;
  data snapshot first, then what a rollback destroys: container logs, `docker
  diff`, the failed image, the failed database) → **restore** in order of least
  disruption → **verify** → **diagnose from the bundle while the service is back
  up** → the re-fix goes through the normal line.
- Restore an image and its data **together**. Open WebUI's docs say its
  database migrations are one-way, so the old image may not run on the new
  schema.
- Keep the old image yourself. Cosmos prunes all unused images after an update,
  and `docker image prune -a` removes any image no container uses. A stopped
  "keeper" container or a `docker save` tarball protects it.
- OS-level rollback on Ubuntu in 2026. **zsys is finished**: no upstream commit
  since January 2024, and packaged only up to 24.04. Use ZFS with
  **ZFSBootMenu** (v3.1.0) or btrfs with **snapper**. grub-btrfs is not in the
  Ubuntu archive. On ext4 the options are **LVM snapshots** (a default Ubuntu
  Server install leaves VG space free beyond a 100 GiB root) or
  **Timeshift** (rsync mode). Use restic 0.19.1 or borg 1.4.5 for data. Borg 2
  is still beta.
- Run restore drills on a schedule. Their results gate §30's "Undo by restore"
  class.

---

## Part 1. Tailscale-only access

### 1.1 Three ways to put a web app on a tailnet

| Option | How | Reachable from | Notes |
| ------ | --- | -------------- | ----- |
| **A. Loopback + Serve** (recommended) | app binds `127.0.0.1:<p>`; `tailscale serve --bg --https=443 http://127.0.0.1:<p>` | tailnet peers allowed by the policy, on `https://matrix.<tailnet>.ts.net`; local processes on matrix | HTTPS with a real certificate; works whatever the boot order, because loopback always exists; identity headers are added |
| B. Bind the tailnet IP | app binds `100.x.y.z:<p>` | tailnet peers; local processes | plain HTTP unless the app terminates TLS with `tailscale cert` files; binding fails if tailscaled is not up yet (needs `IP_FREEBIND`/`FreeBind=yes`, or ordering after `tailscale-online.target`); the app can call LocalAPI whois on the real peer address |
| C. Bind `0.0.0.0` + firewall | app binds all addresses; UFW allows only `tailscale0` | depends on the firewall; **Docker ports skip UFW** | fragile: one wrong rule exposes the LAN or IPv6; this is the setup the audit treats as a finding |

- Option B ordering: the packaged units include `tailscale-wait-online.service`
  (`ExecStart=/usr/bin/tailscale wait`, `WantedBy=tailscale-online.target`,
  `TS/cmd/tailscaled/tailscale-wait-online.service`). Cockpit's guide recommends
  `FreeBind=yes` whenever a socket names a specific IP
  (https://github.com/cockpit-project/cockpit/blob/4bc7d15/doc/modules/guide/pages/listen.adoc).
- A fourth option, **tsnet** (embedding Tailscale in the app), needs Go. It
  does not suit a stdlib-Python harness.

### 1.2 What `tailscale serve` actually does (from source)

- **Targets.** A port number means `127.0.0.1:<port>`. `localhost:3000`,
  `http://localhost:3000/foo`, `https+insecure://localhost:8443`,
  `unix:/path.sock`, a file or directory, or `text:` are also accepted
  (`TS/cmd/tailscale/cli/serve_v2.go`, help text lines 139-160). Other
  hostnames count as "remote" targets (`isRemote`, same file).
- **Flags** (same file, lines 241-253): `--bg`, `--https <port>` (the default
  mode), `--http <port>`, `--tcp <port>`, `--tls-terminated-tcp <port>`,
  `--proxy-protocol 1|2`, `--set-path`, `--accept-app-caps`,
  `--service svc:<name>`, `--yes`. Subcommands: `status [--json]`, `reset`,
  and `off` (by repeating the command with `off`).
- **Persistence.** With `--bg`, the config is stored by tailscaled and
  survives restarts. Without `--bg`, the config lives only as long as the CLI
  session ("Foreground ... ensure ungraceful shutdowns ... does not expose ports
  that users are not aware of", `TS/ipn/serve.go` lines 70-76). Seraphina should
  always use `--bg` and keep the serve config in the Blueprint.
- **No host listening socket.** Serve ports on the node's Tailscale IP are
  handled inside tailscaled ("should be intercepted by Tailscaled and handled
  in-process", `ShouldInterceptTCPPort`, `TS/ipn/ipnlocal/local.go` line 8443).
  So `ss -tlnp` will **not** show `:443` for serve. The source of truth is
  `tailscale serve status --json`. This conclusion comes from reading the code
  and was not tested on matrix.
- **Headers sent to the backend** (`TS/ipn/ipnlocal/serve.go`):
  - `Host` is the original host (`r.Out.Host = r.In.Host`) for TCP backends.
    For **unix-socket backends** it is the URL's host, `localhost` (lines
    990-995).
  - `X-Forwarded-Host` = the original host, `X-Forwarded-For` = the tailnet
    source IP, and `X-Forwarded-Proto: https` when TLS was used
    (`addProxyForwardedHeaders`, lines 1088-1096).
  - Identity headers: see §1.3.
- **Certificates.** Serve fetches the certificate during the TLS handshake
  through tailscaled's cert cache, which renews a cached certificate that is
  close to expiry (`getTLSServeCertForPort` → `GetCertPEM`,
  `TS/ipn/ipnlocal/serve.go` line 1361; `TS/ipn/ipnlocal/cert.go` lines 82-84).
  A file made with `tailscale cert` for another program, such as Cosmos's
  `PROVIDED` mode, is not renewed by itself. See `cosmos.md` §4.
- **Admin-side prerequisites** (search summaries of
  https://tailscale.com/kb/1153/enabling-https and https://tailscale.com/kb/1312/serve,
  **UNVERIFIED** in detail): MagicDNS and "HTTPS Certificates" must be turned on
  in the admin console. Certificates come from Let's Encrypt through a DNS-01
  TXT record that Tailscale creates under `*.ts.net`, and private keys stay on
  the machine. **Every name that gets a certificate is published in the public
  Certificate Transparency logs** as `machine.<tailnet>.ts.net`, so machine names
  must not be sensitive. Reissuing too often can hit Let's Encrypt limits, with
  a wait of about 34 hours.

### 1.3 Identity headers, and exactly when they can be trusted

What serve sets, from `addTailscaleIdentityHeaders` and
`addAppCapabilitiesHeader` (`TS/ipn/ipnlocal/serve.go` lines 1098-1166):

1. **On every proxied request, serve first deletes** any incoming
   `Tailscale-User-Login`, `Tailscale-User-Name`, `Tailscale-User-Profile-Pic`,
   `Tailscale-Funnel-Request`, `Tailscale-Headers-Info` and
   `Tailscale-App-Capabilities`. A client on the tailnet cannot forge them
   *through serve*.
2. **Funnel traffic** gets `Tailscale-Funnel-Request: ?1` and no identity.
3. Serve calls `WhoIs` on the TCP source address. **If the source node is
   tagged, no identity headers are set** ("Not setting identity headers for
   tagged nodes"). If the source is not a tailnet peer, nothing is set either.
4. Otherwise it sets `Tailscale-User-Login` (login name),
   `Tailscale-User-Name` (display name) and `Tailscale-User-Profile-Pic`, plus
   `Tailscale-Headers-Info: https://tailscale.com/s/serve-headers`. Non-ASCII
   values are RFC 2047 Q-encoded, and invalid UTF-8 becomes empty
   (`encTailscaleHeaderValue`).
5. With `--accept-app-caps=<domain>/<name>[,...]`, serve adds
   `Tailscale-App-Capabilities: {json}`. It holds only the listed capabilities
   that the tailnet policy grants this peer. This lets the **tailnet policy
   decide roles** (for example, "admin" vs "viewer") for an app.

Tailscale's own guidance (search summary of https://tailscale.com/kb/1312/serve)
is to have the backend listen only on localhost. Then only "other services
running on the Serve device" can tamper with the headers.

**On matrix, "other services running on the Serve device" includes the
agents.** The harness runs AI agents whose Bash runs as a local user (§17:
contained only under `bwrap`). Any of them, or any container with host
networking, can run `curl -H 'Tailscale-User-Login: owner@…'
http://127.0.0.1:8765/api/...`. So, for the harness:

- **Trust rule:** treat the identity headers as *claims*. They become *facts*
  only when the backend can be reached by tailscaled alone. Two setups achieve
  that:
  - **(i) Unix socket with tight permissions.** The harness listens on, say,
    `/run/seraphina/web.sock` in a directory that only the harness user can
    enter. tailscaled runs as root, so it can still connect. Agents must run as
    a **different Unix user**, or inside `bwrap` without that path. Catch: serve
    then sends `Host: localhost` and keeps the real name in `X-Forwarded-Host`.
    The harness's Origin check compares Origin with `Host`, so it would refuse
    every browser POST until it is taught to use `X-Forwarded-Host` in this
    mode. That is safe only because no one else can reach the socket.
  - **(ii) Bind the tailnet IP and call LocalAPI whois on the TCP peer
    address** (option B in §1.1). On a tailnet, WireGuard authenticates the
    source address, so it cannot be forged. Catch: a local process can also
    connect to `100.x.y.z` from the server itself. If the server is a
    user-owned node, whois would return **the owner** for that connection. The
    harness must therefore refuse peer addresses that belong to its own node
    (`Self.TailscaleIPs` from `tailscale status --json`). Tagging the server
    (§1.5) helps, because whois then returns a tag, not a user.
- **Recommendation for v2:** keep the token as the gate (no change to v1's
  model). Add a config list `server.tailscale_logins` (for example
  `["owner@example.com"]`). If a request carries a `Tailscale-User-Login` that
  is not on the list, refuse it (403). Write the claimed login into audit
  records, labelled "as reported by Tailscale Serve". Offer strong identity
  (i) as an opt-in once agents run under a separate user.

### 1.4 LocalAPI whois

- **Socket:** `/var/run/tailscale/tailscaled.sock` on Linux
  (`DefaultTailscaledSocket`, `TS/paths/paths.go` lines 53-90). The packaged
  unit passes `--socket=/run/tailscale/tailscaled.sock`
  (`TS/cmd/tailscaled/tailscaled.service`). On Ubuntu, `/var/run` is a symlink
  to `/run`, so both name the same file.
- **Request:** `GET http://local-tailscaled.sock/localapi/v0/whois?addr=<ip>:<port>`
  over the Unix socket. The Host must be `local-tailscaled.sock`. There are
  variants `&proto=tcp`, `&svc_name=` and `&dst_ip=`
  (`TS/client/local/local.go` lines 159-162, 353-430). The CLI equivalent is
  `tailscale whois <ip>` (`TS/cmd/tailscale/cli/whois.go`).
- **Permissions:** on Linux, any local user who can connect to the socket gets
  **read** access, which includes whois. **Write** access is for root or the
  configured operator user. Fetching certificates needs `TS_PERMIT_CERT_UID`
  (`actor.Permissions`, `actor.CanFetchCerts`, `TS/ipn/ipnserver/server.go`
  lines 336-401; `serveWhoIsWithBackend` checks `PermitRead`,
  `TS/ipn/localapi/localapi.go` line 556).
- **From stdlib Python:** `socket.AF_UNIX` plus a small `http.client.HTTPConnection`
  subclass whose `connect()` opens the socket. No dependencies are needed.
  Reply: `{Node, UserProfile, CapMap}` (the `apitype.WhoIsResponse` shape;
  field list not re-checked here).
- whois answers "who owns this tailnet address". It authenticates an HTTP
  request only when the TCP peer address really is that tailnet address, which
  is not the case behind serve, where the peer is 127.0.0.1.

### 1.5 Tags, ACLs/grants, app capabilities

From search summaries of https://tailscale.com/kb/1068/acl-tags,
https://tailscale.com/kb/1538/grants-syntax and https://tailscale.com/kb/1458/grant-examples
(**UNVERIFIED** beyond the summaries):

- **Tag the server** (for example `tag:seraphina`). A tag *replaces* the
  device's user identity. Tagged devices have key expiry turned off by
  default, so the server won't drop off the tailnet when a key expires. Removing
  the user identity also helps §1.3 (ii).
- **Grants** are deny-by-default. Each needs `src`, `dst` and `ip` and/or
  `app`. When several match, the result is their union. App capabilities apply
  only where the peer also has network access.
- A starting policy for matrix (the shape is from the docs above; field names
  and the `tests` syntax are **UNVERIFIED**):
  ```json
  {
    "tagOwners": { "tag:seraphina": ["autogroup:admin"] },
    "grants": [
      { "src": ["autogroup:admin"], "dst": ["tag:seraphina"], "ip": ["tcp:443", "tcp:9090", "tcp:22"],
        "app": { "seraphina.example/cap/role": [{ "role": "admin" }] } },
      { "src": ["group:family"], "dst": ["tag:seraphina"], "ip": ["tcp:8443"] }
    ],
    "nodeAttrs": [],
    "tests": [
      { "src": "group:family", "accept": ["tag:seraphina:8443"], "deny": ["tag:seraphina:443"] }
    ]
  }
  ```
  Here 443 is the harness, 9090 Cockpit (served), and 8443 Open WebUI for family
  members. The harness would run serve with
  `--accept-app-caps=seraphina.example/cap/role`. Note that `nodeAttrs` holds
  **no `funnel`** entry.
- **Tailscale Services** (`tailscale serve --service=svc:<name>`) give each
  service its own MagicDNS name and virtual IP. That would be a clean way to
  give every app its own origin. The October 2025 beta required tagged hosts,
  advertisement and admin approval, and TCP only
  (https://tailscale.com/blog/services-beta, search summary). Whether it is
  generally available in October 2026 is **UNVERIFIED**. Until that is
  checked, use **one port per app** on the server's single name.

### 1.6 Funnel: why it must stay off, and how to prove it is off

- Funnel publishes a serve endpoint **to the public internet**. It creates
  public DNS and a public certificate, and identity headers are absent (§1.3).
  That directly breaks §31.4.
- It is gated twice. The tailnet policy needs a `funnel` node attribute for
  the node (search summary of https://tailscale.com/kb/1223/funnel; the
  attribute is `nodecap.Funnel = "funnel"`, `TS/tailcfg/tailcfg.go` line 2523).
  The node's serve config also needs `AllowFunnel` entries (`TS/ipn/serve.go`
  lines 66-68, `SetFunnel`).
- **Policy:** keep `funnel` out of `nodeAttrs`. A local mistake (`tailscale
  funnel 443 on`) then fails, because the CLI checks that Funnel is enabled
  before starting a new one (`verifyFunnelEnabled`,
  `TS/cmd/tailscale/cli/funnel.go` lines 99-106).
- **Audit:** `tailscale serve status --json` must show no `AllowFunnel`, or an
  empty one. `tailscale funnel status` must report nothing. The harness should
  **refuse to start** when `AllowFunnel` contains its own host:port, and the
  audit raises a critical finding for any entry at all.

### 1.7 How this meets the harness's existing guards (`SF/swfactory/server.py`)

What v1 does today:
- `_allowed_hosts` (lines 213-218): `localhost`, `127.0.0.1`, `[::1]`, the bind
  host, `server.host`, and `server.allowed_hosts`. Matching is on the
  lowercased host, any port. `_check_host` (lines 545-549) returns 403 for
  anything else. That is the DNS-rebinding guard.
- `_check_origin` (lines 551-561) runs for POSTs only. If `Origin` is present,
  its host and port must equal those of `Host`, with the default port taken
  from the Origin's scheme.
- `sf_token` cookie: `HttpOnly; SameSite=Strict; Path=/`, **without `Secure`**
  (lines 637-638). `GET /login?token=…` sets it (lines 652-657).
- The default bind is `127.0.0.1:8765` (lines 1114-1115). §17 says to reach it
  with `ssh -L`.

Behind serve (option A):
1. **Host.** Serve keeps `Host: matrix.<tailnet>.ts.net`, so add exactly that
   name to `server.allowed_hosts`. Do **not** add the 100.x address or a
   wildcard. The harness stays loopback-bound, and `ssh -L` keeps working.
2. **Origin.** The browser sends `Origin: https://matrix.<tailnet>.ts.net` with
   `Host: matrix.<tailnet>.ts.net`. The ports match (443 by the https default
   on both sides), so the check passes. With `--https=8443`, both carry `:8443`.
   No code change is needed for TCP backends. For a unix-socket backend, see
   §1.3 (i).
3. **One origin per app.** Do not mount Open WebUI or Cockpit under the
   harness's origin with `--set-path`. Pages on the same origin send the
   harness cookie (SameSite does not separate paths) and pass the Origin check,
   and a GET's JSON response is readable by their scripts. Use a separate HTTPS
   port per app (443 harness, 8443 Open WebUI, 9090 Cockpit), or separate
   Service names (§1.5).
4. **Cookie.** Add `Secure` when the request arrived over HTTPS. Serve sets
   `X-Forwarded-Proto: https`. A local process could forge that header, but the
   only effect would be a cookie the browser won't send over plain HTTP, which
   is harmless. Keep plain `http://127.0.0.1` logins (ssh tunnel) working
   without `Secure`. Consider the `__Host-` cookie prefix for the HTTPS case.
5. **Token stays primary.** Add the optional `server.tailscale_logins` check
   from §1.3. The doctor gains checks: "serve config forwards 443 to this
   server", "no AllowFunnel", "allowed_hosts contains the MagicDNS name",
   "bind address is loopback".
6. **Event stream.** SSE (`/api/events`) goes through Go's
   `httputil.ReverseProxy` inside tailscaled. That proxy normally flushes
   `text/event-stream` responses at once. This is Go standard-library
   behaviour that I did not test through serve here (**UNVERIFIED**). Test the
   15 s pings end to end.
7. **Suggested config shape** (for the v2 design):
   ```json
   "server": { "host": "127.0.0.1", "port": 8765,
               "allowed_hosts": ["matrix.example-tailnet.ts.net"],
               "tailscale": { "serve_https_port": 443, "logins": ["owner@example.com"],
                              "trust_identity": "claim" } }
   ```
   `trust_identity` would be `"off" | "claim" | "strong"`. `"strong"` would be
   allowed only with a unix socket and agents under another UID (§1.3 (i)).

### 1.8 Exposure audit checklist (proves nothing is reachable beyond loopback/tailnet)

Each line names a check, the command, what passes, and what is a finding.
Expected exceptions are listed so the audit doesn't cry wolf.

**A. Listening sockets on the host**
- `ss -H -tulpn` (as root, so that PIDs show). Any TCP/UDP listener on
  `0.0.0.0`, `*`, `[::]`, a LAN address or a global IPv6 address is a
  **finding**, unless it is on the allowlist. Map each PID to a systemd unit
  (`/proc/<pid>/cgroup`) or container. Expected entries:
  - tailscaled UDP on `PORT` from `/etc/default/tailscaled`, which is
    `PORT="41641"` in the packaged defaults
    (`TS/cmd/tailscaled/tailscaled.defaults`). This is WireGuard, which drops
    unauthenticated packets.
  - `127.0.0.53`/`127.0.0.54` (systemd-resolved) and other loopback addresses.
  - The `100.x`/`fd7a:115c:a1e0::/48` Tailscale addresses
    (https://tailscale.com/kb/1610/reserved-ip-addresses, via `cosmos.md` §6)
    are tailnet-only and fine.
- Known offenders on matrix's stack:
  - **Cockpit:** `cockpit.socket` has `ListenStream=9090`, which means all
    addresses
    (https://github.com/cockpit-project/cockpit/blob/4bc7d15/src/systemd/cockpit.socket.in).
    Fix: a drop-in `/etc/systemd/system/cockpit.socket.d/listen.conf` with
    `ListenStream=` then `ListenStream=127.0.0.1:9090` (listen.adoc above),
    served with `tailscale serve --bg --https=9090 http://127.0.0.1:9090`. Behind
    a proxy, Cockpit needs `[WebService] Origins = https://matrix.<tailnet>.ts.net:9090`
    and `ProtocolHeader = X-Forwarded-Proto` in `cockpit.conf`. The docs warn
    that the proxy must stop clients setting that header, and serve does set it
    (https://github.com/cockpit-project/cockpit/blob/4bc7d15/doc/modules/man/pages/cockpit.conf.5.adoc).
  - **Cosmos:** listens on `0.0.0.0` 80/443 and cannot be told otherwise
    (`cosmos.md` §2, §6). Restrict it with `WhitelistInboundIPs` /
    `AdminWhitelistIPs` set to the tailnet ranges, or a host firewall rule. If
    Constellation is on, UDP 4242 and a DNS server on 53 appear as well; they
    should be off (`cosmos.md` §6).
  - **llama.cpp server:** the default `--host` is `127.0.0.1`. The README's
    Docker examples use `--host 0.0.0.0 -p 8080:8080`, which is a finding if
    copied
    (https://github.com/ggml-org/llama.cpp/blob/8baabd5/tools/server/README.md
    lines 193, 460-463). The same README lists the default `--port` as 9931,
    where older builds used 8080. Read the real port from the unit file, don't
    assume it.
  - **TabbyAPI:** defaults to `host 127.0.0.1`, `port 5000`
    (https://github.com/theroyallab/tabbyAPI/blob/a270c55/common/config_models.py
    lines 40-47).
  - **Open WebUI:** the common `-p 3000:8080` publishes on all addresses
    (Open WebUI rollback example in
    https://github.com/open-webui/docs/blob/cd7fbd8/docs/getting-started/updating.mdx).
- **IPv6:** `ip -6 addr show scope global`. If matrix has a global IPv6
  address, there is no NAT in front of it. The router's IPv6 firewall is the
  only barrier, so any `[::]` listener may be reachable from the internet with
  **no port forward at all**. Report it as a high finding unless the outside
  check (H) proves otherwise.

**B. Docker-published ports (they bypass UFW)**
- `docker ps --format '{{.Names}}\t{{.Ports}}'` and, per container,
  `docker inspect -f '{{json .HostConfig.PortBindings}} {{.HostConfig.NetworkMode}}'`.
  A binding with `HostIp` empty, `0.0.0.0` or `::` is a **finding**. A
  container with `NetworkMode=host` shows its sockets in A instead.
- Why UFW doesn't help: Docker's port rules are in the `nat` table, so
  "packets are diverted before it reaches the INPUT and OUTPUT chains that ufw
  uses" (`DD/network/packet-filtering-firewalls.md`, "Docker and ufw").
- Safe defaults to check: `"ip": "127.0.0.1"` in `/etc/docker/daemon.json` for
  the default bridge, and `"default-network-opts": {"bridge":
  {"com.docker.network.bridge.host_binding_ipv4": "127.0.0.1"}}` for
  user-defined bridges (`DD/network/port-publishing.md` lines 240-330). Swarm
  services ignore this and always use `0.0.0.0` (same page).
- **Docker < 28.0:** "hosts within the same L2 segment ... can reach ports
  published to localhost" (moby/moby#45610, same page). Matrix should run
  28.0 or later. Current is 29.9.2.
- Filtering rules: with the iptables backend, use the `DOCKER-USER` chain
  (match original addresses with `-m conntrack --ctorigdst/--ctorigdstport`,
  `DD/network/firewall-iptables.md` lines 83-95). With the **nftables backend**
  (experimental since Docker 29.0) there is **no `DOCKER-USER` chain**, and an
  "accept" in your own chain does not override Docker's drop; you need
  `--bridge-accept-fwmark` (`DD/network/firewall-nftables.md` lines 10-60,
  150-200). Audit commands: `iptables -S DOCKER-USER`, `iptables -t nat -S DOCKER`,
  `nft list ruleset`, and `docker info --format '{{json .}}'`, whose firewall
  backend field was not checked here (**UNVERIFIED**).
- The fix the audit proposes: re-publish on `127.0.0.1:<p>` and add a serve
  entry, rather than writing firewall rules a novice can't read.

**C. Host firewall**
- `ufw status verbose`: default deny incoming, and allow on `tailscale0` only
  for what is needed. This is a second layer, not proof: it does not cover B.

**D. Tailscale's own exposure**
- `tailscale serve status --json`: no `AllowFunnel`, and every `Web`/`TCP` entry
  is on the allowlist (each served port is a deliberate door).
- `tailscale status --json`:
  - `Self.Tags` should include the server tag.
  - `Self.PrimaryRoutes` and advertised subnet routes, plus `ExitNodeOption`:
    a subnet router or exit node widens who can reach what. That is a finding
    unless intended.
  - Peers with `ShareeNode: true` are in the netmap "because it's owned by a
    shared-to user and that node might connect to us"
    (`TS/ipn/ipnstate/ipnstate.go` lines 318-322). In other words, matrix
    was **shared with someone outside the tailnet**. `tailscale status` hides
    these peers by default, so the audit must read the JSON. List them as a
    finding to review.
- Tailscale SSH and the Tailscale web client (`tailscale web`) are more doors
  to list. Their flags were not checked here (**UNVERIFIED**).

**E. UPnP / NAT-PMP / PCP on the router**
- Tailscale's port mapper asks the router for a mapping of tailscaled's UDP
  port, to make direct connections easier (`TS/net/portmapper/`). That mapping
  is **expected**: it reaches WireGuard only. It can be turned off with
  `TS_DISABLE_PORTMAPPER=1`, or UPnP alone with `TS_DISABLE_UPNP=1`
  (`TS/net/portmapper/portmapper.go` line 45, `upnp.go` line 460), set in
  `/etc/default/tailscaled`. The cost is more DERP-relayed traffic (my
  inference).
- From the LAN, list the router's dynamic mappings with miniupnpc's
  `upnpc -l` and natpmpc (tool names from my own knowledge, **UNVERIFIED** here).
  Any mapping other than tailscaled's is a **finding**. Containers such as
  media servers and torrent clients often request mappings themselves. Best
  practice is UPnP off on the router.

**F. Cosmos and Constellation**
- Cosmos admin and API must not answer on the LAN. Check `AdminWhitelistIPs`,
  `AdminConstellationOnly` and each route's `WhitelistInboundIPs`
  (`cosmos.md` §4, §6). Constellation needs a public lighthouse, which
  conflicts with §31.4 unless it runs on an outside VPS. Recommendation: leave
  it off (`cosmos.md` §6).

**G. Run it on a schedule and after every change**
- The Blueprint already captures listening ports (§28). The audit compares
  each capture with the allowlist, and a new open port appears as a
  plain-language diff ("port 8080 now open to the LAN").

**H. What the server cannot see, and how to check it anyway**
- **Not visible from matrix:** manual port forwards and "DMZ host" on the
  router; the router's IPv6 inbound firewall; whether the ISP uses CGNAT; a
  forward to matrix made by another device on the LAN.
- **How to check anyway:**
  1. **Outside scan.** Use a vantage point outside the home network that does
     not route through matrix: a phone on mobile data (Wi-Fi off) running a port
     scanner app, or a cheap VPS running `nmap -Pn` against the home public IPv4
     and matrix's global IPv6. Scan all TCP ports once, then a short list
     (22, 80, 443, 3000, 5000, 8080, 8443, 9090, the model ports) after each
     change. Seraphina can produce the exact command and the list. matrix can
     learn its own public addresses from `tailscale netcheck` (my own
     knowledge, **UNVERIFIED** here).
  2. **Passive lookup.** Search the public IP on Shodan or Censys for anything
     already indexed. This catches past exposure.
  3. **Guided router check.** A wizard card: "Open your router's page. Look for
     Port Forwarding / Virtual Server / NAT / DMZ / UPnP. Take a screenshot."
     The owner attaches it and the finding is closed by a person, not assumed.
  4. Record the result with a date. A stale outside check (for example, older
     than 90 days, or older than the last router change) is a finding in
     itself.

**Severity suggestion:** Funnel on, or a confirmed outside reachability →
critical. A Docker `0.0.0.0` port, an all-address listener, a global IPv6
address with `[::]` listeners, or a non-Tailscale UPnP mapping → high. Shared
node, subnet route or exit node not on the allowlist → medium. Stale outside
check → low.

---

## Part 2. Maintenance windows

### 2.1 What existing tools do (and the lesson from each)

**unattended-upgrades + apt timers (Ubuntu's default)**
- Timers: `apt-daily.timer` downloads at `OnCalendar=*-*-* 6,18:00` with
  `RandomizedDelaySec=12h`. `apt-daily-upgrade.timer` installs at
  `*-*-* 6:00` with `RandomizedDelaySec=60m`. Both have `Persistent=true`
  (https://github.com/Debian/apt/tree/6b12812/debian; that GitHub mirror is from
  November 2025, and these values have not changed in years). `20auto-upgrades`
  turns both periodic jobs on
  (`APT::Periodic::Update-Package-Lists "1"; APT::Periodic::Unattended-Upgrade "1";`,
  https://github.com/mvo5/unattended-upgrades/blob/5877bee/data/20auto-upgrades).
- Options in `50unattended-upgrades.Ubuntu` (same repo, `data/`):
  `Allowed-Origins`; `Package-Blacklist` (Python regexes, examples `linux-`,
  `libc6$`); `MinimalSteps` (small chunks so SIGTERM can interrupt);
  `InstallOnShutdown`; `Mail` and `MailReport` (`always` / `only-on-error` /
  `on-change`); `Remove-Unused-Kernel-Packages`; `Automatic-Reboot`
  ("*WITHOUT CONFIRMATION*" when `/var/run/reboot-required` exists),
  `Automatic-Reboot-WithUsers`, `Automatic-Reboot-Time "02:00"`;
  `OnlyOnACPower`; `Skip-Updates-On-Metered-Connections`; `Dl-Limit`.
- **needrestart on 24.04 restarts services automatically** at the end of APT
  transactions, including unattended ones. Setting `$nrconf{restart}` may not
  stop it (Launchpad bug 2068543). The supported per-service escape hatch is
  `$nrconf{override_rc}`. A libc6 update once restarted every service using it,
  and nginx failed to come back
  (https://discourse.ubuntu.com/t/needrestart-changes-in-ubuntu-24-04-service-restarts/44671,
  https://discourse.ubuntu.com/t/ubuntu-24-04-unattended-upgrades-failed-to-restart-nginx/67909;
  search summaries). Whether this holds on 26.04 is **UNVERIFIED**.
- **Lesson:** Ubuntu already changes the system on its own schedule, and that
  can restart a model server mid-generation. Seraphina must either run apt
  upgrades itself inside its windows, by turning off the periodic upgrade and
  calling `unattended-upgrade` from the executor, or at least add `override_rc`
  entries for GPU services and the harness. Otherwise "manual mode" is
  untrue.

**systemd timers and inhibitor locks**
- `OnCalendar=` takes wall-clock expressions. `Persistent=true` catches up a run
  missed while the machine was off. `RandomizedDelaySec=` spreads runs, and
  `FixedRandomDelay=` and `RandomizedOffsetSec=` make that spread stable.
  `AccuracySec=` allows coalescing. `WakeSystem=` resumes from suspend
  (https://github.com/systemd/systemd/blob/f34f98c/man/systemd.timer.xml).
- `systemd-inhibit --what=shutdown:sleep:idle --who= --why= --mode=block|delay|block-weak`.
  "Any user may take these locks and privileged users may override these
  locks". logind respects them unless `--check-inhibitors=no` or `--force` is
  used (https://github.com/systemd/systemd/blob/f34f98c/man/systemd-inhibit.xml,
  `man/systemctl.xml`). `block-weak` is new. Whether Ubuntu 24.04's systemd has
  it is **UNVERIFIED**.
- **Lesson:** use calendar expressions and catch-up. Use inhibitor locks as a
  standard "I am busy" signal that other tools already honor. Whether
  unattended-upgrades' timed automatic reboot honors them is **UNVERIFIED**;
  test it.

**Watchtower** (archived 2025-12-17; "This project is no longer maintained",
https://github.com/containrrr/watchtower/blob/ca0e86e/README.md; date from
https://linuxiac.com/?p=204037, search summary)
- `--schedule` takes a **6-field** cron (seconds first), and you set either that
  or `--interval`, not both. Other flags: `--monitor-only`, `--label-enable`,
  `--rolling-restart`, `--no-restart`, `--cleanup` (removes the old image)
  (`docs/arguments.md`).
- Per-container labels: `com.centurylinklabs.watchtower.enable` (true or false),
  `com.centurylinklabs.watchtower.monitor-only`, and scopes for multiple
  instances. By default "arguments will take precedence over labels" unless
  `--label-take-precedence` is set (`docs/arguments.md` lines 263-276,
  `docs/container-selection.md`).
- Lifecycle hooks are labels too: `pre-check`, `pre-update`, `post-update`,
  `post-check`. They run inside the container with a 60 s timeout. An exit code
  other than 0 or 75 (EX_TEMPFAIL) does not stop the update
  (`docs/lifecycle-hooks.md`). The docs imply that 75 does stop it; I did not
  check the source.
- **Lessons:** per-service overrides must say plainly who wins; Watchtower got
  this wrong by default. A pre-update veto ("not now") is useful. `--cleanup`
  is what destroys the rollback image.

**Diun** (notify only; never updates)
- `watch.schedule` is a cron (example `"0 */6 * * *"`) with `jitter` (default
  30 s) and `firstCheckNotif`
  (https://github.com/crazy-max/diun/blob/5a47b2a/docs/config/watch.md).
  `watchByDefault` defaults to false.
- Labels: `diun.enable`, `diun.watch_repo`, `diun.notify_on` (`new;update`),
  `diun.sort_tags`, `diun.max_tags`, `diun.include_tags`/`exclude_tags`,
  `diun.platform`, `diun.metadata.*` (`docs/providers/docker.md` lines 148-204).
  Open WebUI's own docs recommend pinning a version and using Diun for
  notifications on shared instances (updating.mdx above).
- **Lesson:** keep "an update exists" (detection) apart from "apply it"
  (decision). Detection can run often and cheaply. Applying is rare and
  guarded.

**Renovate** (the most careful scheduling model)
- "Setting a `schedule` does not itself cause or trigger Renovate to run. It's
  like putting a sign on your office which says 'DHL deliveries only accepted
  between 9-11am'." Use windows of at least 3-4 hours. Granularity is at least
  one hour. Timezone is UTC unless `timezone` is set. Cron is recommended and
  the later.js text syntax is deprecated
  (https://github.com/renovatebot/renovate/blob/da3366e/docs/usage/key-concepts/scheduling.md
  and `docs/usage/configuration-options.md`).
- `automergeSchedule` is separate from `schedule`, so "prepare" and "apply" have
  different windows. `minimumReleaseAge` waits that long after *each* release.
  `prConcurrentLimit` (default 10) and `prHourlyLimit` cap the rate, but
  security PRs ignore the concurrency limit. `dependencyDashboardApproval`
  waits for a person to tick a box. `updateNotScheduled` (default true) allows
  "desirable" updates to existing PRs outside the window. `packageRules` set a
  per-package schedule (same file).
- **Lessons:** a window is a permission, not a trigger. Release age is its own
  rule (do not install a release on the day it ships, except security fixes).
  Rate limits. An approval queue for anything not auto-approved. Security fixes
  get a faster lane.

**TrueNAS** (Community Edition 25.10 docs,
https://github.com/truenas/documentation/tree/afa4dab/content/SCALE)
- System: "When autocheck is enabled, TrueNAS checks for available updates
  nightly". Update profiles are Developer / Early Adopter / General. "Install
  Update" is always a person's action (`SystemSettings/Update/UpdateScreens.md`).
- Every system update creates a new **boot environment**. Environments can be
  activated, cloned, deleted or marked **Keep**, and the updater deletes
  un-kept ones when space runs short. Environments do not share all state
  ("switching between environments frequently creates configuration
  divergence") (`SystemSettings/Boot/BootScreens.md`).
- Apps: "Check for docker image updates" is on by default and shows an
  "Update available" badge. Update is per app or "Update All Selected". **Roll
  Back** reverts an app to an earlier installed version, and "Roll back
  snapshots" also restores the app's data from the snapshot taken for that
  version (`Apps/AppsScreens.md` lines 176-334).
- A forum user reports hourly update checks even with autocheck off
  (https://forums.truenas.com/t/disabling-of-daily-update-check-is-not-respected/25761;
  a user report, **UNVERIFIED**).
- **Lessons:** checking automatically is fine, but installing is a choice. Every
  update creates its rollback point without asking. App rollback restores
  **code and data together**.

**Synology DSM** (search summary of
https://www.synology.com/knowledgebase/DSM/help/DSM/AdminCenter/system_dsmupdate)
- DSM 7.x has an "important update" label for critical security and bug fixes,
  and an Update Settings option to install those automatically. The help page
  warns that after a DSM update "you cannot roll back". Package Center
  auto-update settings: **UNVERIFIED**.
- **Lesson:** a separate fast lane for security fixes, even in otherwise
  manual mode.

**Cosmos** (`cosmos.md` §11): every day at 02:00 it pulls the tags of running
containers and recreates those labelled `cosmos-auto-update=true`. There is no
backup before, no health check after, and rollback happens only if
create/start fails. Old images are usually pruned. This is the Open WebUI
breakage path from §31.5.

**Comparison of what each offers**

| | Manual | Schedule | Automatic | Per-item override | Busy/veto | Release age | Rate limit | Rollback point |
|---|---|---|---|---|---|---|---|---|
| unattended-upgrades | via `unattended-upgrade` | timers | yes | blacklist only | no (InstallOnShutdown, AC/metered) | no | no | no |
| Watchtower | HTTP API mode | 6-field cron | yes | labels | pre-update hook (exit 75) | no | rolling restart | no (`--cleanup` deletes it) |
| Diun | n/a | cron + jitter | notify only | labels | n/a | n/a | n/a | n/a |
| Renovate | dashboard approval | `schedule` window | automerge + `automergeSchedule` | `packageRules` | CI status | `minimumReleaseAge` | `prHourlyLimit`, `prConcurrentLimit` | git revert |
| TrueNAS | Install/Update buttons | nightly check only | no | per app | no | profiles | no | boot env; app snapshot |
| Cosmos | Update button | 02:00 daily | label | label | no | no | no | only on create failure |

### 2.2 Design rules for Seraphina (derived from the above)

1. **Detect, decide, apply are three stages.** Detection runs often (every 6 h
   with jitter, like Diun) and only writes "update available" cards. Decision
   puts a change in the queue. Apply happens only when the mode, window and
   safety rules all allow it.
2. **Mode answers "who decides when".** Window, busy, blackout, release age and
   the §30 reversibility class answer "is it allowed now". Mode can be
   overridden per service. The safety rules cannot be overridden by any mode,
   only by a person's explicit "Do it now anyway" with a plain warning.
3. **Precedence is visible.** Each service shows its *effective* setting and
   why: "Scheduled: Sundays 03:00-06:00 (from the main setting)" or "Manual
   (set on this service)". This avoids Watchtower's silent args-beat-labels
   default.
4. **A window is a permission, not a trigger** (Renovate). Changes waiting in
   the queue start at the first allowed moment *inside* a window. A window
   missed because matrix was off is caught up at the next boot only if the
   window is still open, or the next window. Never immediately at boot, which
   is the opposite of `Persistent=true` and is deliberate: a boot is often a
   recovery.
5. **One change at a time, then soak.** A global executor lock; after each
   change, health checks plus a soak period (default 15 min, longer for model
   servers) before the next one. **Stop the line**: a failed or rolled-back
   change pauses the queue until a person acknowledges it (§3.1).
6. **Order follows dependencies.** If Open WebUI depends on TabbyAPI, a Tabby
   change and an Open WebUI change don't share a window by default. When
   both are needed, the provider goes first. Dependencies come from the
   Blueprint and the shared-facts registry in §31.5.
7. **Release age.** Default 3 days for normal releases and 0 for security
   fixes. A pinned version is never moved automatically.
8. **Security fast lane** (Synology, Renovate). Security fixes may be
   automatic even when the global mode is scheduled, but only if the owner
   turns that on. They still respect busy and one-at-a-time, and get a
   shorter wait.
9. **Reversibility class gates automatic mode** (§30). Automatic may apply
   "Undo instantly" changes, and "Undo by restore" changes only with a fresh,
   restore-tested backup of exactly what they touch. "Undo by reboot fallback"
   changes (kernel, NVIDIA, netplan) happen only in scheduled or manual mode,
   with the boot-once fallback. "Can't be undone" is never automatic.
10. **Notifications before, during and after**, with an advance notice so
    people can say "not tonight" (§2.6).

### 2.3 The three modes (plus "pinned"), precisely

- **Manual** — "Only when I press the button." Detection still runs, and
  updates appear as cards. Nothing is applied until a person presses "Update
  now". Busy and blackout still block the button; the person sees why and can
  override with a second confirmation.
- **Scheduled** — "At times I choose." The person picks windows (days plus a
  start time and length, in local time). Queued changes are applied inside a
  window, one at a time, when not busy. A change that doesn't fit before the
  window closes waits for the next window. A person still decides *what* is
  queued: approving a card adds it to the queue, or "auto-queue patch and
  security updates" can be turned on.
- **Automatic** — "Let Seraphina decide." Seraphina queues and applies changes
  that pass the policy (release age, reversibility class, allowed kinds such as
  patch/minor/security) when the service has been idle long enough. By default
  it still stays **inside a window**, which can be "any night 01:00-07:00".
  Owners who want truly any time can widen it. Limits: at most N changes per
  day, and never two in a row without a soak.
- **Pinned** (per service only) — "Never update this; keep version X." Detection
  still reports what is available. Applying requires unpinning, which is a
  recorded decision.

### 2.4 Busy detection

**Principle:** a busy probe is a veto. A service counts as idle only after
**all** of its probes have reported idle for `quiet_for` (default 5 min),
sampled every 30 s. While busy, Seraphina waits. When the window ends it skips
with a notification. It never kills work. Only a person can force it, with a
warning naming what will be interrupted.

| Probe kind | Source | Busy when | Notes |
| ---------- | ------ | --------- | ----- |
| `llama_slots` | llama.cpp `GET /slots` | any slot `is_processing: true` | on by default; `--no-slots` turns it off (https://github.com/ggml-org/llama.cpp/blob/8baabd5/tools/server/README.md lines 1027-1110) |
| `llama_metrics` | llama.cpp `GET /metrics` | `llamacpp:requests_processing > 0` or `llamacpp:requests_deferred > 0` | needs `--metrics` (same README, lines 1175-1192) |
| `http_health` | llama.cpp `/health`; TabbyAPI `/health` | 503 means loading or unhealthy: not idle, and a health failure | TabbyAPI `/health` reports only recorded errors, not activity (https://github.com/theroyallab/tabbyAPI/blob/a270c55/endpoints/core/router.py lines 68-76, `common/health.py`) |
| `open_connections` | `ss -Htn state established '( sport = :<port> )'` | count > threshold | the only generic signal for **TabbyAPI**, which tracks in-flight jobs only in its console status line (`common/status_display.py`), not over HTTP; keep-alive connections cause false "busy", so pair it with `gpu_activity` |
| `gpu_activity` | `nvidia-smi --query-gpu=index,utilization.gpu,memory.used --format=csv,noheader,nounits` | utilization above X% (default 10%) for the sample period | query flags from my own knowledge (**UNVERIFIED** against the installed driver) |
| `gpu_foreign_process` | `nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv` | a GPU process not owned by the service being changed (map PID → cgroup → unit/container) | catches a quantisation run (`llama-quantize`, `llama-imatrix`, EXL3 conversion) on a GPU the change would disturb (**UNVERIFIED** flags as above) |
| `process_match` | `/proc/*/cmdline` | a listed program is running | for jobs without a GPU process yet (downloads, conversions on CPU) |
| `inhibitor` | `systemd-inhibit --list` (or logind's D-Bus `ListInhibitors`) | a `block` lock whose `who` matches a configured pattern | **Model Factory jobs should take one**: `systemd-inhibit --what=shutdown:sleep --who=seraphina-model-factory --why="quantising X to EXL3" --mode=block <cmd>`, which also stops reboots by other tools that respect logind |
| `harness_jobs` | Seraphina's own job store | a factory job or chat is running on a provider served by this service, or waiting on its `max_concurrency` slot | the harness knows this exactly (§6, §20) |
| `http_json` | any `GET` plus a JSON path | the value matches a rule | a generic escape hatch for other apps |

- **Drain instead of wait (optional).** For services Seraphina fronts, it can
  stop starting new work: it holds new harness jobs for that provider, and
  later it could switch a serve or Cosmos route to a maintenance page. Then it
  waits for in-flight work to finish. Outside clients such as Open WebUI users
  can't be drained without a proxy, so they get the advance notice instead.
- **The GPU is shared.** With two NVLinked GPUs and one quantisation job, a
  change to *any* GPU service counts as busy if the job uses either GPU. The
  §29 resource broker should own "who has which GPU". Busy detection reads the
  broker's leases first and nvidia-smi second.

### 2.5 Blackout periods

- One-off ranges ("2026-11-02 to 2026-11-09: running a long fine-tune") and
  recurring ones ("weekdays 09:00-18:00"). Each has a reason shown to the
  person.
- A blackout blocks *starting* a change. A change already running finishes or
  rolls back; it is never left half-done.
- A blackout can name services or modules (for example "GPU & AI"), or be
  global. The global kind also defers apt and reboots, which is another reason
  Seraphina must own the apt timers (§2.1).

### 2.6 Notifications

- Channels: the cockpit inbox (always on), plus optional email, ntfy or Gotify
  reached over the tailnet, Telegram, and webhook. Watchtower and Diun use the
  shoutrrr URL format for many services; its status after Watchtower's archive
  is that "shoutrrr will remain active for the time being" (linuxiac summary
  above, **UNVERIFIED**).
- Events: `planned` (advance notice, default 60 min before a window with queued
  work, with "Skip tonight" and "Do it now" buttons), `started`, `succeeded`,
  `rolled_back` (with the incident link), `needs_you` (approval, stopped line,
  failed drill), `skipped` (busy all window, blackout, release age, or no fresh
  backup), and a `digest` (daily or weekly summary). Defaults: `rolled_back`
  and `needs_you` go to every channel; the others go to the inbox and digest.

### 2.7 Data model

Stored with the rest of Seraphina's config (config only; queue items and
history live in the job store):

```json
{
  "maintenance": {
    "timezone": "Europe/London",
    "default": {
      "mode": "scheduled",                       // manual | scheduled | automatic
      "windows": ["sun-early"],
      "auto_queue": ["security", "patch"],       // scheduled: what is queued without a person
      "automatic": { "allowed": ["security", "patch", "minor"], "max_per_day": 3 },
      "release_age_days": { "default": 3, "security": 0 },
      "soak_minutes": 15,
      "busy": { "quiet_for_minutes": 5, "sample_seconds": 30, "on_window_end": "skip" }
    },
    "windows": {
      "sun-early":  { "days": ["sun"], "start": "03:00", "minutes": 180 },
      "any-night":  { "days": ["mon","tue","wed","thu","fri","sat","sun"], "start": "01:00", "minutes": 360 }
    },
    "blackouts": [
      { "id": "finetune-nov", "from": "2026-11-02T00:00", "to": "2026-11-09T00:00",
        "scope": ["module:gpu-ai"], "reason": "Long fine-tune running" },
      { "id": "workdays", "recurring": { "days": ["mon","tue","wed","thu","fri"], "start": "09:00", "minutes": 540 },
        "scope": ["*"], "reason": "Working hours" }
    ],
    "security_fast_lane": false,
    "one_at_a_time": true,                       // shown, not editable: a safety rule
    "notify": { "advance_minutes": 60, "channels": ["inbox", "ntfy:matrix-alerts"],
                "routes": { "rolled_back": ["*"], "needs_you": ["*"], "default": ["inbox"] } }
  },
  "services": {
    "open-webui": {
      "maintenance": { "mode": "inherit", "windows": "inherit", "pin": null,
                       "depends_on": ["tabbyapi"],
                       "busy": [{ "kind": "open_connections", "port": 8080, "max": 0 }] }
    },
    "tabbyapi": {
      "maintenance": { "mode": "manual", "busy": [
        { "kind": "open_connections", "port": 5000, "max": 0 },
        { "kind": "gpu_activity", "gpus": [0, 1], "max_util": 10 },
        { "kind": "harness_jobs", "provider": "tabby" } ] }
    },
    "llama-server-qwen": {
      "maintenance": { "mode": "inherit", "busy": [
        { "kind": "llama_slots", "url": "http://127.0.0.1:9931/slots" },
        { "kind": "gpu_foreign_process", "gpus": [1] } ] }
    },
    "os-packages": {
      "maintenance": { "mode": "scheduled", "windows": ["sun-early"],
                       "exclude": ["^linux-", "^nvidia-", "^libnvidia-", "^cuda-"] }
    }
  }
}
```

**Resolving the effective policy** (shown in the UI with its source):
`service.maintenance.<field>` if set and not `"inherit"` → the module's setting
(for example "GPU & AI"), if modules get settings → `maintenance.default`. Then
apply the safety rules in fixed order: blackout → reversibility class (§30)
→ release age → backup freshness → one-at-a-time and soak → busy. The first
rule that blocks is the reason the person sees.

**Queue item** (in the job store, one per proposed change):
```json
{ "id": "M-0142", "service": "open-webui", "kind": "image.update",
  "from": "ghcr.io/open-webui/open-webui@sha256:…(0.12.1)", "to": "…(0.12.3)",
  "class": "undo_by_restore", "labels": ["minor"], "released_at": "2026-10-18T…",
  "earliest": "2026-10-21T00:00Z", "queued_by": "auto_queue:patch",
  "status": "waiting_window",   // detected|queued|waiting_window|waiting_busy|waiting_release_age|
                                // blocked_blackout|blocked_backup|running|soaking|done|rolled_back|failed|skipped
  "reason": "Next window: Sun 03:00", "attempts": [], "incident": null }
```

### 2.8 Plain-language UI

**Card: "How should updates happen?"** (global setting, three option cards in
the §25 style)
- **Only when I press the button** — "We check for updates and tell you. Nothing
  changes until you press Update." *Choose this when* you want full control.
- **At times I choose** — "Pick quiet times, like Sunday 3-6 am. Updates you
  approve wait for that time." *Choose this when* you know when the server is
  idle. **Recommended**
- **Let Seraphina decide** — "Safe updates are applied automatically when the
  server is quiet, within the times you allow. Risky ones still ask you."
  *Choose this when* you'd rather not think about it.

**Per-service list** (Updates & Enhancements section): one row per service.
- "Open WebUI — **Follows main setting** (at times I choose: Sun 03:00-06:00) ·
  [Change]"
- "TabbyAPI — **Only when I press the button** (set here) · [Use main setting]"
- "llama.cpp (Qwen) — **Pinned to b6512** · [Unpin]"

**Status lines** (on the service card, same words in notifications):
- "Open WebUI 0.12.1 → 0.12.3 is waiting for Sunday 03:00."
- "Waiting: TabbyAPI is generating a reply. We'll check again in 30 seconds and
  give up at 06:00."
- "Skipped tonight: the GPUs were busy quantising *Qwen3.8-Flash-Next EXL3 4.0
  bpw* all night. Next try: Sunday 03:00."
- "Not yet: version 0.12.4 came out today. We wait 3 days before installing new
  releases, unless it's a security fix."
- "Blocked: no tested backup of Open WebUI's data from the last 7 days. [Back up
  and test now]"
- "Done at 03:14. Health checks passed. You can undo this until Nov 4 (we keep
  the old version and a copy of the data)."

**Blackout dialog:** "Don't change anything from [date/time] to [date/time] ·
for [everything / GPU & AI / pick services] · because [reason]."

**"Do it now anyway"** (when busy or in a blackout): "TabbyAPI is answering a
request right now. Updating it will cut that answer off. [Wait until it's
quiet] [Interrupt and update]". The second button needs a typed confirmation
for model servers.

**Grid Card integration:** the Updates module shows counts (queued, waiting,
blocked, done this week), and each blocked item links to the rule that blocks
it.

### 2.9 Proposed defaults for matrix (for the owner to confirm)

- Global: **Scheduled**, Sunday 03:00-06:00 local, auto-queue security and
  patch updates, release age 3 days.
- GPU & AI module (TabbyAPI, llama.cpp servers): **Manual** until busy probes
  have been tested on matrix.
- OS packages: Seraphina runs apt in the window, with kernel, NVIDIA and CUDA
  excluded from automatic changes (they are class "Undo by reboot fallback").
  `needrestart` gets `override_rc` entries for model servers and the harness,
  so no restart happens outside Seraphina.

---

## Part 3. Restore first, then diagnose

### 3.1 The sequence

```
failure detected (health check after a change, a watchdog, or a person pressing "Something's wrong")
  → FREEZE: pause the change queue (stop the line); no other change may start
  → CAPTURE (time-boxed, default 120 s; ordered by "what the restore destroys" and "what is fastest")
  → RESTORE (least disruptive layer first) → VERIFY health → tell the person "back online at 03:21 (7 min)"
  → DIAGNOSE from the bundle while the service runs (read-only agent; may reproduce in a sandbox)
  → RE-FIX proposal → normal line (planner → reviewers → approver → gate → window)
  → close the incident only when the re-fix is applied and verified, or the person accepts the rollback as final
```

- The time box matters. Diagnosis can wait and downtime can't. If capture runs
  past its budget, Seraphina restores anyway and records what it could not
  capture. A **filesystem snapshot of the failed data is taken first**, because
  on ZFS/btrfs it takes seconds and preserves the most valuable evidence. The
  rest can then be read from that snapshot later.
- Each incident becomes a **commitment** in the §31.5 Housekeeping Tracker
  ("re-fix Open WebUI 0.12.3 upgrade"), kept outside any agent's context, so
  "we'll diagnose later" cannot be silently forgotten.

### 3.2 The forensic bundle

Stored at `<data_dir>/incidents/<id>/`, with a `MANIFEST.json` (each file's
size and sha256, as in §26's packages) and secrets redacted by the existing
redaction (§10). Secrets that must stay usable for a sandbox reproduction are
**fingerprinted**, never copied.

| Item | How | Why it must be captured *before* the restore |
| ---- | --- | -------------------------------------------- |
| Data snapshot of the failed state | `zfs snapshot pool/svc@failed-<id>`, `btrfs subvolume snapshot -r`, or an LVM snapshot; without these, a DB-consistent copy (next rows) | rollback overwrites the data; snapshots take seconds |
| SQLite DB of the failed state | Python stdlib `sqlite3.Connection.backup()` (or `sqlite3 .backup`); **never a bare file copy** while running: with WAL mode, recent writes can sit in `webui.db-wal` (https://github.com/open-webui/docs/blob/cd7fbd8/docs/tutorials/maintenance/backups.md line 30) | shows the half-migrated schema (`alembic_version`), which is the usual cause of "upgrade broke it" |
| Postgres / MariaDB of the failed state | `pg_dump -Fc`, `mariadb-dump --single-transaction` | same |
| Container logs | `docker logs --timestamps --since <change start> <c>` | **json-file logs are deleted with the container**; rollback by recreate destroys them |
| Container state | `docker inspect <c>` (state, health log, mounts, env with secrets fingerprinted, networks, restart count), `docker diff <c>` | lost on `docker rm`; `diff` shows files the new version wrote into its layer |
| The failed image | record `RepoDigests` and image ID; keep the image (§3.3, "keeper" container or `docker save`) | Cosmos and `--cleanup` prune it; a sandbox reproduction needs the exact image |
| Compose and app config | the compose file, the resolved `docker compose config`, app config files, Cosmos's container definition (`cosmos.md` §8: overwritten with no history) | rollback rewrites them |
| systemd | `systemctl status`, `systemctl cat <unit>` (with drop-ins), `systemctl show -p NRestarts,ExecMainStatus,Result`, `journalctl -u <unit> --since <change start> -o short-iso` | journald survives (if persistent), but the unit file is about to change |
| Packages | `/var/log/apt/history.log` and `term.log` lines since the change, `dpkg -l` diff, `apt-cache policy <pkgs>` | the next apt run rotates and overwrites them |
| /etc | `etckeeper` diff between the before-capture commit and now (`git -C /etc diff <before>`) | restore will commit over it |
| Network and dependencies | `docker network inspect` for the service's networks; probe results from the service to its dependencies (for example Open WebUI → TabbyAPI `/v1/models`) | §31.5's "connections broke" case; the recreate may change networks (`cosmos.md` §18) |
| GPU and kernel | `nvidia-smi -q`, kernel journal since the change (`journalctl -k`), Xid errors | the next driver load changes the state |
| Health check outputs | the exact requests and responses that failed | proves what "broken" meant |
| Blueprint after-capture | the §28 capture, tagged `failed-<change id>` | the before/after/failed triple is the diagnosis's main input |

### 3.3 Rollback mechanisms per layer on Ubuntu

| Layer | Mechanism | Restore action | Caveats (verified unless marked) |
| ----- | --------- | -------------- | -------------------------------- |
| **Container image** | pin by digest (`image@sha256:…`) in the Blueprint; keep the old image locally | set the image back to the old digest, then `docker compose up -d` | `docker image prune -a` removes "all images without at least one container associated to them" (https://github.com/docker/docs/blob/7e40e5a/content/manuals/engine/manage-resources/pruning.md); Cosmos prunes this way after updates (`cosmos.md` §11). To keep the old image, hold a **stopped keeper container** (`docker create --name keep-<svc>-<date> <old digest>`) or `docker save` it to the backup disk. A registry may still serve the old digest, but that isn't guaranteed. Docker 29 fresh installs use the **containerd image store** by default, while upgraded installs keep overlay2 until switched (https://github.com/docker/docs/blob/7e40e5a/content/manuals/engine/storage/containerd.md lines 11-14), so image files live in different places, which matters for snapshots. |
| **Container data** (volumes, bind mounts) | snapshot the dataset/subvolume holding them, taken with the app stopped or quiesced; restic for off-box copies | roll back the dataset (ZFS `zfs rollback` or clone; btrfs subvolume swap) or `restic restore` | **restore it together with the image**: "If the version you updated to ran a database migration, rolling back the container **does not** undo the migration ... you need to restore from a backup taken **before** the update" (Open WebUI, https://github.com/open-webui/docs/blob/cd7fbd8/docs/getting-started/updating.mdx). Open WebUI's data dir holds `webui.db`, `uploads/`, `vector_db/` (backups.md lines 89-102). Keep `WEBUI_SECRET_KEY` stable across recreates, or every session is logged out (updating.mdx line 135). |
| **Databases** | dumps before every change (SQLite backup API, `pg_dump -Fc`, `mariadb-dump --single-transaction`) | stop the app, restore the dump, start the old image | a dump of the *pre-change* state is the precondition (§30 "Undo by restore"); a dump of the *failed* state goes in the bundle |
| **/etc and unit files** | etckeeper: commits pending `/etc` changes before apt installs packages (`DPkg::Pre-Install-Pkgs`) and the package's changes after (`DPkg::Post-Invoke`); permissions in `/etc/.etckeeper` (https://manpages.debian.org/wheezy/etckeeper/etckeeper.8.en.html, search summary) | `git -C /etc checkout <commit> -- <file>`, then reload the unit | only `/etc`: unit files under `/lib/systemd` belong to packages; compose files elsewhere need the Blueprint's own git |
| **Packages** | `/var/log/apt/history.log`; old `.deb`s in `/var/cache/apt/archives` (if not cleaned); **Ubuntu archive snapshots**: `apt install <pkg> --update --snapshot YYYYMMDDTHHMMSSZ`, archive state from 2023-03-01, kept for at least 2 years, native from 23.10 (https://discourse.ubuntu.com/t/ubuntu-archive-snapshot-service-landing-page-and-documentation/44616, search summary) | `apt install pkg=<old version>` then `apt-mark hold` | downgrades can fail on maintainer scripts and database formats; for many packages a whole-OS snapshot is safer |
| **Whole OS: ZFS** | ZFS root with boot environments; **ZFSBootMenu** v3.1.0 (2026-01-01, built with OpenZFS 2.4.0) boots any environment or snapshot by kexec (https://github.com/zbm-dev/zfsbootmenu/blob/e155032/README.md, `docs/CHANGELOG.md`) | snapshot before the change; on failure, boot the previous environment (or `zfs rollback`) | **zsys is finished**: last upstream commit 2024-01-04 (https://github.com/ubuntu/zsys); Ubuntu publishes it only for 20.04, 22.04 and 24.04, not 26.04 or later (https://launchpad.net/ubuntu/+source/zsys). `zfs-linux` 2.4.4 is in the newest series (https://launchpad.net/ubuntu/+source/zfs-linux). A reported hang when upgrading to 25.10+ with root on ZFS (userspace newer than kernel module) is **UNVERIFIED** (search summary of the foundations-bugs list). |
| **Whole OS: btrfs** | **snapper** (universe, 0.10.6 in 26.04 and later, https://launchpad.net/ubuntu/+source/snapper); Debian's package ships an apt hook `/etc/apt/apt.conf.d/80snapper` that takes pre/post snapshots on apt runs (https://salsa.debian.org/debian/snapper/-/merge_requests/2, search summary, **UNVERIFIED** for Ubuntu's build) | `snapper rollback` or boot a snapshot | booting snapshots from GRUB needs **grub-btrfs**, which is **not in the Ubuntu archive** (PPA only, https://launchpad.net/ubuntu/+source/grub-btrfs). Read-only snapshots need `/var/log` or `/var` on a separate subvolume (https://github.com/Antynea/grub-btrfs/blob/38cd2fa/README.md). |
| **Whole OS: Timeshift** | rsync + hard links on any filesystem, or btrfs snapshots on an Ubuntu-type `@`/`@home` layout; upstream 26.09.0, Ubuntu 25.12.4 | restore from a live system or rescue media; "restoring backups from the running system requires a reboot" | "designed to protect only system files and settings"; home directories are excluded by default; "It is NOT a backup tool" (https://github.com/linuxmint/timeshift/blob/e7e54ab/README.md lines 5-113). Good fit for ext4 matrix; Docker data and models must be excluded or it fills the disk (my inference). |
| **Whole OS: LVM** | thick COW snapshot `lvcreate -s -L <size> -n pre-<id> vg/root`; thin snapshots in a thin pool | `lvconvert --merge vg/pre-<id>`; for a root LV the merge "is deferred until the next time the origin volume is activated", i.e. at reboot (https://github.com/lvmteam/lvm2/blob/0b58c33/man/lvconvert.8_pregen) | needs free VG space. Ubuntu Server's guided LVM ("scaled" policy) gives root half of a 20-200 GiB disk and **at most 100 GiB** of a larger one, leaving the rest of the VG free (`scaled_rootfs_size`, https://github.com/canonical/subiquity/blob/19d5a82/subiquity/common/storage/sizes.py lines 138-150). A thick snapshot that fills up becomes invalid (my own knowledge, **UNVERIFIED** here), so size it and watch it. |
| **Kernel / NVIDIA / boot** | keep the previous kernel and driver; `grub-reboot` boot-once plus a health service that marks success (§30) | the watchdog reboots into the last good entry | already designed in §30; ZFSBootMenu or snapper make it whole-system |
| **Network** | `netplan try` (auto-revert) (§30) | automatic after the timeout | serve and policy changes are separate: keep the previous `tailscale serve status --json` in the Blueprint and re-apply it |
| **Model files** | checksums plus keeping the previous quant until the new one is benchmarked; ZFS dataset snapshots are nearly free | point the server back at the old file | too large for restic every day; snapshot or keep-previous is the practical method |
| **Data backups** | **restic 0.19.1** (2026-07-05) or **borg 1.4.5**; **borg 2 is beta** ("DO NOT USE BORG2 FOR YOUR PRODUCTION BACKUPS", https://github.com/borgbackup/borg/blob/45f8b17/README.rst) | `restic restore <snap> --target /scratch/...` (`--include`, `--dry-run`, `--delete` exist, https://github.com/restic/restic/blob/dd09b6b/doc/050_restore.rst) | snapshots on the same disk are not backups; restic or borg to the backup drive and offsite (§28) |

**Choosing for matrix.** The root filesystem is unknown (open question 1).
- If **ext4 on LVM**: LVM snapshot of root before "Undo by restore" and
  "reboot fallback" changes, plus Timeshift rsync for browsable system history.
  Put Docker data and models on separate LVs or a ZFS pool to get per-service
  snapshots.
- If there is a **ZFS pool for data**: one dataset per service's data (for
  example `tank/apps/open-webui`). A per-service snapshot before every change is
  then the cheapest "Undo by restore" there is.
- A fresh install offered by Seraphina (§30): ZFS root with ZFSBootMenu, or
  btrfs with snapper. Not zsys.

### 3.4 Restore drills

"An untested backup is not a backup" (§30). Drill types, from cheap to
thorough:

1. **Repository check** (weekly): `restic check --read-data-subset=5%`, which
   reads a random 5% of pack files each run, or `n/t` to cover everything over
   t runs (https://github.com/restic/restic/blob/dd09b6b/doc/045_working_with_repos.rst
   lines 503-540). For borg: `borg check --verify-data` (my own knowledge,
   **UNVERIFIED** here).
2. **File restore to scratch** (weekly, rotating components):
   `restic restore latest --target /srv/seraphina/drill/<id> --include <path>`,
   then compare checksums with the live copy or the backup manifest.
3. **App-level drill** (monthly per service): restore the latest backup of
   service X's data into a scratch dataset. Start the **same image digest** on
   an isolated Docker network with no published ports and no GPU. Run its
   health check and a smoke query (for Open WebUI: the DB opens, user count > 0,
   the latest chat is readable). Record pass or fail. This proves the restore
   works, not just that the data exists.
4. **Restore-button drill** (quarterly, in a window, on a low-stakes service):
   take a real before-snapshot, make a harmless change, press Restore, and time
   it. This proves the whole path of §3.1, including the capture time box.
5. **Rebuild-guide drill** (yearly, or after major changes): follow the §28
   rebuild guide on a spare disk or VM far enough to boot and restore one
   service.

Each drill writes a record into the Blueprint and the job store:
`{component, kind, backup_id, started, finished, restore_seconds, result,
evidence_path}`. Two numbers per component are shown in plain words: **how
much you could lose** (age of the newest good backup) and **how long a restore
took** last time. §30's "Undo by restore" precondition then becomes a code
check: *a drill of kind ≥ 3 for this component passed within N days (default
30), and a backup newer than the change exists.* Otherwise the change is
"Blocked: no tested backup" (§2.8).

### 3.5 Data model for restore points and incidents

```json
{ "restore_point": { "id": "R-0142-pre", "change": "M-0142", "taken_at": "…",
    "layers": [
      { "layer": "image",   "service": "open-webui", "digest": "sha256:…", "kept_by": "container:keep-open-webui-20261021" },
      { "layer": "data",    "service": "open-webui", "method": "zfs", "ref": "tank/apps/open-webui@pre-M-0142" },
      { "layer": "db",      "service": "open-webui", "method": "sqlite-backup", "path": "…/webui.db", "sha256": "…" },
      { "layer": "etc",     "method": "etckeeper", "commit": "a1b2c3d" },
      { "layer": "blueprint", "commit": "…" } ],
    "drill": { "id": "D-0098", "passed_at": "2026-10-12T03:40Z" },
    "expires": "2026-11-04T00:00Z" },
  "incident": { "id": "I-0031", "change": "M-0142", "detected_at": "…", "detected_by": "health:openwebui-models",
    "queue_frozen": true, "capture": { "budget_s": 120, "took_s": 74, "missing": [] , "bundle": "incidents/I-0031/" },
    "restored_to": "R-0142-pre", "restored_at": "…", "downtime_s": 420,
    "diagnosis": { "status": "running", "agent_run": "…" },
    "refix": { "status": "open", "commitment": "HK-0207" } } }
```

---

## Open questions

1. **matrix's filesystems.** Root on ext4, LVM, ZFS or btrfs? Where do Docker
   data, model files and app data live? Is there free VG space? This decides
   §3.3's choice.
2. **Ubuntu version on matrix** (24.04 or 26.04?), the Docker version (≥ 28.0?),
   and its firewall backend. Is UFW active?
3. **Does Ubuntu's periodic unattended upgrade currently run on matrix**
   (`/etc/apt/apt.conf.d/20auto-upgrades`), with automatic reboot? Does the
   owner agree that Seraphina takes it over?
4. **Does matrix have a global IPv6 address**, and does the router's IPv6
   firewall block inbound traffic? Is there UPnP on the router?
5. **Is matrix tagged in the tailnet** or owned by the owner's user? Who else is
   on the tailnet (family, other devices, shared nodes)? Does the owner want
   Tailscale policy to define Seraphina roles through app capabilities?
6. **Are agents allowed to run as a separate Unix user** (or always under
   bwrap)? That is the precondition for "strong" Tailscale identity (§1.3 (i)).
7. **Are Tailscale Services generally available now**, and on which plan? If
   yes, one Service name per app gives clean separate origins.
8. **Which processes use the GPUs during quantisation** (EXL3 conversion,
   `llama-quantize`, imatrix)? Will Model Factory jobs adopt the inhibitor-lock
   convention and the resource broker's leases?
9. **How do clients reach TabbyAPI** (Open WebUI, DSH, Qwen Code, the harness)?
   That decides whether `open_connections` is a usable busy signal or needs a
   small proxy that counts in-flight requests.
10. **What is "healthy" for each service?** Seraphina needs one or two concrete
    checks per service, for example Open WebUI listing models from Tabby,
    before automatic mode can be trusted.
11. **Notification channel** the owner actually reads (ntfy over the tailnet,
    email, Telegram?).
12. **The policy-file `tests` syntax, the Tailscale web client and SSH flags, and
    the `docker info` firewall-backend field** were not readable from here. Check
    them on tailscale.com and docs.docker.com.

## Sources

Source and documentation repositories (read at the commits shown):
- Tailscale: https://github.com/tailscale/tailscale (commit `47743a8`, 2026-10-20) —
  `ipn/ipnlocal/serve.go`, `ipn/ipnlocal/local.go`, `ipn/ipnlocal/cert.go`,
  `ipn/serve.go`, `ipn/localapi/localapi.go`, `ipn/ipnserver/server.go`,
  `ipn/ipnstate/ipnstate.go`, `client/local/local.go`, `paths/paths.go`,
  `cmd/tailscale/cli/{serve_v2,funnel,whois}.go`, `cmd/tailscaled/{tailscaled.go,tailscaled.defaults,tailscaled.service,tailscale-wait-online.service}`,
  `net/portmapper/{portmapper,upnp}.go`, `tailcfg/tailcfg.go`
- Docker docs: https://github.com/docker/docs (commit `7e40e5a`, 2026-10-21) —
  `engine/network/{packet-filtering-firewalls,port-publishing,firewall-iptables,firewall-nftables}.md`,
  `engine/storage/containerd.md`, `engine/manage-resources/pruning.md`
- Cockpit: https://github.com/cockpit-project/cockpit (commit `4bc7d15`) —
  `src/systemd/cockpit.socket.in`, `doc/modules/guide/pages/listen.adoc`,
  `doc/modules/man/pages/cockpit.conf.5.adoc`
- llama.cpp: https://github.com/ggml-org/llama.cpp/blob/8baabd5/tools/server/README.md
- TabbyAPI: https://github.com/theroyallab/tabbyAPI (commit `a270c55`) —
  `endpoints/core/router.py`, `common/health.py`, `common/config_models.py`, `common/status_display.py`
- Open WebUI docs: https://github.com/open-webui/docs (commit `cd7fbd8`) —
  `docs/getting-started/updating.mdx`, `docs/tutorials/maintenance/backups.md`,
  `docs/troubleshooting/manual-database-migration.md`
- unattended-upgrades: https://github.com/mvo5/unattended-upgrades (commit `5877bee`) — `data/`
- apt timers: https://github.com/Debian/apt (mirror, commit `6b12812`) — `debian/apt-daily*.timer`
- systemd: https://github.com/systemd/systemd (commit `f34f98c`) —
  `man/systemd.timer.xml`, `man/systemd-inhibit.xml`, `man/systemctl.xml`
- Watchtower: https://github.com/containrrr/watchtower (commit `ca0e86e`, 2025-12-17) — `README.md`, `docs/`
- Diun: https://github.com/crazy-max/diun (commit `5a47b2a`) — `docs/providers/docker.md`, `docs/config/watch.md`
- Renovate: https://github.com/renovatebot/renovate (commit `da3366e`) —
  `docs/usage/key-concepts/scheduling.md`, `docs/usage/configuration-options.md`
- TrueNAS docs: https://github.com/truenas/documentation (commit `afa4dab`) —
  `content/SCALE/SystemSettings/Update/UpdateScreens.md`,
  `content/SCALE/SystemSettings/Boot/BootScreens.md`, `content/SCALE/Apps/AppsScreens.md`
- zsys: https://github.com/ubuntu/zsys (last commit `25de0d4`, 2024-01-04)
- ZFSBootMenu: https://github.com/zbm-dev/zfsbootmenu (commit `e155032`; tag v3.1.0)
- snapper: https://github.com/openSUSE/snapper (commit `521a9ed`)
- grub-btrfs: https://github.com/Antynea/grub-btrfs (commit `38cd2fa`)
- Timeshift: https://github.com/linuxmint/timeshift (commit `e7e54ab`, 26.09.0)
- LVM: https://github.com/lvmteam/lvm2 (commit `0b58c33`) — `man/lvconvert.8_pregen`
- subiquity: https://github.com/canonical/subiquity (commit `19d5a82`) — `subiquity/common/storage/sizes.py`
- restic: https://github.com/restic/restic (commit `dd09b6b`; v0.19.1) — `CHANGELOG.md`, `doc/045_working_with_repos.rst`, `doc/050_restore.rst`
- borg: https://github.com/borgbackup/borg (master `45f8b17`; stable tag 1.4.5) — `README.rst`, `docs/changes.rst`
- Release tags checked with `git ls-remote`: tailscale v1.104.2, moby `docker-v29.9.2`, borg 1.4.5, zfsbootmenu v3.1.0, restic v0.19.1

Ubuntu package pages (launchpad.net HTML, read 2026-10-21):
- https://launchpad.net/ubuntu/+source/zsys, https://launchpad.net/ubuntu/+source/zfs-linux,
  https://launchpad.net/ubuntu/+source/snapper, https://launchpad.net/ubuntu/+source/timeshift,
  https://launchpad.net/ubuntu/+source/grub-btrfs

Pages known only through web-search summaries (could not be fetched here):
- Tailscale: https://tailscale.com/kb/1312/serve, https://tailscale.com/kb/1153/enabling-https,
  https://tailscale.com/kb/1223/funnel, https://tailscale.com/kb/1068/acl-tags,
  https://tailscale.com/kb/1538/grants-syntax, https://tailscale.com/kb/1458/grant-examples,
  https://tailscale.com/blog/services-beta, https://tailscale.com/docs/features/tailscale-services
- Ubuntu: https://discourse.ubuntu.com/t/needrestart-changes-in-ubuntu-24-04-service-restarts/44671,
  https://discourse.ubuntu.com/t/ubuntu-24-04-unattended-upgrades-failed-to-restart-nginx/67909,
  https://discourse.ubuntu.com/t/ubuntu-archive-snapshot-service-landing-page-and-documentation/44616
- Debian snapper apt hook: https://salsa.debian.org/debian/snapper/-/merge_requests/2
- etckeeper: https://manpages.debian.org/wheezy/etckeeper/etckeeper.8.en.html
- Synology: https://www.synology.com/knowledgebase/DSM/help/DSM/AdminCenter/system_dsmupdate
- TrueNAS forum: https://forums.truenas.com/t/disabling-of-daily-update-check-is-not-respected/25761
- Watchtower end of maintenance: https://linuxiac.com/?p=204037
- Open WebUI + Tailscale header SSO: https://docs.openwebui.com/tutorials/auth-sso/tailscale.md
