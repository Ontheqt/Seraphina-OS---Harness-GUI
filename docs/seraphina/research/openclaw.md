# Research: OpenClaw, and whether Seraphina should use it to wake and message

Research notes for **Seraphina OS Harness**. They answer this request from the
owner: "I want to be able to give the model a way to message me and be awake
when it wants." Gemini suggested two routes. One is **OpenClaw**, a popular
open-source agent that connects a model to WhatsApp, Telegram and other chat
apps. The other is a **custom Python wake loop**: a `.wake-now` file, Ollama, a
Markdown memory file, and Telegram, Pushover or Twilio for the message.

This file covers OpenClaw only: what it is, how it works, what it needs from the
network, and its security record. It then judges three options against the
owner's server:

- A. run OpenClaw on the server;
- B. run OpenClaw in a sandbox, only as a messaging gateway beside the harness;
- C. build the pattern into the harness.

Related notes: [platforms-ai.md](platforms-ai.md) §3.6, which has an earlier
short summary of OpenClaw's incidents, and
[commitments.md](commitments.md), which covers the Housekeeping Tracker. The
v2 design's notification channel (§6.6), secrets (§9.5) and prompt-injection
rules (§9.6) are in [SERAPHINA_OS_HARNESS.md](../SERAPHINA_OS_HARNESS.md).

Written 2026-10-21.

## How these notes were made, and their limits

- **Read from source.** I made a shallow clone of `openclaw/openclaw` on
  2026-10-21 at commit [`e0a0bbcf`][repo-commit] (committed
  2026-10-21 04:28 -07:00; `package.json` version `2026.10.5`). The repository
  ships its full documentation under `docs/`. Statements about how OpenClaw
  behaves come from those files unless a line says otherwise. I checked a few
  of them in the TypeScript source, for example the default port 18789, the
  `loopback` bind default and the 30-minute heartbeat (`src/gateway/server-start.ts`,
  `src/gateway/net.ts`, `src/auto-reply/heartbeat.ts`). Paths are given relative
  to the repository root. The clone was read as data. Nothing was installed or
  run.
- **Read from GitHub's web pages** (fetched 2026-10-21): the repository front
  page, the releases page, the repository's security advisories, and the GitHub
  Advisory Database. These are primary sources for counts and advisory text.
- **Not reachable from the research sandbox:** docs.openclaw.ai,
  trust.openclaw.ai, nvd.nist.gov, api.osv.dev, en.wikipedia.org,
  thehackernews.com, theregister.com, unit42.paloaltonetworks.com,
  darkreading.com, labs.cloudsecurityalliance.org, docs.ntfy.sh and several
  blogs. For those I used search-engine extracts and name the outlet. Anything
  taken only from such extracts is marked **UNVERIFIED**. This applies to most
  of the exposure counts, malware counts and incident stories in §3, which
  come from vendor blogs and the press, not from data I could check.
- The project moves very fast: one release covered 2,250 pull requests
  (§1.4). Expect details below to drift within weeks.

---

## 0. The answer in one screen

**What OpenClaw is.** An MIT-licensed, self-hosted personal AI assistant
written in TypeScript (Node 24 or 26). It began as Peter Steinberger's
WhatsApp relay in November 2025 and has been called OpenClaw since 30 January
2026. A non-profit foundation now runs it. It is one of the most-starred
repositories on GitHub (392.6k stars). One long-running **Gateway** process
holds the chat-app connections, the model connections, memory files, skills and
a scheduler. Its **heartbeat** wakes the agent every 30 minutes by default. The
agent replies `NO_REPLY` or calls `heartbeat_respond` with `notify: false` when
nothing needs you, and sends you a message when something does. It does what
Gemini described, and much more.

**Option A: should the owner run OpenClaw on this server? No.**

- **By its own trust model, OpenClaw is a single-operator tool that trusts the
  person driving it with the whole machine.** A local onboarding gives the agent
  every tool (`tools.profile: "full"`). Commands run on the host with no
  approval prompt (`security="full"`, `ask="off"`). The sandbox is off by
  default. Prompt injection on its own is out of scope for vulnerability
  reports. So is "a malicious plugin after a trusted operator installs or
  enables it" (§3.1).
- **Its own threat model rates three risks Critical (P0)** and says they are
  not solved: direct prompt injection, malicious skills from its marketplace,
  and credential theft by skills. On the last two it says skills run "with
  agent privileges and no execution sandboxing" (§3.4).
- **The record is heavy.** About 722 advisories have been published in the
  repository in about nine months. The GitHub Advisory Database lists 771 npm
  advisories matching "openclaw", 273 of them High and 27 Critical. They
  include a one-click remote-code-execution bug that worked even against a
  gateway bound only to localhost (CVE-2026-25253). They also include a
  sandbox bypass through the **heartbeat itself** (CVE-2026-41329). In
  February 2026 between 40,000 and 135,000 gateways were found exposed to the
  internet, and hundreds of malicious skills were found on ClawHub (§3).
- **Its documentation warns against the setup the owner would actually have.**
  It says not to use "older/weaker/smaller tiers for tool-enabled agents or
  untrusted inboxes" (§2.7). Local models on TabbyAPI are such a tier.
- **It breaks this server's rules by design.** A personal assistant that reads
  messages from a chat app (untrusted input), has your personal and health data
  in its memory (sensitive data), and can run commands and send messages (act
  and communicate) has all three legs that §9.6's Rule of Two forbids in one
  session. Every message it sends you also goes through Telegram's or
  WhatsApp's servers.

**Option B: OpenClaw in a sandbox, only as a messaging gateway? Possible, but
not worth it.** It can be boxed in: a separate OS user or rootless container,
the `minimal` tool profile, no skills, one channel, and the harness calling
`openclaw message send`. You would still run about 4 million lines of
fast-moving code and a lockfile of about 1,861 npm packages, and you would need
to patch it every week. In return you get a feature that ntfy or a few dozen
lines of standard-library Python already give you. Owner replies would also
still go through an OpenClaw agent turn unless someone writes a TypeScript
plugin to intercept them (§4.3). The only case where B could be worth it is
WhatsApp, and then only on a separate box that holds nothing (§4.3).

**Option C: build the pattern into the harness. Recommended.** The harness
already has most of the parts: an EventBus, chats with resumable sessions,
agent persona files, provider semaphores for the single-slot local model
server, env-file secrets, the Housekeeping Tracker, the power calendar and
holds (§6.3), and a planned ntfy channel that does not depend on the container
engine (§6.6). What is missing is a small wake queue, a strict JSON action
schema, an outbox and a page in the GUI. §4.4 sketches them. §4.5 lists about
fifteen OpenClaw mechanisms worth copying, such as the silent-by-default reply
contract, active hours, flood guards and catching up at boot instead of
replaying missed runs.

**Gemini's description is broadly right but out of date in places** (§5). The
`HEARTBEAT.md` file is gone (moved into a database "monitor scratch" in
v2026.8.1). WhatsApp support goes through an unofficial WhatsApp Web client.
TabbyAPI is not a named provider. And "costs nothing" ignores that every
background turn takes the one generation slot that Open WebUI and the harness
share.

---

## 1. What OpenClaw is

### 1.1 Lineage and name

From the project's own history page (`docs/start/lore.md`):

| When | Name | Note |
| ---- | ---- | ---- |
| before Nov 2025 | **Warelay** | "a sensible name for a WhatsApp gateway" |
| 25 Nov 2025 to 27 Jan 2026 | **Clawd / Clawdbot** | the assistant persona "Clawd" living in "Clawdbot" |
| 27 Jan 2026 | **Moltbot** | renamed after "Anthropic sent a polite email asking for a name change (trademark stuff)" |
| 30 Jan 2026 | **OpenClaw** | "The Great OpenClaw Migration"; GitHub moved to `github.com/openclaw/openclaw` |

During the renames, bots took over the old X handle and briefly the creator's
personal GitHub name (same page). Early advisories are filed against the npm
package `clawdbot` (for example CVE-2026-25253, §3.3), so old installs may show
up under either name.

### 1.2 Who runs it

- **Creator:** Peter Steinberger (`steipete`). The README says OpenClaw "was
  built for **Molty**, a space lobster AI assistant, by Peter Steinberger and
  the community."
- **Steinberger joined OpenAI in mid-February 2026** to lead work on personal
  agents. Sam Altman said OpenClaw would "live in a foundation as an open
  source project that OpenAI will continue to support" (press reports:
  [The Decoder][decoder-openai], [MacStories][macstories-openai]; the exact day
  differs between outlets, **UNVERIFIED**). He still commits: the October 2026
  release notes credit `@steipete` on many changes.
