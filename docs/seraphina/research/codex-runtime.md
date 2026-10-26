# Research: Codex as an embeddable agent runtime (October 2026)

Research notes for **Seraphina OS Harness**. They answer the second half of a
proposal from GPT Astra, which David asked for and Kate decides on: an
**optional Codex worker behind the harness's executor**. The worker would use
"the official SDK for bounded delegated coding jobs" and "app-server over local
stdio for persistent tasks, streamed activity, cancellation, approval requests
and resume". It would get explicit MCP tools, a scoped workspace and only the
relevant task context. Request and run ids, artifacts, validation evidence and
the actual outcome would go to the receipt system. A transport timeout must
never become a success, and an action that may already have taken effect must
never be repeated blindly.

The first half of the proposal, a Kate-owned browser adapter, is not covered
here. Sections 5.3 to 5.6 say only how such an adapter would reach a Codex
worker.

What this file covers:

- every surface Codex offers for embedding at the pinned release;
- the app-server protocol in detail;
- what the SDKs wrap;
- how `codex exec --json`, which our CLI driver uses, compares;
- how to give Codex explicit MCP tools;
- the Linux sandbox;
- authentication;
- failure semantics;
- exactly what a new app-server driver would add over the `codex` preset in
  `swfactory/drivers/cli.py` (DESIGN §13).

Related: [DESIGN.md](../../../DESIGN.md) §12 (builtin harness, MCP, bwrap),
§13 (CLI driver), §17 (security) and §20 (the owner's local stack: Codex CLI
0.163.0 is already the primary client there, with Strata serving
`/v1/responses`). See also [SERAPHINA_OS_HARNESS.md](../SERAPHINA_OS_HARNESS.md),
principle 9: agents never hold root, and they run as their own Unix user.

Written 2026-10-21. Nothing here was installed on or run against the owner's
server, and the live Aris battery was not touched.

## How these notes were made, and their limits

- **Source, pinned.** I made a shallow clone of `openai/codex` at tag
  [`rust-v0.163.0`][tag] (commit `83086188ae0614bd882818ff1aa23f3194962012`,
  2026-10-15 18:57 UTC), the latest stable release on 2026-10-21. It is the same
  version our CLI driver's flags were checked against (DESIGN §13) and the
  version DESIGN §20 names for the server. `0.164.0-alpha.9` exists but is a
  prerelease. Paths below are relative to the repository root. The clone was
  read as data. Its `docs/` folder now holds only stubs that link to
  developers.openai.com, so the protocol facts come from the Rust protocol types
  and the generated JSON Schema.
- **Binary, probed.** I installed `@openai/codex@0.163.0` from npm into a
  scratch directory in the research sandbox (linux-x64, musl build) and ran it.
  It never talked to a real model and had no credentials: every probe pointed a
  custom model provider at a Responses-API stub on `127.0.0.1`, written in about
  150 lines of standard-library Python, with a throwaway `CODEX_HOME`. Five probes:
  1. **app-server lifecycle:** handshake, a command approval answered with
     decline, interrupt, model HTTP 500, a repeated `clientUserMessageId`, EOF
     in the middle of a turn, resume, and `kill -9` in the middle of a turn;
  2. **`workspace-write` sandbox:** git commit, reads and writes outside the
     workspace, network, and `apply_patch` under `untrusted` and under `never`;
  3. **strict `workspaceWrite` policy and the built-in command policy;**
  4. **per-thread MCP servers:** an `enabled_tools` filter, the approval
     elicitation, and which environment variables the MCP server sees;
  5. **`codex exec --json`:** the stdin prompt, usage across a resume, SIGINT
     against SIGTERM, and HTTP 500.

  Statements marked **Verified (probe N)** were observed in those runs.
- **Registries.** The PyPI and npm JSON APIs were read for the published
  versions: `openai-codex` 0.163.0 (PyPI, 2026-10-15), `@openai/codex-sdk`
  0.163.0 and `@openai/codex` 0.163.0 (npm, 2026-10-15).
- **Not reachable from the research sandbox:** developers.openai.com,
  learn.chatgpt.com and openai.com (DNS failure through the agent proxy). None
  of the three links Astra gave could be opened. Where a statement comes only
  from search-engine extracts of those pages, it is marked **Extract,
  UNVERIFIED**. Source and binary win where they disagree with an extract.
- **Sandbox caveat.** The probes ran as root in a Linux container that has
  `/usr/bin/bwrap` and working user namespaces. On the owner's Ubuntu, AppArmor
  may restrict unprivileged user namespaces, and that can change how bubblewrap
  behaves. This is **UNVERIFIED** on the server (§6.4).
- Other labels: **Source** means read in the pinned source but not run.
  **UNVERIFIED** means inferred or untested.

---

## 0. The answer in one screen

**Surfaces.** At 0.163.0 Codex offers three ways to embed its agent loop:

- `codex exec --json`: one turn per process, with JSONL events on stdout. Our
  `codex` preset uses it.
- `codex app-server`: JSON-RPC over stdio, with persistent threads,
  server-to-client approval requests, interrupt, steer and resume. The VS Code
  extension, the desktop app and the Python SDK use it. In 0.163 `codex exec`
  is itself an **in-process app-server client**
  ([exec/src/lib.rs][exec-lib-imports]).
