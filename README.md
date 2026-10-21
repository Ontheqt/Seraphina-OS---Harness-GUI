# Seraphina OS Harness

An agentic OS harness for the novice Ubuntu server user, complete with a
user-friendly GUI. AI agents do the expert work; every change has a way back.

> **Status: v1 preview, 21 October 2026. Work in progress.**
>
> **Working now:**
> - **Software Factory:** spec → build → four parallel reviews → approver → gate → merge, branch or folder.
> - **Per-step model routing.** Any mix of local and API models, with routing profiles, an escalation ladder, overflow, and cost per job.
> - **Housekeeping Tracker, first part.** It keeps promises, findings and acceptance criteria outside any agent's context until they are proven done.
> - **Chat and Plans**, including chats without a repository.
> - **Open WebUI personas.** Talk to an Open WebUI workspace model from the harness, with its prompt and settings applied live.
> - **Grid Card GUI with wizards.**
> - **A read-only `factory snapshot` server inventory.**
>
> **Being built:**
> - Wake & Messages: personas that wake themselves and message you over ntfy or Telegram, with no open ports.
> - The Seraphina-branded home screen and the GUI for the newest features. The GUI still says "Software Factory".
>
> **Designed for v2:** System Maintenance, Upgrades & Enhancements, Systems Research, Systems Audit, Backups and the Model Factory. See [docs/seraphina/](docs/seraphina/).
>
> The server-factory catalog and some research notes are held back until they are cleaned of owner-specific details.

# Software Factory

A self-hosted coding harness with a web GUI, for an Ubuntu server. You describe a
feature; a line of coding agents specs it, builds it on a branch, reviews it
through several independent lenses and merges it, or hands it to you when a
human should decide. It runs on any mix of local models (llama.cpp, TabbyAPI,
Strata, Ollama, vLLM, LM Studio) and API models (Anthropic Claude, DeepSeek,
NanoGPT, OpenRouter, OpenAI, DashScope).

```
feature ─▶ spec-writer ─▶ builder ─▶ [security · ux · ui · code] ─▶ WAIT FOR ALL
                            ▲                                          │
                            └──── any CHANGES: resume the builder ◀────┤ (max 2 resumes)
                                                                       ▼ all PASS
                     merge to base ◀── APPROVE ── approver ── ESCALATE ─▶ needs-human
                                                                            ▲
                                              GUI / CLI: approve · rework · reject
```

Above the line, an optional **lead** (a frontier model) splits big requests
into ordered jobs and reviews the combined result. Beside it, **Chat** gives you
one coding agent to work with interactively.

Python 3.10+ standard library only: no pip installs, no build step, no
containers required.

## Features

- **The factory line.** spec-writer → builder → parallel reviewers → approver →
  a deterministic merge gate. The reviewers run in parallel and the line waits
  for all of them; every lens must pass. The builder is resumed with the reviews
  (up to 2 times), and anything unclear goes to *needs-human*.
- **Chat.** Talk to a coding agent on a repo, in its own worktree and branch,
  then **hand off** the branch to the factory for review and merge; or chat
  without a repository, with any agent, in a scratch folder.