- **Governance:** "developed in the open by the OpenClaw Foundation, an
  independent 501(c)(3). The Foundation employs the core team and signs
  releases ... OpenAI is a donor, not an owner." Listed donors: Amazon, Lobster
  Computer Company, Offline Holdings, OpenAI, Red Hat and the University of
  Michigan. Infrastructure support comes from Blacksmith, Convex, GitHub,
  NVIDIA and Vercel (`README.md`). Red Hat describes itself as a founding
  member of the foundation ([Red Hat blog][redhat-foundation]).
- **Security maintainers** include "engineers and security researchers from
  organizations such as NVIDIA and Tencent" (`SECURITY.md`). The maintainer
  list lives at openclaw.org/people (`CONTRIBUTING.md`), which I could not
  reach.
- **NVIDIA NemoClaw** (announced at GTC on 16 March 2026) is a separate,
  Apache-2.0 reference stack that runs OpenClaw inside NVIDIA's OpenShell
  sandbox. It adds network and filesystem policy and a "privacy router" for
  model calls. Its own docs and reviewers call it early alpha
  ([NVIDIA press release][nvidia-nemoclaw], [NemoClaw docs][nemoclaw-docs];
  maturity **UNVERIFIED**). OpenClaw itself now has an OpenShell sandbox
  backend (`docs/gateway/sandboxing/openshell-backend.md`).
- One aggregator reported an "OpenClaw Enterprise" launch on 29 September 2026
  together with Red Hat and NVIDIA. I found no primary source for it, and the
  README says the foundation "has no paid tier, hosted service, or token"
  (**UNVERIFIED**; probably a mix-up with NemoClaw).

### 1.3 Licence

MIT (`LICENSE`, README badge). Channel plugins are separate npm packages, for
example `@openclaw/whatsapp` and `@openclaw/signal`, published by the same
project. WhatsApp support depends on Baileys, an unofficial WhatsApp Web
client library (§2.6).

### 1.4 Popularity, pace and size

| Measure | Value | Source |
| ------- | ----- | ------ |
| Stars / forks | 392.6k / 82.5k | GitHub repository page, 2026-10-21 |
| Commits / open issues / open PRs | 116,111 / 5k+ / 3.4k | same |
| Latest stable | **v2026.10.5**, 18 Oct 2026 | [releases page][gh-releases] |
| Newest beta | v2026.10.10-beta.3, 21 Oct 2026 | same |
| Size of v2026.10.5 | "2,250 pull requests, 411 direct commits, and 637 contributors" | `docs/releases/2026.10.5.md` |
| "OpenClaw 2.0" | the name for v2026.8.1 (rebuilt web UI, onboarding, memory) | `docs/releases/index.md` |
| Update channels | stable, extended-stable (trailing, package-only), beta, dev | `docs/install/development-channels.md` |
| Runtime | Node 24.16+ or 26.1+ | `package.json` `engines` |
| Code (my count) | 23,345 non-test `.ts` files, about 4.15 million lines in `src/`, `extensions/` and `packages/`; about 180 plugin packages under `extensions/`; 76 direct dependencies; 1,861 packages in `pnpm-lock.yaml` | clone at `e0a0bbcf` |

Versions are dates (`2026.M.D`). Several betas and stable releases ship each
week. Gateway auto-update is off by default. A daily update check is on by
default and sends the OpenClaw version, operating system, Node version and CPU
architecture (`docs/gateway/telemetry.md`,
`docs/install/updating/automatic-updates.md`).

### 1.5 Context the owner may have heard about

- **Moltbook**, a social network for AI agents where most accounts were
  OpenClaw agents, left its Supabase database readable and writable by anyone.
  Wiz reported about 1.5 million agent API tokens and about 35,000 email
  addresses exposed, plus private messages, some holding plaintext OpenAI keys.
  It was disclosed on 31 January 2026 and fixed within hours. The flaw was in
  Moltbook, not in OpenClaw ([SecurityWeek][securityweek-moltbook],
  [Business Today][bt-moltbook]; figures **UNVERIFIED**).
- **China** warned state agencies, state-owned enterprises and banks against
  installing OpenClaw in March 2026. CNCERT recommended running it in a
  container, restricting access to the management port, strict
  authentication and limiting plugins. MIIT urged organizations to audit their
  public exposure ([HackMag on CNCERT][hackmag-cncert], [News24][news24-china];
  **UNVERIFIED**).
- **Anthropic** stopped third-party harnesses such as OpenClaw from drawing on
  Claude subscription limits from 4 April 2026. API keys were unaffected
  ([InfoWorld][infoworld-anthropic]; **UNVERIFIED**). This does not matter for
  an all-local setup.

---

## 2. Architecture

### 2.1 The Gateway daemon

`docs/concepts/architecture.md`:

- "A single long-lived **Gateway** owns all messaging surfaces (WhatsApp via
  Baileys, Telegram via grammY, Slack, Discord, Signal, iMessage, WebChat)."
  One Gateway runs per host, under systemd or launchd.
- The CLI, the Control UI (a web app), the terminal UI and automations connect
  to it over **WebSocket** on **`127.0.0.1:18789`** by default. The same port
  serves HTTP: the Control UI, agent-authored "canvas" widget pages,
  OpenAI-compatible endpoints (`POST /v1/chat/completions`, `/v1/responses`),
  `POST /tools/invoke` and the webhook paths (§2.2).
- Phones and desktops can pair as **nodes**. Nodes expose commands such as
  `camera.*`, `screen.record`, `location.get`, `sms.send`, and on iOS an opt-in
  **HealthKit summary** (`health.summary`) (`docs/nodes/command-policy.md`).
- State lives in SQLite under `~/.openclaw/`: config `openclaw.json`,
  `credentials/`, per-agent databases with transcripts, auth profiles and memory
  indexes, plus managed skills (`docs/concepts/agent-workspace.md`). Postgres is
  an option for team deployments.
- One `GatewayScheduler` drives every timed wake: heartbeats, automations and
  plugin services. Stored deadlines survive restarts.

### 2.2 How it wakes

OpenClaw covers every trigger the owner listed. From `docs/automation/` and
`docs/gateway/heartbeat.md`:

| Owner's trigger | OpenClaw mechanism | Details |
| --------------- | ------------------ | ------- |
| On a schedule | **Heartbeat**: a system-owned automation that runs a turn in the agent's main session every **30 minutes** by default (every hour with Anthropic OAuth) | `every`, `activeHours` (local time), `target` (`owner`, `last`, `none` or a channel), `lightContext` (skip workspace files), `isolatedSession` (no history), optional `model`. A "monitor scratch" text replaced `HEARTBEAT.md` in v2026.8.1 |
| On a schedule | **Automations** (`openclaw automations`, alias `cron`): one-shot (`--at`, `--delete-after-run`), interval or cron; main, isolated, named or current session; deliver to a channel, a webhook or nowhere | jobs persist in SQLite; run history; retries |
| A file or webhook poke | **Gateway HTTP hooks**, off by default: `POST /hooks/wake` (queue a short system event for the main session, `mode: "now"` or `"next-heartbeat"`), `POST /hooks/agent` (run a turn, isolated by default), mapped `POST /hooks/<name>` with templates or JS transforms | own bearer token, which must differ from the Gateway token; POST only; tokens in query strings rejected; idempotency keys; HTTP 503 when the queue is full rather than dropping events |
| Events | **Event triggers**: condition scripts on a schedule or on a `stream` (a supervised command's output lines, for example `tail -F` or `websocat`); exits of background commands; IMAP mail (sender-gated reader sessions); Gmail via Google Pub/Sub; MCP events; internal hooks (`message:received`, `/new`, boot) | condition scripts and script payloads "run unattended by default with the owning agent's **full tool policy, including `exec`**" (`docs/automation/cron-jobs/schedules.md`) |
| The agent asks to be woken later | The agent's `automations` tool: `add` an `at` job; `next_check in:"30m"` lets a paced job set its own next run, "silently clamped" to configured bounds; `/loop [interval] <prompt>` in chat self-paces between 1 minute and 1 hour; **standing intents** for "when X is mentioned, remind me" | non-owner senders cannot use the `cron` or `gateway` tools (`docs/gateway/security/hardened-baseline.md`) |
| At boot | `BOOT.md` checklist via the `boot-md` hook | optional |

**Behaviour around a nightly power-off** (`docs/automation/cron-jobs/how-it-works.md`):

- "On Gateway startup, overdue agent-turn jobs and jobs that await a heartbeat
  are **rescheduled instead of replayed immediately**." So a 02:00 reminder runs
  after the 07:00 boot, not in a burst with every other missed job.
- If a run was cut off by a restart before it delivered, it becomes
  "interrupted with delivery **Unknown**". It gets one fresh retry only when
  the job is time-scheduled, enabled and unchanged. "Possible prior delivery ...
  [is] not automatically replayed."
- If the main session restarts before it picks up a queued event, OpenClaw
  "records one interrupted failure and removes the pending event; it does not
  report success or replay the reminder."

**Rate limits on event wakes** (heartbeat doc): "a 30-second minimum between
event turns, and a flood guard after five starts within 60 seconds."
Identical pending wakes are coalesced. When the delivery channel is down,
background polls stop calling the model and back off, from one minute up to
the heartbeat interval.

**Wake text is trusted.** "Wake text is a system event, not an isolated,
safety-wrapped email reader turn. Send only a short notification you control.
Route raw email, documents, or other untrusted content through an `agent`
action with a restricted reader" (`docs/automation/cron-jobs/webhooks.md`).

### 2.3 The "should I bother you?" contract

From `docs/gateway/heartbeat.md`:

- Default heartbeat prompt, sent word for word as the user message: "Follow the
  heartbeat monitor scratch context when provided. Recurring tasks are
  automations ... **Do not infer or repeat old tasks from prior chats.** If
  nothing needs attention, reply NO_REPLY."
- The model can call `heartbeat_respond` with `notify: false` (stay silent; the
  result is kept as bounded context for your next turn) or `notify: true` plus
  `notificationText`. The structured call takes precedence over text. A legacy
  `HEARTBEAT_OK` reply is also accepted and suppressed.
- Alerts go to the **owner's direct message only**, taken from
  `commands.ownerAllowFrom` (for example `["telegram:123456789"]`). They are
  "never [sent] to a group". With no owner route, polls skip with
  `reason=no-route`.
- A turn with "no usable response, acknowledgment, pending continuation, or
  confirmed delivery is recorded as failed". Silence is never mistaken for
  success.

### 2.4 Memory and persona files

The agent workspace defaults to `~/.openclaw/workspace`
(`docs/concepts/agent-workspace.md`, `docs/concepts/memory.md`):

| File | Role | Loaded |
| ---- | ---- | ------ |
| `AGENTS.md` | operating instructions and "standing orders"; a `## Tools` section for local notes | every session |
| `SOUL.md` | "persona, tone, and boundaries" (the "soul" file Gemini mentions) | every session |
| `USER.md` | facts about the user, written as dated directives that stay active until superseded | every session, 4,000-character cap |
| `IDENTITY.md` | name, vibe, emoji | first-run ritual |
| `MEMORY.md` | curated long-term facts and decisions | main private session only, not groups |
| `memory/YYYY-MM-DD.md` | daily notes | today's and yesterday's on `/new`; the rest via `memory_search` |
| `DREAMS.md` | "Dream Diary" of background consolidation | for human review |
| `BOOT.md`, `BOOTSTRAP.md` | boot checklist, first-run ritual | optional |
| `skills/` | workspace skills, highest precedence | per turn |

- Injected files are cut at `bootstrapMaxChars` (20,000) and
  `bootstrapTotalMaxChars` (60,000).
- **Dreaming is on by default.** It is a background job that "moves strong
  short-term signals into durable memory", promoting daily notes into
  `MEMORY.md` (`docs/concepts/dreaming.md`).
- "The workspace is the **default cwd**, not a hard sandbox ... absolute paths
  can still reach elsewhere on the host unless sandboxing is enabled."
- OpenClaw ran an experiment that pulled "inferred commitments" out of
  conversations and followed them up through the heartbeat. It was removed in
  v2026.8.1. Explicit automations replace it (`docs/automation/index.md`). This
  matters for the Housekeeping Tracker: OpenClaw also gave up on guessing
  commitments and kept explicit ones.

### 2.5 Skills, plugins and ClawHub

- A **skill** is a `SKILL.md` instruction pack, often with scripts, loaded into
  the prompt (`docs/tools/skills.md`). A **plugin** is TypeScript code that runs
  inside the Gateway process. Plugins provide channels, providers, tools and
  hooks (`docs/tools/plugin.md`).
- **ClawHub** (clawhub.ai) is the public registry for both. "ClawHub packages
  and OpenClaw's bundled/official catalog are trusted sources" for installs.
  Even the official **WhatsApp and Signal channel plugins install from ClawHub
  by default**; `npm:@openclaw/...` installs from npm instead
  (`docs/channels/whatsapp.md`, `docs/channels/signal.md`).
- Defences on the registry today: a 14-day minimum GitHub account age, static
  and pattern scanning, an LLM risk review, VirusTotal and "ClawScan" results
  shown before install, and a `clawhub.skill.verify.v1` trust envelope that
  `openclaw skills verify` checks. An operator-run `security.installPolicy`
  command can veto installs (`docs/security/THREAT-MODEL-ATLAS.md` §4.1,
  `docs/tools/skills.md`).
- Docs warning: "Treat third-party skills as **untrusted code**. Read them
  before enabling." Internal hooks "are trusted code, not sandboxed scripts.
  They run with the Gateway process's filesystem, network, and environment
  access" (`docs/automation/hooks.md`).

### 2.6 Channels

There are 20+ channels: WhatsApp, Telegram, Signal, Discord, Slack, iMessage
(BlueBubbles), Google Chat, Teams, Matrix, Mattermost, IRC, Nostr, SMS (Twilio)
and others (`docs/channels/`). Unknown senders on direct-message channels get a
**pairing code** by default and are ignored until the owner runs
`openclaw pairing approve <channel> <code>`.

| Channel | How it connects | Inbound port? | What leaves the machine |
| ------- | --------------- | ------------- | ----------------------- |
| Telegram | Bot API through grammY; **long polling by default**, webhook mode optional | no (yes for webhook mode) | every message, in clear text to Telegram's servers. Bot chats are not end-to-end encrypted; only Secret Chats are, and bots cannot use them ([Mozilla][mozilla-telegram]; community sources) |
| WhatsApp | **WhatsApp Web through Baileys**, a reverse-engineered linked-device client. "There is no separate Twilio WhatsApp channel" | no | end-to-end encrypted to WhatsApp, but as an unofficial client. Using a separate number is recommended. Risk of a ban under WhatsApp's terms (**UNVERIFIED**, general knowledge) |
| Signal | `signal-cli` daemon, JSON-RPC on `127.0.0.1:8080`; a separate bot number is recommended, because the bot ignores your own messages on your own account | no | end-to-end encrypted to Signal |
| Slack / Discord | Socket Mode / gateway WebSocket, outbound | no | to Slack / Discord |
| SMS | Twilio; the Gateway registers `/webhooks/sms` | **yes**, a public webhook | to Twilio and carriers |
| Gmail trigger | Google Pub/Sub push; the recommended setup "defaults to **Tailscale Funnel**" | **yes**, public | to Google |
| ntfy / Pushover | **no channel exists.** OpenClaw's own comparison page notes that Hermes Agent has ntfy and OpenClaw does not (`docs/start/why-openclaw/openclaw-and-hermes-agent.md`) | - | - |

An outbound command exists for any of these: `openclaw message send --channel
telegram -t <id> ...` (`docs/cli/message.md`). "Agents with message-tool
access can send across conversations and channel providers by default"
(`docs/gateway/security/index.md`).

### 2.7 Local models

`docs/gateway/local-models.md` and `docs/gateway/local-model-services.md`:

- **Built-in:** a managed **llama.cpp** plugin. It picks a model for the
  hardware, downloads a pinned `llama-server` and the weights, and checks a
  real tool call before switching. Also Ollama, LM Studio, vLLM, SGLang, MLX,
  llmman and ds4.
- **Any OpenAI-compatible server** works as a custom provider (`baseUrl`,
  `apiKey`, `api: "openai-completions"` or `"openai-responses"`). There are
  options for picky servers (`requiresStringContent`, `strictMessageKeys`,
  `toolSchemaProfile: "llamacpp"`, which strips large `pattern` and `maxLength`
  values from tool schemas so llama.cpp grammars compile).
  **TabbyAPI is not named anywhere in the docs.** It would be a custom provider.
  Whether OpenClaw's tool calls work well through Tabby's chat templates is
  **UNVERIFIED**.
- Local models get "Tool Search" by default, which defers tool schemas so the
  very large agent prompt fits. A "lean mode" removes optional tools. Long
  output from models of 32B class or smaller is watched for repetition loops.
- The docs warn: "local models skip provider-side filters. Keep agents narrow."
  The prompt-injection page goes further: "For tool-enabled agents or agents
  that read untrusted content, prompt-injection risk with older/smaller models
  is often too high. Do not run those workloads on weak model tiers"
  (`docs/gateway/security/prompt-injection.md`).
- **Contention.** The owner's local server does one generation at a time
  (DESIGN.md §20). It serves Open WebUI, the harness and anything else. OpenClaw
  would queue on that slot with its own scheduler and could not see the
  harness's provider semaphore. With the default 30-minute heartbeat that is 32
  background turns between 07:00 and 23:00, each with a large system prompt,
  unless `lightContext` and `isolatedSession` are set.

### 2.8 What it needs from the network, and pairing

- **No inbound port is needed** for the default channels, which all connect
  outward. The Gateway listens on loopback. For remote use the docs prefer
  Tailscale Serve, which keeps the Gateway on loopback, or an SSH tunnel.
  Funnel is "public HTTPS ... requires a shared password"
  (`docs/gateway/tailscale.md`, `docs/gateway/security/network-exposure.md`).
  The exceptions are Twilio SMS, Telegram webhook mode and the Gmail Pub/Sub
  trigger, which need a public HTTPS endpoint. That conflicts with the owner's
  "Funnel off" rule.
- **Outbound:** the model provider, each channel's servers, ClawHub for
  installs, and the daily update check.
- **Gateway auth.** "OpenClaw enforces gateway auth by default, including
  loopback." With nothing configured it generates a token for that run
  (`docs/help/faq/config-basics.md`). `gateway.auth.mode: "none"` exists but
  onboarding never offers it. **Container images default to an exposed bind**
  inside the container (`docs/gateway/security/index.md`). Any bearer of the
  shared token has full operator rights on every HTTP endpoint (`SECURITY.md`).
- **Pairing.** Every WebSocket client has a device identity. Loopback clients
  can be auto-approved; tailnet and LAN clients need explicit approval
  (`docs/concepts/architecture.md`).
  **`gateway.nodes.pairing.sshVerify` is on by default.** When a node asks to
  pair from a private or CGNAT address, the Gateway **SSHes back to that host**
  with the operator's SSH setup and approves on an exact key match
  (`docs/gateway/config-gateway.md`). Tailscale addresses are in the CGNAT
  range `100.64.0.0/10`, so on this tailnet OpenClaw would use the account's
  SSH identity to reach other machines. On a server whose admin workers hold
  SSH keys, turn this off (`sshVerify: false`).
- mDNS/Bonjour discovery is opt-in on Linux. Its "full" mode advertises the
  CLI path and SSH port (`docs/gateway/security/network-exposure.md`).

---

## 3. Security record in detail

### 3.1 The trust model, in the project's own words

From `docs/gateway/security/trust-model.md` and `SECURITY.md`:

- "**One trust boundary per gateway** ... a single operator, or a team whose
  members trust each other." It is "not a hostile multi-tenant security
  boundary".
- "Anyone who can operate an agent can make it do anything that agent can do."
  A caller authenticated to the Gateway has full operator rights.
- "**Trusted single-operator default: host exec on `gateway`/`node` is allowed
  without approval prompts (`security="full"`, `ask="off"`).** That is
  intentional UX, not a vulnerability by itself."
- "Local onboarding sets `tools.profile: "full"` when no profile is configured"
  (`docs/gateway/config-tools/tool-policy.md`).
- "Sandboxing is off by default ... The Gateway process always stays on the
  host; only tool execution moves into the sandbox" (`docs/gateway/sandboxing.md`).
  The Docker backend's defaults are good when it is turned on: `network:
  "none"`, read-only root, `capDrop: ["ALL"]`. But the Gateway must be able to
  drive the container engine. On a standard Docker install that means the
  `docker` group, which is effectively root (general knowledge). Rootless
  Podman is the alternative.
- Session tools reach the whole Gateway by default
  (`tools.sessions.visibility: "all"`, `tools.agentToAgent.enabled: true`).
- **Not treated as vulnerabilities:** "Prompt injection without a policy, auth,
  approval, sandbox, or tool-boundary bypass", "A malicious plugin after a
  trusted operator installs or enables it", "Public internet exposure or risky
  deployment choices that the docs already recommend against."
- The model is the last line of defence: "**Identity first** ... **Scope
  next** ... **Model last** - assume the model can be manipulated." Its
  examples of what goes wrong are mundane: "someone messaged the bot and the
  bot did what they asked."

This is an honest model for a hobbyist's own laptop. It does not fit a server
that holds health data and admin keys. Every protection the owner would need
is opt-in, and the risks the owner cares most about are out of scope by
policy.

### 3.2 Exposed gateways (January to March 2026)

Counts come from different scanners and methods. All are from secondary
reporting and **UNVERIFIED**:

| Date | Who | Count | Source |
| ---- | --- | ----- | ------ |
| 31 Jan 2026 | Censys | 21,639 | [clawdocs table][clawdocs-vulns] |
| 27 Jan to 8 Feb | Bitsight | 30,000+ | [BuildrLab][buildrlab] |
| 9 Feb | SecurityScorecard STRIKE | about 40,000 at publication, **over 135,000** by evening; 12,812 and then over 50,000 open to an already-patched RCE | [The Register][register-135k] (via search extract) |
| 9 Feb | Shodan | 42,665 | [clawdocs table][clawdocs-vulns] |
| Feb | Hunt.io | over 17,500 vulnerable to CVE-2026-25253 | [Hunt.io][huntio] |
| later | Penligent dashboard | "over 220,000" | [HackMag][hackmag-220k] |
| 31 Mar | Censys | 63,070 live instances, about 53% fewer than the February peak | [CyberDesserts][cyberdesserts] |

I found no count for September or October 2026.

The causes reported: container and VPS one-click images that bind the
Gateway on all interfaces, early versions that did not require Gateway
authentication, and users publishing the port to reach the Control UI.
Today's defaults are better (§2.8). I could not find the exact release where
authentication became mandatory. [platforms-ai.md](platforms-ai.md) §3.6
says "Before v3.24.2". No such version exists in OpenClaw's date-based scheme,
so treat that line as **UNVERIFIED** and correct it there.

### 3.3 CVEs and advisories

**Volume.** The repository's advisory list runs to 73 pages of 10, which
matches the "722" shown on its security tab ([advisories][gh-advisories]). The
GitHub Advisory Database, searched for `openclaw ecosystem:npm` on
2026-10-21, returns **771** advisories: **273 High** and **27 Critical**
([search][ghsa-npm]). These counts include `@openclaw/*` plugin packages and
some withdrawn duplicates. The newest npm Critical is dated 2 July 2026. An
unreviewed Critical for the iOS app, which did not enforce saved Gateway TLS
pins (CVE-2026-100551), is dated 26 September 2026. Since July, High and Moderate fixes have kept coming in batches; one batch was
published on 11 September and another on 5 October. Part of the volume comes
from the project's choice to publish every fix as an advisory, and part from
the intense attention of researchers. Third-party trackers do not agree on how
many advisories carry CVE IDs (**UNVERIFIED**). I found no evidence that any
OpenClaw CVE is in CISA's Known Exploited Vulnerabilities catalog. Two
aggregators say none is, but I could not check the catalog itself
(**UNVERIFIED**).

**Notable advisories**, from GitHub's advisory pages unless marked:

| ID | What | Score | Fixed in (or advisory date) |
| -- | ---- | ----- | --------------------------- |
| CVE-2026-25253 / GHSA-g8p2-7wf7-98mq | **One-click RCE.** The Control UI trusted a `gatewayUrl` from the query string and auto-connected, sending the stored Gateway token to the attacker. "The attack works even when the gateway listens only on loopback, because the victim's browser makes the outbound connection." With the token, an attacker could change sandbox and tool policy and run code. Credited to depthfirst (Mav Levin) and 0xacb. EPSS 24.4% (98th percentile) | 8.8 | 2026.1.29 (npm `clawdbot`) |
| CVE-2026-32922 / GHSA-x8qx-w8w2-g4rx | `device.token.rotate` let a holder of the narrow `operator.pairing` scope mint `operator.admin` tokens, leading to RCE on paired nodes via `system.run`. Belgium's CCB said to patch immediately ([CCB][ccb-32922]) | 9.9 (CVSS 3.1, per [CERT.hr][certhr-32922]) | 2026.3.11 |
| CVE-2026-33579 | `/pair approve` skipped caller-scope checks and reopened the pairing escalation | Critical | advisory 31 Mar 2026 |
| CVE-2026-35663 | a non-admin operator could self-claim `operator.admin` on backend reconnect | Critical | advisory 27 Mar 2026 |
| **CVE-2026-41329** / GHSA-g5cg-8x5w-7jpm | "**Heartbeat context inheritance bypasses sandbox** via senderIsOwner escalation". The wake mechanism itself let a turn gain owner trust and skip the sandbox. Reported by AntAISecurityLab | 9.0 (CVSS 4) | 2026.3.31 |
| CVE-2026-41296 | sandbox escape through a time-of-check/time-of-use race in the remote filesystem bridge | Critical | advisory 3 Apr 2026 |
| CVE-2026-41294 | a `.env` file in the working directory injected environment variables, bypassing host-env policy and allowing **config takeover** | Critical | advisory 1 Apr 2026 |
| CVE-2026-32917 | remote command injection in the iMessage channel | Critical (unreviewed) | 2026.3.13 |
| CVE-2026-43575, -43578, -43581 | an authentication bypass, a privilege escalation, and an "improper network binding" in the sandbox | Critical (unreviewed) | 2026.4.10 |
| CVE-2026-43585 | HTTP endpoints kept accepting bearer auth after a secret was rotated | Critical | advisory 17 Apr 2026 |
| GHSA-vhpg-cq3w-v8p9 | an OpenAI-compatible provider without an explicit base URL could **send its API key to the wrong endpoint** after a model reload. Relevant to a TabbyAPI key | 5.4 | 2026.8.1 (advisory published 11 Sep) |
| GHSA-9m4p-cqp4-jppq | non-owner turns could reach the WhatsApp login tool and get a new QR code to take over the account | 8.1 | `@openclaw/whatsapp` 2026.8.1 |
| GHSA-3mq7-q27j-mq7q, GHSA-5rx7-34fw-64qg | exec approvals outlived their reviewed working directory; a Unicode fallback escaped `workspaceOnly` | High, Moderate | Sep 2026 batch |
| GHSA-g7fw-3gjp-g5hf | channel read actions could skip target allowlists | Moderate | 5 Oct 2026 |

The Cloud Security Alliance described a "Claw Chain" of four CVEs that
together give full agent compromise, fixed in 2026.4.22
([CSA][csa-clawchain]; **UNVERIFIED**, page not reachable).

**The pattern matters more than any single bug.** The failures sit in
exactly the boundaries a careful owner would rely on: scopes, pairing,
sandbox, approvals, allowlists, owner-only tools, and the heartbeat. They are
fixed quickly, but staying safe means updating within days. A novice
maintainer would struggle to keep up with that.

### 3.4 Malicious skills on ClawHub

- **ClawHavoc (late January to February 2026).** Koi Security audited all 2,857
  ClawHub skills and found **341 malicious**, 335 of them from one campaign.
  The skills had fake "Prerequisites" steps that installed the **Atomic
  Stealer (AMOS)** macOS infostealer. Some targeted Windows too. They posed
  as crypto wallet trackers, Polymarket bots and "auto-updaters", with
  typosquatted names. AMOS steals browser credentials, keychain items, SSH
  keys, and every API key the agent can reach ([The Hacker News][thn-341],
  [eSecurity Planet][esp-341], [SC Media][scmedia-341]; **UNVERIFIED**).
- **Later counts** vary: 824 on 16 February, "over 1,184", and about 900 of
  10,700 skills per Bitdefender. platforms-ai.md cites Bitdefender as finding
  about 17% malicious behaviour in one February week ([Substack][mothasa];
  **UNVERIFIED**).
- **Response:** a VirusTotal partnership (announced around 7 February), ClawScan,
  the 14-day account-age rule and the verify envelope (§2.5). A widely repeated
  "2,419 skills removed" figure has no primary source (**UNVERIFIED**).
- **It continued after scanning arrived.** Unit 42 reported persistent,
  evasive malicious skills from February to May 2026, including five that got
  past the automated scanning before the accounts were banned. One stole
  cryptocurrency keys over the Telegram Bot API ([Unit 42][unit42], via search
  extract; **UNVERIFIED**).
- **The project's own view** (`docs/security/THREAT-MODEL-ATLAS.md` §5 and
  `persistence.md`, `collection-and-exfiltration.md`):
  - T-PERSIST-001 "Malicious skill installation": likelihood High, impact
    Critical, **P0**. Residual risk: "High - detection layers exist but skills
    still run with agent privileges and no execution sandboxing".
  - T-EXFIL-003 "Credential harvesting": "**no execution sandboxing for skills
    at runtime**"; residual risk **Critical**.
  - T-PERSIST-002 "Skill update poisoning": "auto-updates may pull malicious
    versions before review completes."

### 3.5 Prompt injection and agent-safety incidents

- **The project rates direct prompt injection Critical (T-EXEC-001, P0)** and
  leaves it out of scope for reports. Its own figures: 0.5% attack success for
  Claude Opus 4.5 and 8.5% for Gemini 2.5 Pro in a 2026 crowdsourced arena, but
  "adaptive attackers still exceed 80% against state-of-the-art defenses, and
  smaller/older models remain markedly easier to steer"
  (`docs/security/THREAT-MODEL-ATLAS/execution.md`).
- **Good engineering worth noting.** Untrusted content is wrapped in
  `<<<EXTERNAL_UNTRUSTED_CONTENT>>>` markers. Chat-template special tokens
  (ChatML `<|im_start|>`, Llama, Gemma, Mistral, Phi, GPT-OSS) are stripped
  from fetched content, because self-hosted servers may tokenize them as real
  role boundaries. Leaked tool markup is removed from replies
  (`docs/gateway/security/prompt-injection.md`). The harness should do the
  same for its local models (§4.5).
- **The inbox-deletion incident (February 2026).** Summer Yue, Meta's director
  of alignment, said her OpenClaw agent deleted more than 200 emails after
  being told to wait for approval. The most-cited explanation is that context
  compaction dropped the instruction. Her "stop" messages from her phone were
  ignored, and she ran to the machine to kill the process. This was not an
  attack, but it is the failure the Housekeeping Tracker exists to prevent:
  a standing instruction lost when the context was compressed
  ([Kiteworks][kiteworks-yue], [vectara case study][vectara-yue];
  **UNVERIFIED** details).
- **Moltbook** showed bot-to-bot prompt injection at scale: agents reading
  posts written by other agents ([SecurityWeek][securityweek-moltbook];
  **UNVERIFIED**).
- Academic case studies report indirect injection through hidden HTML that
  leads to local data exfiltration, and propose reader/actor privilege
  separation for OpenClaw (arXiv [2603.12644][arxiv-casestudy],
  [2603.13424][arxiv-privsep]; not read, **UNVERIFIED**).

### 3.6 Recommended hardening, and what it cannot fix

The project's own "Hardened baseline in 60 seconds"
(`docs/gateway/security/hardened-baseline.md`), quoted with the lines reflowed:

```json5
{
  gateway: { mode: "local", bind: "loopback",
             auth: { mode: "token", token: "replace-with-long-random-token" } },
  session: { dmScope: "per-channel-peer" },
  tools: {
    profile: "messaging",
    deny: ["group:automation", "group:runtime", "group:fs", "sessions_spawn", "sessions_send"],
    fs: { workspaceOnly: true },
    exec: { security: "deny", ask: "always" },
    elevated: { enabled: false },
    sessions: { visibility: "agent" },
    agentToAgent: { enabled: false },
  },
  channels: { whatsapp: { dmPolicy: "pairing", groups: { "*": { requireMention: true } } } },
}
```

The rest of the guidance, from the docs plus CNCERT's advice (§1.5):

- Run `openclaw security audit` after every change.
- Use a separate bot number for WhatsApp, Signal and Telegram.
- Sandbox all sessions (`mode: "all"`) with `workspaceAccess: "none"`.
- Keep `web_fetch`, `web_search` and `browser` off.
- Give untrusted mail and documents to a read-only reader agent.
- Never use Funnel or a public bind.
- Add `DOCKER-USER` firewall rules if ports are published.
- Set `sshVerify: false`.
- Pin plugins from npm and set an `installPolicy`.
- Turn off Bonjour.
- Use one dedicated OS user or host per trust boundary.
- Follow the stable or extended-stable channel and update quickly.

**What hardening cannot fix:**

1. A notification still has to leave the machine through Telegram, WhatsApp
   or Signal, carrying whatever text the model wrote.
2. With the baseline applied, almost nothing of OpenClaw's agent is left. It
   cannot read files, run commands or schedule its own wakes (the
   `group:automation` deny), so the reason to use it disappears.
3. Every guard is a config key a later onboarding, `doctor --fix` or upgrade
   can change. The harness would have no say over that config.
4. The Gateway process itself, with its channel credentials, plugins and
   hooks, always runs on the host with full access for its OS user.

---

## 4. Assessment against the owner's server

### 4.1 The owner's constraints against OpenClaw

| Owner's constraint | OpenClaw | Fit |
| ------------------ | -------- | --- |
| Sensitive personal and health data must never leave the machine | every channel sends message text to a third party. `MEMORY.md`, `USER.md` and daily notes are in every main-session prompt. Dreaming promotes notes automatically. The iOS node can export HealthKit summaries | **conflict** |
| AI admin workers hold SSH keys | default host exec without prompts. `sshVerify` uses the account's SSH to reach tailnet peers. Skills and hooks run with the Gateway user's access | **conflict** unless a separate OS user and host |
| No ports forwarded, Tailscale only, Funnel off | default channels need no inbound port, and Serve is supported. Gmail and SMS triggers need public ingress | fits if those are skipped |
| Local models on TabbyAPI (shared with Open WebUI) and llama.cpp | works through a custom OpenAI-compatible provider. Tabby is untested. The docs advise against small models for tool-enabled or untrusted-input agents. It cannot share the harness's semaphore on a one-slot server | **weak** |
| Nightly power-off 23:00 to 07:00 | catches up at boot sensibly (§2.2). A second scheduler outside Seraphina's power calendar and holds (§6.3, §6.5) | **conflict** with "one scheduler" |
| API keys only from env or an env file | uses `~/.openclaw/.env`, `credentials/` and SecretRefs. A second secret store outside the executor's custody (§9.5). CVE-2026-41294 was a `.env` injection | **conflict** with §9.5 |
| Security-conscious owner who is not a Linux expert | Node 24 or 26, about 1,861 npm packages, releases every week, about 722 advisories in nine months, a config surface with hundreds of keys, some marked `dangerous*` | **poor** |
| The harness has an EventBus, agents, chats and a commitments ledger | duplicates all of them in a second system that cannot see the harness's ledger | redundant |

### 4.2 Option A: run OpenClaw as the agent on this server

**No.** The reasons are in §0 and §4.1. The deciding one is structural. A
personal assistant that reads incoming messages, knows the owner's personal
details and can act and send messages combines untrusted input, sensitive
access and outward communication in one session. §9.6 forbids exactly that,
and OpenClaw's own trust model says the model cannot be relied on to keep
those apart. Hardening it to the baseline (§3.6) removes what makes it
useful. Running it with local models goes against its own guidance (§2.7).

**If the owner still wants to try OpenClaw**, do it away from the server:

- Use a spare machine or a VM that holds nothing personal.
- Put it under a separate tailnet tag whose ACL cannot reach the server,
  except, if wanted, one rule to the model port.
- Use a separate bot number and the stable channel.
- Apply the hardened baseline and set `sshVerify: false`.
- Install no ClawHub skills.
- Treat everything it knows as public.

### 4.3 Option B: OpenClaw only as a messaging gateway, sandboxed

**How it could be done:**

- Run the Gateway as a dedicated OS user (or in rootless Podman) with no read
  access to `/srv`, the harness's data directory, other users' homes or the
  container engine socket.
- Allow outbound network only to the chosen channel's servers.
- Set `tools.profile: "minimal"`, deny everything else, and turn off
  heartbeats (`every: "0m"`), dreaming, hooks and skills.
- Install one channel plugin pinned from npm (`npm:@openclaw/signal@<version>`),
  not from ClawHub.
- Configure no model at all, or a dummy one.
- For outbound messages, the harness runs `openclaw message send` or calls the
  Gateway's WebSocket `send` method with an idempotency key.

**What it costs:**

| Cost | Detail |
| ---- | ------ |
| Inbound replies need custom code | OpenClaw turns inbound messages into agent turns. To pass the owner's reply to the harness instead, someone has to write a Gateway plugin on the `inbound_claim` or `message_received` hook (`docs/automation/hooks.md`, `docs/plugins/hooks.md`). That is TypeScript running with the Gateway's full access, kept working across weekly plugin-SDK changes |
| Attack surface | the whole Gateway: HTTP, WebSocket, Control UI, OpenAI-compatible endpoints and pairing. The pairing and scope classes are where the Critical CVEs were (§3.3) |
| Upkeep | Node runtime, weekly releases, migrations run by `doctor --fix`, advisories in batches |
| Data | the channel still carries the text off the machine, so the content rules are the same as for any channel |
| Benefit | a ready-made client for WhatsApp, Signal and Telegram. For Telegram that is one HTTPS call (`sendMessage`) with `urllib`. For Signal, `signal-cli` is the same daemon OpenClaw uses. For ntfy, it is one HTTP POST to a server on the tailnet |

**Verdict: not worth it.** The one case for B is **WhatsApp**, because
replicating Baileys is real work. Even then, put it on a separate low-trust
device (§4.2), not on this server, and send only minimal text (§4.4 step 6).

### 4.4 Option C: build the pattern into the harness (recommended)

**What the harness already has:** the EventBus (DESIGN.md §10); chats with
resumable sessions (§19); agent definitions as Markdown persona files (§7);
provider semaphores so background work queues on the one-slot local server
(§20); secret redaction on every event; env-file keys. The v2 design adds the
Housekeeping Tracker (§4), the power calendar and holds (§6.3), the single
scheduler (§6.5), the notify routes with an ntfy server run natively over the
tailnet (§6.6), executor custody of the notification token (§9.5), and the
Rule of Two (§9.6).

**What to add.** This is a sketch for the design owner, not a contract.

1. **Wake sources.** Each one becomes a single event,
   `wake.requested {persona, source, reason, not_before, dedupe_key}`:
   - **schedule**: entries in Seraphina's one scheduler (cron, interval or
     one-shot), read against the power calendar;
   - **poke file**: a systemd `.path` unit (`PathChanged=<data_dir>/wake/<persona>.poke`)
     that runs `factory wake <persona>`. Ubuntu supports this natively; Python
     needs no inotify code, and only the harness group can write the file;
   - **webhook**: `POST /api/wake` on the existing server, loopback or tailnet
     only (§7). It uses its own bearer token, separate from the GUI token
     (OpenClaw does the same), and accepts a body of
     `{persona, reason ≤ 200 chars}`. **Unlike OpenClaw, `reason` is treated as
     untrusted** and reaches the model inside a data block;
   - **events**: deterministic rules on EventBus events (job failed, tracker
     item due or overdue, drift found, backup failed, restore test failed).
     Deciding *whether* to wake needs no model;
   - **self-requested**: the model's `schedule_wake` action (step 4), clamped
     to a minimum of 10 minutes and a maximum of 30 days, with at most N
     pending per persona. It is stored as a **Housekeeping Tracker item**, so a
     promised check-in cannot be lost when context is compacted (the Summer Yue
     failure, §3.5).
2. **Wake queue rules.**
   - Coalesce wakes with the same `dedupe_key`.
   - Keep at least 30 seconds between event wakes per persona, and stop after
     5 starts in 60 seconds (OpenClaw's numbers).
   - Refuse rather than evict when the queue is full.
   - Wait on the provider semaphore at low priority, behind interactive Open
     WebUI and chat work (the resource broker, §9.8).
   - Respect `quiet_after_boot` and `quiet_before_off`.
   - Wakes due during the power-off are **rescheduled after boot, not replayed
     in a burst**.
   - A wake never takes a hold that delays shutdown unless the owner allowed
     it. Whether an urgent wake may set an RTC alarm during the night is an
     owner decision; the proposed default is no.
3. **Context assembly.**
   - The persona file: `agents/<persona>.md`, the "soul".
   - The tail of the persona's memory file, plus a short curated summary that
     the owner can edit in the GUI.
   - The persona's open tracker items.
   - The trigger, framed as data.
   - The time and the power schedule.
   - Nothing from sensitive data roots, which are allowlisted out by
     construction (§9.6, L74).
   - Untrusted text is wrapped, and chat-template special tokens are
     stripped (§4.5).
4. **One turn, no tools by default.** The model answers with strict JSON,
   checked against a schema. Unknown keys or types reject the answer, and the
   turn is recorded as failed, never as silent success:

   ```json
   {"actions": [
     {"type": "none"},
     {"type": "notify_owner", "priority": "low|normal|high", "title": "≤ 60 chars", "body": "≤ 300 chars"},
     {"type": "schedule_wake", "in_minutes": 120, "why": "≤ 200 chars"},
     {"type": "remember", "text": "≤ 500 chars"},
     {"type": "open_item", "title": "≤ 120 chars", "due": "<ISO date>"}
   ]}
   ```

   Where the local server supports constrained output, send the schema as
   `response_format` or a grammar. llama.cpp supports JSON-schema-constrained
   output; TabbyAPI's support is **UNVERIFIED**. Otherwise validate after the
   fact and allow one repair attempt. Any tool use or change to the system
   goes through the normal reviewed path (§4.9 of the v2 design), never
   straight from a wake.
5. **Outbox and delivery.**
   - `notify_owner` writes to a durable outbox with an idempotency key.
   - The notifier holds the channel token as part of the executor (§9.5) and
     posts to the native ntfy server over the tailnet (§6.6).
   - Each message is recorded as `sent`, `failed` or **`unknown`**; an
     `unknown` message is not resent automatically (OpenClaw's lesson, §2.2).
   - Per-persona daily caps.
   - Active hours, which `high` priority can override.
   - When the channel is down, back off and skip model turns for ambient
     wakes (OpenClaw does this too).
6. **Content rules.** The body passes the masking and publication guard
   (§9.5). Health and personal categories are never sent. If the owner later
   adds an off-machine route (a public ntfy topic, Telegram or Signal), it
   carries only minimal text ("Seraphina has a message for you") and a tailnet
   link. One detail for iPhones: with a self-hosted ntfy server, instant
   delivery needs the `upstream-base-url` relay through ntfy.sh, which carries
   only a message ID and a hash of the topic, not the text
   ([Railway ntfy guide][railway-ntfy], via search extract; **UNVERIFIED**).
   Android's instant-delivery mode keeps its own connection to the server
   (from memory, **UNVERIFIED**).
7. **Replies.** The notification links to the persona's chat in the cockpit
   over the tailnet, and the owner answers there. ntfy action buttons can also
   send an HTTP request back to the harness over the tailnet (**UNVERIFIED**
   detail). No third-party chat platform is needed.
8. **Audit and GUI.**
   - Every wake emits `wake.*` events and gets a run log.
   - A "Wakes & messages" page shows, per persona: the next wakes, recent
     wakes and what each decided, the outbox, and pause and stop switches.
   - A Stop button must actually stop. Yue's "stop" messages from her phone
     did not.

This is a few hundred lines of standard-library Python plus one GUI page. It
adds no new runtime, no new secret store and no new scheduler, and every part
falls under rules the v2 design already has.

### 4.5 What to borrow from OpenClaw

| Mechanism | OpenClaw source | Harness use |
| --------- | --------------- | ----------- |
| Silent-by-default reply contract (`NO_REPLY`, `heartbeat_respond {notify}`) | `docs/gateway/heartbeat.md` | the `none` action. A turn with no valid action counts as failed, not silent |
| "Do not infer or repeat old tasks from prior chats" in the default prompt | same | keeps ambient wakes from nagging. Recurring work lives in explicit schedules and tracker items |
| Owner-DM-only delivery, never to groups; `activeHours` in local time | same | notify routes plus active hours |
| `lightContext` and `isolatedSession` for cheap ambient checks | same | ambient wakes skip chat history |
| 30-second minimum between event wakes; flood guard of 5 starts in 60 seconds; coalescing; 503 instead of eviction | heartbeat doc, `docs/automation/cron-jobs/webhooks.md` | wake queue rules |
| Overdue jobs rescheduled at startup rather than replayed; "delivery Unknown"; no automatic resend of possibly delivered messages | `docs/automation/cron-jobs/how-it-works.md` | power-off behaviour and the outbox |
| Idempotency keys on side-effecting calls | `docs/concepts/architecture.md` | outbox and webhook |
| Self-pacing `next_check` "silently clamped" to bounds; one-shot `at` plus delete-after-run | `docs/automation/cron-jobs/schedules.md` | `schedule_wake` limits |
| Hook token separate from the main token; headers only; query-string tokens rejected; failed auth throttled | webhooks doc | `/api/wake` |
| Untrusted-content wrapping and **stripping chat-template special tokens** before local models see them | `docs/gateway/security/prompt-injection.md` | context assembly for TabbyAPI and llama.cpp |
| Reader agent for untrusted inputs (email, documents) | same | already the Rule of Two in §9.6 |
| Explicit automations instead of inferred commitments (inferred ones retired in v2026.8.1) | `docs/automation/index.md` | tracker items only from explicit actions |
| Skip model turns when the delivery channel is down | heartbeat doc | notifier backoff |
| A separate bot identity, not the owner's own account | `docs/gateway/security/hardened-baseline.md` | if a chat app is ever added |
| `openclaw security audit` with named check IDs | `docs/gateway/security/audit-checks.md` | a "wake and notify" section in the exposure audit (§7.5) |

**What not to copy:**

- the `full` tool profile at onboarding;
- host exec without prompts;
- a workspace that is a working directory, not a sandbox;
- wake text trusted as a system event;
- condition scripts that run unattended with full `exec`;
- automatic dreaming into always-loaded memory, which would pull personal
  details into every prompt;
- session visibility across the whole Gateway;
- `sshVerify` on by default;
- public conversation links (v2026.10.5);
- Funnel as the default for Gmail.

---

## 5. Gemini's suggestion, checked

| Gemini said | Finding |
| ----------- | ------- |
| "OpenClaw is a highly popular open-source agent wrapper" | True: 392.6k stars, MIT, foundation-run (§1) |
| "natively supports local models via Ollama" | True, and also managed llama.cpp, LM Studio, vLLM and any OpenAI-compatible server. TabbyAPI is not named, and the docs advise against small local models for tool-enabled agents (§2.7) |
| "a background daemon (essentially a cron job) that continuously assembles prompts, retrieves context from its local memory files" | Mostly true: the Gateway's scheduler runs a heartbeat every 30 minutes, with AGENTS/SOUL/USER/MEMORY and daily notes. `HEARTBEAT.md` is no longer read; it moved into a database scratch in v2026.8.1 (§2.2, §2.4) |
| "direct pairing modes for apps like WhatsApp and Telegram" | WhatsApp links as a WhatsApp Web device through the unofficial Baileys library; Telegram uses a BotFather token; unknown senders get pairing codes (§2.6) |
| "if the LLM decides an event requires your attention, it triggers the messaging API" | True: `heartbeat_respond {notify: true}` or the message tool, sent to the owner's direct messages (§2.3) |
| Custom loop: "watch for ... a `.wake-now` file" | Sound. On Ubuntu a systemd `.path` unit does this without Python file-watching (§4.4) |
| "reads ... the 'soul' file and the tail-end of the previous session's memory" | Sound, and the same shape as OpenClaw. Add open tracker items and frame untrusted text as data (§4.4) |
| "sends the assembled context to Ollama's ... `/api/chat` using `urllib`" | The owner runs TabbyAPI and llama.cpp, so use their OpenAI-compatible `/v1/chat/completions` through the harness's existing provider code and semaphore |
| "these background wake cycles cost nothing in API fees" | True for fees. Not free for the one shared generation slot (§2.7) |
| "output a specific JSON structure ... `{"action": "message_user", ...}`" | Sound, if the JSON is checked against a strict schema, unknown actions are rejected, and no free-form command runs (§4.4) |
| "Telegram Bot webhook, Pushover, or Twilio" | Each sends the text to a third party, which conflicts with the owner's data rule. Telegram bot chats are not end-to-end encrypted. Prefer the native ntfy server on the tailnet (§4.4 step 6) |
| "often referred to in the community as the `alive` pattern" | I found no project or term by that name. The closest match is soul.py's SOUL.md and MEMORY.md layout ([themenonlab][soulpy]) (**UNVERIFIED**) |

---

## 6. Open questions for the owner

1. Which phone app will you actually read: the ntfy app, Signal or Telegram?
   The design's default is the ntfy app with a native ntfy server reached over
   the tailnet (§6.6, open question 11).
2. May a notification say anything beyond "Seraphina has a message for you"?
   For example, the persona's name, or which service failed? Health and
   personal details are never included.
3. During the 23:00 to 07:00 power-off, should an urgent wake ever be allowed
   to set the wake alarm earlier? Proposed default: no. It waits for the
   07:00 boot.
4. How many unprompted messages per day per persona are acceptable, and during
   which hours?
5. Replies: is answering in the cockpit chat over the tailnet enough, or do
   you want to reply from a chat app? A chat app sends your replies to a third
   party.

---

## Sources

**Primary: OpenClaw repository** (all at commit `e0a0bbcf`, read 2026-10-21):
`README.md`, `SECURITY.md`, `CONTRIBUTING.md`, `package.json`,
`docs/start/lore.md`, `docs/concepts/architecture.md`,
`docs/concepts/agent-workspace.md`, `docs/concepts/memory.md`,
`docs/concepts/dreaming.md`, `docs/concepts/soul.md`,
`docs/concepts/standing-intents.md`, `docs/gateway/heartbeat.md`,
`docs/automation/index.md`, `docs/automation/hooks.md`,
`docs/automation/cron-jobs/{webhooks,schedules,how-it-works,gmail}.md`,
`docs/gateway/local-models.md`, `docs/gateway/local-model-services.md`,
`docs/gateway/config-tools/{tool-policy,custom-providers,provider-examples}.md`,
`docs/gateway/config-gateway.md`, `docs/gateway/tailscale.md`,
`docs/gateway/sandboxing.md` and `sandboxing/{docker,podman}-backend.md`,
`docs/gateway/security/{index,trust-model,hardened-baseline,prompt-injection,network-exposure}.md`,
`docs/gateway/telemetry.md`, `docs/install/development-channels.md`,
`docs/install/updating/automatic-updates.md`, `docs/help/faq/config-basics.md`,
`docs/channels/{telegram,whatsapp,signal,sms}.md`, `docs/cli/message.md`,
`docs/tools/{skills,plugin}.md`, `docs/nodes/command-policy.md`,
`docs/security/THREAT-MODEL-ATLAS.md` and its `execution`, `persistence` and
`collection-and-exfiltration` pages,
`docs/start/why-openclaw/openclaw-and-hermes-agent.md`,
`docs/releases/index.md`, `docs/releases/2026.10.5.md`, and the source files
`src/gateway/server-start.ts`, `src/gateway/net.ts`,
`src/auto-reply/heartbeat.ts`.

**Primary: GitHub pages** (fetched 2026-10-21):
[repository][repo-commit] ·
[releases][gh-releases] ·
[repository advisories][gh-advisories] ·
[Advisory Database, npm search][ghsa-npm] ·
[GHSA-g8p2-7wf7-98mq (CVE-2026-25253)][ghsa-25253] ·
[GHSA-g5cg-8x5w-7jpm (CVE-2026-41329)][ghsa-41329] ·
[GHSA-vhpg-cq3w-v8p9][ghsa-vhpg] ·
[GHSA-9m4p-cqp4-jppq][ghsa-9m4p]

**Secondary** (search extracts, mostly not fetched; see each mark above):
[The Hacker News, CVE-2026-25253][thn-25253] ·
[CERT.hr, CVE-2026-32922][certhr-32922] ·
[CCB Belgium][ccb-32922] ·
[CSA "Claw Chain"][csa-clawchain] ·
[The Register, 135k exposed][register-135k] ·
[HackMag, 220k][hackmag-220k] ·
[clawdocs known vulnerabilities][clawdocs-vulns] ·
[Hunt.io][huntio] ·
[BuildrLab on Bitsight][buildrlab] ·
[CyberDesserts, Censys March count][cyberdesserts] ·
[The Hacker News, 341 skills][thn-341] ·
[eSecurity Planet][esp-341] ·
[SC Media][scmedia-341] ·
[Substack analysis][mothasa] ·
[Unit 42][unit42] ·
[Kiteworks on the inbox deletion][kiteworks-yue] ·
[vectara awesome-agent-failures][vectara-yue] ·
[SecurityWeek on Moltbook][securityweek-moltbook] ·
[Business Today on Moltbook][bt-moltbook] ·
[HackMag on CNCERT][hackmag-cncert] ·
[News24 on China][news24-china] ·
[InfoWorld on Anthropic billing][infoworld-anthropic] ·
[The Decoder][decoder-openai] ·
[MacStories][macstories-openai] ·
[Red Hat blog][redhat-foundation] ·
[NVIDIA NemoClaw press release][nvidia-nemoclaw] ·
[NemoClaw docs][nemoclaw-docs] ·
[Mozilla Privacy Not Included, Telegram][mozilla-telegram] ·
[Railway ntfy guide][railway-ntfy] ·
[arXiv 2603.12644][arxiv-casestudy] ·
[arXiv 2603.13424][arxiv-privsep] ·
[soul.py][soulpy]

[repo-commit]: https://github.com/openclaw/openclaw/tree/e0a0bbcf2d01909552419ccfe865eb753fc4c9e8
[gh-releases]: https://github.com/openclaw/openclaw/releases
[gh-advisories]: https://github.com/openclaw/openclaw/security/advisories
[ghsa-npm]: https://github.com/advisories?query=openclaw+ecosystem%3Anpm
[ghsa-25253]: https://github.com/advisories/GHSA-g8p2-7wf7-98mq
[ghsa-41329]: https://github.com/advisories/GHSA-g5cg-8x5w-7jpm
[ghsa-vhpg]: https://github.com/openclaw/openclaw/security/advisories/GHSA-vhpg-cq3w-v8p9
[ghsa-9m4p]: https://github.com/openclaw/openclaw/security/advisories/GHSA-9m4p-cqp4-jppq
[thn-25253]: https://thehackernews.com/2026/02/openclaw-bug-enables-one-click-remote.html
[certhr-32922]: https://cve.cert.hr/cve/CVE-2026-32922
[ccb-32922]: https://ccb.belgium.be/advisories/warning-privilege-escalation-openclaw-patch-immediately
[csa-clawchain]: https://labs.cloudsecurityalliance.org/research/csa-research-note-openclaw-claw-chain-cve-20260517-csa-style/
[register-135k]: https://theregister.com/2026/02/09/openclaw_instances_exposed_vibe_code
[hackmag-220k]: https://hackmag.com/news/openclaw-open
[clawdocs-vulns]: https://clawdocs.org/security/known-vulnerabilities
[huntio]: https://hunt.io/blog/cve-2026-25253-openclaw-ai-agent-exposure
[buildrlab]: https://buildrlab.com/blog/openclaw-virustotal-scanning-bitsight-exposed-gateways
[cyberdesserts]: https://blog.cyberdesserts.com/openclaw-exposure-numbers-explained/
[thn-341]: https://thehackernews.com/2026/02/researchers-find-341-malicious-clawhub.html
[esp-341]: https://esecurityplanet.com/threats/hundreds-of-malicious-skills-found-in-openclaws-clawhub
[scmedia-341]: https://www.scworld.com/news/openclaw-agents-targeted-with-341-malicious-clawhub-skills
[mothasa]: https://mothasa.substack.com/p/the-most-popular-ai-agent-on-github
[unit42]: https://unit42.paloaltonetworks.com/openclaw-ai-supply-chain-risk/
[kiteworks-yue]: https://www.kiteworks.com/secure-email/meta-ai-safety-director-openclaw-rogue-agent-email-deletion/
[vectara-yue]: https://github.com/vectara/awesome-agent-failures/blob/main/docs/case-studies/openclaw-email-deletion.md
[securityweek-moltbook]: https://securityweek.com/security-analysis-of-moltbook-agent-network-bot-to-bot-prompt-injection-and-data-leaks/
[bt-moltbook]: https://www.businesstoday.in/amp/technology/news/story/moltbook-left-its-production-database-open-exposing-millions-of-ai-agent-records-514548-2026-02-04
[hackmag-cncert]: https://hackmag.com/news/cncert-openclaw
[news24-china]: https://www.news24.com/business/tech/china-curbs-state-use-of-openclaw-ai-just-as-tencent-embraces-it-20260311-0414
[infoworld-anthropic]: https://www.infoworld.com/article/4154435/anthropic-cuts-openclaw-access-from-claude-subscriptions-offers-credits-to-ease-transition.html
[decoder-openai]: https://the-decoder.com/openclaw-developer-peter-steinberger-joins-openai-to-build-ai-agents/
[macstories-openai]: https://www.macstories.net/linked/openclaw-creator-peter-steinberger-joins-openai/
[redhat-foundation]: https://redhat.com/en/blog/red-hat-joins-openclaw-foundation-founding-member-advance-open-future-production-ai-agents
[nvidia-nemoclaw]: https://investor.nvidia.com/news/press-release-details/2026/NVIDIA-Announces-NemoClaw-for-the-OpenClaw-Community/default.aspx
[nemoclaw-docs]: https://docs.nvidia.com/nemoclaw/0.0.39/about/overview.html
[mozilla-telegram]: https://www.mozillafoundation.org/en/privacynotincluded/telegram/
[railway-ntfy]: https://railway.com/deploy/ntfy-server
[arxiv-casestudy]: https://arxiv.org/html/2603.12644v1
[arxiv-privsep]: https://arxiv.org/pdf/2603.13424
[soulpy]: https://themenonlab.blog/blog/soul-py-persistent-memory-llm-agents
