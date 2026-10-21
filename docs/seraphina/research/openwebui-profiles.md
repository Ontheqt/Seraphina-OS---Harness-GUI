# Research: using an Open WebUI workspace model ("Kate") from the harness

Research notes for **Seraphina OS Harness**. The owner keeps specialist
profiles as Open WebUI **workspace models**. A workspace model is a saved
preset: a system prompt, sampler settings, a base model (served by TabbyAPI or a
llama.cpp-family server), and optionally attached tools, filters, actions,
skills and knowledge. One of them is a specialist called Kate. The owner wants
Kate to work in the harness GUI. Two designs are on the table:

- **(A) Through Open WebUI.** The harness treats Open WebUI as an
  OpenAI-compatible provider (`base_url` `http://127.0.0.1:8083/api`, model id =
  the workspace model id), and Open WebUI applies the preset itself.
- **(B) Imported.** The harness copies the preset's system prompt and settings
  (from `GET /api/v1/models/export` or the UI's Export JSON) into its own agent
  file and runs it on its own model routing. Open WebUI-only parts are listed as
  unavailable, and a fingerprint detects drift.

This page answers seven questions from Open WebUI's source code, then gives a
recommendation. It is a companion to [ai-stack-drift.md](ai-stack-drift.md),
which covers the wider "one source of truth" problem (DESIGN.md §31 point 5).

Written 2026-10-21.

## How these notes were made, and their limits

- I read the source and docs directly, from shallow clones made on 2026-10-21:

  | Project | Repository | Commit (date) | Version |
  | ------- | ---------- | ------------- | ------- |
  | Open WebUI | [open-webui/open-webui](https://github.com/open-webui/open-webui) | `91ea4e3421d1d8069f13d4a463fcb5986d94ea60` (2026-10-16), the `v0.12.3` tag | 0.12.3 (`package.json`) |
  | Open WebUI docs | [open-webui/docs](https://github.com/open-webui/docs) | `cd7fbd8f574b74624f5a315e94ec9c00761861f2` (2026-10-21) | n/a |

- **Citations.** A path such as `main.py:1148 chat_completion` means the file
  `backend/open_webui/main.py` at the commit above, line 1148, function
  `chat_completion`. Paths starting with `src/` are the Svelte frontend. Permalink
  form: `https://github.com/open-webui/open-webui/blob/91ea4e3421d1d8069f13d4a463fcb5986d94ea60/backend/open_webui/<path>#L<line>`.
- Every behaviour below was read from code, not observed on a running server.
  Where the docs say the same thing, I say so. Where code and docs differ, the
  code wins and I point out the difference.
- **This is upstream 0.12.3, not the owner's install.** The owner's version is
  unknown. Older versions behave differently in places that matter here (for
  example, the tool-calling mode called "Default" before 0.10.0 is now called
  "Legacy", see §2). The harness must check `GET /api/version` and run the
  contract tests in §11 before trusting any of this.
- I did not read anything under `private/`, and I do not know what Kate's preset
  actually contains. The recommendation covers every case: Kate with or without
  tools, knowledge and filters.

## Summary

| Question | Short answer (Open WebUI 0.12.3, API caller, no `chat_id`) |
| -------- | ---------------------------------------------------------- |
| 1. Does it apply the preset's system prompt and sampler settings? | **Yes, on every request, read live from the database.** Template variables are filled in on the server. The preset's prompt goes **first**. A system message from the caller is kept and **appended after it**, so neither replaces the other. The caller cannot switch the preset's prompt off. For sampler settings, **the caller's values win**: the preset only fills in what the request left out. The one exception is the preset's **Stream Chat Response** setting, which overrides the request's `stream`. |
| 2. Does it run the preset's attached tools? | **No, not by itself.** `meta.toolIds` is a browser default. The backend never reads it for chat requests. A caller that sends `tool_ids` gets different results by mode. In **native** mode (the default), the tool calls come back **unexecuted**. In **legacy** mode (the old "Default", prompt-based), one round of tools runs before the answer, and the results go into the prompt as context. A full server-side tool loop needs `chat_id` + `id` + `stream: true`, and then the answer goes into a saved chat, not into the HTTP response. |
| 3. Knowledge, filters, other features? | **Filters: yes.** Global filters and the preset's always-on filters run. Toggleable filters run only if the caller names them in `filter_ids`. `outlet()` runs, but its changes are not returned to the caller. **Attached skills: yes**, injected in full. **Knowledge: not in native mode.** It is applied only in legacy mode, or when the caller passes the knowledge ids in `files` itself. Many features work only in the web UI (§3.4). |
| 4. The caller's own OpenAI `tools`? | **Passed through unchanged**, and the `tool_calls` come back to the caller as normal OpenAI deltas. Sending `tools` switches off **all** server-side tool handling for that request, so the two kinds **cannot be mixed**. |
| 5. Authentication? | An API key (`sk-…`) needs `ENABLE_API_KEYS`. A non-admin also needs the `features.api_keys` permission. An optional global endpoint allowlist applies to every key. Model, tool and knowledge access control applies to the key's user. Exporting a model needs write access to it (plus the `workspace.models_export` permission for non-admins). Session JWTs also work, but they expire. |
| 6. Data shapes? | `/api/v1/models/export` and the UI's Export file have **the same shape**: a JSON array of full model records. They hold **no credentials**, but they do hold the full system prompt, user and group ids, and knowledge names. `GET /api/models` lists workspace models by id, with `preset: true`, but **without** their prompt or sampler settings. |
| 7. Limits? | No rate limits on chat. No body-size limit. No timeout to the upstream server unless one is configured. No context compaction on this path. The harness's own timeouts and context guard stay in charge. |

**Recommendation: both, with A as the way Kate runs.** Design A keeps Open
WebUI as the single source of truth for Kate's prompt, settings, skills and
filters, with nothing copied. It still lets Kate use the harness's own tools. B
is added in a reduced form: an **importer that reads Kate without running her**,
used to show what reaches the harness and to detect drift. A full B runtime is
kept only as an explicit offline mode. Details are in §9 and §10.

---

## 1. What Open WebUI does with `POST /api/chat/completions` for a workspace model

### 1.1 The request path

1. `main.py:1146` (`/api/chat/completions`, alias `/api/v1/chat/completions`)
   `chat_completion` checks access and merges settings (§1.4). It builds
   `metadata` from the request (`chat_id`, `session_id`, `tool_ids`,
   `filter_ids`, `files`, `features`, `variables`, `chat_variables`, …).
2. `utils/middleware.py:2021 process_chat_payload` runs the steps between
   request and model: inlet filters, features, skills, tool resolution,
   knowledge retrieval, and the `request` filters.
3. `utils/chat.py:148 generate_chat_completion` routes the request to the
   provider router. For a TabbyAPI or llama.cpp connection that is
   `routers/openai.py:1409 generate_chat_completion`. That function swaps the
   workspace id for `base_model_id`, **applies the preset's stored settings and
   system prompt** (lines 1441-1458), and posts to `<connection>/chat/completions`.
4. `utils/middleware.py:6466 process_chat_response` sends the response back.

**Which fields the caller sends decides how it behaves.** There are three
cases, as `main.py:1818` and `utils/middleware.py:4136` / `:3909` branch on
them:

| Caller sends | What happens | What the HTTP caller gets |
| ------------ | ------------ | ------------------------- |
| No `chat_id` (a plain OpenAI client, which is what the harness is) | Synchronous. The upstream answer is passed through (`utils/middleware.py:6384`, "Fallback to the original response"). | A normal OpenAI stream or JSON |
| `chat_id` + `id`, no `session_id` | The server consumes the stream itself, runs its native tool loop, and writes the result to the chat record and the socket | `null`, once the loop has finished |
| `chat_id` + `id` + `session_id` (what the browser sends) | Runs as a background task (`main.py:1818-1909`). Built-in tools are offered too. | `{"status": true, "task_ids": [...], "chat_id": ...}` straight away. The answer arrives over socket.io. |

The docs say the same in
[reference/server-side-tool-calling.md](https://github.com/open-webui/docs/blob/main/docs/reference/server-side-tool-calling.md)
("The one rule that decides your whole integration").

### 1.2 System prompt

- **Where it is applied.** In `routers/openai.py:1441-1458`, the router loads
  the preset with `Models.get_model_by_id(model_id)` **on every request**. It
  pops `params.system` and calls `utils/payload.py:69 apply_system_prompt_to_body`.
  Edits made in Open WebUI therefore take effect on the next request, with no
  cache to refresh. (Ollama base models take the same step in
  `utils/chat.py:283-290`.)
- **How it combines with the caller's system message.** The router calls
  `apply_system_prompt_to_body` with `replace=False`, which calls
  `utils/misc.py:701 add_or_update_system_message`. If the first message is a
  system message, the preset's prompt is put in front of it
  (`f'{preset}\n{caller}'`). Otherwise a new system message is inserted.
  `utils/misc.py:650 merge_system_messages` (via
  `utils/middleware.py:210 normalize_messages_for_model`) later folds every
  system message into one at position 0. **Result: one system message, the
  preset's prompt first, then the caller's.** The docs describe this as "the
  model's system prompt takes precedence"
  ([chat-params.md](https://github.com/open-webui/docs/blob/main/docs/features/chat-conversations/chat-features/chat-params.md)).
  In code, precedence means the caller's text is kept but placed second, and
  the preset's prompt cannot be removed.
- **The caller cannot switch it off.** `bypass_system_prompt` is read only from
  `request.state`, "to prevent external clients from setting them"
  (`routers/openai.py:1422-1429`). Only internal callers set it, for example
  the server's own tool-loop follow-ups (`utils/middleware.py:6041`).
- **`params.system` in the request does not override the preset on this
  path.** `main.py` merges the caller's `params` over the preset's
  (`main.py:1229`). But `utils/payload.py:105 apply_params_to_form_data` drops
  `system` from the outgoing body (it is one of the Open WebUI-only keys,
  `utils/payload.py:156 remove_open_webui_params`). The router then injects the
  preset's **stored** prompt, not the merged one. The request's `params.system`
  is used only to rebuild the prompt inside the server's own tool loop
  (`metadata['base_system_prompt']`, `utils/middleware.py:2943-2957`). The
  harness should send its additions as a system message, never as
  `params.system`.
- **The caller's own system message also goes through variable substitution**
  (`utils/middleware.py:2185-2191`, `replace=True`). So a literal
  `{{CURRENT_DATE}}` or `{{user.variables.x}}` in the harness's prompt would be
  replaced too.

### 1.3 Template variables

`utils/payload.py:37 resolve_system_prompt` fills them in, in this order:

1. **Chat variables** `{{chat.variables.<key> | <definition>}}`
   (`utils/chat_variables.py:267 render_chat_variables`). Values come from the
   request's `chat_variables` (or the saved chat). A missing value falls back to
   the placeholder's `default=`, otherwise it becomes empty. On this path
   `required` is not enforced.
2. **User variables** `{{user.variables.<key>}}` from the key owner's profile
   (`render_user_variables`).
3. **The request's `variables` object**, as literal replacements
   (`utils/task.py:31 prompt_variables_template`). The browser sends
   `{{USER_NAME}}`, `{{USER_EMAIL}}`, `{{USER_LOCATION}}`, `{{CURRENT_DATETIME}}`,
   `{{CURRENT_DATE}}`, `{{CURRENT_TIME}}`, `{{CURRENT_WEEKDAY}}`,
   `{{CURRENT_TIMEZONE}}` and `{{USER_LANGUAGE}}` here, computed **in the
   browser** (`src/lib/utils/index.ts:1236 getPromptVariables`, sent from
   `src/lib/components/chat/Chat.svelte:4344-4350`).
4. **Server-side fallback** (`utils/task.py:37 prompt_template`, commented
   "Legacy (API Usage)"):
   - `{{CURRENT_DATE}}` (YYYY-MM-DD), `{{CURRENT_TIME}}` (hh:mm:ss AM/PM),
     `{{CURRENT_DATETIME}}` and `{{CURRENT_WEEKDAY}}`, all from the **server's
     clock and timezone**;
   - `{{USER_NAME}}`, `{{USER_EMAIL}}`, `{{USER_BIO}}`, `{{USER_GENDER}}`,
     `{{USER_BIRTH_DATE}}`, `{{USER_AGE}}`, `{{USER_LOCATION}}` and
     `{{USER_GROUPS}}`, all from **the user the API key belongs to**.

Consequences for an API caller:

- `{{USER_NAME}}` and the date work without any extra input. The date uses the
  container's timezone (often UTC), not the owner's.
- `{{CURRENT_TIMEZONE}}` and `{{USER_LANGUAGE}}` have **no server-side value**
  (no Python code mentions them). They reach the model as literal text unless
  the caller sends them in `variables`.
- Because step 3 runs before step 4, the harness can send `variables` to get
  exactly what the browser would produce (local date, timezone, language).

### 1.4 Sampler settings

Precedence, from strongest to weakest:

1. **Top-level fields in the request** (`temperature`, `max_tokens`, `top_p`,
   `stop`, …). `apply_params_to_form_data` (`utils/payload.py:105`) and
   `apply_model_params_to_body` (`utils/payload.py:89`) only set a key `if …
   key not in form_data`.
2. **The request's `params` object**, merged over the preset
   (`main.py:1219-1229`, `utils/misc.py:34 merge_model_params`; `custom_params`
   are merged key by key).
3. **The preset's `params`**, including `custom_params`. String values are
   parsed as JSON, then deep-merged, then **every key is sent as a top-level
   field** (`utils/payload.py:186 apply_model_params_to_body_openai`). That is
   how TabbyAPI or llama.cpp extras such as `top_k`, `min_p` or
   `repetition_penalty` reach the server. `stop` entries are unescaped with
   `unicode_escape`, so `\n` becomes a newline.
   **Correction (review of the harness slice, DESIGN.md §33):** on the chat
   path that does not hold. The preset's params are merged into
   `form_data['params']` first (`main.py:1209-1229`) and become top-level
   fields in `middleware.py:2063 apply_params_to_form_data`, which has no
   `stop` mapping; the router's `apply_model_params_to_body_openai`
   (`routers/openai.py:1453`) then only fills keys still missing, so its
   `unicode_escape` never runs and a stored `\n` reaches the model as a
   backslash and an `n`.
4. **Global "Model Defaults"** (`models.default_params`, set with
   `DEFAULT_MODEL_PARAMS`; `main.py:1208-1212`).

Exceptions and extras:

- **Stream Chat Response wins over the request.** If the preset sets
  `stream_response`, it overwrites the request's `stream` (`main.py:1245-1246`).
- **Model controls.** Each control's default option is applied as
  `custom_params`, fill-only (`utils/payload.py:20 apply_model_controls`, called
  at `main.py:1652-1662`). (Correction: it is deep-merged into the
  preset's `custom_params`, so for the same key the control's option wins over
  the preset's custom param; only the resulting top-level fields are
  fill-only against the request.) A caller may pick other options with
  `params.model_controls`. Non-admins need the `chat.controls` and
  `chat.params` permissions (`main.py:1220-1225`).
- Open WebUI-only keys never reach the upstream server: `stream_response`,
  `stream_delta_chunk_size`, `function_calling`, `reasoning_tags`,
  `compact_token_threshold`, `system`, `note_id`, `tool_approval_mode` and
  `model_controls` (`utils/payload.py:156`).
- The docs say the same ("The parameters in your request are kept",
  [api-endpoints.md](https://github.com/open-webui/docs/blob/main/docs/reference/api-endpoints.md)).

**For the harness:** `OpenAIProvider._body` always sends `max_tokens`
(`swfactory/harness/providers.py:368-380`), so the harness's value beats
Kate's. To use Kate's value instead, the model entry can set
`extra_body: {"max_tokens": null}`, which `_merge_extra` treats as "remove this
key" (`providers.py:730-737`).

---

## 2. Attached tools (`meta.toolIds`)

**The backend ignores the preset's tools for chat requests.**

- The only place that copies `meta.toolIds` into a request is the browser:
  `src/lib/components/chat/Chat.svelte:1146-1176` preselects them, and
  `:4320` sends them as `tool_ids`.
- The backend reads only the request's `tool_ids`
  (`utils/middleware.py:2407`, `tool_ids = form_data.pop('tool_ids', None)`).
- Scheduled automations prove the point: they must add the preset's
  `toolIds`, `defaultFilterIds`, `terminalId` and `defaultFeatureIds` themselves
  (`utils/automations.py:167 _resolve_model_defaults`).

A grep of the backend for `toolIds` finds only `routers/models.py` (save-time
validation) and `utils/automations.py`.

**What happens when a caller sends `tool_ids`**, by function-calling mode. The
mode is the request's `params.function_calling`, else the preset's, else
`'native'` (`main.py:1357-1361`). Only the exact value `'legacy'` selects
legacy, so a preset still holding the pre-0.10 value `'default'` now runs
native (CHANGELOG 0.10.0: "Native tool calling is now the default … the old
behavior has been renamed 'Legacy'"). The cases:

| Mode / fields | Who runs the tools | What the caller sees |
| ------------- | ------------------ | -------------------- |
| **native** (default), no `chat_id` | **Nobody.** The tools are offered to the model as OpenAI `tools` (`utils/middleware.py:2912-2920`), but the tool loop lives only in the branch that has an event emitter (`utils/middleware.py:4136`), and that needs `chat_id` + `id`. Non-streamed, only "finish the turn" (`:3951`). | Raw `tool_calls` (stream deltas or `message.tool_calls`) for tools the caller cannot run, because they are Python inside Open WebUI |
| **legacy** (old "Default", prompt-based), any request | **Open WebUI, one round, before the answer.** `utils/middleware.py:1005 chat_completion_tools_handler` asks the task model for a JSON tool choice, runs the tools, and adds each result as a citation source. The sources are put into the prompt with the RAG template (`:2962-2963`, `:644 apply_source_context_to_messages`). | A normal completion. When streamed, the **first SSE frame is `data: {"sources": [...]}`** (`:6404-6415`) before the OpenAI deltas. Non-streamed: a top-level `"sources"` key (`:3377 merge_events_into_response`). No tool calls are shown. |
| **native** + `chat_id` + `id` + `stream: true` (+ `session_id`) | **Open WebUI, full multi-round loop** (up to `CHAT_RESPONSE_MAX_TOOL_CALL_ITERATIONS`, default 256, `env.py:1091`) | Nothing useful over HTTP. The answer is written to the chat (read it with `GET /api/v1/chats/{id}`) and sent as socket.io `events` (`socket/main.py:1401 get_event_emitter`). |

- **`features`** (`web_search`, `image_generation`, `code_interpreter`,
  `memory`) is needed only for those built-in features. In native mode they
  become built-in tools, and only when the request has a `session_id`
  (`utils/middleware.py:2076-2080`, `:2812-2814`). In legacy mode, web search
  and image generation run as forced pre-steps. Memory adds context in both
  modes, if enabled and permitted (`:2340-2353`).
- **Streaming on the pass-through path** is the upstream server's own SSE,
  relayed line by line (`utils/session_pool.py:119 stream_wrapper`,
  `utils/misc.py:1291 stream_chunks_handler`), with `stream` filters applied to
  each JSON frame (`utils/middleware.py:6417-6440`). The only non-OpenAI frames
  are:
  - `{"sources": [...]}` first, when retrieval or legacy tools produced sources;
  - `{"selected_model_id": ...}` for arena models only (`utils/chat.py:246-249`);
  - `{"error": {"message": "Chat completion stream failed"}}` followed by
    `[DONE]` if the relay breaks (`:6453-6457`).

  The harness's parser already skips frames without `choices` and raises on
  `error` (`providers.py:395-414`, `:769`).

---

## 3. Knowledge, filters, skills, and what only works in the web UI

### 3.1 Knowledge (`meta.knowledge`)

- **Native mode (the default), API caller: not applied at all.** The RAG
  injection of the preset's knowledge runs only
  `if model_knowledge and … function_calling == 'legacy'`
  (`utils/middleware.py:2263`). The native alternative (an
  `<attached_knowledge>` list plus knowledge tools) is added only when
  `use_builtin_tools` is true, and that requires a `session_id`
  (`:2076-2080`, `:2812-2834`). The comment there reads: "Only inject when the
  request originates from the UI (identified by session_id). API callers don't
  expect hidden tools."
- **Legacy mode:** the knowledge items are added to `files`, then
  `utils/middleware.py:1704 chat_completion_files_handler` asks the task model
  for search queries (`generate_queries`, skipped when
  `ENABLE_RETRIEVAL_QUERY_GENERATION` is off). It then retrieves chunks
  (`retrieval/utils.py:1364 get_sources_from_items`, with per-user access checks
  on each collection or file) and puts them into the **last user message**
  through the RAG template (into the system message if `RAG_SYSTEM_CONTEXT=true`,
  `env.py:372`).
- **Any mode:** a caller can send `files: [{"type": "collection", "id": "<kb id>"}, …]`
  itself. The files handler runs whether or not tools are present
  (`:2934-2941`), as long as the preset's `capabilities.file_context` is not off.
  The item shapes are the same as `meta.knowledge` entries (legacy entries
  that carry `collection_name(s)` are converted at `:2276-2299`).
- **Cost.** Retrieval runs on **every** request. In an agent loop that means
  every model call, so it adds a task-model call plus an embedding search each
  time. On a one-slot TabbyAPI that also serves as the task model, those calls
  queue behind each other.

### 3.2 Filters (`meta.filterIds`, `meta.defaultFilterIds`)

- `utils/filter.py:59 get_model_filter_ids` takes the global filters plus the
  preset's `filterIds`, keeping only active ones.
- `utils/filter.py:68 resolve_filter_pipeline` keeps a **toggleable** filter
  only if the request names it in `filter_ids`. `defaultFilterIds` is applied
  only by the browser (`Chat.svelte:1195-1199`).
- On the pass-through path:
  - **`inlet`** runs (`utils/middleware.py:2307-2325`). A filter that fails
    fails the whole request with 400: it is loud, not silent.
  - **`request`** runs too (`:3000-3010`).
  - **`stream`** runs on every SSE frame (`:6417-6440`).
  - **`outlet`** runs after the reply (gated by `ENABLE_API_OUTLET_FILTERS`,
    default true, `env.py:1060`), but **what it changes is not returned**:
    `utils/middleware.py:3729 outlet_filter_handler` saves its result only to a
    saved chat or the socket. The docs say the same ("outlet() does not rewrite
    the HTTP response body", api-endpoints.md). `POST /api/chat/completed`
    (`main.py:2133`, deprecated) still returns outlet output for a payload
    you send it.
- Pipelines-server filters run too (`process_pipeline_inlet_filter`, `:2305`).

### 3.3 Skills (`meta.skillIds`) and model controls

- **Skills: applied for API callers.** `utils/middleware.py:2464` adds the
  preset's `skillIds` to the active set. Without built-in tools (no
  `session_id`), each skill's content goes **in full** into the system message
  as `<skill id=… name=…>` (`:2512-2548`).
- Model controls: see §1.4.

### 3.4 What only works in the web UI

| Feature | Why it needs the browser or a chat (source) |
| ------- | ------------------------------------------- |
| Server-side native tool loop | Needs `chat_id` + `id` + stream; the result goes to the chat, not HTTP (§2) |
| Built-in tools (knowledge browsing, web search, memory, notes, code interpreter, image generation, sub-agents, …) | Need `session_id` (`utils/middleware.py:2076-2080`) |
| Code interpreter on pyodide, direct (user) tool servers, user-terminal shell tools, ask-user and elicitation prompts, tool-approval confirmations | Need a live socket.io session (`socket/main.py:1577 get_event_call`, which refuses sessions the user does not own) |
| Actions (`meta.actionIds`), the buttons under a reply | `POST /api/chat/actions/{id}` needs `chat_id` and `session_id` (`main.py:2153`, `utils/actions.py`) |
| Preset defaults for tools, toggleable filters, features, terminal and skills menu | Chosen by `Chat.svelte:1146-1225`, not by the server |
| Account-level system prompt and parameters ("Settings > General") | The browser puts them in the request (`Chat.svelte:4210-4215`, `:4306-4312`); the server does not add them for an API key |
| `{{CURRENT_TIMEZONE}}`, `{{USER_LANGUAGE}}`, browser-local date and time, user location | Computed in the browser (§1.3) |
| Title, tag and follow-up generation | `background_tasks` from the browser on a saved chat |
| Context compaction | Only for saved chats with a user message id (`utils/middleware.py:2140-2160`) |
| Outlet-modified text | Saved to the chat and the socket only (§3.2) |
| Folder ("project") prompt and knowledge | Need a `folder_id` or a chat in a folder (`:2228-2261`) |

One fidelity note: what the owner sees in the browser is Kate **plus the
owner's account-level settings** (system prompt and advanced parameters from
`GET /api/v1/users/user/settings`, `ui.system` and `ui.params`). An API key does
not get those. If the owner relies on them, the harness should send them too
(or the owner should move them into Kate).

---

## 4. The caller's own OpenAI tools

- **Passed through unchanged.** `payload_tools` is read before anything else
  (`utils/middleware.py:2062`). The comment at `:2641-2645` says: "When the
  caller provides an explicit `tools` key in the request body, skip all
  server-side tool resolution and pass the caller's tools through unchanged."
  With no `chat_id`, the upstream SSE is relayed as it is, so the base model's
  `tool_calls` deltas reach the caller in normal OpenAI form. The caller runs the
  tools and sends back `role: "tool"` messages, as with any OpenAI server.
- **Messages the caller sends are cleaned lightly:**
  - `process_messages_with_output` (`:1930`) drops the keys `id`, `output`,
    `model`, `usage`, `meta` and `files`, and keeps `tool_calls` and
    `reasoning_content`;
  - `sanitize_tool_pairs` (`:2000`) drops tool calls that have no result, and
    results that have no call;
  - `merge_system_messages` (§1.2) merges the system messages.

  Well-formed harness history goes through unchanged.
- **The two kinds cannot be mixed in one request.** The guard at `:2645`
  covers **all** server-side tool handling: `tool_ids` (Python tools, MCP and
  OpenAPI servers), terminal tools, built-in tools, direct tool servers, and the
  legacy pre-round. Filters, skills, knowledge `files` and the system prompt
  still apply. (A `tools` key set as a **custom parameter on the preset** is
  different: it is merged after the snapshot, so it does not trigger this
  guard. That is an edge case.)
- **Ways around this, outside Open WebUI:**
  - MCP servers that Open WebUI uses (`server:mcp:<id>`) can be connected to the
    harness directly, since it has its own MCP client (DESIGN.md §12 `mcp.py`).
  - Python workspace tools cannot run anywhere but inside an Open WebUI chat
    completion: `routers/tools.py` has no endpoint that executes a tool.

---

## 5. Authentication for API callers

- **API keys.**
  - Format `sk-<32 hex>` (`utils/auth.py:374 create_api_key`), **one per
    user**. `POST /api/v1/auths/api_key` replaces the old key
    (`routers/auths.py:1497`). Keys do not expire.
  - The key can be sent as `Authorization: Bearer`, as the `token` cookie, or
    in `x-api-key` (configurable with `CUSTOM_API_KEY_HEADER`;
    `utils/asgi_middleware.py:126 _set_token`, `env.py:818`).
  - `utils/auth.py:506 get_current_user_by_api_key` enforces three things:
    - `ENABLE_API_KEYS` (stored setting `auth.enable_api_keys`, default
      `False`, `config.py:2530`);
    - for non-admins, the permission `features.api_keys` (default `False`,
      `USER_PERMISSIONS_FEATURES_API_KEYS`, `config.py:1956`);
    - if `ENABLE_API_KEYS_ENDPOINT_RESTRICTIONS` is on (alias
      `ENABLE_API_KEY_ENDPOINT_RESTRICTIONS`), the allowlist
      `API_KEYS_ALLOWED_ENDPOINTS` (alias `API_KEY_ALLOWED_ENDPOINTS`). This is a
      comma-separated list matched against the raw path, exactly or as a prefix
      followed by `/` (`config.py:2532-2544`). The list is **global**: it
      applies to every key on the instance.
  - With persistent config on (the default), environment values only seed
    these settings. After first start the admin-panel value wins (see
    ai-stack-drift.md §2.1).
- **Endpoints each design needs** (paths for an allowlist):

  | Need | Endpoint | Allowlist entry |
  | ---- | -------- | --------------- |
  | A: list models, Doctor and Test | `GET /api/models` | `/api/models` |
  | A: chat | `POST /api/chat/completions` | `/api/chat/completions` |
  | B: read the preset | `GET /api/v1/models/export?ids=kate`, or `GET /api/v1/models/model?id=kate` | `/api/v1/models` (prefix covers both, plus `/model/history`) |
  | B: global defaults (admin only) | `GET /api/v1/configs/models` (`DEFAULT_MODEL_PARAMS`) | `/api/v1/configs/models` |
  | Both: version | `GET /api/version` | `/api/version` |

- **JWTs.** Session tokens from sign-in work on the same endpoints
  (`utils/auth.py:389 get_current_user`). They expire (`JWT_EXPIRES_IN`,
  default `4w`) and are revoked on password change or logout (`session_stamp`,
  `utils/auth.py:272 is_valid_token`), and MFA may be required. **Not suitable
  for an unattended harness.** Use an API key.
- **Access control applies to the key's user:**
  - `GET /api/models` is filtered per user (`utils/models.py:557 get_filtered_models`).
  - Chat checks read access to the preset **and every base model in its chain**
    (`utils/models.py:493 check_model_access`;
    `utils/access_control/__init__.py:302 has_base_model_access`, where a base
    model with no database row is admin-only). The check is skipped for admins
    when `BYPASS_ADMIN_ACCESS_CONTROL` is true (the default, `config.py:2131`)
    and for everyone when `BYPASS_MODEL_ACCESS_CONTROL` is set.
  - Tools, knowledge collections and skills are each checked for that user.
- **Reading the preset is stricter than using it:**
  - `GET /api/models` never includes `params`: only `model_controls` labels
    survive (`utils/models.py:35 public_model_params`).
  - `GET /api/v1/models/model?id=` returns `params` only to users with write
    access (`routers/models.py:799-840`).
  - `GET /api/v1/models/export` needs admin, or the `workspace.models_export`
    permission (default `False`, `config.py:1785`), and write access to every
    requested id. Otherwise it returns 403 (`routers/models.py:443-470`).
  - So design A works with a **read-only** key that can never read Kate's
    prompt. Design B needs the owner's own (or a write-granted) key.

---

## 6. Data shapes

### 6.1 `GET /api/v1/models/export` and the UI's Export JSON

The UI's per-model **Export** calls the same endpoint with `ids=[model.id]` and
saves the result as a one-element array, `<id>-<ms>.json`. "Export all" saves
`models-export-<ms>.json` (`src/lib/components/workspace/Models.svelte:342-369`,
`src/lib/apis/models/index.ts:3 exportModels`). **Both files have the same shape
as the endpoint's response:** a JSON array of `ModelExportResponse`
(`routers/models.py:439`). That is `ModelModel` (`models/models.py:199`) plus
`background_image_data`:

```json
[{
  "version_id": "…",                 // changes on every edit of name/base_model_id/params/meta
  "id": "kate",                      // the workspace model id (used as "model" in chat requests)
  "user_id": "…",                    // owner's user id
  "base_model_id": "…",              // the upstream model id as Open WebUI lists it (with any connection prefix)
  "name": "Kate",
  "params": {                        // ModelParams, extra keys allowed (models/models.py:97)
    "system": "…",                   // the system prompt, with {{…}} placeholders unrendered
    "temperature": 0.6, "top_p": 0.95, "top_k": 20, "max_tokens": 8192, "stop": ["…"],
    "custom_params": {"repetition_penalty": "1.05"},   // values may be JSON strings
    "function_calling": "native", "stream_response": true, "reasoning_tags": […],
    "model_controls": {…}
  },
  "meta": {                          // ModelMeta, extra keys allowed (models/models.py:143)
    "profile_image_url": "data:image/…;base64,…",
    "background_image_url": null,    // always null in an export; image travels in background_image_data
    "description": "…", "i18n": {…}, "tags": [{"name": "…"}],
    "capabilities": {"vision": …, "file_context": …, "citations": …, "usage": …, "builtin_tools": …, "terminal": …},
    "knowledge": [{"id": "…", "name": "…", "type": "collection|file|note", "description": "…", "context": "full"?, "legacy": true?, "collection_name(s)": …}],
    "toolIds": ["…", "server:mcp:<id>"], "skillIds": […], "filterIds": […], "defaultFilterIds": […],
    "actionIds": […], "defaultFeatureIds": ["web_search", …], "builtinTools": {…}, "terminalId": "…",
    "suggestion_prompts": […], "voice": {…}, "voice_avatar": {…}, "tts": {…}, "hidden": false
  },
  "access_grants": [{"id": "…", "resource_type": "model", "resource_id": "kate",
                     "principal_type": "user|group", "principal_id": "…", "permission": "read|write",
                     "created_at": 0}],
  "is_active": true, "updated_at": 1760000000, "created_at": 1750000000,
  "background_image_data": null      // or a data: URI
}]
```

The example values are illustrative.

- Only keys that are set appear in `params`, because the editor deletes
  empty and `null` values before saving
  (`src/lib/components/workspace/Models/ModelEditor.svelte:506-510`). The
  `meta` keys likewise appear only when used (`:405-487`).
- Knowledge entries are reduced to references when saved (`:365-384`), and
  the server strips any extracted text (`models/models.py:36`).
- **Secrets.** A model record holds **no credentials**:
  - connection keys live in the `openai.*` config;
  - tool and filter valves live on the tool and function records
    (`/api/v1/tools/id/{id}/valves`), not in the model.

  The file is still **private material**: the full system prompt (the owner's
  instruction set), the owner and group ids in `user_id` and `access_grants`,
  knowledge and tool names, and any text the owner typed into `custom_params`.
  Store it outside the repo, under the harness's user config, never in
  `examples/` or logs.

### 6.2 `GET /api/models`

`main.py:933 get_models` (alias `GET /api/v1/models`) returns `{"data": [...]}`,
the OpenAI list shape the harness already reads. It holds:

- base models from every connection;
- arena models and pipes;
- **workspace models with their own ids**, built in
  `utils/models.py:241-293`:

```json
{"id": "kate", "name": "Kate", "object": "model", "created": 1750000000,
 "owned_by": "openai", "connection_type": "…", "preset": true,
 "info": {"version_id": "…", "id": "kate", "user_id": "…", "base_model_id": "…", "name": "Kate",
          "params": {"model_controls": {…}}?,         // system prompt and samplers removed
          "meta": {… as above, minus profile_image_url …, "chat_variables_schema": {…}?},
          "access_grants": […], "is_active": true, "updated_at": …, "created_at": …},
 "actions": [{"id", "name", "description", "icon"}], "filters": [ toggleable filters only ],
 "tags": [{"name": "…"}]}
```

- Only models the user can read are listed. Filter pipelines are hidden.
- A workspace model is still listed when its base model is missing (it falls
  back to `owned_by: "openai"`), but chatting with it then fails with 400
  "Model not found". The exception is `ENABLE_CUSTOM_MODEL_FALLBACK=true`, which
  **silently answers with the first default model** (`main.py:1172-1186`, `:1231-1237`).
- `info.version_id` and `info.meta` are present. So `/api/models` alone is
  enough to notice that Kate changed, and to read her tool, skill, filter and
  knowledge ids. It is not enough to read her prompt or settings.
- `version_id` is replaced whenever the snapshot of `name`, `base_model_id`,
  `params` and `meta` changes (`models/models.py:312-315`,
  `models/model_history.py:12 model_snapshot`, which ignores `hidden` and
  `chat_variables_schema`). The history is at
  `GET /api/v1/models/model/history?id=` (writers only).

---

## 7. Limits relevant to long agent turns

- **No rate limit on chat.** `RateLimiter` is used only for sign-in, MFA and
  OAuth token exchange (`routers/auths.py:95-99`, `routers/mfa.py:31`).
- **No request body size limit** in Open WebUI. `MAX_BODY_LOG_SIZE` (2048) only
  trims audit logs (`main.py:840-847`).
- **Timeouts to the upstream server:**
  - `AIOHTTP_CLIENT_TIMEOUT` is **unset by default, meaning no total timeout**
    (`env.py:614-618`, `utils/session_pool.py:50 get_client_timeout`). If the
    owner's compose file sets it (older releases documented 300), a long
    generation is cut at that many seconds.
  - `AIOHTTP_CLIENT_STREAM_IDLE_TIMEOUT` optionally caps the gap between chunks
    (unset by default).
  - uvicorn runs with no request timeout (`backend/start.sh:107-112`).
  - A reverse proxy in front (Cosmos) may have its own timeout. Point the
    harness at `127.0.0.1:8083` directly.
- **Stream line cap.** If `CHAT_STREAM_RESPONSE_CHUNK_MAX_BUFFER_SIZE` is set,
  any longer SSE line is **dropped silently** (`utils/misc.py:1291`). Leave it
  unset.
- **No compaction or iteration cap on the pass-through path.** Context
  compaction runs only for saved chats (`utils/middleware.py:2140`).
  `CHAT_RESPONSE_MAX_TOOL_CALL_ITERATIONS` (256) caps only Open WebUI's own
  loop. **The harness's context guard (DESIGN.md §20) and its own iteration
  limits stay in charge.**
- **Per-call overhead.** Every harness model call is a separate Open WebUI
  request. Each one costs:
  - a few database reads (preset, access checks, filters, skills);
  - the inlet, request and stream filters;
  - with knowledge `files`, a query-generation call plus retrieval (§3.1).
- **Usage.** If the preset has the `usage` capability, Open WebUI forces
  `stream_options.include_usage` (`main.py:1248-1250`). The harness asks for it
  anyway.
- **Concurrency.**
  - Open WebUI does not queue chat requests itself. TabbyAPI's one slot is
    shared between browser users, Open WebUI's own task-model calls (title, tags,
    retrieval queries) and the harness.
  - The harness's `max_concurrency` semaphore is per provider. An `openwebui`
    provider and the existing `tabby` provider would not share a slot count even
    though they reach the same GPU.

---

## 8. The two designs, element by element

| Preset element | (A) through Open WebUI, harness sends its own `tools` | (B) imported into a harness agent |
| -------------- | ------------------------------------------------------ | --------------------------------- |
| System prompt | Applied live, Kate first, harness system text appended | Copied. Drifts until re-imported. |
| Template variables | Rendered by Open WebUI (key owner's name, server date). Harness can send `variables` for browser-local values. | Harness must re-implement the variable set (§1.3) |
| Sampler settings, `custom_params`, model controls, global defaults | Applied live, fill-only. Drop the harness's `max_tokens` to use Kate's. | Copied. Importer must reproduce the merge order (§1.4), the `custom_params` parsing, `stop` unescaping and control defaults, and fetch `DEFAULT_MODEL_PARAMS` (admin). |
| Stream Chat Response | Overrides the harness's `stream`. Harness must match it or sniff the response (§10). | Copied as `stream` |
| Base model | Open WebUI's connection to TabbyAPI | Must be mapped to a harness provider and model (strip Open WebUI's connection `prefix_id`). Can silently differ. |
| Skills (`skillIds`) | Applied live | Copy via `/api/v1/skills/export`, or list as unavailable |
| Always-on filters (inlet, stream, request) | Run. Outlet runs, its output is not returned. | **Unavailable** (Python inside Open WebUI) |
| Toggleable default filters | Run if harness sends `filter_ids` (read live from `info.meta.defaultFilterIds`) | Unavailable |
| Knowledge | Not by default. Works if harness sends `files` built live from `info.meta.knowledge` (or `params.function_calling: "legacy"`). Cost per call (§3.1). | Unavailable (the harness has no retrieval) |
| Kate's Python tools | **Unavailable while harness tools are in use** (§4) | Unavailable |
| Kate's MCP servers (`server:mcp:<id>`) | Not through Open WebUI. Harness connects to the same MCP server directly. | Same: harness connects directly |
| Actions, built-in tools, code interpreter, terminal | Unavailable (web UI only, §3.4) | Unavailable |
| Harness coding tools (Read, Bash, …) | Yes, passed through | Yes |
| Works when Open WebUI is down | No | Yes |
| Drift | None for everything Open WebUI applies. Behaviour can still change on an Open WebUI upgrade (contract tests). | By construction. Needs a fingerprint and a re-import flow. |
| Key needed | Read access to Kate is enough | Write access (owner key), plus admin for global defaults |

---

## 9. Recommendation

**Use both, with different jobs. Design A runs Kate. A reduced design B reads
Kate, to show what reaches the harness and to detect drift, and it can run her
only as an explicit offline mode.**

Why A runs Kate:

1. **It is the only way to have zero copies.** DESIGN.md §31 point 5 asks for
   one source of truth with drift detection. Under A, Open WebUI is the source
   for Kate's prompt, settings, model controls, skills and always-on filters, and
   the harness never holds a second copy. The router reads the preset from the
   database on every request (§1.2), so an edit in Open WebUI reaches the
   harness on the next model call.
2. **It is the more faithful of the two.** A gets everything B gets, and also:
   - server-side variable rendering with the owner's identity;
   - Open WebUI's own merge of global defaults, `custom_params` and model controls;
   - skills;
   - the inlet, stream and request filters;
   - knowledge, once the harness passes `files`.

   B has to re-implement each of those and can only drift.
3. **The harness's tools still work.** Pass-through (§4) gives Kate the
   harness's Read, Edit and Bash tools, plus MCP, in the harness's own loop,
   with streaming and the normal Cancel.

What neither design can give is **Kate's Open WebUI Python tools inside a
harness tool loop.** That is structural (§4), not a setting. If the owner
needs Kate's own tools, there are two options:

- **Option C, the native loop.** The harness drives an Open WebUI chat:
  1. create the chat;
  2. `POST /api/chat/completions` with `chat_id` + `id` + `stream: true`;
  3. poll `GET /api/v1/chats/{id}`, which overlays the partial reply while it
     runs (`routers/chats.py:72 overlay_response_streams`);
  4. delete the chat afterwards.

  That uses Kate's tools, but not the harness's. The chat is created in the
  owner's Open WebUI account, and text arrives only by polling.
- **Port the tool** to an MCP server that both Open WebUI and the harness use.

Both are follow-ups, not part of the first version.

Why B still has a job:

- **Display.** Show the owner what Kate is and which parts do not reach the
  harness.
- **Drift and audit.** Record which version of Kate answered each turn, and
  notice when Kate or Open WebUI changed.
- **Offline mode.** Run a snapshot of Kate on the harness's own routing when
  Open WebUI is down. This must be an **explicit, labelled** switch ("Kate,
  imported snapshot of <date>, version <id>"), never an automatic fallback.
  DESIGN.md §20 says: "No silent fallback". The snapshot must be refused, or at
  least warned about, once its fingerprint no longer matches the live preset.

---

## 10. What the harness must add

Everything here is new work in the harness. Nothing in Open WebUI needs to
change.

1. **Chats without a repository checkout.**
   - Today a chat needs a repo and an agent that has tools
     (`swfactory/chats.py:304 new_chat`; `_has_callable_tools` at `:238` refuses
     agents without tools).
   - Add a mode with no repo: the `cwd` is a scratch folder under
     `<data_dir>/chats/<id>/work`, there is no branch or worktree, and Hand off
     is hidden.
   - Allow an agent with no tools, for a plain conversation with Kate.
2. **A provider preset `openwebui`.**
   - Settings: `{"type": "openai", "base_url": "http://127.0.0.1:8083/api",
     "api_key_env": "OPENWEBUI_API_KEY", "auth": "bearer", "max_concurrency": 1,
     "timeout": 1800}`. The key is the owner's `sk-…` key, stored in the user env
     file like the other keys.
   - Doctor checks:
     - `GET /api/models` with the key returns Kate, and without the key returns 401;
     - `GET /api/version` is recorded;
     - a warning when `ENABLE_CUSTOM_MODEL_FALLBACK` could silently swap the
       model. It cannot be read with a non-admin key, so compare the `model`
       field of a test reply with Kate's base model instead.
   - Decide how this provider shares TabbyAPI's single slot with the `tabby`
     provider: for example a shared slot group, or document that both queue at
     Tabby.
3. **A model entry per workspace model**, for example:
   - `models.kate`: `{"provider": "openwebui", "model": "kate", "stream": <Kate's
     stream_response, default true>, "max_tokens": <Kate's, for the context
     budget>, "context_tokens": <Tabby's>, "extra_body": {"max_tokens": null}}`;
   - the "Add model from template" list (DESIGN.md §22) gains "Open WebUI
     workspace model", which fills the model list from `GET /api/models`
     entries with `preset: true`.
4. **A thin agent file for Kate chats** (`agents/kate.md` or a user-dir
   override):
   - The body describes only the harness's environment and tools. Kate's
     persona stays in Open WebUI, and the harness's text is appended **after**
     it (§1.2), so a coding-agent body like `coder.md` would compete with Kate's
     instructions.
   - Its `tools:` line is the owner's choice (none, read-only, or all).
5. **A live profile binding, read at chat start** (and checked once per turn),
   from `GET /api/models` → `info`:
   - record `info.version_id` in the chat and in each turn's log, so every
     answer says which Kate gave it;
   - build `files` from `info.meta.knowledge` and `filter_ids` from
     `info.meta.defaultFilterIds` when the owner turns them on (the provider's
     `extra_body` can carry these fields today);
   - send `variables` for `{{CURRENT_TIMEZONE}}`, `{{USER_LANGUAGE}}` and the
     owner's local date and time, so Kate sees what the browser would show;
   - **never send `tool_ids`, `chat_id`, `id` or `session_id`** on this path;
   - show `toolIds`, `actionIds`, `terminalId`, `builtinTools` and
     `defaultFeatureIds` in the chat header as "not available in the harness".
     Offer to bind a `server:mcp:<id>` to a harness MCP server with the same URL.
     That URL is in Open WebUI's admin tool-server config; copy the URL, and keep
     the secret as an env reference.
6. **Handle Open WebUI responses robustly** in `OpenAIProvider`:
   - when `stream` was requested but the response is `application/json` (the
     preset's Stream Chat Response is off), parse it as a whole response instead
     of failing with "response stream ended before the model finished" and
     retrying six times (`providers.py:300-336`, `:395-414`);
   - optionally surface the first `{"sources": [...]}` frame as citations.
7. **An importer** (the B side):
   - input: `GET /api/v1/models/export?ids=<id>` with the owner key, or an
     uploaded Export JSON file (same shape, §6.1);
   - output: a read-only "profile card" in the GUI, plus, only when the owner
     asks for the offline mode, a snapshot agent file and model entry;
   - for the snapshot, compute the effective parameters exactly as §1.4
     describes:
     - `DEFAULT_MODEL_PARAMS` from `GET /api/v1/configs/models`, when the key is
       an admin's;
     - drop the Open WebUI-only keys;
     - parse `custom_params` values and merge them;
     - unescape `stop`;
     - apply the model-control defaults;
   - map `base_model_id` to a harness model through Open WebUI's connection
     prefix, and ask when it is ambiguous;
   - translate the supported placeholders, and flag any other `{{…}}`;
   - record the Open WebUI version, `version_id`, `updated_at` and the
     fingerprint below.
8. **A drift check**, run at chat start, by Doctor and on a schedule:
   - **Cheap signal:** `info.version_id` from `GET /api/models` (read access is
     enough). It changes exactly when Open WebUI's own snapshot of the preset
     changes (§6.2).
   - **Behaviour fingerprint:** sha256 of canonical JSON (sorted keys, no
     spaces) over:
     - `base_model_id` and `params`;
     - `meta.toolIds`, `skillIds`, `filterIds`, `defaultFilterIds`,
       `actionIds` and `defaultFeatureIds`;
     - the `(type, id)` of each `meta.knowledge` entry;
     - `meta.capabilities`, `builtinTools` and `terminalId`;
     - `DEFAULT_MODEL_PARAMS`;
     - the `version_id` of each attached tool, function and skill (these
       records carry `version_id` since 0.12).

     This tells a cosmetic edit (description, image) apart from a change in
     behaviour. `version_id` alone does not cover tool code, filter code,
     knowledge contents or global defaults.
   - **Findings** go into the commitments tracker (DESIGN.md §31 point 5), with
     the diff from `/api/v1/models/model/history`.
   - **An Open WebUI version change** triggers the contract tests in §11.

---

## 11. Risks, and the contract tests to write first

Risks:

- **Open WebUI becomes a dependency for the Kate chat.** Upgrades have broken
  the owner's setup before (DESIGN.md §31 point 5), and the behaviour described
  here moved between 0.10 and 0.12:
  - 0.10.0 renamed the "Default" mode "Legacy", made native the default, and
    started running outlet inline;
  - 0.11.1 stopped the model's saved settings from overwriting values sent in
    an API request;
  - 0.12.0 made an API `max_tokens` override the model's value for Ollama too
    (all from CHANGELOG.md).

  The harness must pin the expected behaviour with contract tests, not assume it.
- **Mixed instructions.** The harness's system text is appended to Kate's.
  A heavy agent body produces contradictions, the exact failure the owner
  described. Keep the Kate agent body thin, and run the contradiction check from
  ai-stack-drift.md §6.3 over the combined text.
- **Silent model swap.** `ENABLE_CUSTOM_MODEL_FALLBACK=true` makes Open WebUI
  answer with a default model when Kate's base model is gone (§6.2). Doctor
  should flag it.
- **Wrong defaults leak through:**
  - the harness's `max_tokens` beats Kate's unless removed (§1.4);
  - Kate's `stream_response` can break a streaming client (§10 item 6);
  - a preset holding the pre-0.10 value `function_calling: "default"` now runs
    native (§2).
- **Knowledge cost.** Retrieval runs on every model call (§3.1). On a one-slot
  TabbyAPI that is also the task model, it slows long agent turns. Mitigations:
  - set a separate task model, or turn off retrieval query generation;
  - or send `files` only when Kate's knowledge matters.
- **Key handling.**
  - The owner's key (often an admin's) lives in the harness env file.
  - There is one key per user: regenerating it in Open WebUI breaks the harness
    until the env file is updated.
  - The endpoint allowlist is global, so turning it on can break the owner's
    other integrations.
  - A dedicated Open WebUI user with only read access to Kate (and to her base
    model, tools and knowledge) limits the damage for design A. The catch:
    `{{USER_NAME}}` and the user variables then resolve to that user, not the
    owner.
- **Privacy.** The export carries Kate's full instruction set and the owner's
  ids (§6.1). Keep imports in the user config dir, out of the repo and logs.
- **B's own drift.** Even with a fingerprint, a snapshot re-implements Open
  WebUI's merging and rendering rules. Those can change on an upgrade without
  Kate changing. Treat the snapshot as an emergency tool.

Contract tests (to run on every Open WebUI version change, in the style of
ai-stack-drift.md §8):

1. `GET /api/models` with the key lists `kate` with `preset: true`. Without the
   key it returns 401.
2. A test preset whose system prompt holds a marker and `{{USER_NAME}}`, used
   with a plain chat request:
   - the reply shows the marker and the owner's name;
   - a harness system message is still honoured;
   - the upstream request has one system message, the preset's text first.

   (A small echo model behind a test connection makes this deterministic.)
3. A request with a harness `tools` array returns unexecuted `tool_calls` in
   standard OpenAI deltas, ending with `[DONE]`.
4. A request with `files` holding one of Kate's knowledge collections begins
   with a `sources` frame.
5. With `stream: true`, the reply is SSE, unless the preset forces otherwise.
6. `GET /api/v1/models/export?ids=kate` with the owner key returns one record
   with `params.system`. With a read-only key it returns 403.

## 12. Open questions

- What Kate actually contains: her tools, filters, knowledge, skills, model
  controls, and whether `stream_response` and `function_calling` are set. The
  importer's profile card answers this on the owner's machine.
- Whether the owner relies on account-level settings (`ui.system`,
  `ui.params`) that the browser adds to every Kate chat (§3.4).
- Which Open WebUI version runs on "matrix", and whether `ENABLE_API_KEYS`,
  endpoint restrictions, `ENABLE_CUSTOM_MODEL_FALLBACK`, `AIOHTTP_CLIENT_TIMEOUT`
  and `CHAT_STREAM_RESPONSE_CHUNK_MAX_BUFFER_SIZE` are set.
- Whether Kate's Open WebUI tools are essential. If they are, Option C (§9)
  or porting them to MCP becomes the next step.
- Whether the shared-facts registry (ai-stack-drift.md §4) should become Kate's
  source of truth later, with Open WebUI and the harness both as consumers. If
  so, design A stays the same: the harness still reads Kate from Open WebUI. The
  registry's adapter would push into Open WebUI with `POST /api/v1/models/import`,
  and the drift check above becomes the registry's check.

## Sources (primary)

Open WebUI at `91ea4e3421d1d8069f13d4a463fcb5986d94ea60` (v0.12.3), paths under
`backend/open_webui/` unless noted:

- `main.py`: `get_models` (933), `chat_completion` (1146-1918), `chat_completed`
  (2133), `chat_action` (2153)
- `utils/middleware.py`:
  - `process_chat_payload` (2021-3014)
  - `chat_completion_tools_handler` (1005)
  - `chat_completion_files_handler` (1704)
  - `apply_source_context_to_messages` (644)
  - `process_messages_with_output` (1930), `sanitize_tool_pairs` (2000)
  - `outlet_filter_handler` (3729)
  - `non_streaming_chat_response_handler` (3892)
  - `streaming_chat_response_handler` (4100; API fallback at 6384)
  - `process_chat_response` (6466)
- `utils/payload.py`: `apply_model_controls` (20), `resolve_system_prompt` (37),
  `apply_system_prompt_to_body` (69), `apply_model_params_to_body` (89),
  `apply_params_to_form_data` (105), `remove_open_webui_params` (156),
  `apply_model_params_to_body_openai` (186)
- `utils/misc.py`: `merge_model_params` (34), `merge_system_messages` (650),
  `add_or_update_system_message` (701), `stream_chunks_handler` (1291)
- `utils/task.py`: `prompt_variables_template` (31), `prompt_template` (37)
- `utils/chat_variables.py`: `render_chat_variables` (267),
  `render_user_variables` (289)
- `utils/chat.py`: `generate_chat_completion` (148)
- `routers/openai.py`: `generate_chat_completion` (1409)
- `utils/models.py`: `public_model_params` (35), `get_all_models` (90),
  `check_model_access` (493), `get_filtered_models` (557)
- `utils/filter.py`: `get_model_filter_ids` (59), `resolve_filter_pipeline` (68)
- `utils/tools.py`: `get_tools` (345), `get_attached_knowledge` (562)
- `utils/automations.py`: `_resolve_model_defaults` (167)
- `utils/auth.py`: `create_api_key` (374), `get_current_user` (389),
  `get_current_user_by_api_key` (506)
- `utils/asgi_middleware.py`: `AppHTTPMiddleware._set_token` (126)
- `utils/access_control/__init__.py`: `has_base_model_access` (302)
- `utils/session_pool.py`: `get_client_timeout` (50), `stream_wrapper` (119)
- `routers/models.py`: `get_models` (227), `export_models` (443),
  `get_model_by_id` (799)
- `routers/auths.py`: `generate_api_key` (1497)
- `routers/chats.py`: `overlay_response_streams` (72)
- `routers/configs.py`: `get_models_config` (769)
- `models/models.py`: `ModelParams` (97), `ModelMeta` (143), `ModelModel`
  (199), version handling in `_write_model` (271-327)
- `models/model_history.py`: `model_snapshot` (12)
- `socket/main.py`: `get_event_emitter` (1401), `get_event_call` (1577)
- `retrieval/utils.py`: `get_sources_from_items` (1364)
- `config.py`:
  - permissions (1785, 1956);
  - `BYPASS_ADMIN_ACCESS_CONTROL` (2131);
  - API key settings (2530-2546);
  - stored keys (3255-3258)
- `env.py`:
  - timeouts (614-631);
  - `CUSTOM_API_KEY_HEADER` (818);
  - `BYPASS_MODEL_ACCESS_CONTROL` (838);
  - `ENABLE_CUSTOM_MODEL_FALLBACK` (1041);
  - `ENABLE_API_OUTLET_FILTERS` (1060);
  - tool-loop cap (1091);
  - stream buffer cap (1116)
- `backend/start.sh` (96-112)
- `CHANGELOG.md`: 0.10.0 "Native tool calling is now the default"; 0.11.1
  "Custom model parameters combining"; 0.12.0 "Output limit for Ollama through
  the API"
- Frontend:
  - `src/lib/components/chat/Chat.svelte`: preset defaults (1146-1225),
    request body (4206-4366);
  - `src/lib/utils/index.ts:1236 getPromptVariables`;
  - `src/lib/components/workspace/Models.svelte:342-369`;
  - `src/lib/components/workspace/Models/ModelEditor.svelte:365-510`;
  - `src/lib/apis/models/index.ts:3`.

Open WebUI docs at `cd7fbd8f574b74624f5a315e94ec9c00761861f2`:

- `docs/reference/server-side-tool-calling.md`
- `docs/reference/api-endpoints.md` (Chat Completions, Filter and Function
  Behavior with API Requests, Programmatic Model Management)
- `docs/features/chat-conversations/chat-features/chat-params.md`
- `docs/reference/env-configuration.mdx` (`AIOHTTP_CLIENT_TIMEOUT`)

Harness, read only:

- `DESIGN.md` §7, §12, §19-22 and §31
- `swfactory/chats.py`
- `swfactory/harness/providers.py`
- `docs/seraphina/research/ai-stack-drift.md`
