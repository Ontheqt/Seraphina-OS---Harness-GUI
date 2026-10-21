# Presets

Each `*.json` file here is a config fragment. The routing presets set
`providers`, `models` and `agents`: the tiers are the `opus`, `sonnet` and
`haiku` entries under `models`, and every agent of the factory line gets an
explicit route: spec-writer, builder, approver, the four reviewers, lead, coder
and repo-scout. `local-first.json` also brings routing `profiles` and a
`default_profile` ([below](#profiles)). `rea.json` is an add-on that only adds
MCP servers ([below](#add-on-rea)), and `openwebui.json` one that only adds the
Open WebUI provider and a model entry ([below](#add-on-openwebui)). `prices/` holds dated price lists
([below](#prices)), which are not presets. JSON allows no comments, so the
explanations live in this file.

## Applying a preset

- **GUI**: Settings → Routing → **Preset** menu.
- **CLI**: `factory init --preset hybrid` writes a new config from the preset.
- **API**: `POST /api/config/preset` with `{"name": "hybrid"}`.

A preset replaces whole provider, model, agent and profile entries that have
the same name, and leaves every other entry alone. It never touches your repos,
the server token, data dir, limits, harness or pipeline. Applying one routing
preset after another gives the second one's routing, because every routing
preset routes every factory agent and sets `default_profile` to its own (or to
none), so a default profile an earlier preset brought cannot route the agents
again. The profiles themselves stay until you remove them. Agents you added yourself (for example `reviewer-perf`) keep
their routes. Without an explicit route, an agent falls back to its tier.
Before you rely on a route, run Settings → Models → Test (it checks a plain
reply and a tool call) or `factory doctor --probe`.

The tiers are copies, not references: in `hybrid`, for example, `opus` is a
copy of `local`. When you change `local` in Settings (or with `POST
/api/config`) and leave the tier alone, every tier that was a copy of it
changes too, so agents without an explicit route follow. If you change both in
one save, both changes are kept as you made them. When you edit `config.json`
by hand, change the tier entries yourself.

## The presets

**deepseek**: the built-in default. The opus tier is `deepseek-v4-pro` and
the sonnet tier is `deepseek-v4-flash`, both with thinking on, through
DeepSeek's OpenAI-compatible API. Needs `DEEPSEEK_API_KEY`.

**anthropic**: Claude only. The tiers are `claude-opus-5-5` (effort `high`),
`claude-sonnet-5-5` (effort `medium`) and `claude-haiku-5-5` (effort `low`).
Every Claude entry has adaptive thinking with summarized display, prompt
caching, `max_tokens` 64000, append-only history and eager tool streaming.
Opus and Sonnet also use the server-side refusal fallback (`fallbacks:
"default"` plus the provider's `anthropic-beta` header). Haiku has no fallback.
Needs `ANTHROPIC_API_KEY`.

**nanogpt**: Claude through NanoGPT's OpenAI-format API, with one
`NANOGPT_API_KEY`. The model ids are guesses (`claude-opus-5-5` and so on), so
check them against `https://nano-gpt.com/api/v1/models`. Thinking knobs are
left out because how NanoGPT maps them is unverified. If NanoGPT supports
`reasoning_effort`, add it to `extra_body`.

**ollama**: all local. Every agent and tier runs on Ollama with the
qwen3-coder template (`qwen3-coder:30b`, 262144 context). Ollama's default
context is small, so start it with `OLLAMA_CONTEXT_LENGTH=262144` (or
`PARAMETER num_ctx`). With one slot and `concurrency_scope: "run"`, agents
take turns, and each keeps its prefix cache hot.

**strata**: all local. Every agent and tier runs on the owner's Strata
Qwen3.8-Flash-Next (`http://127.0.0.1:18080/v1`, 262144 context, one slot,
`concurrency_scope: "run"`). The key comes from `STRATA_API_KEY`, or from
`/etc/ai-stack/strata.env`.

**hybrid**: local where the volume is, API where independence pays off (DESIGN
§23). This is the recommended preset for the owner's server, along with
`strata`.

| agent | model entry |
| ----- | ----------- |
| spec-writer, builder, reviewer-security, coder, repo-scout | `local`: Flash-Next on TabbyAPI (`tabby`, `http://127.0.0.1:5000/v1`) |
| reviewer-code, reviewer-ux, reviewer-ui | `claude-sonnet` (`claude-sonnet-5-5`, effort `medium`) |
| approver | `claude-opus` (`claude-opus-5-5`, effort `high`) |
| lead | `claude-fable` (`claude-fable-5-1`, effort `high`) |

The tiers are `opus` → local, `sonnet` → claude-sonnet and `haiku` →
`claude-haiku-5-5`. The tabby provider uses `auth: "both"`, one slot and run
scope. Set `models.local.model` to the name of the model that Tabby has
loaded. To use Strata instead, set `models.local.provider` to `strata` and
`models.local.model` to `strata` in Settings; the `opus` tier follows (see
above). In a hand-edited `config.json`, make the same change to
`models.opus`. Needs `TABBY_API_KEY` and `ANTHROPIC_API_KEY`.

**mixed**: two `llama-server` instances, one model per RTX 3090, plus API
reviewers. Qwen 3.8 27B runs on `127.0.0.1:8081` and does spec-writer,
builder, coder and repo-scout. Gemma 4 runs on `127.0.0.1:8082` and does the
ux, ui and code reviews, so the code review comes from the other model family.
Claude Opus 5.5 does the security review, the approver and the lead. Each GPU
is its own one-slot provider, so the two work in parallel. Gemma's context is
131072, its native size.

**gpt-oss**: gpt-oss-120b on llama.cpp (`127.0.0.1:8080`, 131072 context,
`chat_template_kwargs.reasoning_effort: "high"`). It does spec-writer,
security review, approver and repo-scout. The builder, coder and lead use
DeepSeek V4 Pro, and the ux, ui and code reviewers use DeepSeek V4 Flash, so
they run in parallel instead of queueing on the one local slot. VRAM is
shared: you can run gpt-oss-120b or two small per-GPU models (the `mixed`
preset), but not both at once. On vLLM or OpenRouter, use
`extra_body: {"reasoning_effort": "high"}` instead.

**nanogpt-value**: all API, through NanoGPT with one `NANOGPT_API_KEY`, for
value without economising the quality gates (DESIGN §23). Claude runs on
NanoGPT's Anthropic-format endpoint (`nanogpt-anthropic`,
`https://nano-gpt.com/api`), so adaptive thinking, effort and prompt caching
keep working; the server-side refusal fallback is left out, since a gateway
may reject it. Each placement carries its reason.

| agent | model | reason |
| ----- | ----- | ------ |
| spec-writer, approver, lead | `claude-opus` (Opus 5.5, effort high) | capability: the gates |
| builder, coder | `muse-spark` (Muse Spark 1.3, coding 75.8, $1.25/$4.25) | cost: the most tokens |
| builder on CHANGES | `gpt-sol-high`, then `gpt-sol-xhigh` (GPT-6.1 Sol) | escalate, don't retry |
| repo-scout | `mimo` (MiMo V2.6 Pro, $0.43/$0.87) | cost |
| reviewer-security | `qwen-max` (Qwen3.8 Max) | stage fit: Qwen family, not the builder's |
| reviewer-code | `gpt-sol-high` | stage fit: OpenAI family |
| reviewer-ux | `claude-sonnet` (Sonnet 5.5, effort medium) | stage fit: Anthropic family |
| reviewer-ui | `gemini-argon` (Gemini 4 Argon) | stage fit: Google family |

**local-first**: the owner's agreed routing for their server. Servers are
`127.0.0.1` placeholders: `tabby` (`:5000`, TabbyAPI, `kind: "tabby"`),
`strata` (`:18080`) and `llamacpp` (`:8080`), each one slot with run scope,
plus NanoGPT for the API models. Only one big local server holds the two GPUs
at a time, and the routing copes with whichever it is:

- `flash-next` has two **builds**: TabbyAPI's EXL3 build first, then Strata's
  GGUF build (`builds`). Each run uses the first whose server answers a short
  probe (3 s, cached 30 s). When neither answers, or a free slot does not come
  within `overflow_after_seconds` (900), the run **overflows** to `mimo`
  (MiMo V2.6 Pro), and the run's events and the job say so and why.
- `tabby` has `kind: "tabby"`, so the factory reads which model TabbyAPI has
  loaded (`GET /v1/model`). `allow_model_swap` is off: while the owner (or
  Open WebUI) has another model loaded, the Tabby build counts as unavailable
  and the run goes to Strata or the overflow; the factory never makes Tabby
  load another model. Turn it on (TabbyAPI needs inline model loading) to let
  a run switch models, and `swap_back` to load the original one again when the
  job stops. Set `models.flash-next.model` and its first build to the folder
  name of your EXL3 build.
- The builder **escalates**: on its first CHANGES round (or rework) it moves to
  GPT-6.1 Sol high, on the second to GPT-6.1 Sol xhigh. A new model starts a
  fresh session with a hand-over prompt (spec, build.md, the review findings,
  the diff so far).
- The other local entries (`swift-27b`, `thinkingcap-27b` on Tabby,
  `qwen-27b-q8`, `gemma-4-31b` on llama.cpp) are there for profiles and your
  own routes; a model Tabby has not loaded is unavailable while swaps are off.

| agent | model | reason |
| ----- | ----- | ------ |
| builder, coder, repo-scout | `flash-next` (local) | cost: the most tokens, free locally |
| reviewer-security | `flash-next` (local) | stage fit: a security fine-tune, findings stay local |
| reviewer-code | `gpt-sol-high` | stage fit |
| reviewer-ux | `claude-sonnet` | stage fit |
| reviewer-ui | `gemini-argon` | stage fit |
| spec-writer, approver, lead | `claude-opus` (Opus 5.5 high) | capability |

Needs `TABBY_API_KEY`, `STRATA_API_KEY` (or `/etc/ai-stack/strata.env`) and
`NANOGPT_API_KEY`. The NanoGPT model ids are from NanoGPT's model pages where
known and best guesses elsewhere: run `factory doctor`, which lists each
provider's models and warns for an id it does not list, and says which local
server is up and which model Tabby has loaded.

## Profiles

A profile is a named set of per-agent placements chosen per job (`"profile"`
in `POST /api/jobs`, `POST /api/plans`, `POST /api/chats` and a chat
hand-off, `--profile` on `factory submit`, `run` and `plan`); it applies over
the saved routing, and the job records which one ran. `default_profile` is
used when a job or chat names none; `""` means no profile. A chat keeps the
profile it was created with, and repo-scout always runs with the default
profile, so a profile's `coder` and `repo-scout` placements apply to new chats
and scouting once it is the default (or, for a chat, named when you create
it). Each placement is `{model, effort?, escalate?, reason}`. None of them
economises the gates: the spec, approver and lead stay on Opus 5.5 high, and
the four lenses come from four model families.

The independence has one limit, which the agreed lists accept: once the
builder escalates, its rung can be a judge's model. In `local-first`,
`balanced`, `thrifty` (its third rung) and `nanogpt-value` the rung is
GPT-6.1 Sol, the code lens, which then reviews code it wrote; in `best` the
rung is Opus 5.5, which also approves. In `local-first` and `thrifty` the
security lens is the builder's own Flash-Next (its security fine-tune, a
stage-fit choice). `fastest` keeps every judge off the builder's models.
DESIGN §23 has the table; route the code lens (or `best`'s approver)
elsewhere if you want independence on every round.

| profile (local-first) | what changes |
| --------------------- | ------------ |
| `local-first` | the base routing above (the default) |
| `balanced` | the security review on Qwen3.8 Max, so all four lenses run at once (time) |
| `best` | Claude Fable 5.1 builds and chats (coding 81.6), escalating to Opus 5.5 high, then max; the code review at xhigh; Muse Spark reviews UX |
| `fastest` | GPT-5.6 Sol builds and chats (escalating to GPT-6.1 Sol high, then xhigh), DeepSeek V4.1 Flash scouts, every review on an API; nothing queues on the local slot |
| `thrifty` | local building; CHANGES tries the local Qwen 27B Q8 second opinion (skipped while llama.cpp is not the server up), then MiMo V2.6 Pro, then GPT-6.1 Sol high |

`factory estimate <repo> --profile <name>` (or `GET
/api/estimate?repo=<repo>&profile=<name>`) predicts tokens, cost and duration
per step before a run.

## Prices

`prices/nanogpt-2026-10-21.json` lists NanoGPT's prices in USD per million
tokens (`price_in`, `price_out`) as of that date, with the intelligence and
coding indices where known. The presets copy them into their model entries
(`price_in`, `price_out`; `price_cached_in` when known), and
`tests/test_presets.py` keeps the copies in step. A model with no price is
unknown, not free, except on a local server (free by default); a job's cost
total says when it leaves runs out. When prices change, add a new dated file
and update the presets.

## Add-on: rea

`rea.json` adds two stdio MCP servers for [REA](https://github.com/morluto/rea)
(`rea-agents` 6.5.0, the owner's pinned version; it needs Node 22.19+, 24.11+
or 26+)
and changes no routing, so apply it on top of any preset. The `analyst` Chat
agent uses `rea`; [docs/tools.md](../docs/tools.md#rea-reverse-engineering-over-mcp)
has the measurements and caveats.

- **`rea`**: static analysis that needs no Java or Ghidra. Its allowlist is
  `binary_session` (session identity), `open_binary`, `inspect_artifact` and
  `close_binary` (archives and packages: inspect_artifact needs an open
  target), `analyze_javascript_application`, `inspect_analysis_view` and
  `get_evidence_bundle` (retained views), and `export_evidence_bundle`.
- **`rea-native`**: the same plus `binary_overview`, `list_procedures`,
  `search_procedures` and `analyze_function`, with `REA_ANALYSIS_PROVIDER=ghidra`
  and `GHIDRA_HEADLESS_MAXMEM=2G`. Set `GHIDRA_INSTALL_DIR` (Ghidra 12.1.x) and
  `JAVA_HOME` (a full JDK 21) in `~/.config/software-factory/env` or in this
  entry's `env`. Give an analyst copy `mcp: rea-native` to use it.

Both start `~/.local/bin/rea-mcp`, the owner's wrapper that runs the installed
package's MCP server (`rea mcp`), with `REA_MCP_INPUT_SCHEMA_PROFILE=compact`
(the selected tools' input schemas shrink from about 35,000 to 3,700
characters), a 450-second tool timeout and a 30 or 60-second start-up timeout.
`"write_args": {"export_evidence_bundle": ["path"]}` makes the factory check
the export path like a Write (inside the agent's checkout, symlinks resolved)
and create its directory before REA sees it; keep it when you copy the
entries, because REA on its own writes any file the factory's user can.
Without the wrapper, run the installed package directly: change only
`command` and `args` in each entry (the `node` that `rea` finds on the
factory's `PATH` must be one REA supports):

```json
"command": "~/.local/npm/bin/rea",
"args": ["mcp"],
```

## Add-on: openwebui

`openwebui.json` adds the provider `openwebui` (`kind: "openwebui"`, Open
WebUI's OpenAI-compatible API at `http://127.0.0.1:8083/api`, the key from
`OPENWEBUI_API_KEY`, one slot with run scope, a 1800 s timeout) and the model
entry `kate` (the workspace model `kate` on it, `extra_body: {"max_tokens":
null}` so the preset's own output limit applies), and changes no routing. The
shipped `agents/kate.md` persona chats through it (DESIGN §33; README "Open
WebUI profiles"). Open WebUI applies the workspace model's prompt and settings
itself, so no sampler settings are copied here. Change `models.kate.model` if
your workspace model's id is not `kate`, and add one entry like it per
workspace model you want to chat with. The `openwebui` provider has its own
slot: it does not share the `tabby` provider's, although both reach TabbyAPI,
which then queues the two.

## Local servers

llama.cpp: start `llama-server` with `--jinja` (tool calling needs the model's
chat template), `--host 127.0.0.1`, `-c <ctx>` set to the model entry's
`context_tokens`, `-ngl 99` and `--parallel 1`. Pick the GPU with
`CUDA_VISIBLE_DEVICES`. For gpt-oss-120b on less than 48 GB of VRAM, add
`--n-cpu-moe <n>` to keep some experts in RAM. Tool calling quality varies by
model and template.

If you start a server with more slots (for example Strata `--parallel 2`, if
KV memory allows), raise that provider's `max_concurrency` to match.
