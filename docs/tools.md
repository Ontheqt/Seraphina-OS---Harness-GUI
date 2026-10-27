# Tools: REA, agent-browser, the browser adapter and billion-context

What each outside tool does for the factory, how to connect it, and what was
actually checked. Everything marked **verified** was run on 2026-10-21 in the
build sandbox (Ubuntu 24.04, Node 22.22.0, Python 3.12, Playwright's Chromium
141), installed into a scratch directory, never globally. The owner's server
(Node 24.22, `~/.local/npm` prefix, user `ai-dev`) has not been touched.

`tests/test_tools_integration.py` repeats the checks. Without the tools it only
checks the shipped files; with them:

```bash
FACTORY_E2E_TOOLS=1 \
FACTORY_REA_BIN=~/.local/npm/bin/rea \
FACTORY_SKILLS_CLONE=~/src/mattpocock-skills \
FACTORY_AGENT_BROWSER=~/.local/npm/bin/agent-browser \
python3 -m unittest tests.test_tools_integration -v
```

## REA: reverse engineering over MCP

[REA](https://github.com/morluto/rea) (npm `rea-agents`, MIT) is one MCP
server with 207 analysis tools: native binaries (through Ghidra, Hopper or
IDA), JavaScript and Electron apps, archives and packages, .NET, Android and
more. The factory attaches it to the **analyst** Chat agent
(`agents/analyst.md`, `mcp: rea`).

### Install (the owner's plan, phase H)

```bash
npm view rea-agents@6.5.0 version engines dist.integrity --json
npm install --global --prefix ~/.local/npm rea-agents@6.5.0
rea --version                     # 6.5.0
```

- **Version.** 6.5.0 exists on npm (verified; 6.7.0 is newer, the plan pins
  6.5.0). Integrity
  `sha512-C6BG7LyrAdR74mSjertM6PrxJkcNZ2bfQNOJornPo9erDEp2tyPRVYPnV5Yodr+1RqewCQla68WulXlVnveD+A==`.
- **Node.** `engines` is `^22.19.0 || ^24.11.0 || >=26.0.0`: at least 22.19
  on the 22 line, 24.11 on the 24 line (the owner's 24.22 qualifies), or 26
  and later. The `rea` script starts with `#!/usr/bin/env node`, so the `node` it
  should use must be first on the server's `PATH` (or set it in the wrapper).
- **Linux.** Static JavaScript analysis and Ghidra need a procps `ps` on `PATH`.

### The MCP command

`rea mcp` starts the stdio MCP server (`rea --mcp` does too; both verified).
`rea setup --client codex --dry-run --json` shows what setup would register for
Codex: the command `"<prefix>/bin/rea" "mcp"` in `~/.codex/config.toml`, plus
the skill `~/.agents/skills/reverse-engineer-anything`. The factory searches
`~/.agents/skills`, so after `rea setup` the analyst finds that skill in its
catalog (it parses cleanly; 22 KB of workflow notes).

The plan wraps the command as `~/.local/bin/rea-mcp`, so no session downloads
the package again. A minimal wrapper with the same effect:

```sh
#!/bin/sh
export PATH="/opt/ai-stack/node-v24.22.0-linux-x64/bin:$PATH"
exec "$HOME/.local/npm/bin/rea" mcp "$@"
```

### Connect it: `examples/rea.json`

Apply the add-on fragment (Settings → Routing → Preset → `rea`, `POST
/api/config/preset {"name": "rea"}`, or copy its `mcp_servers` into
`config.json`). It adds two servers and changes no routing:

| server | tools (allowlist) | for |
| ------ | ----------------- | --- |
| `rea` | `binary_session`, `open_binary`, `inspect_artifact`, `analyze_javascript_application`, `inspect_analysis_view`, `get_evidence_bundle`, `export_evidence_bundle`, `close_binary` | session identity, artifact and JavaScript analysis, retained views, export; needs no Java or Ghidra |
| `rea-native` | the same plus `binary_overview`, `list_procedures`, `search_procedures`, `analyze_function` | Ghidra function analysis; env `REA_ANALYSIS_PROVIDER=ghidra`, `GHIDRA_HEADLESS_MAXMEM=2G` |

Both use `~/.local/bin/rea-mcp`, `REA_MCP_INPUT_SCHEMA_PROFILE=compact`,
`timeout` 450 s per tool call (the plan's `tool_timeout_sec`),
`startup_timeout` 30 s (`rea`) or 60 s (`rea-native`) and
`"write_args": {"export_evidence_bundle": ["path"]}` (see
[What REA may write](#what-rea-may-write)). The analyst uses `rea`;
for native work give a copy of it `mcp: rea-native` (Agents view → Edit, or
`factory agents new`). `GHIDRA_INSTALL_DIR` (a Ghidra 12.1.x release) and
`JAVA_HOME` (a full JDK 21) are read from the factory's environment, which MCP
servers inherit minus secrets: put them in `~/.config/software-factory/env` or
in `rea-native`'s `env`.

`factory doctor` checks the servers agents name without starting them. Only
the analyst, a Chat agent, uses `rea`, so before the fragment is applied the
check passes as advice ("not configured (optional: only Chat uses it) … the
rea preset adds it"), and after it a missing `~/.local/bin/rea-mcp` is a
warning, not an error. A server a factory-line agent uses is a warning when it
has no entry and an error when its command is missing.

**Reconciling the plan's allowlist with 6.5.0** (as the plan asks):
`inspect_artifact` works only on a target `open_binary` opened (without it
REA answers `target_unavailable`, "No app is open", verified), so the static
profile includes `open_binary` and `close_binary`; opening a zip archive, a
JavaScript file or an ELF executable needs no Ghidra (verified). The native profile adds overview,
procedures and functions; `search_procedures` is there so the agent can ask for
the few names it needs instead of listing every procedure. The REA tools that
launch or debug processes, drive browsers or touch devices are left out.

### What was measured (verified, through `swfactory.harness.mcp.McpClient`)

| | full schemas | compact (`REA_MCP_INPUT_SCHEMA_PROFILE=compact`) |
| - | -: | -: |
| all 207 tools: input schemas + descriptions | 745,189 + 90,143 chars | 89,481 + 90,143 chars |
| `rea` (8 tools) | 35,065 + 9,733 chars (~11K tokens) | 3,690 + 9,733 chars (~3.4K tokens) |
| `rea-native` (12 tools) | 37,115 + 11,146 chars | 4,481 + 11,146 chars (~3.9K tokens) |

The largest compact schemas are `analyze_javascript_application` (850 chars)
and `open_binary` (805); the longest description is `inspect_analysis_view`
(5,781 chars). Without the allowlist the catalog alone would fill a 32K-token
model.

- Start-up (spawn, initialize, list 207 tools): 3.7 to 5.8 s.
- `binary_session`: 1.2 s, about 48.5K chars, in this order: `capabilities`
  (3.6K), `analysis_provider_candidates` (19K), `tool_availability` (27K),
  `client_features`, `server_identity` (1.3K; `rea-agents` 6.5.0). The factory
  cuts tool output to `limits.tool_output_chars` (30,000) or to an eighth of a
  local model's context budget (9,216 chars for a 32K-token model with
  `max_tokens` 8192), keeping the first 60% and the last 40%. At both sizes
  `capabilities` and `server_identity` survive and most of
  `tool_availability` is lost (verified), so the analyst calls it once, reads
  only those two parts and takes its tool list as what it can call. On a
  smaller context `capabilities` is cut too.
- `analyze_javascript_application` on a two-file app with `detail: "summary"`:
  0.9 s, 10K chars, an evidence id, coverage and limitations; the JavaScript is
  parsed, never executed.
- `open_binary` on a zip, then `inspect_artifact` (`detail: "summary"`): 3.5K
  chars; `get_evidence_bundle` (`detail: "summary"`) lists retained records.
- `export_evidence_bundle`: an 80 KB canonical bundle with both records; the
  input files were unchanged.
- `binary_overview` without Ghidra: `provider_unavailable`, "Set
  GHIDRA_INSTALL_DIR to an extracted Ghidra 12.1.x release directory". **Native
  analysis with Ghidra was not run** (no Ghidra or JDK in the sandbox); the
  plan's native acceptance (open → overview → procedures → `analyze_function`
  on its C fixture) still needs the server.
- A tool outside the allowlist is refused by the factory's client before it
  reaches REA.
- REA itself writes an export only into a directory that exists ("Evidence
  output directory does not exist") and otherwise anywhere the factory's user
  can write, through symlinks too (verified); see the next section.
- The whole analyst loop (agent file, `mcp: rea`, the builtin runner, a
  scripted model calling `mcp__rea__binary_session` and
  `mcp__rea__analyze_javascript_application`, exporting to
  `analysis/evidence/` in a fresh checkout, trying to export over a file
  outside it, directly and through a symlink in the checkout (both refused),
  then writing `analysis/NOTES.md`) ran end to end, and so did REA under
  `"sandbox": "bwrap"` (it analysed a case outside the checkout and exported
  into it; with the check turned off, an export outside failed with "Evidence
  file could not be accessed").

### What REA may write

`export_evidence_bundle` writes, or with `overwrite: true` replaces, the file
its `path` names. REA checks only that the path is absolute and its directory
exists, so on its own it is a way to write any file the factory's user can
write. Two things stop that:

- **`write_args`** (an `mcp_servers` entry's `{tool: [argument, ...]}`): the
  builtin harness checks each listed argument the way Write checks
  `file_path` (symlinks resolved, inside the agent's write policy, nothing
  named `.git`), creates its missing parent directories and passes the
  resolved absolute path on. A refused path never reaches the server: the
  model gets "Access denied: … is not writable by this agent". `rea.json`
  lists `export_evidence_bundle`'s `path`, so the analyst can export only into
  its chat's checkout, and `analysis/evidence/` is created on first use.
- **bwrap.** With `"harness": {"sandbox": "bwrap"}` the builtin harness starts
  MCP servers inside the run's sandbox, with the same binds as the agent's
  Bash: the checkout (and the git state a commit needs) writable, everything
  else read-only, a private `/tmp`, and no network when `sandbox_network` is
  false. A server's writes are then held to what Bash may write, whatever the
  tool, and a symlink swapped in after the check cannot lead outside either.
  REA's static profile works there (verified). A server that keeps state
  under `HOME` must be pointed elsewhere through its `env` (the sandbox's
  `/tmp` or a directory in the checkout), and one that needs the network needs
  `sandbox_network: true`. Native analysis under bwrap was not run: Ghidra
  keeps settings under `~/.ghidra`, so if `rea-native` fails there, try
  `"HOME": "/tmp"` (the sandbox's own, writable `/tmp`) in its `env`. A
  command installed under `/tmp` is hidden by the private `/tmp`.

Without bwrap, MCP servers run as the factory's user like Bash does, and the
`write_args` check covers only the arguments it lists.

### Caveats

- A cold Ghidra query can take minutes. Raise `rea-native.timeout` (and REA's
  `REA_GHIDRA_STARTUP_TIMEOUT_MS`) on evidence, not by default.
- Compact schemas drop annotation prose, not tools: keep the allowlist small,
  ask for summaries, page with `inspect_analysis_view`, export evidence.
- REA reads whatever the factory's user can read (it analyses static files
  only and never runs them, but the analyst may point it anywhere); keep
  secrets out of that user's reach.
- Run REA as the unprivileged `ai-dev` user. It analyses statically, but
  Ghidra, Java and native helpers use real memory: watch the host headroom the
  plan describes.

## agent-browser: pages as text

[agent-browser](https://github.com/vercel-labs/agent-browser) (npm
`agent-browser`) is a CLI that drives Chrome and prints accessibility trees,
text, computed styles and layout. The builder, the ux and ui reviewers and the
Chat coder use it when it is installed, because a text model cannot see
screenshots.

### Install

```bash
npm install --global --prefix ~/.local/npm agent-browser@0.39.1
agent-browser install            # Chrome for Testing into ~/.agent-browser
agent-browser --version
```

- **Version.** 0.39.1 is the newest; it declares Node >= 24. On Node 22 npm
  quietly picks 0.27.0 unless you pin the version. The CLI itself is a native
  binary and 0.39.1 ran on Node 22 (verified). The owner's server has Node 24.
- **Browser.** `agent-browser install` could not download Chrome for Testing in
  the sandbox (the egress proxy blocks `googlechromelabs.github.io`). Pointing
  it at an existing Chromium works: `AGENT_BROWSER_EXECUTABLE_PATH=<chrome>`
  (verified with Playwright's Chromium 141). Put the variable in
  `~/.config/software-factory/env`; agent shells inherit it.

### The commands the prompts use (verified on the fixture todo app)

Served with `python3 -m http.server --bind 127.0.0.1`, run through the
factory's own `Sandbox` exactly as an agent's Bash tool runs them:

| command | output |
| ------- | ------ |
| `agent-browser --session <s> open <url>` | `✓ Todo` and the URL (about 1 s) |
| `… snapshot -i` | `heading "Todo"`, `textbox "New item"`, `button "Add"` with refs `e1` to `e3` |
| `… get text '#empty'` / `get text main` | `Nothing to do yet.` / the page's text |
| `… get styles button` | all computed styles: about 400 lines, 9.8 KB. Filtered with the reviewer-ui grep: 15 lines (`background-color: rgb(47, 111, 222)`, `font-family: Arial`, `font-size: 13.3333px`, paddings, radius) |
| `… get box .card` | `x`, `y`, `width: 528`, `height` |
| `… set viewport 390 844` | `✓ Done` |
| `… eval "document.documentElement.scrollWidth > innerWidth"` | `false` (scrollWidth 390 = innerWidth 390: no overflow at phone width) |
| `… errors`, `… console` | empty when the page logs nothing |
| `… close` | `✓ Browser closed` |
| `agent-browser skills get core` (`--full`) | the version-matched usage guide |

The computed styles already show something a reviewer should flag: the
fixture's button keeps the browser's default `Arial 13.33px` instead of the
body's `system-ui 16px`.

### What the prompts had wrong, and the fix

- **Every command needs the same `--session`.** A command without it goes to
  the `default` session, which has no page (`get url` prints `about:blank`,
  verified). The builder, reviewer-ux and reviewer-ui prompts named the
  session only on `open` and `close`; they now say to pass it on every command.
  The coder's `agent-browser snapshot` (no session, no `open`) is now `open`,
  `snapshot -i` and `close` with one session.
- **`get styles` is long.** reviewer-ui now pipes it through
  `grep -E '^(color|background-color|font-(family|size|weight)|line-height|(padding|margin)-[a-z]+|border-top-left-radius):'`.

### Sandboxing

- **Plain Bash** (`harness.sandbox: "none"`): the first command starts a
  background daemon per session, later commands reach it through a socket in
  `~/.agent-browser/`, and each call returns promptly.
- **Close every session.** The daemon starts its own process session, so the
  factory's end-of-run cleanup, which kills the run's process groups, does not
  reach it (verified): a session an agent never closes keeps its daemon and
  Chromium until agent-browser's idle timeout, one hour by default. The prompts
  say to `close`; also set `AGENT_BROWSER_IDLE_TIMEOUT_MS=600000` (ten
  minutes) in `~/.config/software-factory/env`.
- **Socket path limit.** `$HOME/.agent-browser/<session>.sock` may be at most
  103 bytes; with `/home/ai-dev` a session name can be about 70 characters
  (job ids are at most 40, plus `-builder`). Set `AGENT_BROWSER_SOCKET_DIR` to
  a short directory if `HOME` is long.
- **bwrap** (`harness.sandbox: "bwrap"`): `HOME` is read-only, so agent-browser
  fails with "Failed to create socket directory: Read-only file system".
  Set `AGENT_BROWSER_SOCKET_DIR=/tmp/agent-browser` in the factory's env file.
  Each Bash call then gets its own `/tmp`, so a later call cannot reach the
  socket of a browser an earlier call opened and starts a new daemon on a
  fresh `about:blank` page. The earlier browser is not stopped: bwrap runs
  without `--die-with-parent` (so a dev server started with `&` survives the
  call), and its PID namespace, the daemon and Chromium keep running until
  the run's cleanup (verified: about 15 browser processes after one unclosed
  `open`, 28 after a second call). So the prompts say: when a command after
  `open` shows an empty page, run `open`, the checks and `close` in one call,
  `ab="agent-browser --session <s>"; $ab open <url> && $ab snapshot -i; $ab close`
  (verified: nothing of it is left running afterwards). With
  `sandbox_network: false` each call also gets its own network namespace, so
  a dev server started in an earlier call should not be reachable either (not
  tested).
- Chromium tries to reach Google services in the background; where the network
  blocks them nothing breaks.

## The browser adapter: `sf-browser` (DESIGN §43)

agent-browser stays the coding agents' Bash tool. The **browser adapter** is a
different thing for a different caller: a browsing persona (Kate's
`kate-browser`) gets it as MCP tools, `mcp__browser__observe`, `read`,
`navigate`, `click`, `fill`, `press`, `wait_for`, `screenshot` and `close`
(3,170 characters of schemas), never Playwright's own 25 tools. Design:
[docs/seraphina/BROWSER_AND_CODEX.md](seraphina/BROWSER_AND_CODEX.md) §2.

| | agent-browser | the browser adapter |
| --- | --- | --- |
| who | the builder, the UX and UI reviewers, the Chat coder, through Bash | a browsing persona only (`tools: none`, `mcp: browser`) |
| pages | the job's dev server, any URL its Bash can reach | the fixture site and a job's preview, in a namespace with no network |
| records | the run's transcript | an action log (intent before, outcome after), a receipt per run, screenshots as artifacts |
| a timeout | an error string | `outcome_unknown`, a fresh observation, the origin on hold; never repeated |

### The runtime (K1)

The harness's own runtime lives in one directory, `browser.runtime.dir`, laid
out as `node/bin/node`, `playwright-mcp/node_modules/@playwright/mcp` (from
`npm install @playwright/mcp@0.0.83` in `playwright-mcp/`) and
`chrome/chrome-headless-shell-linux64/chrome-headless-shell`; each can be given
explicitly (`browser.runtime.node`, `.playwright_mcp`, `.chrome`, relative to
the directory). The grant names only these paths, and the namespace binds only
them: a Node or Chrome on `PATH` is never used. `factory doctor` compares each
with `browser.pins` (version, sha256, and Playwright MCP's npm integrity from
the install's lock file). The pins: Playwright MCP 0.0.83
(`sha512-oNcl+Ae2…azUKOg==`) and chrome-headless-shell 155.0.8059.12; set the
sha256 values once you have checked the builds.

### `examples/browser.json`

```json
"mcp_servers": {
  "browser": {
    "command": "sf-browser", "args": ["mcp"], "sandbox": "self", "required": true,
    "timeout": 60, "startup_timeout": 30,
    "tools": ["observe", "read", "navigate", "click", "fill", "press", "wait_for", "screenshot", "close"]
  }
}
```

`sandbox: "self"` is accepted only for the installed `bin/sf-browser` (a bare
`sf-browser` names it): it builds its own, tighter namespace, so the run's
sandbox does not wrap it. `required` fails a turn whose browser does not start.
The harness appends the turn's grant path to the args; nothing else is
configured per run.

### What was checked (on this repository's machine, 26 Oct 2026)

Through the shim, the real Playwright MCP 0.0.83 with chromium-headless-shell
141.0.7390.37 (not the pinned 155: see
`tests/fixtures/browser/README.md`), in the namespace, as root with
`--no-sandbox` (O11): the fixture loads; the host's loopback, a tailnet address
and a public host do not; a page's `javascript:` navigation would run in
Playwright (the shim refuses it first); a redirect off `--allowed-origins` is
followed (the shim answers `origin_not_allowed`); two clicks on Submit are two
submissions in the browser (the shim refuses the second); a 2-second commit
limit on the slow shop gives `outcome_unknown` with a fresh observation showing
"Accepted order #1" and exactly one order in the fixture's log; `--caps
testing` is accepted and offers `browser_verify_text_visible`. Not checked: a
non-root user with Chromium's own sandbox, the owner's server, and Chrome 155.

## billion-context: a context proxy in front of Strata

The owner's plan (phase J, optional) evaluates billion-context 0.1.197, an
OpenAI-compatible proxy that keeps conversation history on disk, replaces old
turns with summaries and offers retrieval tools, between a client and Strata:

```
client → http://127.0.0.1:18081/bili/http://127.0.0.1:18080/v1 → Strata
```

It was **not installed or run** for this. To the factory it is one more
provider, a `base_url`:

```json
"providers": {
  "strata-bili": {"type": "openai",
                  "base_url": "http://127.0.0.1:18081/bili/http://127.0.0.1:18080/v1",
                  "api_key_env": "STRATA_API_KEY", "api_key_file": "/etc/ai-stack/strata.env",
                  "max_concurrency": 1, "concurrency_scope": "run", "timeout": 1800}
},
"models": {
  "local-bili": {"provider": "strata-bili", "model": "strata", "max_tokens": 16384,
                 "context_tokens": 262144, "keep_reasoning": false, "stream": true, "extra_body": {}}
}
```

Then point an agent at it (`"agents": {"coder": {"model": "local-bili"}}`) and
run `factory models test local-bili` before relying on it.

Caveats:

- **Unverified wire format.** The factory calls
  `<base_url>/chat/completions` (Chat Completions); the plan's pilot checks
  only the Responses API that Codex uses. Test both probes (plain reply and
  tool call) first.
- **Its own tools.** The proxy can offer the model retrieval tools
  (decompress, search_context). If the model calls one and the proxy passes
  the call through, the factory answers with an "Unknown tool" error result;
  watch the first transcripts.
- **Slots are per provider.** `strata` and `strata-bili` each get their own
  one-slot semaphore, so routing some agents to each lets two requests reach
  the one-slot Strata at once. Route all local agents through one of them at a
  time.
- **Two context managers.** Keep `context_tokens` at what Strata really runs:
  the proxy does not enlarge the model's window. The factory's own guard still
  elides old tool output, and the proxy may also rewrite history, so their
  token counts diverge. Compressed history also changes the prompt prefix, so
  the hot prefix cache that `concurrency_scope: "run"` buys is partly lost.
- **Credentials.** The Strata key is sent as the Bearer header and forwarded
  upstream; never put it in the URL. The proxy has no inbound authentication:
  keep both listeners on loopback (port 18081 is reserved for it; the factory
  stays on 8765).
- **No silent fallback.** If the proxy is down, calls fail and the job fails
  (retryable); the factory does not switch to direct Strata on its own.
- Doctor's reachability check (`GET <base_url>/models`) goes through the
  proxy. Doctor also asks every loopback provider that has a key for
  `<base_url>/models` without the key, `strata-bili` included, and warns
  "answers without the key" unless that answer is 401 or 403: it passes only
  when the proxy forwards the request and Strata rejects it.