- **Your Open WebUI profiles.** Chat with an Open WebUI workspace model such as
  "Kate" through Open WebUI, which applies her prompt and settings live; read
  her profile card, and be told in the tracker when she changes there. See
  [Open WebUI profiles](#open-webui-profiles-kate).
- **Housekeeping Tracker.** What the agents promised, found or were asked to do
  is tracked outside their context until it is proven done: spec criteria,
  review findings, rework notes, product decisions and "I'll do that later".
  A must-do item that is not proven stops the merge, and whatever is still
  open when a job ends moves to the repo's follow-ups. See
  [Housekeeping Tracker](#housekeeping-tracker).
- **Your choice of output.** An approved job merges into your base branch, or
  stays on its branch for you, or is exported to a folder (patch, changed
  files and a report) without touching your repo. See [Output](#output-merge-branch-or-folder).
- **Plans.** A frontier lead model turns a large request into ordered,
  dependency-aware factory jobs, runs them, and reviews the combined change.
- **Routing per stage.** Every agent (and every run) can use a different
  model and runtime. Local models where the volume is, API models where
  independent judgment pays off. A model may have several builds (the same
  model on other servers) and an overflow for when they are down or busy; the
  builder can climb an escalation ladder when reviews send it back (a fresh
  session with a hand-over, never another model's session); routing profiles
  per job; prices, a cost per run and estimates before you start. See
  [Routing](#routing).
- **Local + API in any mix.** Any OpenAI- or Anthropic-compatible endpoint is a
  provider. Small local servers get a per-provider slot limit, so parallel
  agents queue instead of failing. No silent fallback to a hosted model.
- **GUI.** Live board with review chips, streamed agent transcripts, diffs,
  rendered job files, backlog, chats, plans, a Routing view, agent prompt
  editor, settings with model Test buttons, and a Doctor page. Vanilla JS, no
  CDN, works on a phone.
- **CLI.** Everything scriptable: `factory submit`, `status`, `logs -f`,
  `approve`, one-shot `factory run` without a server, and more.
- **Agents are files.** Claude Code subagent syntax (markdown + frontmatter);
  override or add agents without touching Python.
- **The line explains itself to its agents.** Every factory-line agent's
  system prompt carries the *Factory Grid Card*: the line its job runs on
  (its real reviewers, WAIT FOR ALL, the approver, the gate, needs-human and
  the CHANGES loop), with "you are here" on the agent's own step.
  `agent.start` and job.json record a receipt (`sha256`, `bytes`) of the
  card each run was sent; that proves delivery, not that the model used it.
  The process maps are also published as Mermaid source (`/api/docs/maps`).
- **Product decisions stay yours.** The spec marks what only a person should
  decide **(product)**; a job with such a line waits for you instead of
  merging until you confirm it. See [Product decisions](#product-decisions-product).
- **Skills and MCP.** `SKILL.md` skills (Claude Code / Codex / Qwen Code /
  mattpocock/skills format) and stdio MCP servers with a tool allowlist.
- **Other runtimes.** Run any agent through Qwen Code, Claude Code or Codex
  instead of the builtin harness.
- **Claude Code kit.** The same factory line as a `/factory` skill for Claude
  Code (`kits/claude-code`).

## Quick start on Ubuntu

Ubuntu 22.04 or 24.04, as the user who will run the factory (for example a
dedicated `ai-dev` user). No sudo needed.

```bash
git clone <this repo's URL> ~/software-factory
cd ~/software-factory
./install.sh                         # default preset: deepseek; see --help
```

`install.sh` checks python3 ≥ 3.10 and git, links `factory` (and `sf-items`,
see [Your own agents](#your-own-agents-sf-items)) into `~/.local/bin`, writes
`~/.config/software-factory/config.json` with a fresh login token, and creates `~/.config/software-factory/env` (mode 600) for API
keys. Pick another starting point with `--preset` (see
[Models & providers](#models--providers)).

1. **Keys.** Put them in the env file, one per line (or later in the GUI,
   see [API keys](#api-keys)):
   ```bash
   nano ~/.config/software-factory/env      # DEEPSEEK_API_KEY=sk-...
   factory doctor                           # checks keys, providers, git, agents
   ```
2. **Run it.** In a terminal (or tmux):
   ```bash
   factory serve
   ```
   Or as a systemd user service that survives logout:
   ```bash
   ./install.sh --systemd-user --start
   sudo loginctl enable-linger "$USER"      # once, by an admin
   ```
3. **Open the GUI.** The server listens on 127.0.0.1:8765 only. From your
   laptop, tunnel to it, then open the login link:
   ```bash
   ssh -N -L 8765:127.0.0.1:8765 ai-dev@your-server     # on the laptop
   factory url                                           # on the server; open the printed link
   ```
4. **Add a repo** (GUI, or `factory repo add ~/code/my-app`).
5. **Run Scout** on the repo. The repo-scout agent drafts an `AGENTS.md`
   (stack, setup, dev server, test/lint/build commands, conventions). Review
   it, commit it to the repo: every agent reads it.
6. **Describe a feature** with **New feature** → *Run now*, and watch the
   card move across the board. Or from the shell:
   ```bash
   factory submit my-app "Rate limit the signup form"
   factory logs my-app/001-rate-limit-signup-form -f
   ```

## Recommended setup for the owner's server

The owner's machine: 2× RTX 3090 (48 GB VRAM over NVLink) and an i9-12900KS.
Its main local model is **Qwen3.8-Flash-Next**, served by **TabbyAPI** (about
100 tokens/s) or **Strata** (about 70 tokens/s), both at a 262K context. Those
are the owner's measurements, not promises; both servers use both GPUs, so run
one at a time. The Tabby build is a cyber-security fine-tune, which is why it
takes the security lens.

Use the **`local-first`** preset: the routing the owner agreed (DESIGN §23).
Flash-Next on TabbyAPI (its EXL3 build first, then Strata's GGUF build, MiMo
V2.6 Pro through NanoGPT when both are down or busy too long) builds, chats,
scouts and reviews security; the builder escalates to GPT-6.1 Sol high, then
xhigh, when reviews send it back; GPT-6.1 Sol reviews code, Claude Sonnet 5.5
UX, Gemini 4 Argon UI; Claude Opus 5.5 high writes the spec, approves and
leads. It never makes TabbyAPI load another model while the owner (or Open
WebUI) has one loaded. Five routing profiles come with it (see
[Routing profiles](#routing-profiles)).

```bash
./install.sh --preset local-first --data-dir /srv/ai-work/factory --systemd-user --start
# ~/.config/software-factory/env:  TABBY_API_KEY=...  STRATA_API_KEY=...  NANOGPT_API_KEY=...
factory doctor   # which local server is up, what Tabby has loaded, unverified NanoGPT model ids
```

Set `models.flash-next.model` (and its first build) to the folder name of
your EXL3 build. The **`hybrid`** preset is the simpler two-vendor setup
(Claude through the Anthropic API):

| step | model | why |
| ---- | ----- | --- |
| spec-writer, builder | `local`: Flash-Next on TabbyAPI | the most tokens, long iterative runs: free and fast locally |
| reviewer-security | `local` (the security fine-tune) | a specialist on its lens; the only local reviewer, so nothing queues |
| reviewer-code, reviewer-ux, reviewer-ui | `claude-sonnet` (Claude Sonnet 5.5) | a strong, fast reviewer from a different model family; runs in parallel |
| approver | `claude-opus` (Claude Opus 5.5) | independent judgment at the merge gate |
| lead (Plans) | `claude-fable` (Claude Fable 5.1) | frontier planning and final review |
| coder (Chat), repo-scout | `local` | interactive and exploratory |

```bash
./install.sh --preset hybrid --data-dir /srv/ai-work/factory --systemd-user --start
# ~/.config/software-factory/env:  TABBY_API_KEY=...  ANTHROPIC_API_KEY=...
```

- Set `models.local.model` to the model name TabbyAPI has loaded (Settings →
  Models). To switch to Strata, set `models.local.provider` to `strata` and
  `models.local.model` to `strata`; its key comes from `STRATA_API_KEY` or
  `/etc/ai-stack/strata.env`. The `strata` preset puts every step on it.
- Keep the factory on port 8765 (it must not collide with Strata's 18080/18081).

**Alternative fleets** (VRAM is shared: run one GPU fleet at a time):

- **`mixed`**: two `llama-server` instances, one model per GPU. A coder on GPU0
  (qwen3-coder, or Qwen 3.8 27B) for spec-writer, builder, coder and
  repo-scout; Gemma 4 12B on GPU1 for the ux, ui and code reviews; Claude Opus
  5.5 for security, approver and lead. The two GPUs work in parallel, each with
  a hot prefix cache.
  ```bash
  CUDA_VISIBLE_DEVICES=0 llama-server -m <coder.gguf> --jinja --host 127.0.0.1 --port 8081 \
      -c <context_tokens> --parallel 1 -ngl 99
  CUDA_VISIBLE_DEVICES=1 llama-server -m <gemma-4-12b.gguf> --jinja --host 127.0.0.1 --port 8082 \
      -c <context_tokens> --parallel 1 -ngl 99
  ```
- **`gpt-oss`**: gpt-oss-120b on llama.cpp for spec-writer, security review,
  approver and repo-scout (reasoning `high`), with API models for the builder
  and the other reviewers. If it does not fit in 48 GB of VRAM, keep some MoE
  experts in RAM with `--n-cpu-moe <n>`.

## Routing

The principle the presets follow:

- **Local for volume.** spec-writer, builder, Chat and repo-scout read and write
  the most tokens over long, iterative runs.
- **API for independence.** Reviewers and the approver judge work they did not
  write. A different model family catches different mistakes, and API models
  run every lens at once instead of queueing on one local slot. They read a
  diff and a spec, so they cost little.

Each step's route is `{model, driver, extra_body?, effort?, escalate?,
reason?}` in the config's `agents` section (`*` is the default model and
driver, and takes no `effort`, `escalate` or `reason`; an agent without a
route uses its file's tier, `opus` or `sonnet`):

```json
"agents": {
  "*":                 {"driver": "builtin"},
  "builder":           {"model": "local"},
  "reviewer-security": {"model": "local"},
  "approver":          {"model": "claude-opus", "extra_body": {"output_config": {"effort": "xhigh"}}}
}
```

`extra_body` is deep-merged over the model entry's own, for that step only;
`effort` (`low` to `max`; `minimal` on OpenAI-format APIs only) sets the
reasoning effort the way the provider takes it (`output_config.effort` for
Claude, `reasoning_effort` for OpenAI-format APIs); `escalate` lists the models
the builder moves up to when reviews send it back; `reason` says why the step
runs there (`"cost: free locally"`, or `{"kind": "capability", "text":
"..."}`; kinds: capability, time, stage-fit, cost). A run that overflows or
escalates shows its own reason instead (`time: overflow from flash-next: ...`,
`capability: escalation rung 1 of 2 ...`).

- **Routing view** (Settings → Routing, also linked from the factory-line
  strip): the line drawn as boxes with a model and a driver dropdown on each
  agent (plus Chat and Scout), the provider, a local/API badge and the last
  measured tokens/s. Its **Preset** menu applies a preset's routing in one click.
- **Per-run override.** The New feature dialog's *Routing* menu picks the saved
  routing, any preset, or custom per-step choices for one job. CLI:
  `factory submit my-app "..." --preset hybrid` (also `factory run`). The job
  stores a snapshot of every step's route, so editing settings never changes a
  running job; retry and rework reuse the snapshot, and "Change routing" on a
  stopped job edits it.

### Placement: builds, overflow, escalation

The owner's rule: use local models where their quality holds, never economise
the quality gates, escalate rather than retry, and give every placement a
reason. The engine behind it:

- **Builds.** A model entry may list the same model on other servers:
  `"builds": [{"provider": "tabby", "model": "..."}, {"provider": "strata",
  "model": "strata"}]`. Each run uses the first build whose server answers a
  short probe (3 s, the answer cached 30 s); the run's events say which ran.
- **Overflow.** `"overflow": "<model key>"` on a model entry (or a provider)
  runs that model when no build answers, or when a free slot does not come
  within the provider's `overflow_after_seconds` (job steps, chat turns, the
  plan lead and repo-scout alike). The job (or chat) records that it
  overflowed and why; nothing switches servers silently.
- **Loaded models.** A provider with `"kind": "tabby"` (or a
  `loaded_model_url`) reports which model it has loaded. With
  `allow_model_swap` off (the default) a build naming another model counts as
  unavailable, so the run goes to the next build or the overflow; with it on,
  the run makes the server load its model (TabbyAPI needs inline model
  loading), and `swap_back` loads the original one again when the job stops.
- **Escalation.** With `"escalate": ["gpt-sol-high", "gpt-sol-xhigh"]` the
  builder moves to the first model on its first CHANGES round (or a rework),
  to the second on the next, and stays on the last; a model none of whose
  builds answers is skipped. A new model starts a fresh session with a
  hand-over prompt (the spec, build.md, the review findings, the diff so far):
  sessions never move between models, because each provider passes reasoning
  back differently. A chat turn that lands on another model (another build,
  the overflow) likewise starts a fresh session with the conversation so far;
  the chat keeps showing every message.
- **Cost.** Model entries take `price_in` and `price_out` (USD per million
  tokens; `price_cached_in` when known). A model without prices is unknown,
  not free, except on a local server. Every run's cost is in its `agent.end`
  event and in `job.runs`; the job, chat or plan keeps a running `cost` total
  that says when it leaves unpriced runs out.

### Routing profiles

A profile is a named set of placements, picked per job: `"profile"` in `POST
/api/jobs` (also plans, new chats and chat hand-offs), `factory submit ...
--profile best`. It applies over the saved routing, `default_profile` is used
when a job or chat names none (`""` for none; repo-scout always uses it), and
the job records which one ran.

```json
"profiles": {
  "balanced": {"description": "All four lenses at once.", "agents": {
    "reviewer-security": {"model": "qwen-max", "reason": "time: runs beside the other lenses"}}}
},
"default_profile": "balanced"
```

`local-first` ships `local-first`, `balanced`, `best`, `fastest` and
`thrifty` ([examples/](examples/README.md#profiles)); none economises the
spec, the reviewers, the approver or the lead (but once the builder escalates,
its rung can be a reviewer's model: GPT-6.1 Sol then reviews code it wrote,
see DESIGN §23). **Estimate** before a run:
`factory estimate my-app --profile best` or `GET
/api/estimate?repo=my-app&profile=best` (add `--kind plan` / `kind=plan` for a
plan) gives each step's expected tokens (the median of the repo's last jobs once it
has three, else a default table), cost from the prices, and a duration that
counts provider slots (a one-slot local server reviews one lens after another,
an API all at once; `tokens_per_second` on a provider sets its speed).

## Models & providers

`factory init --preset NAME` (or Settings → Routing → Preset) starts from one
of the files in [`examples/`](examples/README.md):

| preset | what runs where | keys |
| ------ | --------------- | ---- |
| `deepseek` (default) | DeepSeek V4 Pro (opus tier) and V4 Flash (sonnet tier) | `DEEPSEEK_API_KEY` |
| `anthropic` | Claude Opus 5.5 / Sonnet 5.5 / Haiku 5.5 | `ANTHROPIC_API_KEY` |
| `nanogpt` | Claude through NanoGPT, one key for many vendors | `NANOGPT_API_KEY` |
| `ollama` | everything local on Ollama (qwen3-coder) | none |
| `strata` | everything local on Strata Qwen3.8-Flash-Next | `STRATA_API_KEY` |
| `hybrid` | local Flash-Next (TabbyAPI) + Claude reviewers, approver and lead | `TABBY_API_KEY`, `ANTHROPIC_API_KEY` |
| `mixed` | two llama.cpp models, one per GPU, + Claude Opus | `ANTHROPIC_API_KEY` |
| `gpt-oss` | gpt-oss-120b on llama.cpp + DeepSeek | `DEEPSEEK_API_KEY` |
| `nanogpt-value` | all API through NanoGPT: value builder with an escalation ladder, four independent review families, Opus 5.5 gates | `NANOGPT_API_KEY` |
| `local-first` | the owner's agreed routing: local Flash-Next (Tabby, then Strata, then MiMo overflow) + NanoGPT gates and reviewers, five profiles | `TABBY_API_KEY`, `STRATA_API_KEY`, `NANOGPT_API_KEY` |

A preset replaces same-named providers, models, routes and profiles and keeps
your repos, token, data dir, limits and pipeline. The NanoGPT presets copy
their prices from the dated list in `examples/prices/`; their model ids are
unverified, and `factory doctor` warns for any id NanoGPT does not list. Two more files there are
add-ons, not routing presets: `rea` only adds the REA MCP servers (see
[MCP](#mcp)), and `openwebui` only adds the Open WebUI provider and the
`kate` model entry (see [Open WebUI profiles](#open-webui-profiles-kate)).

**Adding a local server.** Any OpenAI-compatible server works. Shipped
providers include `llamacpp` (127.0.0.1:8080), `tabby` (127.0.0.1:5000),
`strata`, `ollama`, `vllm` and `lmstudio`; keyless ones set `"api_key_env": null`.

```json
"providers": {
  "gpu0": {"type": "openai", "base_url": "http://127.0.0.1:8081/v1", "api_key_env": null,
           "max_concurrency": 1, "concurrency_scope": "run", "timeout": 1800}
},
"models": {
  "coder": {"provider": "gpu0", "model": "qwen3-coder", "max_tokens": 16384,
            "context_tokens": 262144, "stream": true, "extra_body": {}}
}
```

- **llama.cpp**: start `llama-server` with `--jinja` (OpenAI-style tool calls
  need the model's chat template), `--host 127.0.0.1`, `-c` equal to the
  entry's `context_tokens`, `--parallel 1`, `-ngl 99`, and choose the GPU with
  `CUDA_VISIBLE_DEVICES`.
- **TabbyAPI**: the `tabby` provider sends both `x-api-key` and Bearer auth
  (`auth: "both"`) with `TABBY_API_KEY`; set the model entry's `model` to the
  loaded model's name.
- **`context_tokens`**: what the server was started with. The builtin harness
  budgets its context from it (and from `max_tokens`, which must fit the
  server's limit), eliding old tool output before the server would overflow.
- **`max_concurrency` and `concurrency_scope: "run"`**: all agents on a
  provider share its slots. With scope `run` an agent holds its slot for the
  whole run, so reviewers on a one-slot server go one after another, each with
  a hot prefix cache, instead of evicting each other's cache on every call.
  Local presets default to this. Start the server with more slots and raise
  `max_concurrency` to match to run two at once.
- **Test** (Settings → Models, or `factory models test <key>`) sends two probes:
  a plain reply, then a call of a trivial tool, and reports latency, whether
  the model called the tool, and tokens/s. Confirm tool calling before giving
  a local model to an agent.
- **A context proxy in front of a server** (the owner's optional
  billion-context pilot, `http://127.0.0.1:18081/bili/http://127.0.0.1:18080/v1`)
  is one more provider whose `base_url` is the proxy URL, with the same key.
  Keep `context_tokens` at what the real server runs, and do not route agents
  to the proxy and to the server it fronts at the same time: each provider has
  its own slots, so a one-slot server would get two requests. Unverified with
  the factory's Chat Completions calls; see
  [docs/tools.md](docs/tools.md#billion-context-a-context-proxy-in-front-of-strata).

## Drivers

| driver | runs the agent with | notes |
| ------ | ------------------- | ----- |
| `builtin` (default) | the factory's own agent loop | any provider; Read/Write/Edit/Glob/Grep/Bash, Skills, MCP; file tools enforce the step's write policy |
| `qwen` | Qwen Code (`qwen`) | resumable sessions |
| `claude` | Claude Code (`claude`) | resumable sessions; uses your Claude login or `ANTHROPIC_*` |
| `codex` | Codex CLI (`codex exec`) | supports a Codex `profile` |

Point an agent at a driver with `"agents": {"builder": {"driver": "codex"}}`.
`command` may be a wrapper script that passes its arguments through, such as
the owner's `codex-local` and `qwen-local`:

```json
"drivers": {
  "codex": {"type": "cli", "preset": "codex", "command": "~/.local/bin/codex-local", "profile": "strata"},
  "qwen":  {"type": "cli", "preset": "qwen",  "command": "~/.local/bin/qwen-local",
            "env": {"OPENAI_API_KEY": "${STRATA_API_KEY}"}}
}
```

The CLIs run in the job's worktree with secrets removed from their
environment (except the CLI's own login variables); `"${NAME}"` in a driver's
`env` passes one variable through on purpose, and `null` removes one. A
run's header in the job drawer names the model the CLI is launched with (for
example `haiku` for the sonnet tier with `"models": {"sonnet": "haiku"}`) and
the driver as its provider, not the tier's API model; the job's routing
snapshot records it as `cli_model`. Qwen Code runs with its
managed auto-memory, auto-dream and auto-skill off (they would carry one job's
repo content into your cross-project memory and other jobs); the factory does
it with a system settings file beside the run's session file, your
`/etc/qwen-code/settings.json` merged in, and setting
`QWEN_CODE_SYSTEM_SETTINGS_PATH` in the driver's `env` opts out. A CLI that
only reports connection errors for a minute or more (Codex retries a down
server forever) is stopped, so the job fails and can be retried instead of
waiting for the wall-clock limit. A
relative `command` with a `/` (such as `bin/codex-local`) is resolved against
the config file's directory, never the worktree, so a repo cannot plant the
binary. After every run, whatever the driver, the factory checks the file the
step had to write and the step's write policy: anything changed in the job
folder outside the step's own files is reverted and stops the job, so the
line works the same whichever runtime did the work.

## Skills

Skills are folders with a `SKILL.md` (frontmatter `name`, `description`; body =
instructions), the format Claude Code, Codex, Qwen Code and
[mattpocock/skills](https://github.com/mattpocock/skills) use. The builtin
harness searches, in order: the worktree's (then the repo's) `.claude/skills`,
`.qwen/skills`, `.agents/skills`; `~/.claude/skills`, `~/.qwen/skills`,
`~/.agents/skills`, `~/.config/software-factory/skills`; then
`harness.skills_dirs`. Collections nest (up to four levels), so a clone works
as is:

```bash
git clone https://github.com/mattpocock/skills ~/.agents/skills/mattpocock
```

Agents with the `Skill` tool (the Chat `coder` and `analyst`) see a catalog of
model-invocable skills and load one when it fits. To **preload** skills into
an agent's system prompt, list them in its frontmatter (`skills: tdd`) in an
override of that agent. `disable-model-invocation: true` keeps a skill out of
the catalog but still preloadable.

**Matt Pocock's seven** (the owner's plan installs them per project in
`<repo>/.agents/skills/<name>`): all load as they are. They suit Chat best;
`tdd` and `diagnosing-bugs` can be preloaded into the builder,
`writing-for-agents` into repo-scout. Several assume a person to ask,
sub-agents or an issue tracker, so the shipped agents preload none of them.
[docs/skills.md](docs/skills.md) has the per-skill table, how to enable each
and the caveats.

## MCP

The builtin harness attaches stdio MCP servers as tools named
`mcp__<server>__<tool>`. Expose only what an agent needs with a `tools`
allowlist: the agent sees nothing else, and a call outside it is refused.

**REA** (`rea-agents` 6.5.0, reverse engineering; Node 22.19+, 24.11+ or
26+) is the worked example. Its 207 tools would fill a small model's context
on their own,
so [`examples/rea.json`](examples/rea.json) (apply it like a preset, or copy
its entries) adds two allowlisted servers that start the owner's wrapper
`~/.local/bin/rea-mcp`, which runs `rea mcp`:

```json
"mcp_servers": {
  "rea": {
    "command": "~/.local/bin/rea-mcp",
    "args": [],
    "env": {"REA_MCP_INPUT_SCHEMA_PROFILE": "compact"},
    "tools": ["binary_session", "open_binary", "inspect_artifact", "analyze_javascript_application",
              "inspect_analysis_view", "get_evidence_bundle", "export_evidence_bundle", "close_binary"],
    "write_args": {"export_evidence_bundle": ["path"]},
    "timeout": 450,
    "startup_timeout": 30
  }
}
```

**What a server may write.** An MCP server runs as the factory's user and
writes wherever its tools are told to (REA's `export_evidence_bundle`
replaces any file it can reach, through symlinks too). `write_args` lists, per
tool, the arguments that name a file it writes: the builtin harness checks
each like Write (symlinks resolved, inside the agent's write policy, nothing
named `.git`), creates missing parent directories and passes the resolved
path on, so a refused path never reaches the server. With
`"harness": {"sandbox": "bwrap"}` the servers also run inside the run's
sandbox, with the same binds as the agent's Bash (its checkout writable, the
rest read-only, a private `/tmp`), so whatever a tool writes stays where Bash
may write. Both are in [docs/tools.md](docs/tools.md#what-rea-may-write).

`rea` covers static analysis (JavaScript and Electron apps, archives and
packages, retained evidence views and export) and needs no Java; `rea-native`
adds `binary_overview`, `list_procedures`, `search_procedures` and
`analyze_function` for Ghidra (`GHIDRA_INSTALL_DIR` and `JAVA_HOME` in the env
file). The **analyst** Chat agent (`agents/analyst.md`, `mcp: rea`) uses it:
its prompt has it hash and identify the target first, read small views,
export the evidence to `analysis/evidence/`, keep its findings in
`analysis/NOTES.md` in the chat's checkout and never run the target. Those
are instructions, not enforced limits: like any Chat agent it may write
anything in its checkout and has Bash, so point it at untrusted binaries with
`"harness": {"sandbox": "bwrap"}` and the factory running as an unprivileged
user. It is not part of the factory line.

To give another agent a server, list it in the agent's frontmatter
(`mcp: rea`); for a shipped agent, make a copy first with `factory agents new`
or the Agents view.

## Tools

Besides MCP servers, agents use command-line tools that are on the server's
`PATH`; the factory installs none of them. What was checked, with
measurements and caveats, is in [docs/tools.md](docs/tools.md):

- **REA** over MCP (above): the round trip through the factory's MCP client,
  the tool names and schema sizes, and what still needs Ghidra on the server.
- **agent-browser** (`npm install --global --prefix ~/.local/npm
  agent-browser@0.39.1`, then `agent-browser install`, or point
  `AGENT_BROWSER_EXECUTABLE_PATH` at an existing Chromium): the builder, the
  ux and ui reviewers and Chat check pages as text with it (accessibility
  snapshot, text, computed styles, layout, overflow at 390 px). Every command
  of a check uses the same `--session`. With `"sandbox": "bwrap"`, set
  `AGENT_BROWSER_SOCKET_DIR=/tmp/agent-browser` in the env file; a later Bash
  call cannot reach a browser an earlier one opened, which keeps running until
  the run ends, so the prompts then open, check and close in one call. Set
  `AGENT_BROWSER_IDLE_TIMEOUT_MS` (for
  example 600000) so a session an agent forgets to close does not linger.
- **billion-context**: a provider, not a tool (see
  [Models & providers](#models--providers)).

`tests/test_tools_integration.py` re-runs these checks when the tools are
installed (`FACTORY_E2E_TOOLS=1`; paths in its docstring).

## Plans

For work bigger than one job:

```bash
plan=$(factory plan my-app "Add team accounts: invitations, roles, billing per seat")
factory plans                     # wait for "ready"
# read / edit <data_dir>/plans/$plan/plan.md (or GUI → Plans), then:
factory plans run "$plan"
```

1. The **lead** reads the repo (read-only) and writes `plan.md`: `PLAN: READY`
   with an ordered `## Jobs` list (`1. <feature> (after: 2, 3)`), or
   `PLAN: QUESTIONS` for you to answer: write your answers into that plan.md
   (GUI → Plans) and **Retry**; the lead plans again with them.
2. You read and edit it while it is `ready`, then **Run**.
3. Jobs are submitted as their dependencies merge, so later jobs build on
   earlier ones. A job that needs a human blocks the plan until it merges.
   You can edit plan.md while the plan is ready, blocked or needs-human: an
   unchanged line keeps its job, a line whose job was rejected or cancelled
   gets a new one, and an edit that drops or rewords the line of a job still in
   progress is refused (cancel that job first).
4. When everything has merged, the lead reviews the combined diff against the
   request. Findings become follow-up jobs once; a second round of findings
   goes to you.

The GUI's Plans view shows the request, the rendered plan, a job graph
coloured by stage, and the final reviews. A plan's routing (`POST /api/plans`
`routing`, a preset or per-agent routes) goes to each of its jobs and routes the
lead too (`hybrid` puts the lead on `claude-fable`). So does its delivery mode
(`delivery`, see [Output](#output-merge-branch-or-folder)): with `branch` or
`folder`, each job waits for your approval before the jobs after it start.
The lead's run logs are at `GET /api/plans/<repo>/<id>/log?file=logs/<NN-lead>.jsonl`.

## Chat + hand-off

**Chat** is one builtin coding agent (`agents/coder.md`, all tools) on a repo.
By default it works in its own worktree on branch `chat/<id>`, so your
checkout is untouched (*repo* mode works in the checkout itself; the GUI warns).
Replies stream with collapsible tool calls; the session survives restarts.
A turn is placed like a job's run (see [Placement](#placement-builds-overflow-escalation)):
when it lands on another model, build or overflow, the chat starts a fresh
session with a hand-over (the end of the conversation so far), since a session
never moves between models; the conversation still reads as one. What the
coder promises in a reply is tracked as a suggestion under that reply.

When the work is ready, **Hand off to factory**: a job is created from the
chat's branch, the spec-writer describes what it does and must do, and the
job goes straight to review, the approver and the merge gate (or the output
you pick for it, see below). The chat's open items move into that job.

A chat can use any agent with tools, not only the coder: `POST /api/chats`
takes `agent`, for example `analyst` for reverse engineering with REA
(see [MCP](#mcp)).

**A chat without a repository** (`POST /api/chats {"mode": "none", "agent":
...}`) works in a scratch folder of its own (`<data_dir>/chats/<id>/work`,
deleted with the chat), with no branch, worktree or hand-off. Its agent comes
from the shipped or your own agents (never a repository's), and it may have no
tools at all (`tools: none` in the agent file) for a plain conversation, as
with a persona such as your Open WebUI "Kate" ([below](#open-webui-profiles-kate)).
An agent file with `wake: true` is never a chat agent: a chat refuses it when it
starts, and a running chat whose agent file gains `wake: true` (or becomes a
persona such as Kate) stops at its next message with that reason, before
anything is sent. Its tracked items stay in the chat: there are no follow-ups
to move them to later, so neither you nor its agent can defer one; deleting
the chat moves what is still open to the system list.

## Open WebUI profiles (Kate)

You keep specialist profiles as Open WebUI **workspace models**: a system
prompt, settings, a base model, and maybe knowledge, filters, skills and tools.
The harness chats with one **through Open WebUI**, so Open WebUI stays the one
place where the profile lives and every edit there applies on the next message
(DESIGN.md §33; the evidence is
[docs/seraphina/research/openwebui-profiles.md](docs/seraphina/research/openwebui-profiles.md)).

1. Apply the `openwebui` preset (Settings → Routing → Preset, or `POST
   /api/config/preset {"name": "openwebui"}`). It adds the provider `openwebui`
   (`http://127.0.0.1:8083/api`, one slot) and the model entry `kate`, and
   changes no routing. If your workspace model's id is not `kate`, change
   `models.kate.model` (and the persona's `openwebui:` below).
2. In Open WebUI, create an API key (Settings → Account → API keys; an admin
   turns API keys on first) and store it as `OPENWEBUI_API_KEY` in
   `~/.config/software-factory/env` (or `POST /api/secrets`, see
   [API keys](#api-keys)). A read key is enough to chat;
   importing a profile card through the API needs your own (owner) key.
3. `factory doctor` shows whether Open WebUI answers, accepts the key and lists
   the workspace models, and whether `kate` is among them.
4. Start a chat with Kate: `POST /api/chats {"persona": "kate"}` (the GUI's
   chat page will offer it). It has no repository: it works in a scratch folder
   of its own, and there is nothing to hand off.

**What a Kate chat sends.** Open WebUI applies Kate's prompt (first), her
settings, skills and filters. The harness adds only a short note after her
prompt, never its coding-agent instructions, and on every message: your local
date, time, weekday, time zone and language (as Open WebUI's browser page
would), her default filters as `filter_ids` (`filters: false` in the persona
file turns them off), no `max_tokens` of its own, so hers applies, and never
`tool_ids`, `chat_id`, `id` or `session_id`. Her **knowledge** is off unless your
persona file says `knowledge: true`: sending it makes Open WebUI run retrieval
on every model call, with its own task and embedding models. Each reply records which version of Kate answered
(`chat.json` → `persona.turns`). What Open WebUI runs only in its own web page
(her Python tools, actions, built-in features, the terminal) is listed as not
available here; an MCP tool server she uses can be connected to the harness
directly ([MCP](#mcp)). Whatever Kate's "Stream Chat Response" says (and
whenever you change it), her answers are read as they come, streamed or in one
piece.

**The harness's part stays on this machine.** A persona chat runs only on a
models entry whose provider is a loopback address, on the builtin harness, and
(bound to Open WebUI) only on its workspace model; that is checked again on
every message, so an edit, a build or an overflow can never move it to an API.
What Open WebUI does next is its own configuration: it sends your words to
Kate's base model's connection (and, by its settings, to its task and
embedding models). The chat and `factory doctor` warn when Open WebUI does not
list her base model for your key, or does not label its connection **local**;
Open WebUI labels an OpenAI-type connection external until you change it, so
if her TabbyAPI runs on this machine, set that connection's type to Local in
Open WebUI. If it really is external, your words leave the machine there.

**The persona file.** `agents/kate.md` is a few lines: `openwebui: kate`,
`model: kate`, `tools: none`, `privacy: private` and a two-line note. Put your
own in `~/.config/software-factory/agents/` (another id, `knowledge: true`, `tools: Read, Grep` to
let her read files in her scratch folder, `timezone: Europe/London`, `language:
en-GB`); keep a `tools:` line, since an agent file without one gets every
builtin tool, Bash included. Personas are never read from a repository.

**Profile card and drift.** `POST /api/personas/import` with the file Open
WebUI's workspace **Export** saves (`{"export": [...], "model_id": "kate"}`;
`factory personas import kate-….json` strips the images for you), or with
`{"source": "openwebui", "model_id": "kate"}` and your owner key, saves a
read-only **profile card**: base model, the settings as Open WebUI sends them
(with each model control's default option; stop sequences exactly as stored,
since Open WebUI 0.12.3 sends a typed `\n` as a backslash and an `n`),
knowledge, filters, skills, tools and which of them cannot run through the API,
version and fingerprints, in
`~/.config/software-factory/profiles/openwebui/kate.json` (mode 600). The card
keeps the length and a hash of Kate's prompt, never its text. `GET
/api/personas/kate/card` shows it beside what Open WebUI lists now. The **drift
check** (`factory personas check`, `POST /api/personas/check`; also when a
chat starts and when a reply comes from a new version) tells a cosmetic edit
(description, picture) from a change in what Kate does, and for the latter (or
when your key cannot tell) opens one item in the tracker's system scope: "Kate
changed in Open WebUI". Importing a card and then chatting with nothing edited
is no change. Run it daily from a timer if you like. Without `OPENWEBUI_API_KEY`
these calls answer 400 (nothing was asked of Open WebUI); 502 means Open WebUI
did not answer or refused.

**Offline copy, only if you ask.** Adding `"snapshot": {"name":
"kate-offline"}` to an import also writes a persona that runs a copy of Kate's
prompt and settings on one of the harness's own local models, for when Open
WebUI is down. It is labelled "Kate, imported snapshot of <date>, version …",
never used automatically, warns when the live Kate has changed, and has none of
her knowledge, filters, skills or tools. When her base model matches none or
several of your local model entries, the import asks which (`"model": "<key>"`).
The name must be new: one of your models keys (`flash`, `opus`, …) or agents is
refused, so a snapshot never takes over what other agents run on; importing the
same snapshot again replaces its settings whole, and an import that fails
leaves nothing behind.

## Housekeeping Tracker

Agents hyper-focus: they say they will also check the other callers, a reviewer
notes a gap, the spec has an acceptance criterion nobody re-checks, and as the
context moves on and compacts the promise is silently lost. The factory keeps
every such promise, finding and requirement as an **item** in a ledger it owns,
outside every agent's context (DESIGN.md §32):

- **Where items come from.** Every numbered acceptance criterion of `spec.md`
  (the builder owns it, the approver verifies it); every spec assumption marked
  **(product)** (a decision a person owns, see [Product
  decisions](#product-decisions-product)); every finding of a CHANGES review
  (the builder fixes it, that reviewer verifies it); the findings of a PASS
  review and review notes, which become suggestions (a passing review never
  holds the merge); your rework notes; the agents' own `## Items` lines and
  builtin `Items` tool calls; and a sweep of each agent's final text for
  "later", "TODO", "could not run", "I'll … once …", which become suggestions
  to accept or dismiss.
- **How agents answer.** One grammar for every runtime, in the one file each
  agent writes: `- I-5 done: commit 4f1c2a9`, `- I-6 doing: …`,
  `- I-3 verified: re-ran the tests`, `- I-4 not met: …`, `- new: …`
  (`- I-7, I-8 done: commit 4f1c2a9` answers several at once). Each prompt
  lists the agent's items, `<job folder>/items.md` has the full list, and the
  builtin harness re-shows the list at the end of the agent's context every 3
  tool turns on a local model, every 6 on an API model (cache-friendly: it
  never edits earlier messages). Status words: Suggested, To do, Working on
  it, Waiting for a check, Proven, Later, Dropped.
- **The rules, in code.** "done" needs evidence; a commit is checked against the
  branch (a plain number such as a duration is not taken for one); a ticked
  build.md criterion needs what was run or seen after it (`1. [x] …: <evidence>`,
  or a line that names a command, commit or file, or a result in its own words:
  a bare "done", "yes" or the criterion cut short is sent back); agents
  cannot record a person's or the factory's evidence; nobody verifies an item
  they marked done; a must-do item can only be moved to later, dropped or
  dismissed by a person, and only a person answers a **(product)** decision
  (the approver cannot). An agent may mark an item must-do only when it adds
  it, never unmark one; a reviewer's must-do findings count only under
  CHANGES. Text a tool returns that poses as the factory's own item list
  (`<factory-items …>`) is escaped before the agent sees it. A builder that
  leaves must-do items untouched in its own file (`## Items` lines of
  build.md), or claims one done with a commit that fails its check, is sent
  back once, then the job stops for you. The merge gate holds a job while any
  must-do item is not proven ("2 blocking items not proven: …"), so you prove,
  drop or move them, then approve.
- **Approving past open items.** A job at needs-human (or a delivered one)
  that still has must-do items not proven is approved only when you name
  them: `factory approve <repo>/<id> --carry-open-items` lists them and
  moves them to the repo's follow-ups (API: `approve {carry_items: [ids]}`;
  without every id the server answers 409 with `open_blocking`).
- **Nothing is lost.** Items are never deleted. Moving an item to later in a
  job, chat or plan, whether you do it (`defer`, or `update` to `defer`) or the
  agent that owns it does, moves it to the repo's **follow-ups** at once. When
  a job merges, is delivered, rejected or cancelled, every open item moves
  there too, with a due date (to the plan, for a plan's job), however many
  follow-ups there already are; a human override merge says how many it moved.
  A finished job takes no new items: add a follow-up instead. Deleting a chat
  moves its items there too, and handing a chat off moves them into the job
  before it starts (a pending wake's check-in stays in the chat, where the wake
  fires). A hand-off's acceptance criteria are yours (no builder ran its first
  round): you or the approver may prove those, and what the chat passed on,
  directly; findings and rework of the job itself still need the builder's
  done first.
  "Make a job" on a follow-up runs it through the factory (once: a second click
  is refused while that job runs) and proves it when that job merges.
- **A cap, not a loss.** A job, chat or plan holds at most 300 open items (a
  repo at most 300 follow-ups not proven or dropped; suggestions not counted,
  unless they are must-do). An agent that floods it stops the job for you
  after the step's own checks, with a reason that names every problem found;
  nothing is dropped, and verifying, dropping or moving items to later frees
  room. Your rework notes are never refused. Past 300 suggestions in one
  place (`max_proposals_per_scope`), new ones fold into a single overflow
  item, one sentence each: its text lists them while they fit (2000
  characters) and its history (`/api/items/history`) keeps every one; the
  same sentence is never suggested twice. Dismiss them together with
  `POST /api/items/dismiss {refs, reason}`.
- **The machine's own items.** Work that belongs to no repo (rotate a key,
  check a backup) lives in the **system** list, `system:S-1`, `system:S-2`, …
  (`GET` / `POST /api/system/items {title, detail?, kind?, blocking?, due?}`,
  kept in `<data_dir>/housekeeping/system.json`). The factory puts two things
  there itself: an Open WebUI profile that changed (see "Personas and Open
  WebUI profiles") and what was still open in a deleted chat without a
  repository. You prove, drop or move them to a later date like follow-ups.
  Item references may name the machine (`host:<id>/…`); this version knows
  only its own.
- **Where to see them.** `GET /api/items?waiting=1` is everything waiting for
  you (suggestions, must-do items on stopped jobs, overdue follow-ups, items you
  own, anything left open in a finished job); each job, chat and plan has its
  own `…/items` route, `GET /api/items/history?ref=…` is one item's whole
  history, and every job card carries an `items` summary. The GUI view is
  coming next.

Settings live under `"housekeeping"` in the config (re-injection cadence, the
stop-guard attempts, stale rounds, follow-up due days, the sweep, the
suggestion limit); `"enabled": false` keeps recording items but stops holding
jobs for them. `"cli_hooks"` (default off) gives Claude Code runs two hooks:
the item list after a compaction, and one more turn when the agent stops with
must-do items it has not answered. It stays off until it is verified against
a real Claude Code; without it a CLI agent still gets its items in the prompt
and the factory's checks after the run.

### Your own agents: sf-items

Agents the factory does not run (a Claude Code or Qwen Code you use by hand,
a script) can keep their promises in the same ledger. Make a token for each
(`POST /api/agent-tokens {name}`, shown once; revoke with
`POST /api/agent-tokens/<name>/revoke`) and give it to `sf-items`:

```bash
export SF_ITEMS_TOKEN=sfi_…    # SF_ITEMS_URL: default this machine's server
sf-items add "Rotate the backup key" --detail "after the new disk"   # system:S-1, owned by the agent
sf-items add "Update the README" --scope repo:my-app                  # a follow-up of my-app
sf-items list                                                         # its own items: what it added or you gave it
sf-items done system:S-1 --command "restic check" --result "no errors"
```

`sf-items mcp` is the same as a stdio MCP server (tools `items_list`,
`items_add`, `items_update`) for the agent's MCP settings, with both variables
in its `env`. A token reaches the item routes only, works only on items its
agent owns, adds only commitments, gaps, risks and notes, and can never
verify, drop, dismiss or unblock: a person checks what the agent marked done.
It also **reads** only its own items: outside agents may run on hosted models,
so your own system items and everything a persona wrote stay on this machine.
To give an agent one of yours, assign it to the agent
(`POST /api/items/assign {ref, owner: "agent:<name>"}`); an item a persona or
a private turn wrote is never given to one. That includes what the tracker
suggests from Kate's replies in a persona chat: those suggestions are marked
as hers (and private, unless her file says `privacy: clean`), and keep the
marks when a deleted chat moves them to the system list.

## Product decisions: (product)

The spec-writer ends an assumption with **(product)** when only a person
should decide it: pricing, public copy, what users may see or do, legal or
business rules. The approver escalates a job on such a line that nobody has
confirmed, so the job stops at needs-human instead of merging. Confirm the
lines you agree with at any time, even while the job runs, and the approver
reads them:

```bash
factory confirm my-app/004-pricing-page           # [ ] / [x] per (product) line of spec.md
factory confirm my-app/004-pricing-page --all     # you decide: confirm every line not confirmed yet
```

(`POST /api/jobs/<repo>/<id>/confirm`, see the API table.) The job's
`confirmed.md` is written from what you confirm; a copy anyone else writes
(an agent, or a hand edit) is put back before the approver reads it.

Each such line is also a decision item of the [Housekeeping
Tracker](#housekeeping-tracker), which holds the merge until a person answers
it. It is one answer either way: confirming a line proves its decision item,
and proving the item (`verify`) confirms the line; reopening the item takes
the confirmation back. A mention such as "not (product)" is no mark.

## Output: merge, branch or folder

What happens once the approver approves a job is its **delivery mode**:

| mode | what you get | your repo |
| ---- | ------------ | --------- |
| `merge` (default) | the deterministic gate, then a `--no-ff` merge into the base branch | base moves |
| `branch` | the branch `factory/<id>` with all the work, its worktree removed | untouched except the branch |
| `folder` | the branch, plus an export to the output folder | untouched except the branch |

Set the default in the config (`"merge": {"delivery": "folder"}`, or in the
GUI's setup) and override it per job: the New feature dialog, `POST /api/jobs
{"delivery": "branch"}`, `factory submit|run ... --delivery folder`. A plan's
delivery (`factory plan ... --delivery`) goes to each of its jobs, and a chat
hand-off takes one too.

A `branch` or `folder` job stops at **delivered**. Nothing was merged, so the
protected-path, size, conflict and auto-merge rules do not apply (every
review must still pass). **Approve** a delivered job to merge it (the human
override: it still refuses conflicts), or **reject** it to delete the branch.
In a plan, a delivered job holds the plan until you approve it, because the
jobs after it build on the base branch.

A `branch` or `folder` job that stops for a human *before* its delivery (the
approver escalated, the rounds ran out, a review did not pass) is never merged
by an approve: **Approve** delivers it (base untouched; REPORT.md notes that a
human approved it), and a second **Approve** of the delivered job merges it.
A rework sends it back to the line; it is delivered again after the next
approval.

A folder delivery goes to `<output_dir>/<repo>/<job-id>/`, where `output_dir`
is `merge.output_dir` (default `<data_dir>/output`):

```
output/my-app/004-rate-limit-signup/
  changes.patch   the branch's commits since base: `git am changes.patch` applies them
  files/          every file the branch changed, as it is at the branch tip
                  (symlinks and submodules are skipped and listed; deleted files are listed)
  REPORT.md       the feature, a spec and build summary, every round's verdicts, the decision
```

A retry of a failed delivery replaces the folder; the branch stays until you
approve or reject the job.

## Make it yours

The factory is meant to be reshaped without touching Python.

- **Add a review lens.** `factory agents new reviewer-perf` copies
  reviewer-code to `~/.config/software-factory/agents/reviewer-perf.md` (the
  review file it writes is renamed too). Edit the prompt, then add `"perf"` to
  `pipeline.reviewers`. The board, chips and factory-line strip follow the list.
- **Swap a model per agent** in the Routing view or `agents.<name>.model`.
- **Edit prompts** in the GUI's Agents view (Edit / New / Reset). Overrides go
  to `~/.config/software-factory/agents/`; a repo can carry its own in
  `.factory/agents/`. The shipped `agents/` stay pristine. Reset deletes your
  override: the shipped agent takes over again, and an agent that only existed
  as your file is gone.
- **Attach skills or MCP servers** with `skills:` and `mcp:` in an agent's
  frontmatter.
- **Change the escalation rules.** `escalate_paths` (globally and per repo)
  lists globs that always go to a human (CI config, lockfiles, migrations,
  agent instructions, secrets...). Also `pipeline.max_auto_merge_lines`,
  `pipeline.max_resumes`, and `pipeline.auto_merge: false` to approve every
  merge yourself.
- **Deeper changes**, such as a new stage, live in `swfactory/orchestrator.py`:
  one readable method per step (`_setup`, `_spec`, `_build`, `_build_check`,
  `_review_round`, `_approve`, `_gate`, `_merge`).

## API keys

Keys live in one file, `~/.config/software-factory/env` (next to the config
when `$FACTORY_CONFIG` moves it), as `NAME=value` lines. `factory` loads it at
startup (variables already set win) and the systemd unit reads it with
`EnvironmentFile=`, so edit it with any editor, or from the GUI:

- `GET /api/secrets` lists every provider's key variable plus the
  credential-looking names in the file, by name only: `{env_file, secrets:
  [{name, set, in_env_file, providers}]}` (`set`: the key resolves now, from the
  environment or the provider's `api_key_file`).
- `POST /api/secrets {"name": "ANTHROPIC_API_KEY", "value": "sk-..."}` writes
  the line (atomically, mode 600, other lines kept, a commented `# NAME=`
  placeholder filled in) and sets it in the running server at once, so no
  restart is needed. `"value": ""` removes it. Only a provider's
  `api_key_env` or a name that looks like a credential (`*_API_KEY`,
  `*_TOKEN`, `*_SECRET`, ...) is accepted, so whatever you store is also
  redacted from events and kept out of agent environments. A value must be
  printable text on one line (no control, zero-width or Unicode line
  separator characters), at most 4096 characters, without `'` or surrounding
  spaces. The value is never logged or sent back.

## Security model

- **Local only by default.** The server binds 127.0.0.1; reach it through an
  SSH tunnel. Every request needs the token (Bearer header or an HttpOnly,
  SameSite=Strict cookie from the login link), and Host and Origin are checked
  against DNS rebinding and cross-site requests.
- **Agents run code.** File tools are confined to each step's worktree and
  files (and never write anything named `.git`), but Bash is only contained
  with `"harness": {"sandbox": "bwrap"}` (bubblewrap). Run the factory as a
  dedicated Unix user (for example `ai-dev`), or enable bwrap, or both. Under
  bwrap an agent can commit on its branch but not touch the repository's
  config, hooks or main; each command gets its own PID namespace, so it cannot
  reach them through another process's `/proc/<pid>/root` either. Under bwrap
  MCP servers run in the run's sandbox too; without it they can write what the
  factory's user can, except through the arguments their `write_args` list,
  which are checked (see [MCP](#mcp)).
- **The factory's own git runs no hooks.** Every git command it runs
  disables hooks and `core.fsmonitor`, and status and stash first check that a
  worktree's `.git` file still points into the configured repo. Filter and
  merge drivers set up in the repo's `.git/config` still run, so that file must
  stay yours: bwrap keeps agents from editing it.
- **Keys** come only from environment variables or env files
  (`~/.config/software-factory/env`, a provider's `api_key_file`). They are
  never written to job files or logs, are redacted from events (also the keys
  of providers that only a per-run preset brings), and are removed from agent
  shells, CLI and MCP server environments. So is every variable whose name
  looks like a credential: ending in `_KEY`, `APIKEY`, `_KEY_ID`, `_TOKEN`,
  `_SECRET`, `_PWD`, `_AUTH` or `_CONNECTION_STRING`, or containing `SECRET`,
  `PASSWORD`, `PASSWD`, `PASSPHRASE` or `CREDENTIAL` (`AWS_SECRET_ACCESS_KEY`,
  `STRIPE_SECRET_KEY`, `PGPASSWORD`, `GOOGLE_APPLICATION_CREDENTIALS`), plus
  `ANTHROPIC_*`, `OPENAI_*`, `DEEPSEEK_*` and `FACTORY_*` (a CLI keeps its own
  login variables). Anything else passes through: a credential under another
  name (a password inside `DATABASE_URL`) reaches the agents, so keep such
  variables out of the factory's environment.
- **Repo content is untrusted.** Agents are told to treat instructions in
  files, specs and reviews as data, and the merge gate is deterministic code: a
  prompt-injected approver cannot merge a protected path, an oversized change,
  a conflict, or anything a reviewer did not pass.
- **No silent fallback.** A busy or down local server makes a call wait or the
  job fail (retryable); the factory never switches to a hosted model on its own.
- **Never pushes** unless `merge.push` is true, and never force-updates a ref.
  Your checkout is only touched by worktree commands, branch create/delete and
  the final merge.

## CLI reference

```
factory init [--force] [--preset NAME] [--data-dir DIR]   write the config + token
factory serve [--host H] [--port P]                       run the server and workers
factory url                                               login URL (+ SSH tunnel command on stderr)
factory doctor [--probe] [--json]                         checks; exit 1 on an error-level failure
factory repo add <path> [--name N] | list | remove <name>
factory submit <repo> "<feature>" [--preset P] [--delivery D] [--profile R]   queue a job, print <repo>/<id>
factory run <repo> "<feature>" [--preset P] [--delivery D] [--profile R]      one job in-process, no server
factory status [<repo>] [--json]
factory approve | retry | cancel <repo>/<id>               approve: merges, or delivers a branch/folder job
factory approve <repo>/<id> --carry-open-items            approve and move its unproven must-do items to follow-ups
factory rework <repo>/<id> "<note>"
factory reject <repo>/<id> ["<reason>"]
factory confirm <repo>/<id> [LINE ...] [--all]            list, or confirm, the spec's (product) decisions
factory backlog <repo> [add "<text>" | next]
factory logs <repo>/<id> [-f]                             agent transcript; -f follows the server
factory plan <repo> "<request>" [--delivery D] [--profile R]   hand a big request to the lead
factory plans [--json] | plans run | cancel | retry <repo>/<plan-id>
factory agents [--json] [--repo R] | agents new <name> [--from AGENT]
factory models [--json] | models test <key>
factory personas [--json] | personas card <name> [--json]  personas (Open WebUI profiles such as Kate)
factory personas import [FILE] [--model ID] [--snapshot NAME [--snapshot-model KEY]]   a profile card (server)
factory personas check [<name>] [--json]                  the drift check, now (server)
factory estimate <repo> [--kind feature|plan] [--profile R] [--preset P] [--json]   tokens, cost, time per step
factory snapshot --out DIR [--sudo]                       read-only server inventory (see below)
```

- Commands that act on jobs, backlogs and plans talk to the running server
  with the config's token, at the address `factory serve` recorded in
  `<data_dir>/factory.lock` (else `server.host`:`server.port`). `init`, `repo`,
  `run`, `logs`, `doctor`, `agents`, `models`, `personas` (list and `card`),
  `estimate`, `snapshot` and `url` work without it (`repo add` goes through the
  server when one runs).
- `"-"` as the feature, request or note reads it from stdin.
- Data goes to stdout and hints to stderr, so `factory submit ...` and
  `factory url` are easy to capture.
- `--delivery` is `merge`, `branch` or `folder` (see [Output](#output-merge-branch-or-folder));
  without it, the config's `merge.delivery`. `factory status` shows where a
  delivered job went (its folder or branch).
- `factory run` exits 0 when merged or delivered, 2 at needs-human, and 1 when failed,
  rejected or cancelled (Ctrl-C cancels the job). It takes the same lock as
  `serve`, so it refuses to run beside a server on the same data dir. It runs
  only that job: plans and chats left on disk wait for the next `factory serve`.

### Server snapshot (`factory snapshot`)

The first step towards Seraphina OS Harness's Blueprint: a read-only
inventory of an Ubuntu server, so its next version is designed against what
is really there.

```bash
factory snapshot --out ~/snapshot           # as you; root-only details are recorded as denied
factory snapshot --out ~/snapshot --sudo    # root-only probes through `sudo -n` (never prompts)
```

It writes `snapshot.json` (every probe's command, exit code, output and
duration, then indexes of systemd units, packages, containers, compose files,
listening endpoints and their relationships, what was not captured, a
redaction review and a summary) and a short `overview.md`, both readable
only by you. It covers the host, GPUs, disks and mounts, network and
firewall, packages, systemd, cron, logging, Docker and compose files,
Cockpit, Cosmos, Tailscale and backup tools.

- **Read-only by construction.** Only an allowlist of side-effect-free
  command shapes can run, each with a timeout; anything else is refused in
  code. It never runs `lxc` (on Ubuntu that can start the LXD installer),
  package managers, `docker start/exec/run/pull/logs`, or backup tools
  against their repositories; it does not ask Docker or snapd when their
  daemon is stopped, or when systemd runs but cannot list its units (their
  sockets would start it); a command that is not installed is recorded, not
  installed. `--sudo` on a host without sudo runs those probes as you (or
  directly, when you are root) and says so.
- **No private data.** It never reads application data, databases, Docker
  volumes, log messages, env files or files under home directories (their
  paths are listed). Compose files elsewhere are read with every
  `environment:` value removed. A file reached through a symlink is read only
  when it leads somewhere a probe may read (a compose file to another compose
  file, never to a database); a path it may not read is listed, not read.
- **Secrets redacted:** every container environment value, anything named
  like a key, token, secret, password or auth (in YAML, the whole block under
  such a key, so WireGuard and Wi-Fi keys from `netplan get` go), the value
  after a `--token`-like flag (also in a compose `command:` list), private
  keys, JWTs, passwords in URLs and the factory's own API keys. Pattern matching is not proof:
  read `snapshot.json` before you share it.

The format is DESIGN.md §28, "Capture format v0".

## Configuration reference

- Config: `~/.config/software-factory/config.json` (mode 600;
  `$FACTORY_CONFIG` overrides the path). Every key is optional and deep-merged
  over the defaults; the GUI's Settings page edits it safely.
- API keys: `~/.config/software-factory/env`, `NAME=value` lines (the `env`
  file next to the config when `$FACTORY_CONFIG` moves it). `factory` reads it
  at startup (variables already set win), and so does the systemd unit; the
  GUI writes it through `POST /api/secrets` (see [API keys](#api-keys)).
- Output: `"merge": {"push": false, "remote": "origin", "delivery": "merge",
  "output_dir": null}`. `delivery` is the default for new jobs (`merge`,
  `branch` or `folder`); `output_dir` is where folder deliveries go (null:
  `<data_dir>/output`; `~` and paths relative to the config's directory work).
- Routing: `"profiles": {name: {description, agents}}` and
  `"default_profile"`, plus model entries' `builds`, `overflow` and prices and
  providers' slot and overflow settings (see [Routing](#routing)).
- Housekeeping Tracker: `"housekeeping": {"enabled": true,
  "reinject_every_tool_turns": 3, "reinject_every_tool_turns_api": 6,
  "reinject_max_chars": 1200, "stop_guard_attempts": 2, "stale_rounds": 2,
  "followup_due_days": 14, "sweep": "regex", "max_proposals_per_run": 10,
  "max_proposals_per_scope": 300, "cli_hooks": false}` (`sweep`: `off`,
  `regex`; `model`, `sweeper_model` and `mirror_cli_todos` are reserved for
  the next version; `cli_hooks`: see [Housekeeping
  Tracker](#housekeeping-tracker)). Agent tokens live in
  `<data_dir>/auth/agent-tokens.json` (mode 600, hashes only).
- Your agents and skills stay in `~/.config/software-factory/agents/` and
  `~/.config/software-factory/skills/`, also when `$FACTORY_CONFIG` moves the config.
- Data: `data_dir` (default `~/.local/share/software-factory`) holds
  `jobs/<repo>/<id>/` (job.json, spec, build, reviews, decision, items, logs,
  sessions), `jobs/<repo>/backlog.md`, `jobs/<repo>/followups.json`, `worktrees/`, `tmp/` (short-lived merge
  worktrees), `chats/`, `plans/`, `output/` (folder deliveries, unless
  `merge.output_dir` moves them) and `factory.lock` (the running server's pid
  and URL).
- Saving settings (GUI or `POST /api/config`) keeps your file's own keys and
  adds only values that differ from the defaults, so settings you never set
  follow newer defaults.
- The full schema is in [DESIGN.md](DESIGN.md) (§11 config, §18–§25 for
  providers, models, routing, chats, plans, delivery and setup, §26 for Grid
  delivery and §32 for the Housekeeping Tracker); presets with
  explanations are in [examples/](examples/README.md). Each preset file also
  carries a one-line `description` (shown in the GUI's preset cards; never
  copied into your config).

### HTTP API

Everything the GUI does goes through a JSON API on the same port (token as
`Authorization: Bearer <token>` or the login cookie; POSTs need
`Content-Type: application/json`). The full table is DESIGN §14 plus the
"HTTP additions" of §19, §22–§25 and §32; the routes you are most likely to script:

| method | path | body / query | answer |
| ------ | ---- | ------------ | ------ |
| GET | `/api/state` | | repos, jobs, pipeline, models, drivers, agents |
| POST | `/api/jobs` | `{repo, feature, routing?, delivery?, profile?}` | `{job}` |
| POST | `/api/jobs/<repo>/<id>/<action>` | approve `{carry_items?}`, retry, cancel, rework `{note}`, reject `{reason}`, routing `{routing, profile?}` (null: the saved config) | `{job}`; approve with unnamed unproven must-do items: 409 `{error, open_blocking}` |
| GET | `/api/estimate` | `?repo=&kind=feature\|plan&profile=&routing=<preset>` | per-step tokens, cost and duration, and the basis used |
| GET | `/api/jobs/<repo>/<id>/assumptions` | | `{marked, confirmed, unconfirmed}`: the spec's (product) lines |
| POST | `/api/jobs/<repo>/<id>/confirm` | `{lines: [...]}` or `{all: true}` | `{job, assumptions}`: you confirm product decisions |
| GET | `/api/jobs/<repo>/<id>/log` | `?file=logs/<run>.jsonl&offset=` | `{lines, offset, more}` |
| POST | `/api/plans` | `{repo, request, routing?, delivery?, profile?}` | `{plan}` |
| GET | `/api/plans/<repo>/<id>/log` | `?file=logs/<NN-lead>.jsonl&offset=` | `{lines, offset, more}` |
| POST | `/api/chats` | `{repo, model?, mode?, agent?, profile?}`, `{mode: "none", agent?}` (no repository) or `{persona}` | `{chat}` |
| POST | `/api/chats/<id>/handoff` | `{feature, delivery?, profile?}` | `{job}` |
| GET | `/api/setup` | | `{configured, steps: {providers, models, routing, repos, tools, output}}`, each `{ok, detail}` |
| GET / POST | `/api/secrets` | POST `{name, value}` | key names and whether each is set; never values |
| GET | `/api/config` / POST `/api/config` | partial config | the config view (presets with descriptions) |
| GET | `/api/docs/procedures` | | every agent's working procedure and the process maps (JSON) |
| GET | `/api/docs/procedures.html` | `?download=1` | the same as one offline HTML booklet, each map's Mermaid source under it |
| GET | `/api/docs/maps` | | `[{id, title, mermaid, sha256}]`: the process maps as Mermaid source |
| GET | `/api/docs/maps/<id>.mmd` | | one map's Mermaid source as a `.mmd` download |
| GET | `/api/items` | `?repo=&scope=job\|chat\|plan\|followups\|system&status=&owner=&waiting=1&overdue=1` | every tracked item, with counts (an agent token: only its own items) |
| GET / POST | `/api/jobs/<repo>/<id>/items` | POST `{title, detail?, blocking?, owner?}` | the job's items, unparsed lines and log; a new item |
| GET | `/api/items/history` | `?ref=job:<repo>/<id>/I-5` (any item ref) | `{item, log}`: that item's every transition |
| POST | `/api/items/<action>` | accept, dismiss, verify, reopen, drop, defer, assign, block, unblock, make-job; `{ref, note?, reason?, owner?, due?}`; update `{ref, to, evidence?, reason?}` (yours: the same as the action it names, so `to: defer` moves a job's item to the follow-ups); dismiss `{refs: [...], reason}` for several | `{item}` (+ `{job}`; `{items, refused}` for several); 400 with the rule when refused |
| GET / POST | `/api/repos/<repo>/followups` | POST `{title, detail?, due?}` | the repo's follow-ups |
| GET / POST | `/api/system/items` | POST `{title, detail?, kind?, blocking?, due?}` | the machine's own items (`system:S-n`): what belongs to no repo |
| POST | `/api/items/propose` | `{scope: "system"\|"repo:<repo>", title, detail?, blocking?}` | `{item}` owned by its proposer |
| GET / POST | `/api/agent-tokens` | POST `{name}`; `/api/agent-tokens/<name>/revoke` | `{tokens, connect}`; a new token is shown once |
| GET | `/api/personas`, `/api/personas/<name>`, `/api/personas/<name>/card` | | personas, a profile card and what Open WebUI lists now |
| POST | `/api/personas/import` | `{export: [...], model_id?, snapshot?}` or `{source: "openwebui", model_id, snapshot?}` | `{card, unavailable, unknown_variables, fingerprint, snapshot}` |
| POST | `/api/personas/check` | `{persona?}` | the drift check: `{checked: [{persona, kind, version_id, item}]}` |
| GET | `/api/openwebui/models` | `?provider=` | the models Open WebUI lists for the key, workspace models first |
| GET | `/api/events` | `?since=<seq>` | the live event stream (SSE) |

## Troubleshooting

- **Start with `factory doctor`** (also the GUI's Doctor page). It checks
  Python, git, the config and token, the data dir, the sandbox, repos, agents,
  every routed provider (reachable, key present, and the configured model ids
  against its model list), the providers of builds, overflows, escalation
  rungs and profiles (and that those references resolve), which local servers
  are up and which model a TabbyAPI-style server has loaded, CLI drivers, every MCP
  server an agent uses (defined, its command found and executable; nothing is
  started; a server only Chat agents use, like the analyst's `rea`, is
  optional: "not configured" is advice and a missing command a warning) and
  every `harness.skills_dirs` entry (exists, how many skills).
  `factory doctor --probe` also sends each routed model the two test probes.
- **Tool calling fails on a local model** (the agent chats but never edits
  files, or tool calls arrive as text): run `factory models test <key>`. For
  llama.cpp, start `llama-server` with `--jinja`; some models or templates
  just do not call tools reliably, so try another model or template. For
  Ollama, raise its context (`OLLAMA_CONTEXT_LENGTH`), which is small by default.
- **HTTP 400 about `reasoning_content`** (DeepSeek and similar reasoning
  models): the reasoning must be sent back with tool calls. The builtin
  harness does this; keep `keep_reasoning` true on those model entries, and do
  not route them through a gateway that strips the field.
- **Context too small** (400s about context length, or a local server
  truncating): start the server with `-c` at least the entry's
  `context_tokens`, or lower `context_tokens` to what the server runs, and keep
  `max_tokens` within the server's limit.
- **Port in use**: set `server.port` (or run `factory serve --port 8770` once;
  the other CLI commands find it through the data dir's lock file), and use
  the same port in your SSH tunnel. "another factory is already running"
  means the data dir's lock is held: check `systemctl --user status factory`.
- **"server not running — start it with: factory serve"**: the CLI could not
  connect to the address a running `factory serve` recorded in
  `<data_dir>/factory.lock`, or (with no server) to `server.host`:`server.port`.
- **git older than 2.38** (Ubuntu 22.04 ships 2.34): everything works, but
  merge-conflict checks use a slower trial merge in a temporary worktree.
- **The user service stops at logout or a CLI is not found**: run
  `sudo loginctl enable-linger <user>`. The unit records your shell's `PATH`
  at install time; re-run `./install.sh --systemd-user` after installing new
  CLIs, or set `PATH` in the env file.

## Claude Code kit

[`kits/claude-code`](kits/claude-code/README.md) packages the same factory
line as a Claude Code skill: `/factory <feature>` runs spec-writer, builder,
the four reviewers and the approver as subagents, with job state in plain
markdown files. Use it when you live in Claude Code; use this harness for the
GUI, local models, Chat, Plans and mixed runtimes. Its gate also stops a job
whose spec marks an assumption **(product)** (a decision only you can make)
until you confirm it with `/factory confirm <job-id>`.

## Credits

- The factory-line design (spec → build → parallel review lenses → approve,
  with resumable rework) is inspired by the zazencodes software factory for
  Claude Code.
- Agent files use Claude Code's subagent syntax; skills use the `SKILL.md`
  format shared by Claude Code, Codex, Qwen Code and
  [mattpocock/skills](https://github.com/mattpocock/skills).
- No third-party code is copied into this project.

## License

Apache License 2.0; see [LICENSE](LICENSE).