- The SDKs. The **TypeScript SDK** spawns `codex exec --experimental-json`,
  which is an alias of `--json`, so it is the same transport as our preset. The
  **Python SDK** (`openai-codex`) drives `codex app-server --listen stdio://`.
  It needs `pydantic`, so it cannot be imported into our standard-library
  harness. Its defaults would also bypass Kate's custody: threads start in
  `auto_review` (Codex's own reviewer subagent decides approvals), and with no
  handler the client **accepts** every command and file-change approval
  ([client.py][py-default-handler]).

`codex mcp-server` (Codex as an MCP server) no longer exists in 0.163.

**What app-server adds over our preset.** In short:

- real approval requests for commands, file changes, MCP tool calls and extra
  permissions, which the harness answers;
- a real interrupt in place of SIGTERM;
- `turn/steer`, guarded by `expectedTurnId`;
- explicit `willRetry` and `codexErrorInfo` in place of our "4 errors in 60 s"
  heuristic;
- per-thread configuration: MCP servers, sandbox policy, model;
- `turn/diff/updated`, and `last` plus `total` token usage;
- `thread/read` to reconcile after a failure;
- no 128 KiB argv limit.

The full list is in §9.

**Failure semantics that matter (verified).**

- `turn/start` does **not** deduplicate on `clientUserMessageId`: the same id
  sent twice started two turns (probe 1).
- Closing stdin ends the app-server in about 65 ms and kills the running turn.
- After an EOF, or a `kill -9`, the in-flight turn reads back as
  **`interrupted`**, the same status a clean interrupt gives. So "interrupted"
  never means "nothing happened".
- Approvals fail closed: a client error becomes a denial (Source).
- An overload reply (`-32001`) can itself be dropped when the outbound queue
  is full (Source).
- `apply_patch` writes files one by one. A failure partway through can leave
  part of a patch applied (Source).
- `codex exec` emits **no** terminal event on SIGINT or SIGTERM (probe 5).

**Sandbox on Linux.** Codex uses bubblewrap plus seccomp. Landlock is legacy
and is rejected for restricted filesystem policies. Under `workspace-write`
(probe 2):

- writes land only in the workspace, plus `/tmp` and `$TMPDIR` unless excluded;
- `.git` is read-only, so `git commit` fails;
- network sockets are refused;
- **reads are not restricted.** A file outside the workspace was read.

The box holds personal and health data, so Codex's sandbox alone is not a
boundary. The Unix user Codex runs as must be unable to read that data. Also,
our preset today runs Codex with **no sandbox at all**
(`--dangerously-bypass-approvals-and-sandbox`, and CLI drivers are not wrapped
in the harness's bwrap).

**Model and data.** Codex 0.163 speaks only the OpenAI Responses API
(`wire_api = "chat"` is a hard error). DESIGN §20 says Strata serves
`/v1/responses` on `127.0.0.1:18080`, so a Codex worker can run on Kate's local
model and keep every byte on the box. A hosted OpenAI model, whether through
ChatGPT sign-in or an API key, sends workspace contents off the box. That is a
separate choice for Kate, and fits only workspaces that contain no personal
data.

**Authentication.** Codex supports:

- ChatGPT browser sign-in, which needs a localhost callback;
- device code (`codex login --device-auth`, or app-server `account/login/start
  {type: "chatgptDeviceCode"}`);
- an API key (`codex login --with-api-key`, read from stdin; or `CODEX_API_KEY`
  for exec);
- for a local provider, nothing beyond its own key, named by `env_key`.

**Never copy `$CODEX_HOME/auth.json` between machines.** It holds bearer
tokens, and ChatGPT refresh tokens are single use: two machines sharing one
copy end with one of them getting "refresh token was already used". The
official docs do describe copying it as a headless fallback (Extract); we
should not.

**Explicit MCP tools (verified, probe 4).**

- `thread/start` takes `config: {"mcp_servers": {...}}`, so each thread gets
  exactly its own servers.
- `enabled_tools` is enforced, and the model sees the tools as namespace
  `mcp__<server>`.
- The MCP server process gets a cleared environment: a short default list
  (`HOME`, `PATH`, `USER`, `LANG` and a few more) plus the named `env_vars`. A
  secret in the app-server's environment did not reach it.
- Under `on-request`, each tool call first comes to the client as an
  `mcpServer/elicitation/request` asking for approval.
- MCP servers run **outside** Codex's sandbox, as the Codex user.

**Smallest phase I would put to Kate (Codex side).**

- **Phase A: no new runtime.** Run the acceptance slice's bounded "fix the
  fixture app" job through the existing `codex` preset, hardened in six small
  ways (§9.4): prompt on stdin, a dedicated `CODEX_HOME`, `web_search` and
  memories off, Strata as the provider, SIGINT before SIGTERM, and git-based
  reconciliation. The harness, not Codex, re-runs the tests and records the
  diff. Who makes the commit follows choice 4 in §11; with Codex's sandbox on,
  only the harness can. This is the SDK path, because the TypeScript SDK does
  exactly this.
- **Phase B: an app-server driver** (§9), once Phase A passes and Kate chooses
  persistent workers. It would use stdio, one process per run, the stable API
  only, a default-deny approval policy owned by the harness, `turn/interrupt`
  for cancel, and reconciliation on every unclean end.

Both phases leave out the WebSocket transport, the daemon and remote control,
both SDK packages as dependencies, Codex memories, `auto_review`, and hosted
models until Kate decides.

---

## 1. The embedding surfaces at 0.163.0

| Surface | What it is | Who uses it | Stability label |
| --- | --- | --- | --- |
| `codex exec [--json]` | One non-interactive run: a prompt goes in, the agent works until the turn ends, and the process exits. `--json` prints JSONL events. `exec resume <id>` continues a saved thread. | Our `codex` preset; the TypeScript SDK; CI scripts | Stable command. The JSONL schema is the `exec_events` types ([exec/src/exec_events.rs][exec-events]) |
| `codex app-server` | A bidirectional JSON-RPC server over stdio (default), a Unix socket, or `ws://IP:PORT`. Persistent threads, turns, items, streamed notifications and server-to-client requests. | VS Code extension, desktop app, TUI (in process), `codex exec` (in process), the Python SDK | `codex --help` labels the whole subcommand **"[experimental]"** at 0.163.0 (probe), and `generate-ts` and `generate-json-schema` too. The **WebSocket transport** is called experimental in the official docs (Extract) |
| TypeScript SDK `@openai/codex-sdk` | A wrapper that spawns `codex exec --experimental-json`, writes the prompt to stdin, and reads JSONL ([sdk/typescript/src/exec.ts][ts-exec]) | Node apps | Version 0.163.0 on npm |
| Python SDK `openai-codex` | A typed JSON-RPC client for `codex app-server --listen stdio://` ([client.py][py-client]). It depends on `pydantic>=2.12` and on `openai-codex-cli-bin==<same version>`, which carries the binary | Python apps | 0.163.0 on PyPI. Classifier "Production/Stable", requires Python 3.10 or later |
| `codex mcp-server` | **Gone.** The 0.163 subcommand list has no `mcp-server` ([cli/src/main.rs][cli-main-subcommands]). A localized SDK page says the command and the standalone binary were removed in favour of app-server (Extract) | (none) | Removed |
| `codex app-server daemon`, `codex remote-control` | A managed background app-server with an optional remote-control path, so ChatGPT clients can drive this machine | Desktop and remote use | Experimental. Remote control was `disabled` by default in probe 1 (`remoteControl/status/changed`) |

**Why `codex exec` and app-server behave alike.** `codex exec` builds an
`InProcessAppServerClient` and sends it `thread/start`, `turn/start` and
`turn/interrupt` ([exec/src/lib.rs][exec-lib-imports]). It then answers the
server's requests in a fixed, non-interactive way:

- command approvals, file-change approvals, `request_user_input` and dynamic
  tool calls are **rejected** ("not supported in exec mode");
- MCP elicitations are **cancelled** ([exec/src/lib.rs][exec-server-requests]).

So exec is app-server with a dumb client, and the JSONL is a lossy projection
of the app-server notifications (§4).

---

## 2. The app-server protocol (stdio JSON-RPC)

### 2.1 Transport and framing

- **Framing.** One JSON object per line on stdin and stdout
  ([app-server-transport/src/transport/stdio.rs][stdio]). It is "not true
  JSON-RPC 2.0": messages neither carry nor expect a `"jsonrpc": "2.0"` field
  ([app-server-protocol/src/rpc.rs][rpc]).
  - A request is `{id, method, params}`.
  - A notification is `{method, params}`.
  - A response is `{id, result}` or `{id, error: {code, message, data}}`.
  - Ids may be strings or integers.
  - Notifications carry `emittedAtMs`.
- **Transports** (`--listen`): `stdio://` (default), `unix://` and
  `unix://PATH`, `ws://IP:PORT` (with websocket auth options), and `off`
  ([cli/src/main.rs][cli-appserver]). Stdio is **single-client mode**: the
  graceful multi-connection drain is not installed ([app-server/src/lib.rs][lib-single]).
- **Lifetime on stdio.** EOF on stdin, or SIGTERM, starts a 45-second process
  deadline, which leaves room for the processor's 30-second RPC drain. When the
  deadline passes, the process exits with code 1
  ([stdio.rs, shutdown watchdog][stdio-watchdog]). **Verified (probe 1):**
  closing stdin in the middle of a turn made the process exit with code 0 after
  0.065 s, with no further notifications. The running turn was abandoned.
- **Backpressure.** Each connection has a 128-message channel
  ([transport/mod.rs][transport-cap]). When the inbound queue is full, a request
  gets error `-32001` "Server overloaded; retry later". If the *outbound* queue
  is also full, that error is dropped with only a log line
  ([transport/mod.rs][transport-overload]). A client must therefore put its own
  timeout on every request, and a request with no reply has an unknown
  outcome.

### 2.2 Handshake

1. The client sends `initialize` with
   `{clientInfo: {name, title?, version}, capabilities?}`
   ([v1.rs][v1-init]). Capabilities include:
   - `experimentalApi`: an opt-in for experimental methods and fields;
   - `optOutNotificationMethods`: exact method names to suppress;
   - `explicitGatewayOauth`, `requestAttestation`, `extensions`.
2. The result is `{userAgent, codexHome, platformFamily, platformOs}`.
3. The client then sends the notification `initialized`.

**Verified (probe 1):** the userAgent was
`swf_probe/0.163.0 (Ubuntu 24.4.0; x86_64) unknown (swf_probe; 0.0.1)`. The
client name is the originator and the server version follows it, so a driver
can check the version against its pin here.

**Experimental gating.** A request that uses an experimental method or field
without `experimentalApi: true` fails with "`<reason>` requires experimentalApi
capability" ([experimental_api.rs][exp-gate]). The **stable** schema generated
without `--experimental` is the contract to build on.

### 2.3 Threads

Stable thread methods (generated `ClientRequest.json`, 105 stable methods in
all):

- `thread/start`, `thread/resume`, `thread/fork`;
- `thread/read`, `thread/list`, `thread/loaded/list`;
- `thread/turns/list`, `thread/items/list`;
- `thread/archive`, `thread/unarchive`, `thread/delete`, `thread/unsubscribe`;
- `thread/name/set`, `thread/metadata/update`, `thread/compact/start`;
- `thread/revert` (`thread/rollback` was removed;
  [app-server/README.md][as-readme]);
- `thread/inject_items`, `thread/shellCommand`.

**`thread/start`, stable params** ([v2/thread.rs][thread-start]; generated
`v2/ThreadStartParams.json`):

- `model` and `modelProvider`;
- `cwd`;
- `approvalPolicy` and `approvalsReviewer`;
- `sandbox`;
- `config`, a free-form map of config overrides;
- `baseInstructions` (replaces Codex's own instructions) and
  `developerInstructions` (added to them);
- `ephemeral` (no rollout on disk, so no resume);
- `personality` (deprecated), `serviceName`, `serviceTier`,
  `sessionStartSource`, `threadSource`.

The response carries `thread` (id, environments with cwd and
`runtimeWorkspaceRoots`, `historyMode: "paginated"` and more), plus the
effective `model`, `modelProvider`, `cwd` and policies. A `thread/started`
notification follows.

Experimental-only fields include `runtimeWorkspaceRoots`, `permissions` (named
permission profiles), `dynamicTools`, `environments` and `historyMode`.

**`thread/resume`** ([v2/thread.rs][thread-resume]) takes `threadId` and the
same overrides as start: model, cwd, policies, sandbox, config, instructions.
Two options control what comes back:

- `excludeTurns` returns metadata only;
- `initialTurnsPage` bootstraps recent turns in the same response.

The 0.163.0 release notes say resuming restores a thread's saved permissions
and network approvals, "while respecting explicit overrides and current managed
policy" ([release commit][tag]).

### 2.4 Turns

- **`turn/start`** ([v2/turn.rs][turn-start]). Stable params:
  - `threadId` and `input` (`text`, `image`, `localImage`, `audio`,
    `localAudio`, `skill`, `mention`);
  - `clientUserMessageId`;
  - `cwd`, `approvalPolicy`, `approvalsReviewer`, `sandboxPolicy`;
  - `model`, `effort`, `summary`, `serviceTier`, `serviceTierForTurn`;
  - `outputSchema`, a JSON Schema for the final message;
  - `parentTurnId`, `rootTurnId`, `turnTrigger`, `toolOutput`,
    `disabledPluginIds`, `personality`.

  An override applies to "this turn and subsequent turns". The response is
  `{turn}` with status `inProgress`. Progress arrives as notifications.
- **`turn/steer`** `{threadId, input, expectedTurnId}`
  ([v2/turn.rs][turn-steer]) adds input to the active turn. `expectedTurnId` is
  a required precondition, and the request fails if it does not match the
  active turn. This is the protocol's only built-in guard against acting twice.
- **`turn/interrupt`** `{threadId, turnId}` returns `{}`. The turn then ends
  with `turn/completed` and `status: "interrupted"`. **Verified (probe 1):**
  during a model stream that never ended, the interrupt was answered and
  `turn/completed {status: interrupted}` arrived within 18 to 19 ms. The 0.163.0
  release notes list "Enable instant interrupts by default" (#51812).
- **Turn object** ([v2/thread_data.rs][turn-struct]): `id` (UUIDv7),
  `rootTurnId`, `items`, `itemsView`, `status`, `error`, `startedAt`,
  `completedAt` and `durationMs`.
  - `status` is one of `completed`, `interrupted`, `failed` or `inProgress`
    ([v2/turn.rs][turn-status]).
  - `error` is `{message, codexErrorInfo, additionalDetails}`. `codexErrorInfo`
    values include `contextWindowExceeded`, `usageLimitExceeded`,
    `rateLimitExceeded`, `serverOverloaded`, `httpConnectionFailed`,
    `responseStreamDisconnected`, `responseTooManyFailedAttempts`,
    `internalServerError`, `unauthorized`, `badRequest`, `sandboxError`,
    `tooManyDenials`, `activeTurnNotSteerable` and `other`
    ([v2/shared.rs][codex-error-info]).

### 2.5 Streamed activity

These are the notifications a driver cares about (the generated
`ServerNotification.json` lists 85):

- **Lifecycle:** `thread/started`, `thread/status/changed` (`active` or
  `idle`), `turn/started`, `turn/completed`.
- **Items:** `item/started` and `item/completed`. Item `type` is one of
  `userMessage`, `agentMessage`, `reasoning`, `plan`, `commandExecution`,
  `fileChange`, `mcpToolCall`, `dynamicToolCall`, `collabAgentToolCall`,
  `subAgentActivity`, `webSearch`, `imageView`, `contextCompaction` and others.
  - `commandExecution` carries `command`, `cwd`, `status` (`inProgress`,
    `completed`, `failed` or `declined`), `exitCode`, `aggregatedOutput` and
    `durationMs`.
  - `fileChange` carries `changes[{path, kind, diff}]` and `status`.
- **Deltas:** `item/agentMessage/delta`, `item/commandExecution/outputDelta`,
  `item/fileChange/patchUpdated`, `item/reasoning/summaryTextDelta`,
  `item/mcpToolCall/progress`.
- **Turn-level:** `turn/diff/updated` (the turn's aggregated unified diff),
  `turn/plan/updated`, and `thread/tokenUsage/updated`, which carries both
  `total` and `last`. Probe 3 showed `total` 22 and `last` 11 after the second
  model call.
- **Errors:** `error {error, willRetry, threadId, turnId}`
  ([v2/notification.rs][error-note]). `willRetry: true` means "transient; the
  server will retry; this will not interrupt the turn". **Verified (probe 1):**
  with `request_max_retries = 0` and a stub returning HTTP 500, one `error`
  arrived with `willRetry: false` and `codexErrorInfo: "internalServerError"`.
  Its message was a canned "We're currently experiencing high demand, which may
  cause temporary errors." even though the server was our own local stub. Read
  `codexErrorInfo`, never the message text. Then came `turn/completed` with
  status `failed`.
- **Other:** `warning` (probe 1 saw "Model metadata for `stub-model` not
  found..." for an unknown model), `serverRequest/resolved`,
  `mcpServer/startupStatus/updated` (`starting`, then `ready`),
  `account/rateLimits/updated`, `remoteControl/status/changed`.

### 2.6 Approvals: requests from the server to the client

Stable server requests (generated `ServerRequest.json`):

- `item/commandExecution/requestApproval`
- `item/fileChange/requestApproval`
- `item/permissions/requestApproval`
- `mcpServer/elicitation/request`, which carries MCP tool-call approvals
  (§5.3) and MCP forms
- `item/tool/requestUserInput`
- `item/tool/call`, a dynamic tool the client implements (§5.5)
- `account/chatgptAuthTokens/refresh`, `attestation/generate`
- the legacy `applyPatchApproval` and `execCommandApproval`

**Command approval** params carry `threadId`, `turnId`, `itemId`,
`approvalId`, `command`, `cwd`, `commandActions`, `reason`, `networkApprovalContext`,
`proposedExecpolicyAmendment` and `availableDecisions` ([v2/item.rs][cmd-approval]).
The decisions are:

- `accept`;
- `acceptForSession`;
- `acceptWithExecpolicyAmendment`, which **persists** a rule;
- `applyNetworkPolicyAmendment`, which persists a host rule;
- `decline`: the agent goes on without running it;
- `cancel`: decline and interrupt the turn ([v2/item.rs][cmd-decisions]).

**Verified (probe 1):** under `approvalPolicy: "untrusted"` the model's
`touch probe-created.txt` came as a server request with `id: 0` and
`availableDecisions: ["accept", {acceptWithExecpolicyAmendment: ...}, "cancel"]`.
Answering `decline` was still accepted: the item ended `declined`, the file was
not created, `serverRequest/resolved` followed, and the turn completed.

**File-change approval** decisions are `accept`, `acceptForSession`, `decline`
and `cancel` ([v2/item.rs][file-decisions]). **Verified (probe 2):** an
`apply_patch` under `untrusted` raised `item/fileChange/requestApproval`.
After `decline` the item was `declined` and the file did not exist. Under
`never` the same patch was applied.

**Fail-closed rules (Source).** If the client answers with an error, or with a
response that cannot be parsed, Codex treats it as "denied: approval request
failed" ([bespoke_event_handling.rs][approval-failclosed]). A pending request
that a turn transition overtakes is resolved with reason `turnTransition` and
dropped ([outgoing_message.rs][turn-transition]). The core's `TimedOut`
decision maps to `decline` ([v2/item.rs][cmd-decisions]). **Nothing in the
protocol turns a missing answer into an approval.**

**Who reviews.** `approvalsReviewer` is `user` (the default: requests go to the
client) or `auto_review` (alias `guardian_subagent`), where "a carefully
prompted subagent ... apply[s] a risk-based decision framework before approving
or denying" ([v2/shared.rs][reviewer]). For Seraphina, `auto_review` means
Codex's own model decides what Kate's custody should decide. It must stay off,
and it can be forbidden on the host (§2.9). `codex exec --approve-for-me` and
the Python SDK's default `ApprovalMode.auto_review` both select it.

### 2.7 Sandbox and approval policy values

- **`approvalPolicy`** (`AskForApproval`; [v2/shared.rs][ask-approval]):
  - `untrusted`: anything not known safe asks;
  - `on-request`: the model asks when it wants out of the sandbox;
  - `never`;
  - `granular {sandbox_approval, rules, skill_approval, request_permissions,
    mcp_elicitations}`, which is experimental (`askForApproval.granular`).

  `on-failure` no longer exists, although the TypeScript SDK's `ApprovalMode`
  type still lists it ([threadOptions.ts][ts-threadoptions]).
- **`sandbox`** on a thread: `read-only`, `workspace-write` or
  `danger-full-access` ([v2/shared.rs][sandbox-mode]).
- **`sandboxPolicy`** on a turn ([v2/permissions.rs][sandbox-policy]):
  - `{type: "dangerFullAccess"}`;
  - `{type: "readOnly", networkAccess}`;
  - `{type: "externalSandbox", networkAccess}`;
  - `{type: "workspaceWrite", writableRoots[], networkAccess,
    excludeTmpdirEnvVar, excludeSlashTmp}`.
- **Permission profiles.** `permissionProfile/list` returned `:read-only`,
  `:workspace` and `:danger-full-access` (probe 3). Choosing a named profile on
  a thread (`permissions`) is experimental. Profiles can deny paths, for
  example `":root" = "read"` with `"/path/.env" = "deny"` in the
  [TypeScript SDK README][ts-readme].

### 2.8 Model selection

- `model` and `modelProvider` are set on `thread/start`, `thread/resume` and
  `turn/start` (per turn: "this turn and subsequent turns"), and `effort` and
  `summary` per turn. `model/list` returns the catalog, checked against managed
  provider requirements ([app-server README][as-readme]).
- **The Responses API only.** `WireApi` has one variant, `responses`. A
  provider with `wire_api = "chat"` fails to load with "`wire_api = "chat"` is
  no longer supported" ([model-provider-info/src/lib.rs][wire-api]). So a
  local model is usable only through a server that implements `/v1/responses`.
  DESIGN §20 says Strata does.
- An unknown model id works with "fallback model metadata" and a `warning`
  (probes 1 to 5). For Strata, the driver's `models` mapping should name the
  model, and the context window may need setting in config. The fallback
  reported `modelContextWindow: 258400` (probe 3), which is **UNVERIFIED**
  against Strata's real limit.
- Provider retry defaults ([model-provider-info/src/lib.rs][provider-defaults]):
  `request_max_retries` 4, `stream_max_retries` 5 (both capped at 100) and
  `stream_idle_timeout_ms` 300000.

### 2.9 Configuration, and what the host can enforce

**Layers**, lowest to highest ([config/src/loader/mod.rs][config-layers]):

1. package defaults;
2. `/etc/codex/config.toml`;
3. cloud;
4. `$CODEX_HOME/config.toml`;
5. a profile `$CODEX_HOME/<name>.config.toml`;
6. `./config.toml` in the cwd, any `.codex/config.toml` in the tree, and the
   repo's `.codex/config.toml`. Each of these is "loaded but disabled when
   untrusted";
7. runtime: `-c` flags, or `thread/start.config`.

Project layers come from **the repository the agent is working on**. A
worktree must never be marked trusted in the worker's `CODEX_HOME`, or that
repository's `.codex/config.toml` could add MCP servers, hooks or providers.

**Requirements** (`/etc/codex/requirements.toml` on Unix) are *enforced* limits
that user and thread config cannot loosen. Fields include
([config/src/config_requirements.rs][requirements]):

- `allowed_approval_policies`, `allowed_approvals_reviewers`,
  `allowed_sandbox_modes`, `allowed_permission_profiles`,
  `default_permissions`;
- `allowed_login_methods`, `allowed_chatgpt_workspaces`,
  `cli_auth_credentials_store`;
- `mcp_servers`, an allowlist;
- `allow_browser_and_computer_use`, `allow_remote_control`,
  `allow_managed_hooks_only`;
- `feature_requirements`, `models`, `network`, `application`,
  `enforce_residency`, `feedback`, `model_provider` and `model_providers`.

Kate holds root, so the host itself can pin what any Codex worker may do:

- `allowed_approvals_reviewers = ["user"]`;
- no `danger-full-access`;
- `allow_remote_control = false`;
- MCP servers limited to the harness's own;
- `cli_auth_credentials_store = "file"`, so nothing goes to a keyring on a
  headless box.

The exact TOML for each field is **UNVERIFIED** (not run).

**Data-leaving-the-box switches** for a worker's config:

- `check_for_update_on_startup = false` (the default is true;
  [core/src/config/mod.rs][update-default]);
- `[analytics] enabled = false` (already off by default for app-server, per
  the `--analytics-default-enabled` help text);
- `[feedback] enabled = false`;
- `web_search = "disabled"` (`web_search` was in the default tool list in
  probe 1);
- `features.memories` stays false. It is off by default
  ([features/src/lib.rs][memories-flag]), and when on it sends past rollouts to
  a model for extraction ([codex-rs/memories/README.md][memories-readme]),
  which would compete with Kate's canonical memory.

### 2.10 Schema versioning, and how to pin it

- There is no protocol version number in the handshake. The version **is** the
  CLI version in `userAgent`. The protocol has `v1` (legacy) and `v2` type
  sets. Fields change between releases, for example `personality` deprecated,
  `thread/rollback` removed, and `readOnly.access` rejected ([app-server README][as-readme]).
- `codex app-server generate-json-schema --out DIR [--experimental]` and
  `generate-ts --out DIR` write the bundle for the installed binary
  ([cli/src/main.rs][cli-generate]).
  - **Verified:** the 0.163.0 npm binary produced a bundle **byte-identical**
    to the checked-in fixtures under
    `codex-rs/app-server-protocol/schema/json` at the tag (`diff -rq` found
    no difference).
  - sha256 of the four files a driver depends on:
    - `codex_app_server_protocol.v2.schemas.json`:
      `d204b96d8560d56600127423b89df9db471b3e22338e8809bb91d5a952998978`
    - `ClientRequest.json`:
      `6b10888f163f569922d42c41e94a98b4261258abc7581c14eca867c3b125dc6f`
    - `ServerRequest.json`:
      `f339be472737a0003efa25fba2e6e6c9237e621cd065b6d6995c51256e9dc1fb`
    - `ServerNotification.json`:
      `8e719512bc26bb3871fcf7b378b79abf912d82787609c0ad418b71f179d0883f`
- **Pinning recipe:**
  1. Install exactly `@openai/codex@0.163.0` (or `openai-codex-cli-bin==0.163.0`).
  2. Commit the stable schema bundle and its sha256 into the harness as test
     fixtures.
  3. At driver start, refuse a `userAgent` whose version differs from the pin.
  4. In `doctor`, regenerate the schema with the installed binary and compare
     the hash.
  5. Run with `experimentalApi: false`.
  6. Run with `--strict-config`, which errors on unknown config fields
     ([cli/src/main.rs][cli-appserver]).

  An upgrade then becomes an explicit change: new pin, new fixtures, and the
  contract tests run again.

### 2.11 Errors

- **Codes** ([app-server/src/error_code.rs][error-codes]):
  - `-32600` invalid request. During shutdown it carries
    `data.reason = "serverShuttingDown"`.
  - `-32601` method not found.
  - `-32602` invalid params.
  - `-32603` internal.
  - `-32001` overloaded.
- The official docs advise retrying `-32001` with exponential backoff and
  jitter (Extract). The Python SDK FAQ says the same: retry only overload, and
  "do not blindly retry all errors" ([faq.md][py-faq]).
- The README's notes on read-state conflicts state the general rule in Codex's
  own words: "Do not blindly retry: the original action may precede an unseen
  result" ([app-server README][as-readme]).

---

## 3. The SDKs: what they wrap

**TypeScript, `@openai/codex-sdk` 0.163.0** ([sdk/typescript/README.md][ts-readme]).

- "The TypeScript SDK wraps the `codex` CLI ... It spawns the CLI and exchanges
  JSONL events over stdin/stdout."
- `Codex.startThread(options).run(prompt)` or `.runStreamed()`; `resumeThread(id)`.
  Thread options: `model`, `sandboxMode`, `workingDirectory`, `skipGitRepoCheck`,
  `approvalPolicy`, `additionalDirectories`, `networkAccessEnabled`,
  `webSearchMode`, `modelReasoningEffort`. Turn options: `outputSchema` and an
  `AbortSignal`.
- Under the hood it runs `codex exec --experimental-json`, with `--config`
  overrides, `--model`, `--sandbox`, `--cd`, `--add-dir`, `--output-schema`
  and `--image`. It writes the prompt to stdin, sets `CODEX_API_KEY` from
  `apiKey`, and kills the child on abort ([exec.ts][ts-exec]).
- **It adds no capability beyond our preset.** It brings no approvals, no
  interrupt short of a kill, and no steering. It also needs Node 18.

**Python, `openai-codex` 0.163.0** ([sdk/python/README.md][py-readme]).

- A synchronous `Codex` and an asynchronous `AsyncCodex` client for
  `codex app-server --listen stdio://` ([client.py][py-client]).
- `thread_start`, `thread_resume`, `thread_fork`; `thread.run()` or
  `thread.turn()`, with `stream()`, `steer()` and `interrupt()` on the turn
  handle.
- Login helpers: `login_chatgpt()`, `login_chatgpt_device_code()`,
  `login_api_key()`. `ExternalMessage` carries untrusted content from other
  agents "with tool-level authority" ([faq.md][py-faq]).
- It depends on `pydantic>=2.12`, `packaging`, and the binary wheel
  `openai-codex-cli-bin==0.163.0`. RELEASING.md says "For CLI version X, the
  SDK version and its exact runtime dependency are both X".
- **Defaults that clash with Kate's custody:**
  - `thread_start(approval_mode=ApprovalMode.auto_review)`
    ([api-reference.md][py-api]), which maps to `on-request` plus the
    `auto_review` reviewer ([_approval_mode.py][py-approval-mode]);
  - with no `approval_handler`, `_default_approval_handler` **returns
    `{"decision": "accept"}`** for both command and file-change approvals
    ([client.py][py-default-handler]).

  Any use would need `ApprovalMode.deny_all` (`never`) or a custom handler.

**Conclusion for the harness.** Our harness is standard-library Python 3.10
and later. Neither SDK can be a dependency without breaking that.

- The TypeScript SDK equals our existing `codex exec` preset, so "use the
  official SDK for bounded jobs" is already true in substance.
- The Python SDK is a good **reference implementation** of an app-server
  client, and the protocol is small enough to speak directly. Our
  `swfactory/harness/mcp.py` already has the shape needed: a stdio JSON-RPC
  client with a writer thread, a reader that routes responses *and answers
  server requests*, a stderr reader and an exit watcher.

---

## 4. `codex exec --json` (our preset) compared with app-server

| | `codex exec --json` (verified on 0.163.0) | `codex app-server` (stdio) |
| --- | --- | --- |
| Process model | One process per run. `exec ... resume <id> <prompt>` continues. Options such as `-C`, `--add-dir`, `-s` and `-p` must come before `resume` (DESIGN §13) | One long-lived process. Many threads and turns over one connection |
| Prompt | Positional argument, **or `-` to read stdin** ([exec/src/cli.rs][exec-cli]). **Verified (probe 5):** a 210,000-byte prompt passed on stdin worked. As an argv item it is capped at 128 KiB (our `MAX_ARG_BYTES`) | `turn/start.input[]` in a JSON line, with no argv limit |
| System text | None; our preset prepends it to the first prompt | `developerInstructions` (added) or `baseInstructions` (replaces Codex's own) on `thread/start` and `thread/resume` |
| Events | `thread.started {thread_id}`, `turn.started`, `item.started`, `item.updated`, `item.completed`, `turn.completed {usage}`, `turn.failed {error}`, `error {message}` ([exec_events.rs][exec-events]). Item types: `agent_message`, `reasoning`, `command_execution`, `file_change`, `mcp_tool_call`, `collab_tool_call`, `web_search`, `todo_list`, `error`. **Verified (probe 5):** a model-metadata warning arrives as an `item.completed` of type `error` | All of §2.5: deltas, diff, plan, token usage (`last` and `total`), warnings, server requests |
| Usage | `turn.completed.usage` is the **thread total** (`usage_from_last_total`; [event_processor_with_jsonl_output.rs][jsonl-usage]), despite the type's doc comment saying "during the turn". **Verified (probe 5):** 20 then 30 input tokens across a resume. Our `usage_scope: "session"` diffing is correct | `thread/tokenUsage/updated` carries `last` and `total` |
| Retry visibility | `error {message}` **drops `willRetry`** ([jsonl error mapping][jsonl-error]), hence our UNREACHABLE heuristic | `error {willRetry, error.codexErrorInfo}` |
| Approvals | `exec` rejects command, file and user-input requests and dynamic tool calls, and cancels MCP elicitations ([exec/src/lib.rs][exec-server-requests]). Headless defaults to `never` ([exec/src/lib.rs][exec-headless]). Our preset adds `--dangerously-bypass-approvals-and-sandbox` | The client answers every request |
| Cancel | Ctrl-C becomes `turn/interrupt` inside exec. **Verified (probe 5):** SIGINT gave exit 1 after 46 ms; SIGTERM gave exit −15 after 37 ms. **Neither printed `turn.failed` or `turn.completed`**: the last JSONL line was `turn.started`. For an interrupted turn exec prints nothing ([jsonl interrupted][jsonl-interrupted]) | `turn/interrupt`, then `turn/completed {status: interrupted}` |
| Exit codes | 0 when the turn completed. 1 when the turn failed or was interrupted, or after a non-retrying `error` ([exec/src/lib.rs][exec-exit]). **Verified (probe 5):** HTTP 500 gave `error` + `turn.failed` + exit 1 | No exit. Each turn reports its own status |
| Stdin trap | With a prompt argument, exec still **reads piped stdin to EOF** and appends it as a `<stdin>` block ([exec/src/cli.rs][exec-cli]). **Verified (probe 5):** a child given an open stdin hung before starting a turn. Our driver uses `stdin=DEVNULL` already | Stdin is the protocol channel |
| Final answer | The last `agent_message`; also `-o FILE` and `--output-schema FILE` | The last `agentMessage`; `outputSchema` per turn |
| Per-run config | `-c key=value`, `--profile`, `--ignore-user-config` (skips `$CODEX_HOME/config.toml`; "auth still uses `CODEX_HOME`"), `--ignore-rules`, `--ephemeral` | `thread/start.config` and the policy fields, per thread |
| Reconciliation | Nothing beyond the session file on disk | `thread/read`, `thread/turns/list` and `thread/items/list`, with item-anchored pagination |

---

## 5. Giving Codex explicit MCP tools

### 5.1 Configuration shape

A server is configured under `[mcp_servers.<name>]` in config TOML, or as the
same keys inside `thread/start.config.mcp_servers` (Verified, probe 4). Fields
([config/src/mcp_types.rs][mcp-raw]):

- **stdio:** `command`, `args`, `env` (values set), `env_vars` (names copied
  from Codex's own environment), `cwd`;
- **HTTP:** `url`, `bearer_token_env_var`, `http_headers`, `env_http_headers`,
  `auth`, `oauth`, `scopes`;
- **control:** `enabled`, `required` (`codex exec` exits with an error if the
  server fails to start), `startup_timeout_sec`, `tool_timeout_sec`,
  `startup_readiness`, `supports_parallel_tool_calls`,
  `tool_input_schema_max_bytes` (default 5,000);
- **tool filtering:** `enabled_tools` (an allowlist) and `disabled_tools`
  (applied after `enabled_tools`; [mcp_types.rs][mcp-config]);
- **tool approval:** `default_tools_approval_mode`, and per tool
  `tools.<name>.approval_mode`, each one of `auto`, `prompt`, `writes` or
  `approve` ([mcp_types.rs][mcp-approval-enum]);
- `tools.<name>.output_token_limit`.

The official MCP page gives defaults of 10 s for startup and 60 s for each tool
call (Extract, UNVERIFIED).

`codex mcp add`, `list`, `get`, `remove`, `login` and `logout` edit the user's
config. They are not needed when the harness passes servers per thread.

### 5.2 What probe 4 showed

The harness passed this `thread/start.config`:

```json
{"mcp_servers": {"probe": {"command": "/usr/bin/python3",
  "args": ["-I", "tests/mock_mcp_server.py"],
  "enabled_tools": ["echo", "env"], "env_vars": ["PROBE_PASSED_VAR"],
  "startup_timeout_sec": 20, "tool_timeout_sec": 30}}}
```

The server was our own test fixture.

- `mcpServerStatus/list {threadId}` showed `probe` with only `echo` and `env`.
  The fixture also has `image`, `fail`, `sleep`, `ask_roots`, `big`, `progress`
  and `write`. Startup went `starting` then `ready`.
- The model was offered the namespace **`mcp__probe`** with tools
  `["echo", "env"]`. It was also offered Codex's built-ins: `exec_command`,
  `write_stdin`, `list_mcp_resources`, `list_mcp_resource_templates`,
  `read_mcp_resource`, `request_user_input`, `view_image`, the namespace
  `multi_agent_v1` (`spawn_agent` and four more), and `get_goal`,
  `create_goal`, `update_goal`. `web_search` was present until it was disabled
  in config.
- When the model called `mcp__probe` / `env` asking for `PROBE_SECRET_TOKEN`
  (set only in the app-server's environment), `PROBE_PASSED_VAR` (named in
  `env_vars`), `HOME` and `CODEX_HOME`, the server returned only `HOME` and
  `PROBE_PASSED_VAR`. The MCP server's environment is built from a short list
  (`HOME`, `LOGNAME`, `PATH`, `SHELL`, `USER`, `LANG`, `LC_ALL`, `TERM`,
  `TMPDIR`, `TZ`) plus the named `env_vars` and the `env` values
  ([rmcp-client/src/utils.rs][mcp-env]), with `env_clear()` first
  ([utils/pty/src/child_command.rs][pty-envclear]). The secret's value did not
  appear anywhere under the probe's `CODEX_HOME` either.

### 5.3 MCP tool approvals are elicitations

Under `on-request`, the `env` call (the tool has no read-only annotation) first
reached the client as **`mcpServer/elicitation/request`**, with:

- `serverName: "probe"` and `mode: "form"`;
- `message: "Allow the probe MCP server to run tool \"env\"?"`;
- `_meta.codex_approval_kind: "mcp_tool_call"` and
  `_meta.persist: ["session", "always"]`;
- `_meta.tool_params`, which carries the exact arguments.

The answer `{"action": "accept", "content": null}` let it run, and an
`mcpToolCall` item completed with the result. Under `codex exec` the same
request would be auto-cancelled, so a tool that needs approval cannot run
there unless approvals are bypassed. The approval policy, together with each
server's `default_tools_approval_mode`, decides whether such a request is
raised. For a browser adapter that is the hook: `navigate` or `read` could be
`auto`, while `click`, `fill` or `submit` on a page with side effects could be
`prompt`, with the harness answering from Kate's rules.

### 5.4 Where MCP servers run

The local stdio launcher spawns the server as Codex's own user, in a new
process group, with **no sandbox**: there is no bubblewrap around it
([rmcp-client/src/stdio_server_launcher.rs][mcp-launch]). An MCP server
therefore has the file and network access of the Codex user, and Codex's
sandbox does not contain it. A browser adapter exposed to Codex should be a thin
MCP client of the browser worker. Profiles, cookies and credentials stay in the
worker, and only observations and results come back.

### 5.5 Harness-implemented tools (dynamic tools)

`thread/start.dynamicTools` (experimental) declares functions or namespaces
`{name, description, inputSchema}` ([protocol/src/dynamic_tools.rs][dyn-tools]).
Codex then sends `item/tool/call {threadId, turnId, callId, namespace, tool,
arguments}`, and the client returns `{contentItems: [inputText | inputImage],
success}` ([v2/item.rs][dyn-call]). That would let the harness serve the
browser tool itself, with no MCP process, and keep the session lease in one
place. It needs `experimentalApi: true`, so it is a later option, not Phase B's
default. The 0.163.0 release notes mention "Complete dynamic tool lifecycles on
cancellation" (#51556).

### 5.6 Codex's own browser features

Codex has `browser_use`, `browser_use_external`, `in_app_browser` and
`computer_use`. These are **desktop-app integrations** ("Allow Browser Use agent
integration in desktop apps"), set by requirements
([features/src/lib.rs][browser-flags]). They do not give a headless server
worker a browser. On the server, the browser reaches Codex as an explicit MCP
server (or a dynamic tool), and `allow_browser_and_computer_use = false` can be
set in requirements.

---

## 6. The Linux sandbox

### 6.1 Mechanism (Source: [codex-rs/linux-sandbox/README.md][linux-sandbox])

- **Bubblewrap** isolates the filesystem and namespaces. Codex prefers the
  first `bwrap` on `PATH` outside the cwd, and falls back to a bundled one. The
  npm package ships `codex-resources/bwrap`.
- **Seccomp** filters system calls, after `PR_SET_NO_NEW_PRIVS`.
- **Landlock** is the "legacy" option and is **rejected** for
  filesystem-restricted policies "because it cannot isolate app-server Unix
  sockets".
- The filesystem is read-only by default (`--ro-bind / /`). Writable roots are
  bound read-write.
- Protected subpaths under writable roots are re-bound read-only: `.git`, the
  resolved `gitdir:`, `.codex`, `.agents` and `.aws` ([protocol/src/permissions.rs][protected]).
  An explicit rule in a permission profile can override this, because each
  default is added only "if no explicit rule" exists ([permissions.rs][protected-default]).
  Overriding `.git` is **UNVERIFIED**.
- Restricted network means `--unshare-net` plus a seccomp block on network
  syscalls. `io_uring` is always blocked. There is a managed proxy mode with a
  TCP-to-UDS bridge.
- There are user and PID namespaces, a fresh `/proc`, and masking of symlinked
  or missing protected paths.

### 6.2 What `workspace-write` does (Verified, probes 2 and 3)

| Action inside the sandbox | Result |
| --- | --- |
| Write a file in the repo (cwd) | Allowed |
| `git add` and `git commit` in the repo | **Fails**: `Unable to create '.../repo/.git/index.lock': Read-only file system` |
| **Read** a file outside the workspace | **Allowed** (`cat` of a file outside the repo printed it) |
| Write under `/tmp` outside the repo | Allowed by default (`/tmp` and `$TMPDIR` are writable roots) |
| The same with `sandboxPolicy {type: workspaceWrite, excludeSlashTmp: true, excludeTmpdirEnvVar: true}` | Write outside the repo fails with `Read-only file system`, write inside succeeds |
| Network: a TCP connect to `127.0.0.1` | Fails at `socket()` with `PermissionError: [Errno 1] Operation not permitted` |
| `... ; rm -f /tmp/x` under `never` | **Rejected before running** by Codex's built-in command policy ("rm -f style commands are not permitted"). **No `commandExecution` item was emitted for it** in 0.163. The repository's unreleased changelog has the fix ("Fix missing command lifecycle events when unified exec cannot create a process") |

### 6.3 Consequences for Seraphina

- **Reads are the gap.** A sandboxed Codex command can read anything the Codex
  Unix user can read, and the model then sees it. If the model is hosted, the
  data leaves the box. Codex's sandbox therefore cannot be the barrier around
  personal and health data. The barrier has to be the Unix user's file
  permissions, the harness's own bwrap mount view, or both. Today the server's
  clients run as `ai-dev` (DESIGN §20). Whether `ai-dev` can read the personal
  data is **UNVERIFIED**: check it with `sudo -u ai-dev test -r <path>` for each
  data root. A deny-read permission profile (`":root" = "deny"` and the like)
  is possible in principle but needs the experimental `permissions` field, and
  was not tested.
- **Who commits.** Under `workspace-write` Codex cannot commit. Our factory's
  builder is expected to commit: DESIGN §6's build check requires commits
  ahead of base and a clean worktree, and otherwise ends in needs-human. With
  Codex's sandbox on, the clean split is **Codex edits and the harness
  commits**: code makes the commit from the worktree after the write-policy
  check, which also makes the commit sha a code-written receipt rather than an
  agent's claim. The alternatives are overriding `.git` in a permission
  profile (UNVERIFIED), or keeping today's no-sandbox mode, where the harness's
  write policy after the run is the only containment (DESIGN §17).
- **MCP servers are outside the sandbox** (§5.4).
- **Our preset today:** `--dangerously-bypass-approvals-and-sandbox` means
  Codex runs every command as the factory's user with full file and network
  access. CLI drivers are not wrapped in the harness's bwrap (`CliRunner._execute`
  calls `subprocess.Popen` directly). This is the documented §17 trade-off
  ("run as a dedicated Unix user, or enable bwrap"), and it is exactly what a
  Seraphina worker should not inherit.

### 6.4 Ubuntu caveat (UNVERIFIED)

Ubuntu 23.10 and later can restrict unprivileged user namespaces through
AppArmor (`kernel.apparmor_restrict_unprivileged_userns`). In that case,
bubblewrap needs an AppArmor profile that allows `userns`. Codex says it
"surfaces a startup warning when bubblewrap cannot create user namespaces"
([linux-sandbox README][linux-sandbox]). The Codex source has no AppArmor
handling. On the server, check the sysctl, and run one sandboxed command with a
probe like probe 2 before relying on the sandbox. Nesting Codex's bwrap inside
the harness's bwrap is also untested.

---

## 7. Authentication

### 7.1 Methods

| Method | CLI | app-server | Notes |
| --- | --- | --- | --- |
| ChatGPT browser sign-in | `codex login` | `account/login/start {type: "chatgpt"}`, which returns an auth URL; then `account/login/completed` | Needs a browser that can reach the local callback port. On a headless server that means an SSH tunnel or a different method |
| Device code | `codex login --device-auth` | `account/login/start {type: "chatgptDeviceCode"}` | Prints a URL and a one-time code that "expires in 15 minutes", with a phishing warning: "If a website or another person gave you this code, cancel" ([device_code_auth.rs][device-code]). A workspace admin may need to allow device-code login (third-party Extract, UNVERIFIED) |
| API key | `printenv OPENAI_API_KEY \| codex login --with-api-key` (stdin only; `--api-key VALUE` is deprecated and refused) ([cli/src/main.rs][cli-login]) | `account/login/start {type: "apiKey", apiKey}` | `codex exec` and the TypeScript SDK also take `CODEX_API_KEY` from the environment ([exec.ts][ts-exec]). Our preset keeps `OPENAI_*` and `CODEX_*` (`env_keep`) |
| Access token | `codex login --with-access-token` (stdin) | `chatgptAuthTokens` is "[UNSTABLE] FOR OPENAI INTERNAL USE ONLY" ([v2/account.rs][account-login]) | Do not use |
| Local provider (Strata) | none: `[model_providers.strata] env_key = "STRATA_API_KEY"` | none | No OpenAI account is involved. The key comes from the server's env file (DESIGN §20) and reaches the CLI only through driver `env: {"STRATA_API_KEY": "${STRATA_API_KEY}"}` (DESIGN §13) |

`codex login status` and `account/read` report the state. `account/logout`
clears it.

### 7.2 Storage, and what must never be copied

- `$CODEX_HOME/auth.json` ([login/src/auth/storage.rs][auth-json]) holds
  `OPENAI_API_KEY`, `tokens` (access, refresh and id tokens), `last_refresh`,
  `agent_identity` (which can hold a private key), `personal_access_token`
  and Bedrock keys.
- `cli_auth_credentials_store` is `file` (the default), `keyring`, `auto` or
  `ephemeral` (memory only, for this process) ([config/src/types.rs][store-modes]).
- MCP OAuth credentials go to the keyring, or to `$CODEX_HOME/.credentials.json`
  ([rmcp-client/src/oauth.rs][mcp-cred]).

**Never copy between machines:**

- `auth.json`, keyring entries (service "Codex Auth") and `.credentials.json`;
- `$CODEX_HOME/sessions/**` (rollout JSONL: the full transcript, including
  file contents and command output the agent saw);
- the state databases: probe runs created `state_5.sqlite`,
  `thread_history_1.sqlite`, `logs_2.sqlite`, `memories_1.sqlite`,
  `goals_1.sqlite` and `queue_1.sqlite`;
- `installation_id`, which identifies an installation.

The concrete reason for tokens: ChatGPT refresh tokens are single use, and
reuse fails with "Your access token could not be refreshed because your
refresh token was already used. Please log out and sign in again"
([login/src/auth/manager.rs][refresh-reused]). Two machines sharing one copy
will lock each other out. Beyond that, the file is a bearer credential for the
account.

The official docs describe "copy `~/.codex/auth.json`" to a headless machine as
a fallback, and warn to "treat it like a password" (Extract). Astra's guidance,
and this note, say not to. Sign in on the server itself with device code or an
API key, or use a local provider and no OpenAI login at all.

**For the harness:**

- each worker gets its own `CODEX_HOME`, owned by the worker's Unix user with
  mode 0700, never under a repository or a job folder an agent can write;
- the driver's environment scrubbing (DESIGN §13) keeps other secrets out;
- `requirements.toml` can pin `allowed_login_methods` and
  `cli_auth_credentials_store`.

---

## 8. Failure semantics

| Event | What Codex does (0.163.0) | What the harness must record and do |
| --- | --- | --- |
| Model API down or slow | It retries HTTP 4 times and stream reconnects 5 times (defaults). The stream idle timeout is 300 s. app-server sends `error {willRetry: true}` while retrying, and finally `willRetry: false` with `turn.status = failed` and a `codexErrorInfo` (Verified for HTTP 500). exec prints `error {message}` only, with no `willRetry` | Outcome `failed`, with the `codexErrorInfo` as the reason, never the message text. The driver's own wall-clock timeout stays, as a last resort |
| Harness wall-clock timeout | Nothing; Codex does not know about it | Send `turn/interrupt` (exec: SIGINT), wait briefly, then close stdin or SIGTERM. Record **`timed_out`** with outcome **unknown until reconciled**, never success. Reconcile (below) |
| Kate or David cancels | app-server: `turn/interrupt` gives `interrupted` in about 20 ms (Verified). exec: SIGINT gives exit 1 with no terminal JSONL event (Verified) | Record `cancelled`, then reconcile. Commands already run and files already written stay as they are |
| Harness crash or restart (EOF on stdio) | app-server exits in about 65 ms, and the turn is abandoned (Verified). On `thread/resume` or `thread/read`, that turn reads **`interrupted`** (Verified) | After a restart, every run that was in flight is **unknown**. Read the thread (`thread/read`) and the worktree (`git status`, `git diff`) before deciding anything. Never re-send its prompt automatically |
| `kill -9` of Codex (OOM, power-off at 23:00) | The turn also reads `interrupted` after the next start (Verified) | Same as above. The nightly power-off means "in flight at 23:00" is a normal case: the morning reconcile must handle it, and long jobs should not start shortly before 23:00 |
| A lost `turn/start` reply | The turn may or may not exist. **No deduplication** by `clientUserMessageId`: the same id twice started two turns (Verified). The id is echoed on the `userMessage` item (`clientId`; [protocol.rs][client-id]) | Before re-sending, `thread/read` (or `thread/items/list`) and look for a `userMessage` whose `clientId` equals the harness's id. Send again only if it is absent. Use a fresh, harness-made id per attempt: the run id plus the attempt number |
| `-32001` overloaded, or no reply at all | Retrying later is safe for read methods. For `turn/start` the reply can be dropped even when the request was refused (Source) | Treat as "lost reply" (above) for `turn/start` and `turn/steer`. Read-only methods may be retried with backoff |
| Pending approval when the client errors or disconnects | Fail closed: denied, "approval request failed" (Source). A turn transition resolves pending requests with `turnTransition` | Record the approval as **not given**. An approval reply that arrives after its turn ended has no effect, so never assume it did |
| A patch in flight | `apply_patch` applies hunks to files **one at a time**. A failed write can modify a file before it errors, and Codex then marks its own delta "no longer exact" ([apply-patch/src/lib.rs][apply-hunks]). Interrupting or killing mid-patch can leave the patch partly applied (UNVERIFIED by a run; inferred from the sequential loop) | The worktree is the truth: record the `git diff` (sha256 of the diff plus the list of paths) as the run's artifact on every ending, clean or not. Kate or a re-run decides whether to keep it. Whoever commits (§11, choice 4) does so only after this check |
| Command rejected by Codex's own policy | Rejected before running. In 0.163 there may be **no item at all** for it (Verified, `rm -f`) | Do not expect one item per tool call. Count outcomes from the files and the git state, not from item counts |
| Duplicate submit from the browser side | (Not Codex.) | Covered by the browser adapter's lease and idempotency design, not here |

**Outcome classes** for the receipt:

- `completed`: `turn.status` completed, the files were checked, and the
  harness re-ran the tests;
- `failed`: with `codexErrorInfo`;
- `declined`: an approval was refused and the turn ended;
- `cancelled`;
- `timed_out`;
- `unknown`: crash or EOF, kept until a reconcile moves it to one of the
  others.

A transport timeout is never `completed`.

---

## 9. What a new app-server driver would add over the `codex` preset

### 9.1 The preset today (cli.py, DESIGN §13)

- **argv:** `exec --dangerously-bypass-approvals-and-sandbox
  --skip-git-repo-check --json -m {model} -C {cwd} --add-dir {job_dir}
  {prompt}`.
- **Resume:** the same options, then `resume {session_id} {prompt}`.
- **Prompts:** `system_mode` is `prepend`, so the system text and the prompt
  form one argv item of at most 128 KiB.
- **Session id:** read from `thread.started`.
- **Output:** JSONL parsed into `agent.text`, `agent.reasoning`,
  `agent.tool_call` and `agent.tool_result` (`_codex_item`, `_codex_tool`).
- **Usage:** the session total, diffed against `sessions/<agent>.cli.json`.
- **Stopping:** cancel and timeout send SIGTERM to the process group, then
  SIGKILL after 3 s. Four `error` events over at least 60 s stop the run as
  "model API unreachable".
- **Environment:** the server's environment minus secrets, plus `OPENAI_*`,
  `CODEX_*` and driver `env`.
- **Containment:** no sandbox and no approvals; the write policy is checked
  after the run (DESIGN §6).

### 9.2 What an app-server driver adds, item by item

1. **Approvals routed to Seraphina.** Command, file-change, permission and
   MCP-tool requests arrive as server requests, and the driver answers each
   one from a policy in code, owned by Kate:
   - default **decline**;
   - **never** `acceptWithExecpolicyAmendment`, `applyNetworkPolicyAmendment`
     or `persist: always`, because those write lasting grants into
     `CODEX_HOME`;
   - each request and its answer becomes an event and goes into the receipt.

   This is the main new capability. With it, Codex's sandbox can be on while
   an out-of-sandbox action is still possible after Kate (or her rules) says
   yes.
2. **Codex's sandbox on, per thread and per turn**, which the preset cannot
   have because it bypasses the sandbox: `sandbox: "workspace-write"` and
   `sandboxPolicy {type: workspaceWrite, writableRoots: [job_dir],
   networkAccess: false, excludeSlashTmp: true, excludeTmpdirEnvVar: true}`.
   It comes with the "harness commits" split (§6.3).
3. **A real cancel.** `turn/interrupt` gives an `interrupted` status in about
   20 ms, then stdin is closed. This replaces SIGTERM, which leaves no terminal
   event (probe 5).
4. **Steering a running turn.** `turn/steer` with `expectedTurnId` lets Kate or
   the harness add a correction without starting over, and the precondition
   stops a second, stale steer.
5. **Explicit retry and error semantics.** `error.willRetry` and
   `turn.error.codexErrorInfo` replace `UNREACHABLE_ERRORS` and
   `UNREACHABLE_SECONDS`. A retrying error never stops the run; a final one
   gives a typed reason such as `usageLimitExceeded`, `unauthorized` or
   `contextWindowExceeded`.
6. **Explicit, per-thread tools.** `thread/start.config.mcp_servers` with
   `enabled_tools`, `env_vars` and timeouts, so each job gets exactly the
   tools it should have (the browser adapter, REA) without editing a shared
   `config.toml`. Later, `dynamicTools`, behind an experimental opt-in.
7. **The system text in the right slot.** `developerInstructions` on
   `thread/start` and `thread/resume`, instead of being prepended to the user
   prompt. Optionally `baseInstructions` for a lean worker prompt. Neither
   carries Kate's persona; it carries only the task's context.
8. **No prompt-size limit,** because input is a JSON line. The preset can get
   most of this too, with `-` and stdin (§9.4).
9. **Finer progress for David.** `item/agentMessage/delta`,
   `item/commandExecution/outputDelta`, `turn/plan/updated` and
   `turn/diff/updated` map onto the existing `agent.*` events (DESIGN §10), so
   the harness GUI can show live output, the plan and the current diff. How
   much of that reaches Open WebUI depends on the persona chat path of DESIGN
   §33, which this note did not check.
10. **Usage without a side file.** `thread/tokenUsage/updated.last` is this
    call's usage, so `_run_usage` and `sessions/<agent>.cli.json` are not
    needed for this driver.
11. **Reconciliation.** `thread/read`, `thread/turns/list` and
    `thread/items/list`, together with the echoed `clientId`, let the driver
    answer "did my turn start, and how did it end?" after any failure (§8).
    exec has nothing like this.
12. **Structured final answer.** `outputSchema` per turn, so a coding job
    returns `{summary, files_changed, tests_claimed}` as JSON. Every claim in
    it is still checked by the harness, never trusted.
13. **A persistent worker when wanted.** One app-server process can hold a
    thread across many turns (a chat-like Codex session). For factory runs I
    would still use one process per run, so a crash affects one run, and the
    process ends when the run does.
14. **A version and schema pin at runtime.** The `userAgent` version is
    checked at `initialize`, and the schema hash in `doctor` and tests (§2.10).

### 9.3 What stays the same, or is reused

- The `Runner` interface: `run()` and `resume()` return an `AgentRun`, and
  `session_ref` is the Codex thread id.
- Command resolution (`_command_argv`), the scrubbed environment
  (`environment()`), the line redaction before any event, and `cli_model()`
  for the model id.
- The orchestrator's write-policy check after every run (DESIGN §6), and the rule
  that a runner never decides pass or fail.
- The mapping to `agent.tool_call` and `agent.tool_result`. The fields are
  camelCase (`commandExecution.aggregatedOutput`, `exitCode`;
  `fileChange.changes[].kind.type`; `mcpToolCall.server`, `.tool` and
  `.result`), so `_codex_tool` needs a sibling, not a rewrite.
- The JSON-RPC plumbing can follow `swfactory/harness/mcp.py`: a writer thread
  that owns stdin, a stdout reader that routes responses and server requests,
  a stderr reader and an exit watcher. Codex omits `"jsonrpc"`, and that is the
  only framing difference.

### 9.4 Fixes worth making to the existing preset now (Phase A)

These came out of the probes and are small:

- **Prompt on stdin.** Pass `-` as the prompt and write the text to stdin, so
  the 128 KiB argv limit disappears (a 210,000-byte prompt was verified). The
  driver then needs a stdin writer in place of `stdin=DEVNULL`, closed right
  after writing, because exec reads stdin to EOF.
- **SIGINT before SIGTERM** on cancel and timeout, so exec can send
  `turn/interrupt` and Codex stops its own child processes in an orderly way
  (exit 1 after 46 ms). Then SIGTERM and SIGKILL as today.
- **A dedicated `CODEX_HOME` per worker**, and a worker `config.toml` (or
  driver `-c` flags) that sets:
  - `web_search = "disabled"`, `check_for_update_on_startup = false`;
  - `[analytics] enabled = false`, `[feedback] enabled = false`;
  - `features.memories = false`;
  - the Strata provider (`wire_api = "responses"`, `env_key =
    "STRATA_API_KEY"`);
  - and, for the job, `request_max_retries` and `stream_max_retries` small
    enough that the CLI fails before the harness's wall clock does.

  Consider `--ignore-user-config` as well, so `$CODEX_HOME/config.toml` is
  skipped and the worker's settings come from `-c` flags. Whether it also
  skips a `--profile` file is UNVERIFIED.
- **Error text.** Do not show exec's `error.message` as the cause. Its text is
  canned (probe 5: "high demand" for a local 500).
- **Warnings.** The parser could surface exec's `item.completed` items of type
  `error` (warnings) as an `agent.output` note rather than drop them.
- **Bounded jobs.** For the acceptance slice's bounded job, the harness re-runs
  the fixture's tests itself and records the diff, and the commit sha when it
  makes the commit, as code-written evidence. The agent's own claims stay
  claims.

### 9.5 What the new driver must not do

- Use the WebSocket transport, the daemon or remote control. On this server
  only local stdio is needed and wanted: no inbound ports, Tailscale only.
- Opt into `experimentalApi` without a pinned need. Choosing `dynamicTools` or
  `permissions` later is a separate, versioned decision.
- Use `approvalsReviewer: auto_review`, `--approve-for-me`, the Python SDK's
  accept-all default, or any decision that persists a grant.
- Mark a worktree as a trusted project, or let a job write into `CODEX_HOME`.
- Retry `turn/start` or `turn/steer` without first reading the thread (§8).

---

## 10. Where the receipts attach

Seraphina's v2 design lists evidence kinds that only the executor or the
harness can write (`executor-receipt` and others; SERAPHINA_OS_HARNESS.md
§4.3). In v1.x the factory already records `agent.start` with model, provider
and Grid Card receipts, `job.runs`, and code-checked `commit` and `file`
evidence (DESIGN §26, §32).

A Codex run's receipt would add:

- **Identity:**
  - the harness request id: job, agent and attempt;
  - Codex `thread.id` and each `turn.id`;
  - the `clientUserMessageId` the harness chose;
  - the Codex version (from `userAgent`) and the schema sha256;
  - `model` and `modelProvider` as the server reported them in the
    `thread/start` response;
  - the effective `sandbox` and `approvalPolicy`.
- **Approvals:** each server request (method, item id, the command or paths,
  the time) and the decision the harness sent, with the rule or person that
  made it.
- **Artifacts:** the final `turn/diff/updated` diff (sha256 and paths), the
  final agent message, and the `outputSchema` JSON if one was asked for.
- **Validation evidence written by code:** the commit sha the harness made, and
  the test commands the harness re-ran, with their exit codes.
- **Outcome:** one of the §8 classes, with `codexErrorInfo` where there is one.

Do not use `turn/start.responsesapi_client_metadata` for these ids. It is
flattened into request metadata sent to the **model provider** ([v2/turn.rs][turn-start]).
With a hosted model that is off the box. `parentTurnId`, `rootTurnId` and
`turnTrigger` are Codex-internal attribution fields. They are harmless, but they
are not receipts.

---

## 11. Choices to put to Kate

1. **Model for a Codex worker.** The local Strata model only, which keeps all
   data on the box and needs no OpenAI account (the recommended default). Or
   also a hosted OpenAI model, through device-code ChatGPT sign-in or an API
   key on the server, and if so, for which workspaces. Each hosted run sends
   the workspace it sees off the box.
2. **Phase.** Phase A (harden the `codex` preset, use it for bounded jobs)
   alone. Or Phase A, then Phase B (an app-server driver for persistent tasks
   and approvals). Phase B is worth building only if Kate wants approvals in
   the loop or long-lived Codex threads.
3. **Approval policy.** Either `never` with Codex's sandbox on, so out-of-sandbox
   actions simply fail and nothing asks. Or `on-request` or `untrusted`, with
   the harness answering. In the second case: which request classes Kate
   approves herself, which her rules approve, and which are always declined.
4. **Who commits.** The harness after the write-policy check (recommended with
   Codex's sandbox on), or the agent (only without Codex's sandbox, as today).
5. **Containment layering.** Which Unix user Codex runs as, and confirming
   that it cannot read the personal and health data. Whether the harness's
   bwrap should also wrap the Codex process: nested bubblewrap is untested on
   the server, and so is the Ubuntu AppArmor setting.
6. **Pin and upgrade.** Pin Codex 0.163.0 and its schema hash. An upgrade is a
   reviewed change with new fixtures and the contract tests run again. Also,
   who installs it on the server, and when, given that the battery stays
   undisturbed.

---

## Sources

All repository links are pinned to tag `rust-v0.163.0`, commit
`83086188ae0614bd882818ff1aa23f3194962012`.

[tag]: https://github.com/openai/codex/tree/83086188ae0614bd882818ff1aa23f3194962012
[as-readme]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/app-server/README.md
[rpc]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/app-server-protocol/src/rpc.rs#L1-L11
[v1-init]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/app-server-protocol/src/protocol/v1.rs#L29-L83
[thread-start]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/app-server-protocol/src/protocol/v2/thread.rs#L62-L166
[thread-resume]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/app-server-protocol/src/protocol/v2/thread.rs#L354-L444
[turn-status]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/app-server-protocol/src/protocol/v2/turn.rs#L33-L38
[turn-start]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/app-server-protocol/src/protocol/v2/turn.rs#L170-L305
[turn-steer]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/app-server-protocol/src/protocol/v2/turn.rs#L311-L350
[turn-struct]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/app-server-protocol/src/protocol/v2/thread_data.rs#L386-L410
[codex-error-info]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/app-server-protocol/src/protocol/v2/shared.rs#L76-L127
[ask-approval]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/app-server-protocol/src/protocol/v2/shared.rs#L206-L220
[reviewer]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/app-server-protocol/src/protocol/v2/shared.rs#L264-L281
[sandbox-mode]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/app-server-protocol/src/protocol/v2/shared.rs#L335-L340
[sandbox-policy]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/app-server-protocol/src/protocol/v2/permissions.rs#L541-L566
[cmd-decisions]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/app-server-protocol/src/protocol/v2/item.rs#L66-L109
[file-decisions]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/app-server-protocol/src/protocol/v2/item.rs#L115-L124
[cmd-approval]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/app-server-protocol/src/protocol/v2/item.rs#L1555-L1660
[dyn-call]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/app-server-protocol/src/protocol/v2/item.rs#L1663-L1692
[error-note]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/app-server-protocol/src/protocol/v2/notification.rs#L62-L69
[account-login]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/app-server-protocol/src/protocol/v2/account.rs#L64-L120
[exp-gate]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/app-server-protocol/src/experimental_api.rs#L30-L32
[approval-failclosed]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/app-server/src/bespoke_event_handling.rs#L1931-L1970
[turn-transition]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/app-server/src/outgoing_message.rs#L221-L235
[error-codes]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/app-server/src/error_code.rs#L1-L13
[lib-single]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/app-server/src/lib.rs#L793-L795
[stdio]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/app-server-transport/src/transport/stdio.rs
[stdio-watchdog]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/app-server-transport/src/transport/stdio.rs#L125-L180
[transport-cap]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/app-server-transport/src/transport/mod.rs#L24
[transport-overload]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/app-server-transport/src/transport/mod.rs#L228-L262
[apply-hunks]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/apply-patch/src/lib.rs#L438-L469
[client-id]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/protocol/src/protocol.rs#L2663-L2666
[dyn-tools]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/protocol/src/dynamic_tools.rs#L13
[protected]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/protocol/src/permissions.rs#L40-L50
[protected-default]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/protocol/src/permissions.rs#L860-L870
[exec-lib-imports]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/exec/src/lib.rs#L19-L58
[exec-headless]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/exec/src/lib.rs#L589-L591
[exec-exit]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/exec/src/lib.rs#L1335-L1404
[exec-server-requests]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/exec/src/lib.rs#L2042-L2140
[exec-cli]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/exec/src/cli.rs#L36-L85
[exec-events]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/exec/src/exec_events.rs#L11-L75
[jsonl-usage]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/exec/src/event_processor_with_jsonl_output.rs#L118-L127
[jsonl-error]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/exec/src/event_processor_with_jsonl_output.rs#L447-L457
[jsonl-interrupted]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/exec/src/event_processor_with_jsonl_output.rs#L559-L563
[cli-main-subcommands]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/cli/src/main.rs#L100-L262
[cli-login]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/cli/src/main.rs#L516-L556
[cli-appserver]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/cli/src/main.rs#L571-L640
[cli-generate]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/cli/src/main.rs#L712-L735
[mcp-approval-enum]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/config/src/mcp_types.rs#L28-L34
[mcp-config]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/config/src/mcp_types.rs#L223-L300
[mcp-raw]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/config/src/mcp_types.rs#L367-L400
[mcp-env]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/rmcp-client/src/utils.rs#L16-L26
[mcp-launch]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/rmcp-client/src/stdio_server_launcher.rs#L262-L300
[mcp-cred]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/rmcp-client/src/oauth.rs#L857
[pty-envclear]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/utils/pty/src/child_command.rs#L97-L99
[linux-sandbox]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/linux-sandbox/README.md
[config-layers]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/config/src/loader/mod.rs#L110-L135
[requirements]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/config/src/config_requirements.rs#L1036-L1079
[store-modes]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/config/src/types.rs#L136-L146
[update-default]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/core/src/config/mod.rs#L4202
[auth-json]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/login/src/auth/storage.rs#L47-L73
[refresh-reused]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/login/src/auth/manager.rs#L206-L208
[device-code]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/login/src/device_code_auth.rs#L149-L158
[wire-api]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/model-provider-info/src/lib.rs#L100-L130
[provider-defaults]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/model-provider-info/src/lib.rs#L66-L75
[memories-flag]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/features/src/lib.rs#L1280-L1284
[browser-flags]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/features/src/lib.rs#L282-L323
[memories-readme]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/codex-rs/memories/README.md
[ts-readme]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/sdk/typescript/README.md
[ts-exec]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/sdk/typescript/src/exec.ts#L95-L205
[ts-threadoptions]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/sdk/typescript/src/threadOptions.ts
[py-readme]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/sdk/python/README.md
[py-client]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/sdk/python/src/openai_codex/client.py#L215-L320
[py-default-handler]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/sdk/python/src/openai_codex/client.py#L833-L839
[py-approval-mode]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/sdk/python/src/openai_codex/_approval_mode.py
[py-api]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/sdk/python/docs/api-reference.md
[py-faq]: https://github.com/openai/codex/blob/83086188ae0614bd882818ff1aa23f3194962012/sdk/python/docs/faq.md

Other sources:

- Package registries, read 2026-10-21: <https://pypi.org/pypi/openai-codex/json>
  (0.163.0, requires `openai-codex-cli-bin==0.163.0`),
  <https://registry.npmjs.org/@openai/codex-sdk> and
  <https://registry.npmjs.org/@openai/codex> (latest 0.163.0, alpha
  0.164.0-alpha.9).
- **Official pages that could not be opened from the research sandbox:**
  - <https://developers.openai.com/blog/codex-as-a-platform> (Astra's link; a
    search found no post by that title, only OpenAI's engineering post
    "Unlocking the Codex harness: how we built the App Server",
    <https://openai.com/index/unlocking-the-codex-harness>, 2026-02-04, per
    search extracts);
  - <https://learn.chatgpt.com/docs/app-server> and
    <https://developers.openai.com/codex/app-server> (transports, the
    experimental WebSocket transport, the `-32001` backoff advice: Extract);
  - <https://learn.chatgpt.com/docs/extend/mcp?surface=cli> and
    <https://developers.openai.com/codex/mcp> (`startup_timeout_sec` default
    10, `tool_timeout_sec` default 60, `enabled_tools` and `disabled_tools`:
    Extract);
  - <https://developers.openai.com/codex/auth> (device code, and the
    `auth.json` copy fallback with its "treat it like a password" warning:
    Extract);
  - <https://developers.openai.com/codex/sdk> (SDK overview: Extract).

  Where these disagree with the pinned source or the probes, the source and
  the probes are what this note relies on.
- **Third-party** (used only to cross-check names, never as the sole basis for
  a claim): <https://codex.danielvaughan.com/2026/03/28/codex-app-server-json-rpc-protocol/>,
  <https://www.promptfoo.dev/docs/providers/openai-codex-app-server/>.
- **This repository:** `swfactory/drivers/cli.py` (the `codex` preset,
  `_codex_item`, `_codex_tool`, `_wait`, `_stop_group`), `swfactory/runners.py`
  (`Runner`, `RunContext`, `AgentRun`), `swfactory/harness/mcp.py`, and
  DESIGN.md §6, §8, §10, §12, §13, §17, §20, §26, §32 and §33.
