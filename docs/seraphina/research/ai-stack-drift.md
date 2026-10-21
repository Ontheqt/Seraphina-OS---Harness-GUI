# Research: keeping a self-hosted AI stack consistent

Research notes for **Seraphina OS Harness** (DESIGN.md §25-31). They serve
DESIGN.md §31 point 5, the owner's main pain: Open WebUI upgrades broke
connections and enhancements, new information reached one service and was never
passed on to the others, instruction sets in Open WebUI, Tabby and DeepSeek
Harness drifted until they were malformed and contradictory, and model profiles
were wrong. The owner's requirement is that shared facts (instruction sets,
model profiles, connections) have **one source of truth with drift detection
across every consumer**, and that commitments are tracked to closure outside any
agent's context.

Written 2026-10-21. Target machine: "matrix" (Ubuntu, i9-12900KS, 2x24 GB
NVLinked GPUs, Cosmos Cloud, Cockpit, Open WebUI, TabbyAPI serving the
cyber-security "Flash-Next" model at about 100 tok/s, llama.cpp servers,
DeepSeek Harness, Qwen Code, 262,144-token contexts, reached only over
Tailscale).

## How these notes were made, and their limits

- I read the **upstream source and docs directly**, by shallow-cloning each
  project on 2026-10-21. The commits read:

  | Project | Repository | Commit (date) | Version at that commit |
  | ------- | ---------- | ------------- | ---------------------- |
  | Open WebUI | [open-webui/open-webui](https://github.com/open-webui/open-webui) | `91ea4e3` (2026-10-16) | 0.12.3 |
  | Open WebUI docs | [open-webui/docs](https://github.com/open-webui/docs) | `cd7fbd8` (2026-10-21) | n/a |
  | TabbyAPI | [theroyallab/tabbyAPI](https://github.com/theroyallab/tabbyAPI) | `a270c55` (2026-10-19) | no release numbers (pyproject says 0.0.1) |
  | llama.cpp | [ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp) | `8baabd5` (2026-10-21) | master |
  | DeepSeek Harness | [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness) | `687f282` (2026-10-17) | dsh 0.2.1-rc.2 |
  | Qwen Code | [QwenLM/qwen-code](https://github.com/QwenLM/qwen-code) | `97567d5` (2026-10-21) | 0.25.3 |

- `docs.openwebui.com` could not be reached from the research sandbox, so Open
  WebUI documentation is cited from its source repository
  ([open-webui/docs](https://github.com/open-webui/docs)). The published pages
  have the same text under `https://docs.openwebui.com/<same path>`.
- **This describes upstream as of October 2026, not the owner's install.** The
  owner's versions are unknown. Older versions keep settings in different places
  (for example Open WebUI's `config` table changed shape, see §2.1). So every
  adapter in the design must detect the version first.
- **UNVERIFIED** marks anything I could not confirm from a primary source, or
  inferred from code without running it.

---

## 1. The problem in one table: one fact, many homes

The same fact about the stack is stored in many places, in different formats,
under different names, and with different precedence rules. Nothing links the
copies. Take the context length of the Flash-Next model:

| Where the context length lives | Name there | Format and location |
| ------------------------------ | ---------- | ------------------- |
| TabbyAPI, global | `model.max_seq_len`, `model.cache_size` | `config.yml` |
| TabbyAPI, per model | `model.max_seq_len` | `models/<name>/tabby_config.yml` |
| TabbyAPI, per load | `max_seq_len` | body of `POST /v1/model/load` |
| llama.cpp server | `-c` / `--ctx-size`, `LLAMA_ARG_CTX_SIZE`, `c =` | command line, environment, or preset INI section |
| Open WebUI | compaction threshold `compact_token_threshold`, `CONTEXT_COMPACTION_TOKEN_THRESHOLD` | model `params` (database) or a database-persisted setting |
| DeepSeek Harness | `contextWindow`, `maxTokens` | `$DSH_HOME/profiles/<profile>/cordis.patch.yml` |
| Qwen Code | `generationConfig.contextWindowSize` | `~/.qwen/settings.json`, inside a `modelProviders` entry |
| Software Factory (v1) | `context_tokens`, `max_tokens` | `config.json` `models.<key>` (DESIGN.md §20) |

That is eight places and eight names, and none of them knows about the others.
The same is true of the API endpoint and key, the chat template, the sampler
defaults, the output token cap, and the instruction text. The design in §4-8
gives every fact one canonical record. It names every place the fact is
copied ("bindings"), and it checks all the copies against the record and
against what the servers report they actually loaded.

---

## 2. What each consumer stores, where, and what broke on upgrade

### 2.1 Open WebUI (0.12.3, released 2026-10-16)

**Where its state lives.**

- Everything is under `DATA_DIR`, which is `/app/backend/data` in the Docker
  image. The main database defaults to `sqlite:///${DATA_DIR}/webui.db`, or
  Postgres when `DATABASE_URL` is set. The vector store and uploaded files are
  stored there too
  ([env-configuration](https://github.com/open-webui/docs/blob/main/docs/reference/env-configuration.mdx)).
- **Settings are rows in the `config` table, one row per key.** The current
  model class is `"Per-key config storage. Each row is one config key."`, with
  columns `key`, `value` (JSON) and `updated_at`
  ([models/config.py](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/models/config.py)).
  The internal keys differ from the environment variable names. For example,
  `OPENAI_API_BASE_URLS` is stored as `openai.api_base_urls`, `OPENAI_API_KEYS`
  as `openai.api_keys`, and `OPENAI_API_CONFIGS` as `openai.api_configs`
  ([config.py](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/config.py)).
  A legacy `config.json` in `DATA_DIR` is imported once at startup and renamed
  to `old_config.json` (same file).
  - The docs give the lock-out recovery command:
    `sqlite3 /app/backend/data/webui.db "UPDATE config SET value = json('true') WHERE key = 'ui.enable_signup';"`.
  - 0.12.x stores one row per key. My recollection is that older versions kept
    the whole configuration as a single JSON row. **UNVERIFIED**, and so is the
    release that changed the shape.

**Environment variables versus the database (PersistentConfig).** This is the
single most important fact about Open WebUI. Settings marked `ConfigVar`
(formerly PersistentConfig) are **read from the environment only on first
start**. After that **the database value wins and later changes to the variable
are ignored** ([env-configuration](https://github.com/open-webui/docs/blob/main/docs/reference/env-configuration.mdx)).

- `ENABLE_PERSISTENT_CONFIG=False` makes environment variables win on every
  start. The docs warn that in this mode the admin UI still appears to save
  changes, but they are lost on restart.
- OAuth settings are the exception. `ENABLE_OAUTH_PERSISTENT_CONFIG` defaults to
  `False`, so `oauth.*` keys always come from the environment
  ([config.py](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/config.py)).
- `RESET_CONFIG_ON_START=true` resets the stored config to the environment
  values on the next start
  ([main.py](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/main.py)).
- Some `ConfigVar` settings have no UI control. The docs say to change those by
  posting the value to `/api/v1/configs/import` with an admin token.

The consequence for Seraphina: **editing the compose file or the Cosmos
ServApp's environment does not change a running Open WebUI.** Changes have to
go through its API, or the drift detector will keep reporting them.

**Connections (endpoints and keys).**

- `OPENAI_API_BASE_URLS` and `OPENAI_API_KEYS` are semicolon-separated lists.
- `OPENAI_API_CONFIGS` holds per-connection settings (`enable`, `prefix_id`,
  `model_ids`, `tags`, `connection_type`), **keyed by the connection's position
  in the URL list**. The URL is also accepted as a legacy key. The variable was
  added in 0.10.2 ([env-configuration](https://github.com/open-webui/docs/blob/main/docs/reference/env-configuration.mdx),
  [CHANGELOG 0.10.2](https://github.com/open-webui/open-webui/blob/main/CHANGELOG.md)).
- Reordering or removing one URL therefore shifts the prefix and allow-list of
  every later connection. That is a drift trap.
- API: `GET /openai/config` and `POST /openai/config/update`. The form takes the
  complete lists: `ENABLE_OPENAI_API`, `OPENAI_API_BASE_URLS`,
  `OPENAI_API_KEYS`, `OPENAI_API_CONFIGS`
  ([routers/openai.py](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/routers/openai.py)).
- Tool servers use `TOOL_SERVER_CONNECTIONS` and
  `GET/POST /api/v1/configs/tool_servers`
  ([routers/configs.py](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/routers/configs.py)).
- The whole settings store can be exported with `GET /api/v1/configs/export`,
  which returns every key, **API keys included**. `POST /api/v1/configs/import`
  **upserts only the keys you send**: it merges and never deletes (same router).
- Open WebUI's API needs an API key. `ENABLE_API_KEYS` is **off by default**:
  0.6.37 renamed it from `ENABLE_API_KEY` and turned it off. It also needs a user
  or group permission ([CHANGELOG 0.6.37](https://github.com/open-webui/open-webui/blob/main/CHANGELOG.md)).

**Model profiles ("workspace models").**

- Stored in the `model` table: `id`, `base_model_id` (the upstream model),
  `name`, `params` (JSON), `meta` (JSON) and `is_active`
  ([models/models.py](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/models/models.py)).
  - `params` holds the model's system prompt and sampler parameters. It accepts
    extra keys.
  - `meta` holds the description, capabilities, attached knowledge, and the
    tool, filter and action ids.
- API ([api-endpoints](https://github.com/open-webui/docs/blob/main/docs/reference/api-endpoints.md)):
  - `GET /api/v1/models/export` exports the custom models.
  - `POST /api/v1/models/import` is an additive upsert.
  - `POST /api/v1/models/sync` is admin only. It is a declarative reconcile that
    creates, updates **and deletes** every model not in the payload.
  - The same page recommends keeping `models.json` in git.
- Global defaults: `DEFAULT_MODEL_PARAMS` (stored as `models.default_params`),
  `DEFAULT_MODELS` and `TASK_MODEL_EXTERNAL`. All are `ConfigVar`.
- `num_ctx` is an **Ollama-only** parameter. For OpenAI-compatible backends
  (Tabby, llama.cpp) the context length is set on the server. Open WebUI's own
  context facts are the compaction settings: `ENABLE_CONTEXT_COMPACTION`,
  `CONTEXT_COMPACTION_TOKEN_THRESHOLD` (default 80000), and the per-model
  `compact_token_threshold`, which is clamped to `CONTEXT_COMPACTION_TOKEN_CAP`
  (env-configuration).

**Instruction sets.**

- The system prompt has three levels: per model (admin, in the model's
  `params`), per account (user settings), and per chat (Chat Controls).
  - **For the system prompt, the per-model value wins.**
  - For sampler parameters it is the other way round: per chat beats per
    account, which beats per model
    ([chat-params](https://github.com/open-webui/docs/blob/main/docs/features/chat-conversations/chat-features/chat-params.md)).
  - So two users can get different sampler values from the same "model".
- Prompts (slash commands), and Skills since 0.12, have their own routers with
  version history:
  - prompts: `/api/v1/prompts/...` with `/history`;
  - skills: `/api/v1/skills/export`, `/import` and `/history`
    ([routers/prompts.py](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/routers/prompts.py),
    [routers/skills.py](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/routers/skills.py)).

**Tools, functions, pipes, filters.**

- Functions (pipes, filters, actions) and tools are **Python source code stored
  in the database**. They run inside the Open WebUI process and import its
  internals.
- API ([routers/functions.py](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/routers/functions.py),
  [routers/tools.py](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/routers/tools.py)):
  - `GET /api/v1/functions/export` and `GET /api/v1/tools/export`;
  - per-item valves, and history since 0.12.0;
  - `POST /api/v1/functions/sync`. Sync **loads every module**, so it fails on a
    function that no longer imports. The code also deletes existing functions
    missing from the payload (`session.delete(existing[id])` in
    [models/functions.py](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/models/functions.py)).
    **UNVERIFIED** at runtime.
- `SAFE_MODE=true` disables all functions. That is useful for getting a broken
  instance back up (env-configuration).
- Pipelines (the separate server) is still supported. 0.11.2 made it cost
  nothing when it is not configured
  ([CHANGELOG 0.11.2](https://github.com/open-webui/open-webui/blob/main/CHANGELOG.md)).
  Search results say the docs steer new work to Functions and Tools
  ([search extract of docs.openwebui.com/features/extensibility/pipelines](https://docs.openwebui.com/features/extensibility/pipelines/)).
  A formal deprecation is **UNVERIFIED**.

**Knowledge.** Knowledge bases are database rows plus files plus vector-store
collections. The routes are `/api/v1/knowledge/...`, including per-KB export,
`/reindex`, `/sync/diff` and file add/remove
([routers/knowledge.py](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/routers/knowledge.py)).

**Read-only probes.** `GET /api/version`, `GET /health`, `GET /health/db`,
`GET /api/config` and `GET /api/models`
([main.py](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/main.py)).

**What broke on upgrade (history).** All entries are from the official
[CHANGELOG](https://github.com/open-webui/open-webui/blob/main/CHANGELOG.md)
unless another source is given.

| Version (date) | What changed | What it breaks |
| -------------- | ------------ | -------------- |
| 0.3.17 (2024-09-04) | Code moved under the `open_webui.` namespace; 0.3.18 rewrote old imports automatically | Tools and functions with old imports |
| 0.5.0 (2024-12-25) | `open_webui.apps.*` became `open_webui.routers.*`, and `apps.webui` became `open_webui.main`; a "backend overhaul" in which "some Functions using internal variables may face compatibility issues"; websockets made mandatory ([0.5 migration guide](https://github.com/open-webui/docs/blob/main/docs/features/extensibility/plugin/migration/to-0.5.0.mdx)) | Pipes, filters and tools |
| 0.6.23 (2025-08-21) | `ENABLE_ADMIN_WORKSPACE_CONTENT_ACCESS` renamed to `BYPASS_ADMIN_ACCESS_CONTROL`; `WEB_SEARCH_CONCURRENT_REQUESTS` renamed to `WEB_LOADER_CONCURRENT_REQUESTS` | Environment files |
| 0.6.35 (2025-11-06) | Event emitters from direct-connection model servers no longer supported (security fix) | Direct-connection enhancements |
| 0.6.37 (2025-11-24) | `ENABLE_API_KEY` renamed to `ENABLE_API_KEYS`, **off by default**, now needs a permission | Every external script that used an API key |
| 0.6.39 (2025-11-25) | "BREAKING": Docling settings consolidated into `DOCLING_PARAMS`; another env var renamed | Document ingestion settings |
| 0.6.42 (2025-12-21) | Per-source `*_LOG_LEVEL` vars removed; legacy document collections removed | Logging config; old knowledge |
| 0.9.0 (2026-04-20) | Data layer made async: plugins that call model methods must `await` them ([0.9 migration guide](https://github.com/open-webui/docs/blob/main/docs/features/extensibility/plugin/migration/to-0.9.0.mdx)) | Custom tools, functions and pipelines |
| 0.9.2 to 0.9.6 (May 2026) | [Issue #24743](https://github.com/open-webui/open-webui/issues/24743): "External connections and advanced model parameters are lost after restart"; duplicate `config` rows suspected; maintainer could not reproduce it at first. 0.10.0 lists "Settings no longer lost after a restart" | **Exactly the owner's symptom** |
| 0.9.3 (2026-05-09) | Sign-out became POST only | Custom clients |
| 0.10.0 (2026-06-29) | **Native tool calling became the default**, with the old behaviour renamed "Legacy"; default model parameters now actually applied to requests; database migration, no downgrade | Tools on models without good native tool calls; sampler behaviour changed silently |
| 0.11.0 to 0.11.4 (Jul-Sep 2026) | `python-jose`, `langchain-community`, `nltk`, `pymongo` and other packages removed from the image | Functions and tools that imported them without declaring requirements |
| 0.11.3 (2026-08-31) | Fix for "the upgrade failure seen after moving from 0.11.0, 0.11.1, or 0.11.2" | Upgrades themselves |
| 0.12.0 to 0.12.2 (Oct 2026) | Database migrations; Milvus collections copied in full on first start; Valkey must be upgraded or startup fails | Upgrade time and vector stores |

Nearly every release since 0.8.0 repeats the same warning: "back up your
database", and "rolling updates are not supported". The manual-migration guide
says downgrading "can cause **permanent data loss**"
([manual-database-migration](https://github.com/open-webui/docs/blob/main/docs/troubleshooting/manual-database-migration.md)).
For production the updating guide recommends: "Pin a version, review release
notes before upgrading, test in staging first"
([updating](https://github.com/open-webui/docs/blob/main/docs/getting-started/updating.mdx)).

Backups:

- The admin panel's database download copies `webui.db` without its `-wal`
  file. The backups guide uses `sqlite3 webui.db ".backup ..."` instead
  ([backups](https://github.com/open-webui/docs/blob/main/docs/tutorials/maintenance/backups.md)).
- A third-party tracker reports that 0.9.2 needed Postgres connection-string
  changes for a psycopg v3 move
  ([releaseport](https://releaseport.com/tools/open-webui/releases)).
  **UNVERIFIED** against the official notes.

**What this means for Seraphina.**

- Open WebUI must be driven through its API, never through environment edits
  once it has started.
- Its upgrades must be rehearsed on a copy, because migrations cannot be undone
  (§8.4).
- Its Python plugins need their own load-and-run tests after every upgrade.

### 2.2 TabbyAPI (main at `a270c55`, 2026-10-19)

**Where its state lives (all files, no database).**

- **`config.yml`** has the sections `network`, `logging`, `model`,
  `draft_model`, `sampling`, `lora`, `embeddings`, `memory` and `developer`
  ([config_sample.yml](https://github.com/theroyallab/tabbyAPI/blob/main/config_sample.yml)).
- **`api_tokens.yml`** holds `api_key` (one key or a list) and exactly one
  `admin_key`. "Changes to this file are picked up by a running server
  automatically" ([api_tokens_sample.yml](https://github.com/theroyallab/tabbyAPI/blob/main/api_tokens_sample.yml)).
- **`sampler_overrides/<preset>.yml`** works per sampler. `override` gives the
  value, `force` overrides even what the client sends, and `additive` appends to
  lists ([docs 08 Sampler Overrides](https://github.com/theroyallab/tabbyAPI/blob/main/docs/08.-Sampler-Overrides.md)).
- **`templates/*.jinja`** are named chat templates.
- **`models/<name>/tabby_config.yml`** holds per-model load overrides under
  `model:` and `draft_model:`, including a per-model `sampling:` block (added
  2026-09-26) ([common/model.py](https://github.com/theroyallab/tabbyAPI/blob/main/common/model.py)).
- The built-in test chat at `/ui` keeps its settings only in the browser's
  `localStorage`. Nothing is stored on the server
  ([endpoints/ui/static/app.js](https://github.com/theroyallab/tabbyAPI/blob/main/endpoints/ui/static/app.js)).

**Model-profile facts, and their defaults** (from `config_sample.yml`):

- `max_seq_len` defaults to min(max_position_embeddings, cache_size); `-1`
  reads the model's `config.json`.
- `cache_size` defaults to **4096** and must be a multiple of 256.
- `cache_mode` defaults to FP16, or `k,v` bit pairs.
- `prompt_template`: if it is empty, Tabby uses the model's own chat template.
- `template_vars_default` and `template_vars_force` hold reasoning switches such
  as `enable_thinking` (they replaced `force_enable_thinking`).
- Reasoning parser: `reasoning`, `reasoning_start_token`,
  `reasoning_end_token`, `reasoning_budget_tokens`.
- `tool_format: auto`.
- `max_batch_size`.
- `tensor_parallel_backend`: `nccl` is the value recommended for NVLink.
- The sample config's top-level `sampling.override_preset` is `safe_defaults`,
  which fills in `temperature 0.8`, `top_k 40`, `top_p 0.95` and `min_p 0.05`
  when the client sends none
  ([safe_defaults.yml](https://github.com/theroyallab/tabbyAPI/blob/main/sampler_overrides/safe_defaults.yml)).
- The shipped `sample_preset.yml` sets `max_tokens: override: 150`. A copied
  preset therefore caps replies at 150 tokens for clients that send no
  `max_tokens`
  ([sample_preset.yml](https://github.com/theroyallab/tabbyAPI/blob/main/sampler_overrides/sample_preset.yml)).

**Precedence.** Lowest to highest: `use_as_default` keys from `config.yml`, then
the load arguments and the model folder's `tabby_config.yml`. "For a startup load
the file wins … An API request … only carries the options the client set, and
those win over the file" (common/model.py). That last rule came in on
2026-10-12 ([commit history of common/model.py](https://github.com/theroyallab/tabbyAPI/commits/main/common/model.py)).

**Instruction sets.**

- TabbyAPI has **no store for system prompts**. A search of the code found none.
- The only server-side "instructions" are the chat template (which can embed a
  default system message) and `template_vars_*`.
- Any instruction set the owner "manages in Tabby" therefore lives in a template
  file, in a client (Open WebUI, or Tabby's `/ui` in the browser), or in the
  frontends' saved presets. **UNVERIFIED** which one the owner uses (see Open
  questions).

**API** ([endpoints/core/router.py](https://github.com/theroyallab/tabbyAPI/blob/main/endpoints/core/router.py)):

| Purpose | Endpoint | Key |
| ------- | -------- | --- |
| Loaded model card: `max_seq_len`, `cache_size`, `cache_mode`, `max_batch_size`, `prompt_template`, `prompt_template_content`, draft | `GET /v1/model` | API |
| llama.cpp-shaped properties: `n_ctx`, `chat_template`, `total_slots` | `GET /props` | API |
| Model list: **all models in `model_dir` with the admin key, only the loaded model with a normal key** | `GET /v1/models` | API |
| Effective load defaults for a model (its `tabby_config.yml` over `use_as_default`), also as YAML | `GET /v1/model/load_config?model_name=` | admin |
| Load (server-sent events) and unload | `POST /v1/model/load`, `POST /v1/model/unload` | admin |
| Templates: list, switch, unload | `GET /v1/templates`, `POST /v1/template/switch`, `POST /v1/template/unload` | admin for writes |
| Sampler presets: list, switch, unload | `GET /v1/sampling/overrides`, `POST /v1/sampling/override/switch`, `POST /v1/sampling/override/unload` | admin for writes |
| Key permission, health, tokenizer | `GET /v1/auth/permission`, `GET /health`, `POST /v1/token/encode` | API |

Changes made by "switch" and "load" calls last only while the process runs. They
are lost on restart unless the files say the same thing.

**A silent substitution trap.** With `inline_model_loading: false` (the
default), a chat request naming a model that is not loaded only logs a warning
and **runs on whatever model is loaded**
([endpoints/OAI/utils/common_.py](https://github.com/theroyallab/tabbyAPI/blob/main/endpoints/OAI/utils/common_.py)).
A client that lists models with the admin key sees every model folder. It can
offer all of them, and every answer will come from the one loaded model.

**What broke on upgrade.** TabbyAPI has no release notes, so this comes from its
commit history
([common/model.py commits](https://github.com/theroyallab/tabbyAPI/commits/main/common/model.py)):

- 2025-06-13: default `max_seq_len` of 4096, and the inline config "migrated to
  new format". `tabby_config.yml` must nest options under `model:`, otherwise
  Tabby logs "Cannot find inline model overrides".
- 2025-10-05: "Change cache_size/max_seq_len behavior".
- **2026-07-14: "Remove ExLlamaV2 backend".** EXL2 models no longer load on
  `main`. Loading one raises "The exllamav2 backend is no longer supported"
  (common/model.py). The README points to an `exl2-checkpoint` branch
  ([README](https://raw.githubusercontent.com/theroyallab/tabbyAPI/main/README.md)).
- 2026-07-18: `template_vars_default` and `template_vars_force` added, and
  `force_enable_thinking` deprecated.
- 2026-10-12: load-request arguments now take priority over the model folder's
  file.
- 2026-10-19: minimum exllamav3 raised to 1.6.2 (latest commit). Tabby refuses
  to start on an older exllamav3 unless `developer.unsafe_launch` is set
  (config_sample.yml).
- Upstream's own docs drift too. The sampler-override guide says to set a
  `sampler_override` key, but the config key is `override_preset`.

**What this means for Seraphina.**

- Tabby facts are cheap to observe exactly through `GET /v1/model`, `/props`
  and `load_config`, and cheap to change in files.
- Because there are no version numbers, the adapter records the git commit and
  the exllamav3 version.

### 2.3 llama.cpp server, including router mode (master at `8baabd5`, 2026-10-21)

**Where its state lives.**

- Command-line flags, normally in a systemd unit or compose file. Almost every
  flag has an environment twin (`LLAMA_ARG_*`), for example `-c` and
  `LLAMA_ARG_CTX_SIZE`, `--jinja` and `LLAMA_ARG_JINJA`, `--api-key` and
  `LLAMA_API_KEY`, `--api-key-file` and `LLAMA_ARG_API_KEY_FILE`
  ([tools/server/README.md](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)).
- The GGUF file's metadata (chat template, trained context).
- In router mode, the model cache (`LLAMA_CACHE`), a `--models-dir`, and an INI
  preset file `--models-preset` (same README, "Using multiple models").
- Web UI defaults: `--ui-config` / `--ui-config-file`, which renamed the older
  `--webui-config`. After that, the UI's own settings are probably kept in the
  browser. **UNVERIFIED**: I did not read the UI code.

**Router mode and presets.**

- Start `llama-server` with no model. It forwards each request to a child
  instance chosen by the `"model"` field, or by `?model=` on GET endpoints.
- Children inherit the router's flags and environment. The router controls
  host, port, API key and alias.
- Preset INI: `[*]` holds global keys and `[<model>]` holds per-model keys. Keys
  can be long flags, short flags, or `LLAMA_ARG_*` names, for example
  `chat-template = chatml`, `c = 4096`, `jinja = true`.
- Precedence: **command line > model section > `[*]`**.
- Options that exist only in presets: `load-on-startup`, `stop-timeout`,
  `dedup-cache-models`.
- `--models-max` (default 4) caps how many models are loaded at once.
- Endpoints: `GET /models` (with each model's `status` and the `args` it runs
  with), `POST /models/load`, `POST /models/unload`, and `GET /models?reload=1`.
  `reload=1` re-reads the sources, unloads running models that changed, and adds
  new ones.

**Model-profile facts.**

- `-c 0` means "use the model's trained context".
- `-np -1` means automatic slots, and `--kv-unified` is on by default when slots
  are automatic.
- `--jinja` is now **enabled by default**. When Jinja support was added in
  [#11016](https://cdn04132025.gitlink.org.cn/replica/llama.cpp/commit/6171c9d25820ccf676b243c172868819d882848f)
  it was "default: disabled". **UNVERIFIED:** which build changed it.
- `--reasoning-format` defaults to `auto`. `--reasoning-budget` and
  `--chat-template-kwargs` are also available.
- Sampler defaults come from flags. `GET /props` returns
  `default_generation_settings` (with `n_ctx` and every sampler value),
  `total_slots`, `model_path`, `chat_template`, `chat_template_caps` and
  `build_info`.
- `GET /slots` shows each slot's actual `n_ctx`.
- `GET /v1/models` includes `meta.n_ctx_train`.
- `POST /apply-template` renders the chat template for a given conversation.
  That makes it a precise test of template identity.
- `POST /props` changes settings only if the server was started with `--props`.
- (All from the server README.)

**What broke on upgrade.**

- June 2024: binaries renamed (`main` became `llama-cli`, `server` became
  `llama-server`, [#7809](https://cdn04132025.gitlink.org.cn/replica/llama.cpp/src/commit/f4d2b8846a6b34419ff9e9491aee6cd95e444bfc/examples/deprecation-warning)).
  The old names became a stub that prints a warning and **exits with failure**
  (#8283, per the same mirror). Mirror sources only; **UNVERIFIED** on upstream
  GitHub.
- April-July 2026: [issue #22364](https://github.com/ggml-org/llama.cpp/issues/22364)
  (build b8931). The `version = 1` line in a preset file created a phantom model
  named `default` in `GET /models`. Fixed by PR #25643, closed 2026-07-25.
- Defaults move between builds (`--jinja`, slot count, unified KV, reasoning
  format), and a router-mode guide warns that "flags may change between commits"
  ([dev.to guide](https://dev.to/rosgluk/llama-server-router-mode-dynamic-model-switching-without-restarts-1h0j),
  per search extract).

**What this means for Seraphina.**

- llama.cpp exposes almost everything it actually loaded through `/props`,
  `/slots` and `/models`.
- The registry should store the *intended* flags, and compare them with what
  the server reports, not with the unit file alone.
- Record `build_info` with every observation.

### 2.4 DeepSeek Harness, `@deepseek-ai/dsh` (0.2.1-rc.2, 2026-10-17)

The README says: "DeepSeek Harness is in _developer preview_ and iterating
rapidly. **THERE WILL BE COMPATIBILITY-BREAKING CHANGES.**"
([README](https://github.com/deepseek-ai/deepseek-harness/blob/master/README.md)).
It was first released 2026-08-13
([eesel.ai](https://www.eesel.ai/blog/deepseek-harness)). An unrelated PyPI
package `deepseek-harness` also installs a `dsh` command (same source), so
detect the right one.

**Where its state lives.** The home directory is `$DSH_HOME`, or `~/.dsh` if
that is unset.

- `$DSH_HOME/.credentials.yaml` holds API keys. They are write-only in the UI,
  and settings keep only a reference to them.
- `$DSH_HOME/.env` holds environment fallbacks.
- `$DSH_HOME/profiles/<profile>/cordis.patch.yml` holds per-profile plugin
  configuration: providers, models and system-prompt settings. `dsh web` uses
  the `web` profile.
- `$DSH_HOME/profiles/<profile>/package.json` lists installed plugins.
- `$DSH_HOME/cordis.patch.yml` is a home-level patch shared by every profile.
- `$DSH_HOME/AGENTS.md` and `$DSH_AGENTS_HOME/AGENTS.md` (default
  `~/.agents/AGENTS.md`) hold user-global instructions.
- Project instructions are found by walking up from the working directory:
  `AGENTS.md`, `CLAUDE.md`, and the overlays `AGENTS.local.md` and
  `CLAUDE.local.md`.
- Skills come from project roots, `$DSH_HOME`, `~/.agents` and any custom
  directories.
- Sessions are JSONL logs whose persistence format has its own version number.

Sources: [providers guide](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/guide/providers.md),
[network-proxy guide](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/guide/network-proxy.md),
[agent-instructions config.ts](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/context/agent-instructions/src/config.ts),
[render.ts](https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/context/agent-instructions/src/render.ts),
[config catalog](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/config-catalog.md).

**Layering** ([plugin publish guide](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/develop/basic/publish.md)).
The layers apply in this order:

1. bundle patches;
2. the profile's `cordis.patch.yml`;
3. the home-level `$DSH_HOME/cordis.patch.yml`;
4. any `--patch` overlays.

"Later layers win per row, and a patch **replaces a row's entire `config`
value** rather than deep-merging keys." This has two consequences:

- A partial edit wipes the sibling settings in the same row.
- A home-level row for the same plugin silently overrides what the Web UI saves
  into the profile. This is inferred from the documented order and is
  **UNVERIFIED** in practice.

**Model profiles.**

- Custom providers (`llm-pi-ai`) have `baseURL`, `api` (protocol),
  `apiKeyEnv`, and `models: [{id, contextWindow, maxTokens, input,
  reasoningEfforts}]`.
- Overrides for built-in or account models go in `modelOverrides`.
- "The Provider ID is permanent." A session "retains the model recorded in its
  own log", so old sessions keep old routing (providers guide).
- Separate profiles (`web`, `headless`, `sdk`) each have their own patch. Since
  0.2.1-alpha.2 the `headless` profile also **ignores the credential store**
  ([stored-credentials-precedence](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/upgrade-guide/v0.2.1-alpha.2/stored-credentials-precedence/guide.md)).

**Instruction sets.**

- The `dsh-system-prompt` plugin has `personaPrefix` and `personaSuffix`
  templates, in which "`{{variable}}` references are strict".
- Its other settings are `includeHarnessIdentity` (default true),
  `includeRuntimeContext` and `toolOrder` (config catalog).
- On top of that come the AGENTS.md files above and skills.

**Reading and writing it.**

- There is no documented public settings API.
- The Web UI on `127.0.0.1:3080` talks to the host through a generated RPC
  gateway (`POST /api/<namespace>/<method>`), which I treat as internal
  ([api-gateway](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/api-gateway.md)).
  **UNVERIFIED** as a stable external interface.
- The CLI gives a clean read path
  ([cli-help](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/cli-help.md)):
  - `--dump-config` prints the composed profile tree;
  - `--dump-default-config` prints it without the user layer, so the difference
    between the two is exactly the user's edits;
  - `--dump-config-schema` prints a JSON Schema for patches.
  - `dsh --profile headless "<task>"` answers one task and exits, and `--json`
    gives machine-readable events.

**What broke on upgrade.** DSH ships a per-release upgrade guide directory
([docs/upgrade-guide](https://github.com/deepseek-ai/deepseek-harness/tree/master/docs/upgrade-guide)).
Examples that match the owner's pain:

- 0.2.1-alpha.1: `{{cwd}}` was removed from prompt templates. "Custom prompt
  templates that retain an unresolved `{{cwd}}` fail before a model request"
  ([working-directory](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/upgrade-guide/v0.2.1-alpha.1/working-directory/guide.md)).
  An instruction set broke because of a harness upgrade.
- 0.2.1-alpha.1: `dshHome` was removed from `agent-instructions`. A patch that
  still sets it "has no effect: the row keeps the stray key and the plugin
  ignores it" ([instruction-home-override](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/upgrade-guide/v0.2.1-alpha.1/instruction-home-override/guide.md)).
  This is silent, stale configuration.
- 0.2.1-alpha.2: credential precedence flipped. The stored key now beats an
  exported environment variable (stored-credentials-precedence guide).
- 0.2.1-alpha.2: `--from-default-profile` now **deletes the target profile,
  including its `cordis.patch.yml`**, every time it runs
  ([from-default-profile-replaces-target](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/upgrade-guide/v0.2.1-alpha.2/from-default-profile-replaces-target/guide.md)).
- The session persistence format changed across 26 prerelease tags between
  2026-08-10 and 2026-09-12
  ([persistence-changes](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/persistence-changes/releases/README.md)).

**What this means for Seraphina.**

- DSH is the fastest-moving consumer.
- The adapter must read DSH through `--dump-config`, validate every write
  against `--dump-config-schema`, write whole rows, and run the matching upgrade
  guides' checks before and after each DSH update.

### 2.5 Qwen Code (0.25.3, main at `97567d5`, 2026-10-21)

**Where its state lives**
([settings](https://github.com/QwenLM/qwen-code/blob/main/docs/users/configuration/settings.md)).
The layers, lowest to highest precedence:

1. defaults;
2. `/etc/qwen-code/system-defaults.json`;
3. `~/.qwen/settings.json` (`$QWEN_HOME`);
4. the project's `.qwen/settings.json`;
5. `/etc/qwen-code/settings.json`;
6. environment variables, including `.qwen/.env` and `~/.qwen/.env`;
7. command-line flags.

Other files:

- Context ("memory") files: `QWEN.md`, loaded hierarchically. The name is
  configurable through `context.fileName`.
- Subagents in `~/.qwen/agents/`, skills in `~/.qwen/skills/<name>/SKILL.md`,
  commands in `~/.qwen/commands/`, output styles in `~/.qwen/output-styles/`.
- MCP servers under `mcpServers` in `settings.json`.

**Model profiles**
([model-providers](https://github.com/QwenLM/qwen-code/blob/main/docs/users/configuration/model-providers.md)).
These are kept in `modelProviders[<authType>][]` entries, each with `id`,
`name`, `envKey`, `baseUrl`, and `generationConfig`. The `generationConfig`
holds `timeout`, `contextWindowSize`, `samplingParams` (`temperature`, `top_p`,
`max_tokens`), `extra_body` and so on. Two rules are hidden traps:

- "**The modelProvider layer is impermeable**." For a provider model, the
  top-level `model.generationConfig` (including `contextWindowSize`) is
  ignored, and fields the provider entry leaves out are `undefined`.
- When `contextWindowSize` is not set, Qwen Code "determines the context window
  using built-in defaults based on model name matching, with a constant fallback
  value". **A local fine-tune with an unfamiliar name gets a guessed context
  length** (settings.md).
- Also: `model.baseUrl` is saved automatically by the model picker, and "a
  stale hand-edited value can silently route requests to a different same-id
  provider" (settings.md).

**API.** None. It is a CLI. Read the files. For a live check use
`qwen -p "<prompt>" -m <model>`, which runs non-interactively, and `-o` sets the
output format (settings.md, command-line table).

**What broke on upgrade.**

- The settings file has a `$version`, currently 4. Older formats are migrated
  automatically "with old settings files backed up". Some `disable*` keys were
  renamed to `enable*` **with their boolean values inverted** (settings.md).
- Deprecated keys (`tools.allowed`, `tools.exclude`, `tools.core`) are "not
  automatically migrated" but still honoured.
- Removed keys (`model.chatCompression.contextPercentageThreshold`) are
  ignored.
- `security.auth.apiKey` and `security.auth.baseUrl` are deprecated
  (model-providers).
- The free Qwen OAuth tier ended on 2026-04-15
  ([issue #3203](https://github.com/QwenLM/qwen-code/issues/3203),
  [inventivehq](https://inventivehq.com/blog/qwen-code-still-free-2026-shutdown)).
  Anyone relying on it lost model access on an update, or on the date itself.

### 2.6 Two more consumers to include

- **The Software Factory itself.** Its `config.json` `providers` and `models`
  (DESIGN.md §11 and §20) hold the same endpoints, keys (by env var or key file),
  `max_tokens` and `context_tokens` as everything else.
- **Cosmos Cloud.** It owns the Open WebUI container (image tag, environment,
  volumes). The Cosmos README promises it "keeps containers up to date". If
  Cosmos updates an image, that is an upgrade Seraphina did not start, so it
  must trigger the same contract tests (see
  [platforms-ai.md §1](platforms-ai.md); per-container auto-update settings are
  **UNVERIFIED** there).

---

## 3. What actually causes drift: eleven mechanisms

Each mechanism comes from the findings above. The design must answer every one.

| # | Mechanism | Real example |
| - | --------- | ------------ |
| 1 | **Seed-once settings.** The environment is read only on first start, and the database wins after that. | Open WebUI `ConfigVar`. Editing the compose file changes nothing. |
| 2 | **Position-keyed settings.** | Open WebUI `OPENAI_API_CONFIGS` is keyed by URL index. |
| 3 | **Sealed or whole-row layers.** A partial write wipes or ignores the siblings. | Qwen's "impermeable" provider layer; DSH row replacement. |
| 4 | **Silent fallbacks and guesses.** | Tabby serves the loaded model whatever name is asked for; Tabby `cache_size` 4096; Qwen guesses context from the model name; llama.cpp `-c 0`. |
| 5 | **Unknown or stale keys accepted and ignored.** | DSH `dshHome`; Qwen removed keys; Tabby warns once and drops unknown load options. |
| 6 | **Precedence flips between versions.** | DSH credentials (0.2.1-alpha.2); Tabby request versus file (2026-10-12); Open WebUI default params now applied (0.10.0). |
| 7 | **Code stored inside the app that imports the app's internals.** | Open WebUI functions broken by 0.3.17, 0.5.0, 0.9.0, and the package removals in 0.11.x. |
| 8 | **Renamed or removed environment variables**, sometimes with an alias for a while. | Open WebUI 0.6.23, 0.6.37, 0.6.39, 0.6.42, 0.10.0. |
| 9 | **Migrations that cannot be undone.** | Open WebUI "downgrade … permanent data loss". |
| 10 | **State that lives only in a browser or a chat.** | Tabby `/ui` keeps its settings in `localStorage` (probably the llama.cpp web UI too, **UNVERIFIED**); Open WebUI per-chat and per-account prompts. |
| 11 | **One instruction text copied into N places with N template syntaxes.** | Open WebUI `{{VAR}}` prompt variables; DSH strict `{{variable}}`; Jinja in Tabby and llama.cpp templates. |

Mechanism 11 is the root of "malformed and contradictory" instruction sets.
Every copy is hand-adapted to its consumer's syntax and edited where it lives.
Then the copies diverge.

---

## 4. Design: the shared facts registry

### 4.1 Principles

1. **One record per fact**, and **every copy is a named binding**. A binding is
   a pair: the consumer, and the exact location in that consumer (file and key,
   API path, or database key).
2. **Declared versus observed.** The registry stores what we *intend*. Adapters
   report what each consumer *has*, and what each server *actually loaded*. Both
   are kept and compared. Neither silently overwrites the other.
3. **Each binding has one writer.** Its mode is `managed` (Seraphina writes it),
   `observed` (Seraphina only reads it, and a human or the app owns it) or
   `ignored`. Adapters never write `observed` bindings.
4. **The registry is a git repository inside the Blueprint**
   (`<data_dir>/blueprint/facts/`, DESIGN.md §28). Every change is a commit
   with a reason and a link to its tracker item. History, diff and rollback
   come for free.
5. **No secrets inside.** Keys appear as references (`secret_ref`) plus a
   fingerprint (the first 8 hex digits of their sha256), as DESIGN.md §28
   already requires.
6. **JSON on disk.** The harness is stdlib Python and has no YAML parser. JSON
   files keep the registry readable without dependencies. Reading Tabby's and
   DSH's YAML files is an adapter concern (see Open questions).

### 4.2 Schema sketch

Files, one directory per kind:

```
facts/
  endpoints/<id>.json        servers and how to reach them
  models/<id>.json           what a server serves, and how it is loaded
  profiles/<id>.json         how a client should use a model
  instructions/<id>/         instruction sets: body.md + meta.json
  tools/<id>.json            Open WebUI functions/tools, MCP servers, tool servers
  knowledge/<id>.json        knowledge bases and their sources
  consumers/<id>.json        each app that consumes facts, with its bindings
  invariants.json            rules across facts
  contracts/<id>.json        end-to-end contract tests (§8)
```

The examples below describe the owner's stack. The values are illustrative.
The `//` comment lines are labels for this note only; real files are plain JSON.

```json
// facts/endpoints/tabby-main.json
{
  "id": "tabby-main",
  "kind": "tabbyapi",
  "base_url": "http://127.0.0.1:5000/v1",
  "auth": {"api_key": {"secret_ref": "tabby.api_key", "fingerprint": "3f9a01c2"},
           "admin_key": {"secret_ref": "tabby.admin_key", "fingerprint": "77d0e4b1"}},
  "owner": {"systemd_unit": "tabbyapi.service"},
  "expect": {"unauthenticated_models": "rejected", "listen": "loopback"}
}
```

```json
// facts/models/flash-next-sec.json
{
  "id": "flash-next-sec",
  "endpoint": "tabby-main",
  "served_name": "Flash-Next-Sec-exl3-6.0bpw",
  "artifact": {"path": "/srv/models/Flash-Next-Sec-exl3-6.0bpw", "format": "exl3",
               "sha256_of_config": "…"},
  "load": {"max_seq_len": 262144, "cache_size": 262144, "cache_mode": "8,8",
           "tensor_parallel": true, "tensor_parallel_backend": "nccl",
           "max_batch_size": 1},
  "chat_template": {"source": "model", "sha256": "…"},
  "reasoning": {"parser": true, "start": "<think>", "end": "</think>",
                "default_vars": {"enable_thinking": true}},
  "tool_format": "auto",
  "sampler_defaults": {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0},
  "sampler_enforced_at": "server",
  "max_output_tokens": 32768,
  "baseline": {"tokens_per_s": 100, "measured_at": "2026-10-01", "blueprint": "…"}
}
```

```json
// facts/profiles/sec-analyst-chat.json  (one model, used one way)
{
  "id": "sec-analyst-chat",
  "model": "flash-next-sec",
  "instruction_set": "sec-analyst",
  "max_tokens": 16384,
  "context_budget": 262144,
  "compaction_threshold": 200000,
  "sampler": "inherit",
  "tools": ["web-search", "cve-lookup"]
}
```

```json
// facts/instructions/sec-analyst/meta.json  (body.md holds the canonical text)
{
  "id": "sec-analyst",
  "version": 7,
  "purpose": "Cyber-security analyst persona for lab work",
  "owner": "owner",
  "variables": {"LAB_SCOPE": "the 10.20.0.0/24 lab only"},
  "sha256_body": "…",
  "claims": [
    {"id": "exploit-policy", "text": "Proof-of-concept exploit code may be run only inside the lab VM."},
    {"id": "no-prod", "text": "Never touch hosts outside LAB_SCOPE."}
  ],
  "render": {
    "openwebui.model.params.system": {"format": "openwebui-vars"},
    "dsh.agents_md.block":           {"format": "markdown"},
    "qwen.agent_file":               {"format": "markdown-frontmatter"}
  }
}
```

```json
// facts/consumers/openwebui.json
{
  "id": "openwebui",
  "kind": "openwebui",
  "version_seen": "0.12.3",
  "adapter": "openwebui",
  "base_url": "http://127.0.0.1:3000",
  "auth": {"secret_ref": "openwebui.admin_api_key"},
  "bindings": [
    {"fact": "endpoints/tabby-main", "at": "config:openai.api_base_urls[i]", "match": "by_url", "mode": "managed"},
    {"fact": "endpoints/tabby-main.auth.api_key", "at": "config:openai.api_keys[i]", "mode": "managed"},
    {"fact": "profiles/sec-analyst-chat", "at": "model:flash-sec", "mode": "managed",
     "fields": {"base_model_id": "<prefix>.served_name", "params.system": "instruction_set",
                "params.max_tokens": "max_tokens", "params.compact_token_threshold": "compaction_threshold"}},
    {"fact": "tools/cyber-redact-filter", "at": "function:cyber_redact", "mode": "managed"}
  ],
  "unmanaged_policy": "report"
}
```

```json
// facts/invariants.json  (checked in code; each has a plain-language message)
[
  {"id": "ctx-fits", "rule": "profile.context_budget <= observed(model).n_ctx",
   "says": "{consumer} thinks {model} can read {a} tokens, but {server} loaded it with {b}."},
  {"id": "out-fits", "rule": "profile.max_tokens < observed(model).n_ctx",
   "says": "{consumer} asks for replies of up to {a} tokens, which does not fit the {b}-token context."},
  {"id": "one-sampler-owner", "rule": "count(bindings where sampler set and mode=managed) == 1",
   "says": "Sampler values for {model} are set in {n} places ({where}); only one should set them."},
  {"id": "served-name", "rule": "observed(endpoint).loaded == model.served_name",
   "says": "{server} is serving {b}, not {a}. Requests for {a} are answered by {b}."}
]
```

`observed(...)` means "the value an adapter read from the live server in this
run". It never means the value written in the server's config file.

### 4.3 Seeding the registry: adopt what is there

On first use, a wizard runs every adapter's `read`. It groups equal values and
shows the owner one card per fact, with every value found and where it was
found. The owner picks the true value, or types it. For instruction sets the
wizard shows a side-by-side diff of every copy and asks which one is
canonical. A reviewer agent is told to point out contradictions (§6.3). The
result is the first registry commit. Nothing is written to any consumer during
adoption.

---

## 5. Adapters: one per consumer

Every adapter has the same small interface (stdlib Python, no root, no writes):

```python
class Adapter:
    kind: str                                  # "openwebui", "tabbyapi", ...
    def probe(self) -> Probe:                  # reachable? auth ok? version/build/commit
    def read(self) -> Snapshot:                # raw, redacted copy of everything relevant + provenance
    def extract(self, snap) -> list[Observed]: # normalized facts: (fact path, value, where, how sure)
    def render(self, desired, snap) -> list[Action]:  # proposed changes; pure function, no side effects
    def checks(self, actions) -> list[str]:    # contract-test ids that prove the change worked
```

`render` produces actions from the §27 change-bundle vocabulary (`file.edit`
with a diff and a backup, `http.call` with the request body shown,
`systemd.restart`). Only the executor applies them. Each adapter carries a
**version map**: the version or commit ranges it knows, and the locations and
quirks for each. An unknown version makes it read-only, and it reports
"Seraphina does not know Open WebUI 0.13 yet: checking only, not changing".

| Consumer | Read | Write (managed bindings only) | Quirks the adapter encodes |
| -------- | ---- | ----------------------------- | -------------------------- |
| **Open WebUI** | `GET /api/version`; `/api/v1/configs/export` (secrets fingerprinted on arrival); `/openai/config`; `/api/v1/models/export`; `/api/v1/functions/export`; `/api/v1/tools/export`; prompts, skills and knowledge lists. Fallback with no API key: `sqlite3 .backup` to a temporary copy, then read it read-only | `POST /openai/config/update`: read, modify, write the **whole lists in their existing order**, and append new URLs, never reorder. `POST /api/v1/models/import` (upsert). `POST /api/v1/configs/import` for listed keys only. Functions: single-item `update` routes. **Never `/models/sync` or `/functions/sync`** unless every model or function is a managed binding, because both delete what they are not sent | Seed-once env (§2.1): the env value is never a source of truth after first start; record `ENABLE_PERSISTENT_CONFIG`. Needs `ENABLE_API_KEYS` and an admin key. Per-model system prompt beats per-user, but per-chat params beat per-model |
| **TabbyAPI** | `GET /v1/model`, `/props`, `/v1/models` (with the admin key, to list folders), `/v1/model/load_config?model_name=`, `/v1/sampling/overrides`, `/v1/templates`, `/v1/auth/permission`; files `config.yml`, `models/*/tabby_config.yml`, `sampler_overrides/*.yml`, `templates/*.jinja`; `api_tokens.yml` fingerprints only; git commit and exllamav3 version | `file.edit` of `tabby_config.yml`, `config.yml` or a preset, then `POST /v1/model/load` (admin) so it takes effect, then read `/v1/model` again. A runtime-only `switch` call is always paired with the same change in a file | Request-versus-file precedence changed on 2026-10-12. Silent substitution unless `inline_model_loading` is on. The `max_tokens: 150` preset trap. No server-side system prompt |
| **llama.cpp server** | Unit file or compose args (from the Blueprint); preset INI; `GET /props` (and `?model=` in router mode); `/slots`; `/v1/models`; `/models`; `build_info` | `file.edit` of the preset INI, then `GET /models?reload=1`; or a unit override plus `systemd.restart` for flags | CLI beats model section beats `[*]`. The router overrides host, port, key and alias. Children inherit the router's environment, so `LLAMA_ARG_*` set on the router reach every model |
| **DeepSeek Harness** | `dsh --profile <p> --dump-config` and `--dump-default-config` (the difference is the user layer); `--dump-config-schema`; `$DSH_HOME/AGENTS.md`; skills directories; whether `.credentials.yaml` exists (never its contents); `dsh -V` | Rewrite the **complete row** in `profiles/<p>/cordis.patch.yml` (rows replace, they do not merge). Validate against the schema before writing. Run `--dump-config` again afterwards and compare. Managed block in AGENTS.md | Profiles drift apart (`web` versus `headless`); keep shared rows in the home-level patch, or render the same row into each profile. Provider id is permanent. Stray keys are ignored. Strict `{{variables}}` |
| **Qwen Code** | All settings layers (system defaults, user, project, system, `.env`); `QWEN.md` hierarchy; `~/.qwen/agents`; skills; `qwen --version`; `$version` | Rewrite the complete `modelProviders[...]` entry, with the full `generationConfig` because it is sealed. Managed block in `QWEN.md` or an agent file | Always set `contextWindowSize` and `max_tokens` explicitly. Never hand-write `model.baseUrl`. Check that `$version` is one the adapter knows |
| **Software Factory** | `Config.load()`: `providers`, `models` (`context_tokens`, `max_tokens`), drivers | `Config.save()` (writes overrides only, DESIGN.md §11) | Its own doctor already checks reachability and key resolution (DESIGN.md §20) |

**Managed blocks for instruction files.** Seraphina never owns a whole
`AGENTS.md`, `QWEN.md` or system prompt that people also edit. It owns a marked
block:

```
<!-- seraphina:begin set=sec-analyst v=7 sha256=1c0e… (edit in Seraphina, not here) -->
…rendered text…
<!-- seraphina:end set=sec-analyst -->
```

Text outside the markers is left alone. Inside, a hash mismatch means someone
edited the copy where it lives. The detector then shows the edit as a
**proposed change to the canonical set**: a hand edit is adopted only after
review, never thrown away. Open WebUI's `params.system` has no comments. There,
the adapter compares the hash of the whole field against the rendered text, and
records the rendered sha256 in the model's `meta` (which accepts extra keys).

---

## 6. Drift detector

### 6.1 When it runs

- On a schedule: daily, and on the §31.3 maintenance-window schedule.
- Before and after every change bundle, as part of the before and after
  Blueprint captures.
- After any upgrade of any consumer, including image updates Cosmos started. It
  notices those because `probe()` reports a new version.
- On demand, from a "Check AI stack consistency" button on the GPU & AI Grid
  Card.

### 6.2 What it compares

1. **Declared versus each binding.** The registry says X, and a managed copy
   holds Y. This includes instruction-block hashes.
2. **Declared versus observed reality.** The registry says the model is loaded
   with 262,144 tokens, but `/v1/model` reports a different `max_seq_len`, or
   `/props` and `/slots` report a different `n_ctx`. Template sha256 and served
   name are compared the same way.
3. **Consumer versus consumer for the same fact.** This catches drift even in
   `observed` bindings that Seraphina does not manage. Example: Qwen Code
   assumes 131,072 tokens while DSH declares 262,144.
4. **Invariants.** The rules in `invariants.json` (§4.2), such as context fits,
   output fits, one sampler owner, served name.
5. **Unregistered items.** A model, connection, function or instruction copy
   that exists in a consumer but in no registry record. It becomes a finding
   with two choices: "Adopt into the registry" or "Remove".
6. **Upgrade exposure.** The registry knows which tools import what (each
   `tools/*.json` lists its Python imports and requirements) and which
   environment variables each consumer depends on. Before an upgrade, an agent
   reads the release notes between the current and target versions. The code
   then matches them against those lists. One example finding: "0.11.4 removes
   `langchain-community`; your filter `cyber_redact` imports it".

Normalisation happens before comparing:

- token counts to integers;
- sampler names to one vocabulary (`top_p` and `topP` are the same);
- URLs to scheme, host, port and path, with `localhost` and `127.0.0.1` equal;
- templates by the sha256 of their source, and on llama.cpp also by the sha256
  of `/apply-template` on a fixed test conversation.

### 6.3 Contradictions inside instruction sets

Hashes catch copies that differ. They cannot catch two *different* instruction
sets that disagree with each other, or one set that contradicts itself. Two
layers handle that:

- **Rules in code.**
  - Every instruction set declares its key `claims` (short statements, §4.2).
  - Declared `variables` must all be used, and no undeclared `{{...}}` may
    appear. This would have caught the DSH `{{cwd}}` break before an upgrade.
  - The size must fit the consumer's limit, for example DSH
    `agent-instructions.maxBytes`.
  - Rendering for every consumer must succeed. The rendered text is
    syntax-checked for that consumer: Jinja parses, Open WebUI variables are
    known, and Markdown blocks are closed.
- **A reviewer agent**, run on a schedule and on every instruction-set change.
  It gets all instruction sets that can reach the same model, with their claims.
  Its output is limited to a list of suspected conflicts, each with quotes from
  both sides. A suspected conflict becomes a finding marked "possible
  contradiction — a person decides". It is never auto-fixed.

### 6.4 How findings read

Every finding is a card in plain language. It has five parts: what is
different, why it matters, where each value lives, what we suggest, and how to
undo the suggestion (the §30 reversibility badge). Four examples, built from the
real traps above:

> **TabbyAPI loaded Flash-Next with a smaller context than everyone expects.**
> Open WebUI, Qwen Code and DeepSeek Harness are set up for 262,144 tokens.
> TabbyAPI reports 65,536 (`GET /v1/model`). The smaller value comes from
> `models/Flash-Next-Sec-exl3-6.0bpw/tabby_config.yml`, line 3
> (`max_seq_len: 65536`). Long chats will fail or be cut short.
> *Suggested fix:* set it to 262,144 in that file and reload the model, about
> 2 minutes without the model. *You can undo this instantly.*

> **Two instruction sets for the same model disagree.** Open WebUI's
> "flash-sec" system prompt (edited 12 Oct, outside Seraphina) says "never run
> exploit code". DeepSeek Harness's `~/.dsh/AGENTS.md` block from "sec-analyst"
> v7 says "proof-of-concept exploits may run inside the lab VM". *Choose one:*
> keep v7 everywhere, or make the Open WebUI wording v8 and roll it out to
> DeepSeek Harness and Qwen Code.

> **Replies in Open WebUI stop after about 150 tokens.** TabbyAPI's sampler
> preset `my_preset` fills in `max_tokens: 150` when a client sends none. Open
> WebUI sends none for "flash-sec", while Qwen Code sends 16,384. *Suggested
> fix:* set `max_tokens` in the "sec-analyst-chat" profile and remove the
> override from the preset. *You can undo this instantly.*

> **After the Open WebUI upgrade, the "cyber_redact" filter no longer loads.**
> It imports `langchain_community`, which the image stopped shipping in 0.11.4.
> Chats through "flash-sec" now skip the filter. *Suggested fix:* add
> `langchain-community` to the filter's requirements line, or switch it to the
> built-in readers. *Undo:* restore the previous filter version (Open WebUI
> keeps function history since 0.12.0).

### 6.5 Every finding is a tracked commitment

Each finding opens an item in the **Housekeeping Tracker** (DESIGN.md §31.6).
The item has one sub-item per affected binding, plus the contract tests that
must pass. The item closes only when every sub-item is reconciled or
explicitly dismissed by a person, and its tests are green. This is the §31.5
mechanism made concrete. "Updated Tabby, forgot Qwen Code" cannot close,
because the Qwen Code sub-item stays open. The tracker lives in the harness's
own storage, not in an agent's context, so it survives compaction and the end
of the session.

---

## 7. Reconcile through the normal reviewed path

A reconcile is a normal change: a plan, reviewers, the gate, the executor, then
verification (DESIGN.md §27). The registry adds three rules.

1. **Decide which side wins, and record it.** The default is "registry wins".
   The adapters render the registry's values into the drifted bindings. The
   other option is "adopt": the consumer's value becomes a registry commit,
   which is reviewed like code, and is then rendered to every *other* binding.
   A person chooses for instruction sets and for anything marked "possible
   contradiction".
2. **A change to a fact is one bundle that covers every binding.** Changing
   `flash-next-sec.load.max_seq_len` produces one bundle with these steps, in
   dependency order:
   1. the Tabby file edit and model reload (the server first);
   2. the DSH row, the Qwen provider entry, the Open WebUI model params and the
      factory `config.json` (the clients next);
   3. the contract tests named by every adapter's `checks()`.

   The gate refuses a bundle that changes a fact without touching all of its
   managed bindings, unless a person marks a binding "skip, with reason". The
   skip becomes its own tracker item.
3. **Backups first, then apply, then prove.**
   - Before applying: the before-Blueprint capture, the files touched, and
     Open WebUI's `sqlite3 .backup` (DESIGN.md §30 "undo by restore" needs a
     restore-tested backup).
   - After applying: the contract tests.
   - On failure: automatic rollback of that bundle. Per §31.2, the broken state
     is captured first so it can be diagnosed while the service is back up.

Agents hold no root and never edit consumer files directly. The reviewer set for
reconcile bundles adds a **consistency reviewer**. It reads the registry diff,
every rendered binding, and the invariants. It answers one question: after this
bundle, does every consumer agree, and do they still agree with what the
servers report?

---

## 8. End-to-end contract tests

### 8.1 Purpose and shape

A contract test is a small, named promise that a chain of services still keeps.
It is written as one sentence a novice can read. The tests run before an
upgrade (as a baseline), after it, after every reconcile, and nightly. Each test
is a JSON record:

```json
{
  "id": "owui-chat-to-tabby",
  "says": "A chat through Open WebUI to Flash-Next returns text.",
  "consumers": ["openwebui", "tabby-main"],
  "level": 2,
  "steps": [{"http": "POST {openwebui}/api/chat/completions",
             "body": {"model": "flash-sec", "stream": false, "temperature": 0, "max_tokens": 16,
                      "messages": [{"role": "user", "content": "Reply with the single word PONG."}]}}],
  "pass": {"status": 200, "json_path": "choices[0].message.content", "contains": "PONG",
           "within_seconds": 60},
  "cost": {"tokens": 64}, "side_effects": "creates no chat (API call)"
}
```

Levels, cheapest first:

- **L0** reachable and authenticated;
- **L1** facts (declared equals observed);
- **L2** behaviour (a real request through the chain returns the right kind of
  answer);
- **L3** semantic canaries (the instruction set and tools actually take
  effect).

A run stops at the first failing level for that chain, and reports in plain
language which link broke.

### 8.2 The starting set

| Id | Says | How it checks |
| -- | ---- | ------------- |
| `tabby-up` (L0) | TabbyAPI answers and rejects callers without a key. | `GET /health` gives 200; `GET /v1/models` with no key gives 401 |
| `tabby-loaded` (L1) | Tabby is serving the model the registry names, with the declared context, template and cache. | `GET /v1/model`: `id`, `parameters.max_seq_len`, `cache_size`, `cache_mode`, and the sha256 of `prompt_template_content` |
| `tabby-no-substitution` (L1) | Asking for a model Tabby is not serving fails instead of silently using another. | `inline_model_loading` is true, or a request for an unknown name is refused |
| `tabby-preset` (L1) | The sampler preset in force is the one the registry names, and fills only the declared values. | `GET /v1/sampling/overrides` plus the preset file's contents |
| `llama-ctx` (L1) | Each llama.cpp model gives every slot at least the profile's context. | `GET /props?model=` `n_ctx`; `GET /slots` per-slot `n_ctx`; `total_slots` |
| `llama-template` (L1) | The chat template renders exactly as at baseline. | `POST /apply-template` on a fixed conversation, then compare sha256 |
| `owui-up` (L0) | Open WebUI is up, its database is healthy, and the adapter's key works. | `/health`, `/health/db`, `/api/version`, an authorized `GET /api/v1/models/export` |
| `owui-connections` (L1) | Open WebUI's connections match the registry, and still do after a restart. | `configs/export` keys `openai.*` match the registry (URL order, prefixes, model ids); run again after a container restart (catches issue #24743) |
| `owui-models-listed` (L1) | Every model the registry routes through Open WebUI shows up there. | `GET /api/models` contains `<prefix>.<served_name>` and every managed workspace model |
| `owui-chat-to-tabby` (L2) | A chat through Open WebUI to Flash-Next returns text. | The JSON example above |
| `owui-system-prompt` (L1 and L3) | The model's system prompt is the canonical text, and it takes effect. | L1: compare the hash of `params.system`. L3: a canary question whose answer depends on the set, graded with tolerance |
| `owui-functions-load` (L2) | Every managed filter and pipe loads and runs. | Each function is listed, active, and its valves are readable; a chat through a model with the filter attached shows the filter's marker |
| `owui-tool-call` (L3) | A model with a tool attached calls it. | A chat that requires the tool; check for a tool call in the reply. Catches the 0.10.0 switch to native tool calling |
| `dsh-config` (L1) | DeepSeek Harness's composed config matches the registry and its own schema. | `--dump-config` validates against `--dump-config-schema`; the provider `baseURL`, `contextWindow` and `maxTokens` match |
| `dsh-run` (L2) | DeepSeek Harness can finish a one-line task on the local model. | `dsh --profile headless --json "Reply PONG"` with the same provider row as `web`, then check the final message |
| `qwen-config` and `qwen-run` (L1, L2) | Qwen Code's provider entry matches the registry and it answers. | Settings layers show the declared `contextWindowSize` and `max_tokens`; `qwen -p "Reply PONG" -m <id> -o json` returns text |
| `factory-doctor` (L0 and L1) | The Software Factory reaches each provider with a working key. | The existing doctor checks (DESIGN.md §20) |
| `ctx-agree` (L1) | Every client's idea of each model's context fits what the server loaded. | Invariant `ctx-fits` across all bindings |
| `instructions-agree` (L1) | Every copy of every instruction set matches its canonical version. | Managed-block hashes |
| `speed-baseline` (L2) | Flash-Next still generates near its baseline speed. | A 512-token generation; tokens per second within 20% of the Blueprint baseline (about 100 tok/s) |

### 8.3 Making tests with models in them reliable

- Use temperature 0, a fixed seed where the server supports one, short prompts,
  and small `max_tokens`. Test for the presence of a marker, never for exact
  wording.
- For L3 canaries, grade loosely: the answer must contain one of the expected
  phrases. Retry once. Two failures in a row fail the test.
- A test that flaps three nights in a week is marked "flaky", and that opens a
  tracker item. It is not silently skipped.
- The GPU budget is small. A full L0-L2 run costs a few hundred tokens and
  waits for the single Tabby slot (DESIGN.md §20 semaphore). It never runs
  during a Model Factory quantisation window (§29 resource broker).

### 8.4 The upgrade flow these tests enable

For every consumer upgrade, whether Seraphina started it or Cosmos did:

1. **Baseline.** Run L0-L3 on the current version. Record the results with the
   Blueprint capture.
2. **Read ahead.** An agent summarises the release notes between the two
   versions. Code matches them against the registry's dependency lists (the
   upgrade exposure in §6.2).
3. **Rehearse on a copy (Open WebUI).** Take a `sqlite3 .backup` of `webui.db`
   into a scratch `DATA_DIR`. Start the *new* image on a spare loopback port
   against that copy. Run the L0-L2 tests there. Migrations cannot be undone,
   so a failed rehearsal stops the upgrade before the real data is touched. For
   Tabby and llama.cpp the rehearsal is a second checkout or binary on another
   port. For DSH it is `--from-default-profile` into a throwaway profile name.
   Never use the real one: since 0.2.1-alpha.2 that command deletes its target.
4. **Upgrade for real, then run the same tests.** If anything that passed at
   baseline now fails, keep the broken state for diagnosis, restore, and open a
   tracker item (§31.2).
5. **Pin.** Record the new version in the registry. Only an upgrade that passed
   the tests may move the pin. Image tags are pinned, as Open WebUI's own docs
   recommend for production.

---

## 9. How this fits the rest of Seraphina

- **Blueprint (§28):** the registry is one layer of it. Adapter snapshots are
  part of each capture, so "what changed since Tuesday" includes "Open WebUI's
  `flash-sec` prompt changed".
- **Server Maintenance Factory (§27):** adapters are inspectors (read-only, no
  root). `render()` output becomes change bundles. Contract tests are the
  bundles' health checks.
- **Reversibility (§30):** most reconcile actions are "undo instantly" (file
  edits, API upserts with the previous value kept). Open WebUI upgrades are
  "undo by restore". They need a restore-tested database backup, and the
  rehearsal of §8.4.
- **Housekeeping Tracker (§31.5, §31.6):** every finding and every skipped
  binding is a commitment with sub-items, closed only by passing tests.
- **Grid Card:** the GPU & AI module card shows one consistency line, for
  example "All 5 AI apps agree" or "2 disagreements, 1 broken chain". Clicking
  it opens the finding cards.

**Smallest useful first slice (read-only).** Build these first:

- the adapters' `probe`, `read` and `extract` for Tabby, llama.cpp, Open WebUI
  (API), DSH (`--dump-config`), Qwen Code (files) and the factory;
- the adopt wizard;
- the drift report;
- contract tests L0-L2.

It changes nothing on the server, so it is safe to run on matrix on day one.
It also answers the owner's question "why is it buggy?" with a list of specific
disagreements. Writes and reconcile bundles come after the executor exists.

---

## 10. Open questions

1. **Which versions run on matrix today?** We need the Open WebUI image tag, the
   Tabby commit and exllamav3 version, the llama.cpp build, the DSH version and
   profile, and the Qwen Code version. Adapters need a version map that covers
   them. Open WebUI's config storage shape in particular depends on the version.
2. **Where do the "instruction sets in Tabby" live?** TabbyAPI has no
   system-prompt store. Are they Jinja templates, `template_vars`, presets in a
   frontend such as SillyTavern, or the browser-only `/ui`?
3. **Is Open WebUI on SQLite or Postgres?** Is it a Cosmos ServApp, and does
   Cosmos auto-update its image? If so, how does Seraphina hear about it before
   or after?
4. **Can the adapter get an Open WebUI admin API key?** That needs
   `ENABLE_API_KEYS=true` and the permission. Without one the adapter is limited
   to reading a database backup copy.
5. **Which copy of each instruction set does the owner consider canonical
   today?** This seeds the adopt wizard.
6. **How should a stdlib-only harness read YAML?** Tabby's and DSH's files are
   YAML. Qwen Code's are JSON and `.env`, which are fine. There are three
   options:
   - a small vendored YAML subset reader;
   - an optional dependency;
   - relying on the servers' JSON views only (Tabby `load_config`, DSH
     `--dump-config`, whose output format is **UNVERIFIED** as machine-parseable).
7. **Is DSH's internal `/api/<namespace>/<method>` RPC acceptable to call**, or
   only the CLI and files? The CLI path is safer but slower.
8. **How much automation is acceptable for instruction-set contradictions?** The
   design only flags them for a person. Is that the right line?
9. **Does Open WebUI's `/api/v1/functions/sync` really delete absent
   functions?** The code suggests it does. Verify on a rehearsal copy before
   ever using it.
10. **Speed baselines:** what prompt length and output length make the tokens
    per second comparable across Tabby and llama.cpp runs, and at what context
    fill?

## Sources (primary)

- Open WebUI source at `91ea4e3`: [config.py](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/config.py),
  [models/config.py](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/models/config.py),
  [models/models.py](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/models/models.py),
  [routers/configs.py](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/routers/configs.py),
  [routers/openai.py](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/routers/openai.py),
  [routers/functions.py](https://github.com/open-webui/open-webui/blob/main/backend/open_webui/routers/functions.py),
  [CHANGELOG.md](https://github.com/open-webui/open-webui/blob/main/CHANGELOG.md),
  [releases](https://github.com/open-webui/open-webui/releases),
  [issue #24743](https://github.com/open-webui/open-webui/issues/24743).
- Open WebUI docs source at `cd7fbd8`: [env-configuration](https://github.com/open-webui/docs/blob/main/docs/reference/env-configuration.mdx),
  [api-endpoints](https://github.com/open-webui/docs/blob/main/docs/reference/api-endpoints.md),
  [chat-params](https://github.com/open-webui/docs/blob/main/docs/features/chat-conversations/chat-features/chat-params.md),
  [migration to 0.5.0](https://github.com/open-webui/docs/blob/main/docs/features/extensibility/plugin/migration/to-0.5.0.mdx),
  [migration to 0.9.0](https://github.com/open-webui/docs/blob/main/docs/features/extensibility/plugin/migration/to-0.9.0.mdx),
  [backups](https://github.com/open-webui/docs/blob/main/docs/tutorials/maintenance/backups.md),
  [updating](https://github.com/open-webui/docs/blob/main/docs/getting-started/updating.mdx),
  [manual-database-migration](https://github.com/open-webui/docs/blob/main/docs/troubleshooting/manual-database-migration.md).
- TabbyAPI at `a270c55`: [config_sample.yml](https://github.com/theroyallab/tabbyAPI/blob/main/config_sample.yml),
  [endpoints/core/router.py](https://github.com/theroyallab/tabbyAPI/blob/main/endpoints/core/router.py),
  [common/model.py](https://github.com/theroyallab/tabbyAPI/blob/main/common/model.py) and
  [its history](https://github.com/theroyallab/tabbyAPI/commits/main/common/model.py),
  [sampler overrides doc](https://github.com/theroyallab/tabbyAPI/blob/main/docs/08.-Sampler-Overrides.md).
- llama.cpp at `8baabd5`: [tools/server/README.md](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md),
  [issue #22364](https://github.com/ggml-org/llama.cpp/issues/22364).
- DeepSeek Harness at `687f282`: [README](https://github.com/deepseek-ai/deepseek-harness/blob/master/README.md),
  [providers guide](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/guide/providers.md),
  [config catalog](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/config-catalog.md),
  [cli-help](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/cli-help.md),
  [upgrade guides](https://github.com/deepseek-ai/deepseek-harness/tree/master/docs/upgrade-guide),
  [persistence changes](https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/persistence-changes/releases/README.md).
- Qwen Code at `97567d5`: [settings](https://github.com/QwenLM/qwen-code/blob/main/docs/users/configuration/settings.md),
  [model-providers](https://github.com/QwenLM/qwen-code/blob/main/docs/users/configuration/model-providers.md),
  [issue #3203](https://github.com/QwenLM/qwen-code/issues/3203).
- Secondary (search extracts, treat with care): [eesel.ai on dsh](https://www.eesel.ai/blog/deepseek-harness),
  [aireiter on dsh](https://aireiter.com/blog/deepseek-harness),
  [releaseport Open WebUI tracker](https://releaseport.com/tools/open-webui/releases),
  [dev.to router-mode guide](https://dev.to/rosgluk/llama-server-router-mode-dynamic-model-switching-without-restarts-1h0j),
  [llama.cpp deprecation-warning mirror](https://cdn04132025.gitlink.org.cn/replica/llama.cpp/src/commit/f4d2b8846a6b34419ff9e9491aee6cd95e444bfc/examples/deprecation-warning),
  [inventivehq on Qwen OAuth](https://inventivehq.com/blog/qwen-code-still-free-2026-shutdown).
