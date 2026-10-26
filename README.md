# Seraphina OS Harness

An agentic OS harness for the novice Ubuntu server user, complete with a
user-friendly GUI. AI agents do the expert work; every change has a way back.

> **Status: 0.2.0, 26 October 2026. Work in progress.** New here? Read [START_HERE.md](START_HERE.md). What changed since the 21 October preview, and what is not checked yet: [RELEASE_NOTES.md](RELEASE_NOTES.md). Nothing new in 0.2.0 has run on a real server yet; every test runs against mocks.
>
> **Working now:**
> - **Software Factory:** spec → build → four parallel reviews → approver → gate → merge, branch or folder.
> - **Per-step model routing.** Any mix of local and API models, with routing profiles, an escalation ladder, overflow, and cost per job.
> - **Housekeeping Tracker, first part.** It keeps promises, findings and acceptance criteria outside any agent's context until they are proven done.
> - **Chat and Plans**, including chats without a repository.
> - **Open WebUI personas.** Talk to an Open WebUI workspace model from the harness, with its prompt and settings applied live.
> - **An operator persona's records.** Her change receipts, read only and labelled as reported; what she did not check, on the tracker; "what did I do since X" for any agent.
> - **The GUI, for every area:** Home, Routing, the Housekeeping Tracker, Personas, Messages & Wakes, the Operator page and Backups, with the Grid Card and its wizards.
> - **A read-only `factory snapshot` server inventory.**
> - **An operator's toolkit:** a GPU custody client for the operator's register (off until its contract is confirmed), outside drift checks feeding the tracker, and `factory capture` for evidence before a fix. The Operator page and Placement show them in plain words.
> - **Unknown outcomes are never repeated.** A merge, push, message or lease with no confirmed answer is looked at again read only, or put on the tracker for you (DESIGN §40).
> - **Disk protection (SnapRAID).** A Backups area that reads the SnapRAID Daemon (its nightly run, its stops before mass deletions, your disks' health) and, after your go-ahead each time, asks it to protect now, skip a night, stop safely, bring files back or repair damage; without the daemon, a small program reads SnapRAID and protects nothing. Off until you install it and turn it on. Built and tested against a mock of the daemon; not yet run against a real one.
>
> **Being built:**
> - Wake & Messages: personas that wake themselves and message you over ntfy or Telegram, with no open ports.
> - Each new backend feature gets its GUI screen after it.
>
> **Designed for v2:** System Maintenance, Upgrades & Enhancements, Systems Research, Systems Audit, Backups (versioned backups, and disk protection through the SnapRAID Daemon: its runs, skips and restores, each with your go-ahead) and the Model Factory. See [docs/seraphina/](docs/seraphina/) and [docs/model-factory/](docs/model-factory/MODEL_FACTORY.md) (with [LINEAGE.md](docs/model-factory/LINEAGE.md), the persona lineage line, for the persona's decision).
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

Python 3.10 to 3.14, standard library only: no pip installs, no build step,
no containers required. The test suite runs on each of 3.10, 3.11, 3.12, 3.13
and 3.14.

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
- **Messages on your phone.** A job waiting for you, a failed job or a failed
  backup reaches your phone over your own Tailscale network (ntfy) or as a
  detail-free Telegram heads-up, built by code, held during do-not-disturb
  time, with Got it, Snooze and Stop buttons. Personal and health text never
  leaves the machine. See [Messages on your phone](#messages-on-your-phone).
- **Seraphina, an assistant that checks in.** A persona on a model on this
  machine wakes itself every couple of hours, looks at what waits for you
  (jobs, the tracker's promises, reminders) and tells you briefly when
  something needs you; you can write to her, set reminders, and let a coding
  chat schedule its own check-ins. Code decides every action and what reaches
  your phone. See [Seraphina](#seraphina-an-assistant-that-checks-in).
- **An operator persona, if you want one.** Mark one persona as the operator
  of your server: she acts with her own tools as root, outside the harness,
  and writes a change receipt for each change. The harness reads them (read
  only), labels them "reported by the operator", puts every check she did not
  do on the tracker, and answers "what did I do since yesterday" from its
  files. See [An operator persona](#an-operator-persona).
- **An operator's toolkit.** For that operator the harness is a toolkit,
  never a gate: a **GPU custody client** that asks the operator's register
  before any local call that could load or swap a model (off by default),
  **drift sources** whose findings become tracker items that close only with
  evidence, and **error capture**:
  every failed model or tool call keeps its status, response body, request ids
  and exact times, and `factory capture` bundles the logs of a time window
  before anything is fixed. See [Operator toolkit](#operator-toolkit-gpu-custody-drift-sources-capture).
- **No blind retries.** Every call that changes something outside the harness
  ends succeeded, failed or unknown, and an unknown one is never repeated by
  itself: the harness looks again, read only, or puts it on the tracker for
  you. See [When the outcome is unknown](#when-the-outcome-is-unknown).
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

Ubuntu 22.04, 24.04 or 26.04 (their own `python3`: 3.10, 3.12 and 3.14), as
the user who will run the factory (for example a dedicated `ai-dev` user). No
sudo needed. Never run the factory as an account in the `docker`, `lxd` or `sudo` group,
such as your own login account on many home servers: those groups already
give root, and the sandbox cannot take all of it away (see
[Security model](#security-model)). `factory doctor` warns when it does.

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
   Add `--credential <name>:<path>` (repeatable) to give the unit a
   `LoadCredential=` line for a token the harness reads as a systemd
   credential (see [GPU custody](#gpu-custody-off-by-default)). Running it
   again keeps those lines; `--drop-credential <name>` removes one.
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
factory doctor --look-around   # also ask each program listening here whether it is a model server
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
- The presets' addresses (Tabby on 127.0.0.1:5000, Strata on 127.0.0.1:18080)
  and that key file are placeholders: if your servers run elsewhere, set each
  provider's `base_url`. Setup says which usual ports listen on this machine
  (it only reads that; it sends nothing). To find a server on another port,
  press **Look for model servers** on the setup page's **Server details** step
  (the Doctor's "local servers" line links to it), or run
  `factory doctor --look-around`: it asks each program listening on this
  machine whether it is a model server (one `GET /v1/models`, with no key; it
  changes nothing), and where a usual port is silent, setup then offers the
  model servers that answered by their address. It runs only when you ask:
  never when a page loads or on a timer, and at most once in 30 seconds and
  10 times an hour (a press too soon says when you can look again). When a
  configured local server is down, `factory doctor` says how to look. Without `/etc/ai-stack` on this machine, setup leaves
  that file out and you put `STRATA_API_KEY` in the env file.
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
reason?, independent_alternate?}` in the config's `agents` section (`*` is the
default model and driver, and takes none of the last four; an agent without a
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
"..."}`; kinds: capability, time, stage-fit, cost); `independent_alternate`
(reviewers and the approver only) names a model of another family the judge
runs on when the builder runs on its family, or is `false` when you accept
that judge on the builder's family (below). A run that overflows,
escalates or moves to its alternate shows its own reason instead (`time:
overflow from flash-next: ...`, `capability: escalation rung 1 of 2 ...`,
`stage-fit: independent alternate: ...`).

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
- **Independence guard.** Escalation, an overflow or a profile can put the
  builder on a judge's model family (a judge is a reviewer or the approver).
  Before a judge runs, the factory compares its model's family with the
  families the builder ran on: a reviewer with that round's, the approver
  (which decides on the whole branch) with every round's, and a chat
  hand-off's code with the models the chat ran on. On a match the judge runs
  on its `independent_alternate` instead, and the run's reason says so; the
  same happens when the judge's own model is down and its overflow is of the
  builder's family. With no way out (no alternate, one of the builder's
  family too, or one that does not answer: on a server under [GPU
  custody](#gpu-custody-off-by-default), one the register gives no lease) the
  run goes ahead, and the run's
  event, its `job.runs` entry and the job carry `independence: "reduced"`
  with the shared model named, so the GUI can show a badge; with
  `"independent_alternate": false` they say `"accepted"` instead (you chose
  it, say for a local security lens whose findings stay local), and a
  hand-off whose chat recorded no models says `"unknown"`. A family is a
  model entry's `family` label (`"family": "openai"`), else the model name in
  its id: `openai/…` is openai, `claude-…` anthropic, `gemini-…` and `gemma-…`
  google, a `…Qwen3.8…` fine-tune qwen, and a hub or quantizer namespace
  counts for nothing (`hf.co/<user>/Qwen2.5-…-GGUF` and `unsloth/Qwen3-…` are
  qwen; a `-gptq` suffix is not GPT). Label an entry whose id names no family
  (a server alias such as `strata`). `factory estimate` and `GET
  /api/estimate` price each judge where the guard puts it while the builder
  is on its own model, and list, for each model the builder can run on,
  which judges would move, which would run reduced and which share its
  family by your choice.

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
spec, the reviewers, the approver or the lead, and their independent
alternates keep the judges off the builder's family once it escalates
(GPT-6.1 Sol builds, Muse Spark 1.3 reviews the code; in `best` every model
the builder runs on is Claude, so GPT-6.1 Sol xhigh approves instead of
Opus; the local security lens keeps its fine-tune with
`"independent_alternate": false`, so its runs say the shared family was
accepted, see DESIGN §23).
**Estimate** before a run:
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
their prices from the dated list in `examples/prices/`. The `nanogpt` preset
uses the ids NanoGPT's public list gives (`anthropic/claude-opus-5.5` and so on);
`nanogpt-value` and `local-first` reach Claude through NanoGPT's Anthropic-format
route with the dashed `claude-opus-5-5` form, which is unverified. Whether your key
may use an id is unchecked until `factory doctor` reads your key's list: it warns
for any id NanoGPT does not list. Two more files there are
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
  Some servers (Ubuntu 24.04 and later, which restrict user namespaces) allow
  no sandbox inside the sandbox: the browser's own sandbox cannot start inside
  bwrap there. `factory doctor` says so ("sandbox inside the sandbox"), and the
  agents are told to check pages with curl instead. Do not loosen the server's
  user-namespace restriction to make the browser work.
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

A chat in worktree mode on the builtin driver can also be allowed to schedule
its own **check-ins** ("look at CI in 20 minutes") under the sandbox, one per
reply; a check-in can only read and run a few status commands (see
[Seraphina](#seraphina-an-assistant-that-checks-in)). A check-in that is still
waiting when you hand the chat off stays an open item until it has run.

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
prompt, never its coding-agent instructions (her profile card shows it word
for word as `appended_text`; a `harness_note:` line in the persona file
replaces its sentence, `harness_note: none` drops it), and on every message: your local
date, time, weekday, time zone and language (as Open WebUI's browser page
would), her default filters as `filter_ids` (`filters: false` in the persona
file turns them off), no `max_tokens` of its own, so hers applies, and never
`tool_ids`, `chat_id`, `id`, `session_id` or `features` (whatever a models entry
says). Her **knowledge** is off unless your
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
Open WebUI. If it really is external, your words leave the machine there. The
**Local** label alone is not proof: a relay to a hosted service (a proxy signed
in to your subscription, in a container on this machine) can carry it too. So
the harness believes it only for a model id one of your own local servers
serves (a models entry on a provider at 127.0.0.1); for any other id it warns
that your words may leave the machine there. It compares names only, because
Open WebUI does not show where a connection points to an ordinary key: never
give a relay's models the same names as your own server's.

**Which Open WebUI build runs.** Open WebUI reports its version (0.12.3), but a
local or customised build reports the version it started from, and a code file
bound into its container changes what runs without changing the image. Record
the build whose persona tests passed as `reviewed_build` on the Open WebUI
provider (`{"image_id": "sha256:…", "code_binds": {"/app/backend/open_webui/…":
"<sha256>"}, "config_hash": "…"}`). `factory doctor` shows the line
"openwebui … build": today it says **not checked**, because reading what runs
needs a root helper the harness does not have yet; it never says "ok" from the
version number.

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
is no change. With Seraphina's check-ins on (`wakes.enabled`) it runs daily by
itself; otherwise run it daily from a timer if you like. Without `OPENWEBUI_API_KEY`
these calls answer 400 (nothing was asked of Open WebUI); 502 means Open WebUI
did not answer or refused.

**What runs where (the capability map).** The card never claims that a
harness chat is the persona's own chat. Its `capability_map` lists, per
surface, what the persona has and what it does not: in its **own Open WebUI
chats**, everything Open WebUI lists (its Python tools, tool servers, skills,
filters, knowledge); in a **harness chat**, the prompt, settings, skills and
always-on filters applied live (knowledge only with `knowledge: true`), the
persona file's own `tools:` line (nothing with `tools: none`), and never its
Open WebUI Python tools; in an **offline copy**, the copied prompt and
settings; in a **browsing chat** (a separate persona file with `mcp: browser`,
when you add one, and `mcp_servers.browser` configured: until then the map
says the surface is not set up), the browser tools only; in a **wake turn**, the imported
prompt, plus read-only tools when `wake.tools` is `read_only` (without RunCheck
when `wake.commands` is `[]`). Open WebUI's own **built-in memory** has a row of
its own: it is there in her own Open WebUI chats when her account turns it on,
and on no harness surface, because the harness sends no `features`, `chat_id`,
`session_id` or `tool_ids` (the harness side is held by code and a test; that
Open WebUI then adds no memory is read from its source, not checked on a live
server). Two routes
connect them: act on results in the persona's own Open WebUI chat, where its
tools are, and have code written through a factory job. Adding or changing
harness personas writes nothing on the Open WebUI side and leaves every other
persona file's `tools:` line alone; a test holds the harness to that.

**Changes before import.** At the first import the harness has no earlier copy
of the profile, so that import is the **baseline**: there is no diff of what
changed before it, and drift diffs start after it. The card (`GET
/api/personas/kate/card`, and a wake copy's page) says so in its `import`
block. What changed before the import is listed only from the operator's
receipts that you or she attach by id: `factory personas before-import kate
--attach <receipt id>` (or `POST /api/personas/kate/before-import {"attach":
["<receipt id>"], "by": "owner"}`). Each shows its time, intent, how it ended
(a receipt that says its outcome is unknown never shows as verified),
`not_verified` and `verified`, labelled "reported by the operator". An id the harness has not
read is refused; the harness never picks receipts by guessing from their text.
Her profile card page (Personas) and a wake copy's page show this as **Changes
before import**: the baseline sentence first, then each attached receipt as she
reported it (never green or ticked), with **Attach receipts…** (ids typed or
ticked in her receipts list, and who attaches them) and **Detach**.

**Offline copy, only if you ask.** Adding `"snapshot": {"name":
"kate-offline"}` to an import also writes a persona that runs a copy of Kate's
prompt and settings on one of the harness's own local models, for when Open
WebUI is down. It is labelled "Kate, imported snapshot of <date>, version …",
and so is every reply it writes; it is never used automatically, warns when the
live Kate has changed, and has none of her knowledge, filters, skills or tools.
The import turns it on (`agents.kate-offline.offline: true`); a persona file
that merely says `offline: true` is not enough, and a chat with a copy whose
setting is off is refused. When her base model matches none or
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

With `"merge": {"push": true}` the base branch is pushed to `merge.remote`
after the merge (never forced), and the job keeps what the remote answered
(`push` in its job.json). A push with no answer is looked at again; see
[When the outcome is unknown](#when-the-outcome-is-unknown).

## Messages on your phone

The harness can tell your phone when something needs you: a job waiting for
your decision, a failed job, a plan waiting, a failed backup. It needs no model:
code builds every message from fixed templates (DESIGN §34;
`docs/seraphina/WAKE_AND_MESSAGES.md` is the full design, step W1). An assistant
that wakes itself and writes to you is the next section.

**The rule.** Your personal and health data never leaves the machine, and the
phone counts as off the machine. Every message lands in the cockpit's inbox
first, in full. What a phone gets is decided by code:

- **Your private channel**, a small [ntfy](https://ntfy.sh) server on this
  machine that your phone reaches only over Tailscale: "A job (003) is waiting
  for your decision. See it in the cockpit (Jobs, then 003)." Project names are
  hidden unless you mark a project safe to show (`notify.safe_repos` and the
  channel's `operational_names`).
- **Telegram** (optional, works anywhere): fixed heads-ups with no details,
  "A job is waiting for your decision. Open the cockpit." Telegram reads every
  word it carries and sees when messages come. Only if you set
  `"operational": true` on the Telegram channel does it get the job numbers,
  steps and (with `operational_names`) the safe project names that the private
  channel gets; it never gets an assistant's own words.
- Anything with a link, formatting, a long number, something that looks like a
  token, or one of **your sensitive words** goes out only as a heads-up. Write
  those words (health conditions, names, places), one per line, in
  `~/.config/software-factory/sensitive-terms.txt` (mode 600) or through
  `POST /api/notify/terms {"text": "..."}`; `{"none": true}` tells the doctor
  you have none. Edit the file in the cockpit: a hand edit counts as a settings
  change outside the cockpit (below).

Each phone message says why it came ("Job 003 has waited since 11:10").

**When it is not sure a message arrived.** A message the server confirmed is
*sent*. One the server refused, or that never left, is *failed* and is tried
again later (on Telegram, only for the accounts that did not get it). One
that may have arrived is *unknown*: no answer came after sending, or the
server answered with an error of 500 or more. The harness never sends an
unknown one again by itself; the inbox offers **Send again**
([below](#when-the-outcome-is-unknown)).

**Before you turn it on: the sandbox.** Your AI agents run as the same Unix
user as the harness. Without bwrap they could read the message tokens, your
master token and anything you write, and send it anywhere. So phone channels
stay off while `harness.sandbox` is `none` or an agent route uses a CLI driver,
unless you type the acknowledgement (`POST /api/wakes/acknowledge {"text": "I
understand my AI agents can read my messages and send them anywhere"}`; the
doctor keeps reporting it as an error). Under bwrap, agents' commands cannot
read your config directory (hidden whole, with your skills bound back
read-only: `config.json`, the env file, personas), or the message data, not
even while a setting or a key is being saved. A sensitive-words file kept
outside the config directory is hidden by a bind over that one file: change it
in the cockpit (it is rewritten in place), because an editor that saves by
renaming a new file into place makes it readable to an agent's command that is
already running. Start the service **without an SSH agent** in its environment:
the harness removes `SSH_AUTH_SOCK` from agents' commands and hides the usual
SSH and GPG agent sockets from them (`$XDG_RUNTIME_DIR/gnupg`, `keyring`,
`gcr`, `ssh-agent.socket`, `~/.gnupg/S.*` and wherever `SSH_AUTH_SOCK` points),
so a commit an agent makes inside the sandbox is not signed; keep the agent out
anyway. **Exclude `<data_dir>/wakes` and `<data_dir>/notify` from any backup
that leaves the machine**: they hold your messages. `"notify": {"enabled":
false}` turns all of this off: nothing is sent, nothing listens, nothing
answers.

**Set the time zone and your power schedule** (`wakes.timezone`, an IANA name
such as `Europe/London`, and `wakes.power` if your server does not power off at
23:00 and come back at 07:00). Do-not-disturb time follows the schedule (21:30
to 08:00 by default, `notify.quiet_hours`): messages wait, and you get one quiet
evening summary and one morning summary, in counts only. A failed backup or a
stopped service still buzzes (`notify.quiet_breakthrough`). At most 12 messages
a day reach the phone (`notify.phone_max_per_day`); urgent alerts always do.
`factory doctor` compares the machine's wake alarm with your schedule ("wake
alarm"): the morning after the clocks change, an alarm set with the evening's
offset wakes the server an hour early (in autumn) or late (in spring), and the
doctor says so.

**The private channel (ntfy over Tailscale).** Add the channel to the config
(`POST /api/config`):

```json
{"wakes": {"timezone": "Europe/London"},
 "notify": {"channels": {"phone": {"type": "ntfy", "publish_url": "unix:/run/ntfy/ntfy.sock",
   "public_url": "https://<host>.<tailnet>.ts.net:8445", "token_env": "SERAPHINA_NTFY_TOKEN",
   "reply_token_env": "SERAPHINA_NTFY_REPLY_TOKEN"}}}}
```

Then `POST /api/notify/ntfy/prepare {"channel": "phone"}`: the harness makes the
tokens (into its env file), a passphrase for your phone (shown in that answer
only) and the ntfy settings, and answers with three copy blocks you run once
with sudo yourself (never let an admin worker run the steps that create
passwords): install ntfy, install its settings and daily limits, serve it on
your tailnet on port 8445 (Tailscale Funnel cannot use that port). No token is
ever printed in a terminal. `GET /api/notify/ntfy/check?channel=phone` checks
it (ntfy should not answer on port 80; another program there, such as your web
server, is fine and is said so). In the ntfy app, first add the user `owner-phone` with the passphrase for
`https://<host>.<tailnet>.ts.net:8445`, then subscribe to `seraphina` and, on
Android, to `seraphina-chat` (name it "Write to Seraphina" and mute it). Then
`factory notify test` and tap **Got it**. The channel counts as private only
when its address is on your tailnet and its server forwards nothing.
`public_url` is a plain `https://host:port` and `publish_url` a `unix:/path` or
`http://127.0.0.1:<port>`: anything else is refused, since both go into the
files and commands you install as root. A notice links to the cockpit only when
`notify.cockpit_url` is on this tailnet (or loopback).

**Telegram.** Make a bot with @BotFather (a name that says nothing about you),
then `factory secret set SERAPHINA_TELEGRAM_BOT_TOKEN --stdin` and paste the
token (it is never shown or put on a command line). Add `"telegram": {"type":
"telegram", "token_env": "SERAPHINA_TELEGRAM_BOT_TOKEN"}` under
`notify.channels`. Pair in two steps: `POST /api/notify/pair {"channel":
"telegram"}` gives a link and a `/start <code>` message to send the bot; the
bot answers with a 6-digit code, which you type into `POST
/api/notify/pair/confirm {"channel": "telegram", "code": "123456"}`. Only that
account can then talk to the harness. Turn on Two-Step Verification in Telegram.
The bot's description tells you when your server sleeps (from `wakes.power`,
updated when you change it).

**What you can send back.** Buttons: Got it, Snooze 1 h, Quiet until morning
(Stop on an assistant's messages). Typed, as the **whole** message: `stop`,
`snooze 1h|3h|morning`, `quiet 3h|morning`, `status`, `help`. "Stop worrying
about the backup" is just text, kept for you in the cockpit. Commands sent while
the server was off are shown in the inbox, not applied (except `stop`); a
Telegram tap that waited while nothing listened counts as late too, one made
after the server is back does not. Answers (`status`, `help`, "Did you mean
...?") come back as quiet messages and are in the inbox too; while a settings
change waits for your review, only `stop` works.
Nothing from the phone can approve, merge, restart, pair or change a setting:
do that in the cockpit. **A real message never asks you to approve anything,
type a password or code, or open a link. If one does, it is fake: send stop and
tell an admin.** A message on your phone that the harness did not send, or a
fake button tap, is reported on your other channel and in the inbox.

**Stop.** `stop` from the phone, `factory wake pause` or `POST
/api/wakes/control {"action": "pause"}` stops Seraphina (the `PAUSED` file in
`<data_dir>/wakes/`) and confirms it silently on every channel, once: another
`stop` only answers "already stopped". Alerts from your server keep coming.
Restart only in the cockpit or with `factory wake resume`.

**Backups and other event sources.** `POST /api/notify/hooks {"name":
"backup-script", "events": ["backup.failed", "backup.ok"], "subjects":
["nightly"]}` makes a token for your backup job (shown once; the harness keeps
only its sha256) and answers with snippets: a root-only header file for the
token, systemd `OnFailure=` and `OnSuccess=` handlers, a cron wrapper. A
failure alerts once (and buzzes at night unless you said not to), reminds
after 6 hours at most 3 times, and the next success sends "working again".
`factory notify event backup.failed --subject nightly` reports one by hand.
An event source may also send `parity.report` (the SnapRAID Daemon's
forwarder, see Disk protection): it makes no notice by itself; Seraphina reads
the daemon and decides from what it says.

**If something leaks.** `POST /api/notify/channels/<name>/leaked` (Telegram:
with the new token from @BotFather's `/revoke`; ntfy: new tokens and a new
copy block) and `POST /api/notify/lost-phone` (unpairs Telegram, turns the
private channel off, makes new phone credentials and stops Seraphina).
Settings changed outside the cockpit (a hand edit of the `notify` or `wakes`
sections, the pairing, the sensitive words, the reminders, the notice rules or a
clean confirmation) stop Seraphina and hold every
phone message but one alarm until you review them (`POST
/api/wakes/review/accept`). Everything is in the audit log,
`<data_dir>/wakes/audit-YYYY-MM.jsonl`; message text is kept 90 days.

`factory doctor` checks all of this, from the sandbox to whether Funnel is off.
The cockpit's Messages pages (inbox, phone setup) come with the next GUI step;
until then use the routes above and the CLI.

## Seraphina: an assistant that checks in

Seraphina (`personas/seraphina.md`) is a persona that wakes itself every couple
of hours, looks at what the harness knows (jobs waiting for you, promises in the
tracker, reminders), and tells you briefly when something needs you; most of the
time she decides nothing does. You can also write to her, and she can remind you
later. Design: DESIGN §35 and `docs/seraphina/WAKE_AND_MESSAGES.md` (steps W2,
W2b, W3).

**What keeps her safe.** She runs only on a model **on this machine**, which you
confirm once per model server (`POST /api/wakes/servers/<provider>/attest
{"local": true}`); hosted models, `:cloud` names, Open WebUI and proxies are
refused (the harness asks what answers at the server's address before each
check-in, as `factory doctor` does). She waits for people: she never takes the model while you or a job
uses it, gives it back the moment someone needs it, and never wakes a sleeping
model or loads one. Each check-in is one short model call with **no tools**,
answering from a fixed menu (tell you, answer you, wake me later, track a
promise, remember a fact, or politely decline); code decides what is sent. What
she reads from outside (a poke, a hook, a chat) is marked untrusted, and a turn
that read your words or her memory is private: neither ever reaches your phone
as more than a short heads-up. Her budgets, quiet hours and the stop switch
from the previous section apply; five failed check-ins in a row, or more than 20
in an hour, stop her until you restart her.

**Turn her on.** She needs the sandbox (`harness.sandbox: "bwrap"`), a time
zone, and a model:

```json
{"harness": {"sandbox": "bwrap"},
 "wakes": {"enabled": true, "timezone": "Europe/London",
           "personas": {"seraphina": {"enabled": true, "model": "<a local models key>"}}}}
```

Her instructions are hers to keep plain: the shipped text holds nothing about
you, so her own words may reach your private channel. If you edit her file by
hand she stops until you look at the change (`POST
/api/personas/seraphina/integrity/approve`, or `/restore`); after an edit in the
cockpit she is private again until you tick that the new text holds no personal
or health details (`POST /api/personas/seraphina/privacy {"what": "soul",
"ref": "<file_sha256>"}`). For her first 7 days her messages are silent.

**Your choices** (per persona, under `wakes.personas.<name>`):

- `memory.curation`: `"owner"` (you edit her memory summary and accept her
  weekly tidy-up) or `"self"` (she keeps it herself; every change is recorded
  with what it was before and why, and you can read and roll back any version).
  Nothing ever overwrites an edit of yours: a tidy-up proposed before you
  edited can only be rejected (the next one starts from your text), and her
  own rewrite is refused if you edited while she was thinking.
- `memory.external`: if her memory lives in her own tools (a notes folder, a
  SQLite file), the harness only reads it, from folders you list in
  `wakes.external_memory_roots`, and puts anything she wants to remember in an
  outbox file her own tools pick up. It checks the paths again each time and
  never follows a symlink or a hard link there, so a swapped file cannot make it
  read or append to anything else. She then has that one memory: no summary in
  the harness, even with `curation: "self"`. The SQLite `query` is hers: the
  harness assumes no table or column of her store: a query that fails (say,
  one naming a column her store does not have) is never read as an empty
  memory. Her wake is told her database could not be read, and `factory doctor`
  runs the query once, read-only, and says whether it works. One read opens at
  most 100 of her newest notes and 1 MiB in all. **Never open up her live
  SQLite store for the harness**: an ACL on the database or its folder, or a
  looser mode, can break the store. Point `sqlite.path` at a read-only snapshot
  her own tools publish; if there is none, leave `sqlite` out and the harness
  reads only her notes folder. The harness cannot tell a snapshot from a live
  store, so this one is up to you; when it cannot open the database,
  `factory doctor` says the same.
- `memory.external.ledger`: `true` to keep a **read ledger** (off by default; a
  proposal for the operator to approve). Because the harness never writes her
  memory, it records what it reads instead: each read adds one line to
  `<data_dir>/wakes/personas/<name>/external-reads.jsonl` (time, wake, each
  file's name, sha256, size and modification time; for the database, the row
  count and a sha256 of the rows read, or the error when it could not be read),
  and each file version not seen before is copied to `external-versions/`
  beside it (private, hidden from agents). The ledger keeps 32 MiB at most,
  copies and manifests together; when it is full, the oldest copies go first
  and the newest version of each file stays, and `factory doctor` tells you.
  Retention removes it after `wakes.retention.text_days`. Every read is
  recorded, also her wake's own Read of a note (Grep does not search her
  memory while the ledger is on); if a read cannot be recorded, it does not
  happen. `factory personas ledger <name>` lists the versions per file;
  `--show`, `--diff` and `--restore` show one, compare two and prepare a
  restore: the harness gives back the earlier text, and her own tools (or you)
  write it. `--erase` removes one version's copy for good (the ledger keeps
  only its fingerprint, and never copies it again); her own note is not
  touched. The same is on her page's **Memory** tab (Messages & Wakes), marked
  private: each file with its versions, **Show**, **Compare with newer**,
  **Prepare a restore** (the text to apply, never applied by the harness) and
  **Erase this version** (it asks first). Persona setup has the switch "Keep a
  read ledger of her memory" when her memory is external.
- `wake.tools`: `"none"` (default) or `"read_only"`: read files in
  `wake.read_paths`, run local status commands in `wake.commands` (`git status`
  and `git log` by default; they run with no network, so `gh` is refused there),
  list jobs; never write. Even inside `wake.read_paths`, the harness's own
  files and every folder hidden from agents stay unreadable, with one exception
  for the operator: her own wake may read her receipts directory
  (`operator.receipts_dir`) when `wake.read_paths` holds it, because she ruled
  that her wake may read her change receipts. Any other persona's wake is
  refused there. Needs bubblewrap, and anything such a check-in sends is a
  heads-up only. With `wake.commands: []` she runs no command at all ("a wake
  reads; it doesn't poke"): she reads files and the harness's own status (jobs,
  her open items, and for the operator her GPU custody register's residency),
  and RunCheck is not even offered. Leaving the key out (or `null`) keeps the
  default; `[]` is the only way to say "no commands". Persona setup asks it as
  a choice: **Default** (`git status` and `git log`), **No commands** or
  **These commands** (an emptied list is never saved as "no commands"); her
  page says what she may run now, for `[]` "She reads files and harness
  status; runs no commands". With the read ledger on (above), her commands
  run only inside the sandbox (bwrap): without it she runs none, and her page
  and persona setup both say "Her commands are off" and why.
- `reimport_decider`: for a persona imported from Open WebUI, who decides a
  re-import when her profile changes there (`"owner"` or `"persona"`); it is
  never automatic, and she always knows she runs from a copy. To make one from
  a workspace model, send its Export file with a new name: `POST
  /api/personas/import {"name": "kate-wake", "export": [...], "model_id":
  "kate"}` (or `"source": "openwebui"` with your key, used for that one call).
  She starts private and is never a chat partner; Kate herself stays a chat.
- `instance_name`: the name she signs with; the harness's own is
  `harness.instance_name` ("Seraphina OS Harness" by default). Every audit line,
  event, tracker change and message names both.

**The notice rulebook.** Quiet hours, daily limits and what may reach a phone
are kept in one file, `~/.config/software-factory/notice-rules.json`, which the
harness and any notifier of your persona's own both read (agents can read it,
never change it). Change it in the cockpit (`POST /api/notify/rules`); limits can
be made stricter, never weaker. Until you confirm a persona's profile holds no
personal or health details, anything from her reaches your phone only as a
fixed short notice.

**Talking to her, and reminders.** `POST /api/personas/seraphina/messages
{"text": "..."}` (or free text from your phone) starts a turn; her answer is in
the cockpit, and your phone only hears that she answered. `POST
/api/wakes/reminders {"at": "2026-10-22T17:00:00Z", "text": "Call the
plumber"}` is a reminder in your own words, delivered by code with no model;
one asked for while the server is off says so and arrives late, after boot.
`POST /api/wakes {"persona": "seraphina"}` runs a check-in now.

**Pokes, hooks and chat check-ins.** `factory wake poke seraphina --reason
"..."` (works without the server) or an event source allowed to wake her
(`POST /api/notify/hooks {"name": "...", "may": ["wake"], "personas":
["seraphina"]}`, then `POST /api/hooks/wake`) asks her to look. A coding chat
can schedule its own check-ins ("look at CI in 20 minutes") once you turn on
`wakes.chat_wakes.enabled` and allow that chat (`POST /api/chats/<chat>/wakes
{"allow": true}`): one check-in per reply; a check-in can only read and run a
few status commands (`wakes.chat_wakes.commands`; `[]` for none, and then the
check-in is told it runs no commands; Persona setup offers Default, No
commands or These commands), waits for people like she does, and is
skipped if the sandbox, worktree mode or the builtin driver is gone by the time
it is due.

**Stopping her.** `factory wake pause --persona seraphina [--until 18:00]`
(18:00 in `wakes.timezone`; it holds even when you run it with the server down),
`stop` from your phone, or the cockpit; `factory wake resume --persona
seraphina` to restart. Stopping Seraphina also stops a check-in a chat is
running. A restart of the server in the day changes nothing: only what came due
while it was down is merged into one catch-up. `factory doctor` checks her model, her file, the
sandbox and the rulebook. The cockpit's Schedule and Personas pages come with the
next GUI step.

## An operator persona

You can mark one persona (an Open WebUI profile or one that wakes itself) as
the **operator** of your server: an agent you trust with root, who acts through
her own tools outside the harness. The harness never blocks or approves what she
does. It is her toolkit, and it says plainly what it knows: only what she
reports.

```json
"agents":   {"ops": {"role": "operator"}},
"operator": {"receipts_dir": "/path/she/writes/receipts/to"}
```

Only your config marks her (a `role:` line in a persona file means nothing), and
one operator at most unless you list several in `operator.several`. A factory
agent (`builder`, …) cannot be marked, and a mark on a name with no valid
persona file holds nothing until there is one (`factory doctor` says why).

Give her receipts directory to her own user, readable by the harness's user
but **not writable** by it, and keep it apart from the harness's files (its data
directory, config directory and repositories; the harness refuses those). If
the harness's user could write there, a file one of its agents put there would
read as hers: `factory doctor` reports it as an error, and her items from such
a place are marked untrusted.

- **Change receipts.** For every change she writes one JSON file into
  `operator.receipts_dir` (schema `operator-receipt/1`, or the operator's own
  `kate-change-receipt/1`): id, time, actor, intent, undo class, jobs, before,
  after, rollback, `verified` and `not_verified`. The harness reads them and
  never writes there; it refuses symbolic links, hard links and files over the
  size limit, and records a file it cannot read (nested too deeply, `NaN`, a
  time it cannot parse …) as "cannot read", never as an empty receipt; one bad
  file never stops the others being read. Past `operator.max_receipts` files
  the newest are read and the older ones listed as not read. Her undo classes
  are mapped onto the harness's (`undo-restore`
  → `undo_by_restore`, …) with her own word kept. An empty `verified` never
  reads as success, and `not_verified` is always shown first. Fields a schema
  does not have are kept as data, never read.
- **How a change ended.** A receipt in `operator-receipt/1` may also say
  `"outcome": "succeeded" | "failed" | "unknown"` and, for a commit, the other
  side's answer: `"response": {"status": 201, "ids": ["…"], "body_sha256":
  "…"}` (what the server returned, not what she meant to do). An unknown
  outcome becomes a suggestion on the tracker, "Outcome unknown: check before
  retrying (receipt <id>)", until a newer version of that receipt says how it
  ended; then it waits for your check. Her own schema stays as she wrote it:
  an outcome there is kept as data.
- **What she did not check stays visible.** Each `not_verified` entry becomes a
  suggestion on the tracker, owned by her and linked to its receipt, one per
  check however often it is reported again (and whether you mark her before or
  after her first receipts), until a later receipt reports it verified; it then
  waits for your check. Your dismiss, verify or reopen stands until she reports
  something newer, even if the harness loses its index. Never shown to outside
  agents.
- **What did I do since X.** `factory activity --agent ops --since 24h` (or
  `GET /api/activity?agent=ops&since=24h`) lists her receipts, tracker changes,
  messages and wakes audit lines and runs in time order, and what the harness
  asked of her [GPU custody register](#gpu-custody-off-by-default) and what her
  [drift checks](#drift-sources) found, each with the file and line it came
  from, so it still works after her own memory was compacted. It works for any
  agent (`factory activity --agent builder --since 7d`, with the custody leases
  its own runs took). A long list ends with the `--after` value that continues
  it. In the cockpit, **Activity** on each persona's and agent's page opens
  the same list for the last 7 days.
- **Her open items, every turn.** Her chats and wake turns start with her open
  items on the tracker, as factory agents get theirs (without an Items tool:
  you check and close them).
- `factory operator` shows who is marked and the last scan; `factory operator
  receipts` lists them. The **Operator** page in the GUI shows the same.

The harness's guarantees (no agent holds root, changes only through the
executor, approval codes, the build phases) are for its own agents. An
operator's own actions are outside them: the harness records them as she
reports them. See [DESIGN.md §36](DESIGN.md#36-operator-personas-receipts-activity-and-what-the-harness-guarantees-added-v1x-slice).

## Operator toolkit: GPU custody, drift sources, capture

For the [operator persona](#an-operator-persona) the harness is also a
toolkit for her server: it asks her GPU custody register before a local call,
reads her drift checks onto the tracker, and keeps the evidence of a failure.
It never gates her own actions here either; these parts shape only what the
harness's own agents do. DESIGN.md §37 to §39 are the contract.

### GPU custody (off by default)

The operator owns a GPU custody register: which models are resident on which
local server, and who may load or swap one now. The harness never mutates the
register's state directly. A POST /leases is a request that the register
adjudicates, records and answers; the ledger is the operator's. The harness
runs no broker of its own. Name the register on each local provider it covers:

```json
"providers": {
  "tabby": {"allow_model_swap": true, "swap_back": true, "overflow_after_seconds": 120,
            "custody": {"url": "http://127.0.0.1:<port>", "token_credential": "<credential-name>"}},
  "llamacpp": {"lazy_load": true, "custody": {"url": "http://127.0.0.1:<port>", "token_file": "~/.config/custody.token"}}
}
```

- **Which calls need a lease:** every call to a provider with custody that
  loads its model on the first request (`lazy_load`) or loads whatever model
  a request names (`allow_model_swap`). Job steps, chats, the plan lead,
  repo-scout, swap-backs, model Test probes and wake turns all take one, with a
  purpose the register can show.
- **Custody follows the server.** Another provider entry for the same server
  (say one with a longer timeout), or an older job's snapshot of a provider
  renamed since, carries the same register and shares the same lease. Give
  custody to one entry per server, or the same custody to each.
- **A denial is an unavailable build.** A refusal, an error, a timeout or an
  unreachable register makes that build unavailable, so the run goes to the
  next build or the overflow you configured. Nothing bypasses custody: a call
  to such a provider without a valid lease is refused before it is sent. A
  judge's independent alternate on such a server needs its lease too: without
  one the judge stays on its own model and says its independence is reduced.
- **HELD waits, FULL does not.** The register answers with a reason code.
  HELD means someone else holds the card: a job step, repo-scout, a chat turn
  or the plan lead waits for the hold to end, asking again every 5 seconds,
  for at most the provider's `overflow_after_seconds` (no setting, no wait),
  then goes to the next build or the overflow. FULL means the card cannot fit
  the model now, whoever holds it: waiting cannot help, so the run goes at once
  to the model's next build. List another card or a smaller build in the
  model's `builds` for FULL to go to, and an overflow after them. A run that
  overflowed because every build was FULL says "the card cannot fit this model
  now" as its reason; `factory doctor` warns of a model with nowhere else to go.
- **One wait per run.** However many builds answer HELD, a run waits once in
  all: a later HELD answer gets only what is left of the first wait, and none
  after a slot wait that ran out. The builds are asked in order, so a run
  waits for a held first card before it tries the next one, even if that one
  is free. LEASED (another request's lease covers the card) does not wait.
  Whether these two should change is the operator's to decide (DESIGN.md §37,
  Open).
- **Careful by rule.** Residency is read before every lease; an answer older
  than one already seen (its generation), or past its `until`, is refused;
  releasing a lease never counts as free VRAM (residency is read again); a lost
  connection, an answer of 500 or more, or a model's answer cut off mid-stream,
  is "outcome unknown", so the harness reads residency again and never replays
  the request; a restarted
  harness assumes no earlier lease. After a swap-back, the residency is
  compared with what it was before the swap.
- **Cards by UUID.** A register may say which cards a model sits on (`gpus`):
  only by GPU UUID (`GPU-…`). A card's number can change between boots, so an
  entry that names a card by number is kept without it, and the view says so.
- **No register here.** Without one, `factory doctor` notes it for the local
  servers the harness may load or swap models on: other programs that use the
  cards (timers, containers, other model servers) are not coordinated.
- **The epoch and the generation.** The register sends an `epoch` with every
  answer, made from the host's boot id (`/proc/sys/kernel/random/boot_id`)
  and a counter it keeps on disk and moves on at every start, so a reboot or
  a restart gives a new epoch. Its `generation` only grows within one epoch.
  When the epoch changes, the harness starts its count again (the generation
  may restart from 0) and treats every lease of the earlier epoch as lost: it
  sends no more calls under them, not even a renew or a release (the new
  register may have given the same lease id to someone else), and asks for new
  leases. Before it renews or releases a lease, it reads residency first, so
  it sees a restart in time. Within one epoch, an answer with an older
  generation is refused as stale, and `factory doctor` and `GET /api/custody`
  (`fenced`) say so.
- **Tokens as systemd credentials.** `"token_credential": "<credential-name>"`
  reads the token from `$CREDENTIALS_DIRECTORY/<credential-name>`, the
  harness's own copy that systemd gives its unit for a line
  `LoadCredential=<credential-name>:<path to the token file>`. It is read
  again for every request, like `token_file`, and is never set together with
  `token_file` or `token_env`.
  - **Adding the line.** For the system unit, whoever holds root on the
    server (you, the owner) adds it with `sudo systemctl edit
    factory@<user>`. For the user unit, `./install.sh --systemd-user
    --credential <credential-name>:<path>` writes it, and running the
    installer again keeps the unit's credential lines (`--drop-credential
    <credential-name>` removes one).
  - **Only its own copy.** A `token_file` inside a systemd credentials
    directory (`/run/credentials/…`, another service's) is refused.
  - **Hidden from agents under bwrap.** Your agents run as the harness's
    user, so they could read the copy. Under `"sandbox": "bwrap"` their
    commands cannot read the credentials directory or any `token_file`; with
    `"sandbox": "none"`, or through a CLI driver, they can. With the
    register's token an agent could take or release leases as the harness.
  - **The doctor.** When the file is not there, the doctor names the
    credential and the line to add. `factory doctor` in a shell never has the
    harness's credentials: there it gives a warning (not a failure), still
    asks the register, and points you to the running server's doctor (`GET
    /api/doctor`, the GUI's doctor), which checks the credential.
- An https register with a private certificate: add `"ca_file"` (a PEM CA or
  the register's own certificate). With `host_header`, the certificate must
  name that host.
- **The contract is proposed** and waits for the operator's confirmation; one
  adapter class (`custody.RegisterAdapter`) holds all of it.
- `factory custody [--refresh]` and `GET /api/custody` show residency, the
  leases this harness holds, what HELD and FULL do on each provider and the
  register's last refusals in plain words; `factory doctor` checks that each
  register answers. Every claim, release and failure is also recorded in
  `<data_dir>/custody/journal-YYYY-MM.jsonl` (a record, never read back as
  state), which the operator's activity lists.
- **On these pages.** Settings › Providers has the register's fields (the
  token by file, environment name or systemd credential name, one of the
  three; the token itself is never typed there). To switch to a credential,
  type its name, then clear the file or name: the form says "Name one place
  for the token" until only one is left. Placement and the Operator
  page's **GPU custody** tab show each register: what is on the GPUs, the
  leases held, how long a HELD answer waits (the provider's
  `overflow_after_seconds`, once per run; FULL never waits), where its token
  comes from, and every answer in words ("Lease
  denied: the card cannot fit this model now", "Waits up to 120s: the card is
  held by someone else", "Not released: the register restarted, so it no
  longer knows this lease"). A run that waits out a HELD answer says "waiting
  for the GPU custody register: the card is held by someone else"; a build the
  register refused shows "no lease: <provider>/<model>" beside the build it
  ran on. The Doctor shows the `LoadCredential=` line to add, with a copy
  button.

### Drift sources

A drift source is a read-only service (the operator's checker, for example)
that reports drift over HTTP. The tracker reads it:

```json
"drift_sources": {
  "checker": {"url": "https://127.0.0.1:<port>/drift", "token_file": "~/.config/checker.token",
              "host_header": "checker.internal", "ca_file": "~/.config/checker-ca.pem",
              "schema_versions": ["1"], "interval": 3600}
}
```

- **The request:** GET only, exactly the configured Host header, a Bearer
  token read again for every check from a file the harness user can read (or
  a credential name in the env file, `token_env`, or a systemd credential of
  the harness's unit, `token_credential`, as for a custody register), never
  logged, and no Origin or Sec-Fetch-* header. One check at a time per source.
- **HTTPS with a private certificate:** `"ca_file"` names the PEM CA (or the
  source's own certificate) to trust for this source only; the certificate
  must name the `host_header` host (here checker.internal), so the example
  above works against a checker listening on 127.0.0.1.
- **Refusals back off:** 401, 403 and 405 are "cannot read"; 429 is "check in
  progress" (its Retry-After is honoured). An observation is honoured only
  while `observedAt` + `validForSeconds` (at most a year) holds, an older one
  than the last acted on changes nothing, and an unknown `schemaVersion` is
  "cannot read", never "no drift". Whatever goes wrong, the next check backs
  off.
- **Findings become system items** (source `drift:<name>`): one per stable
  key, reopened when a finding comes back, and closed only with evidence: a
  fresh observation that no longer reports it (also when the source's state
  file was lost: the tracker remembers which items are open). A failed, stale
  or unreadable check closes nothing. They share the system tracker with the
  operator's receipt items, each with keys of its own: the same words in a
  finding and in her receipt are two items, and neither closes the other.
- **A record of the checks:** a check that changed its status or moved an item
  is appended to `drift-sources/<name>/checks-YYYY-MM.jsonl` (refs and counts,
  never a finding's title), which the operator's activity lists.
- **A role manifest** is generated from the source's inventory of Open WebUI
  profiles, into the harness data dir only (`drift-sources/<name>/
  role-manifest.json`), never replaced by an older inventory, and never
  applied.
- `factory drift`, `factory drift check <name>`, `GET /api/drift-sources`.

### Error capture: capture before fix

When a model or tool call fails, the run's log keeps an `agent.error` with the
full status and response body (redacted, at most 16384 characters), any request
ids and exact times (UTC, milliseconds), also for an error a server reports
inside a 200 answer; a persona chat through Open WebUI records the model id and
each turn's time window (`persona.window`). Match them against the server's own
log.

Before you restart, edit or retry anything, take the evidence:

```bash
factory capture --since 30m --until now --container open-webui --out ~/captures/2026-10-22-chat-failure
```

It is read-only and writes only into `--out` (a new or empty directory): the
diagnostic events of the harness's run logs in that window (never transcript
text, tool input or output, persona memory or messages: a failed tool call's
error keeps only its output's length and sha256) and, where your user
may run `docker logs`, each container's log, all redacted, with a
`manifest.json`. Then match the request ids and times, read the bundle before
you share it (redaction is pattern-based), and only then fix. The procedure is
DESIGN.md §39, "Capture before fix".

## When the outcome is unknown

Some calls change something outside the harness: a merge or a push, a message
to your phone, a lease from a GPU custody register, an MCP tool call, a note in
a persona's own outbox. Each one records one outcome:

- **succeeded**: the other side confirmed it;
- **failed**: the other side refused it, or it never left the harness;
- **unknown**: it may have happened, and no answer confirmed it (a timeout
  after sending, a cut connection, a crash, a server error of 500 or more).

The harness never repeats an unknown call by itself. It looks again, read
only, when it can (after a push, `git ls-remote`), or puts a tracker item
"Outcome unknown: check before retrying" in front of you. Only calls that are
safe to repeat are retried by themselves: reads, a few that set one value,
and model calls to a model server, which only generate text. A model call
through Open WebUI or under a GPU custody lease is not repeated once it may
have acted (its filters and tools may have run, or a model may have loaded).
DESIGN.md §40 is the rule, with the list of those calls and an audit of every
outside call.

- A merged job with `merge.push` keeps `push` in its job.json: git's own answer
  for the branch, or what the remote showed when it was looked at again. A
  push that never reached the remote (for example, no remote of that name)
  says so, with git's own reason.
- A job the server left in the middle of a merge says, after a restart,
  whether the merge landed. **Retry** finishes it without merging twice. A
  merge that ran past its time limit is looked at the same way.
- A wake turn that had begun to act (a notice, a note in an outbox) is never
  run again after a crash.
- An MCP tool call or a Bash command cut off part way says that what it did is
  unknown, so the agent checks before it tries again. So does a tool call the
  server was running when it stopped: the agent's session keeps the call, and
  its result says the outcome is unknown.
- A message to your phone that nothing confirmed is never sent again by
  itself, but it counts as "not working": an hour of them raises "A phone
  channel is down" on your other channel.
- A receipt from the operator persona that says its outcome is unknown never
  shows as verified.
- **On these pages** every such call says how it ended in words, never only
  as a colour: a merged job's card says "pushed", "push failed" or "push:
  outcome unknown", and its panel shows the remote, the commit, git's own
  line and, after a look at the remote, what the remote has, with a link to
  the tracker item. Tracker items about an unknown outcome are amber and stay
  open until you check them. A message shows per phone "Succeeded", "Failed"
  or "Outcome unknown: it may or may not have happened"; only **Send again**
  sends an unknown one again. The operator's receipts show the outcome she
  reports beside her checks, and a lease request with no answer reads
  "outcome unknown" in her activity.

## Disk protection with SnapRAID

SnapRAID can rebuild a dead data disk, or bring back deleted and damaged
files, exactly as they were at its last run, and only until its next run. It
keeps no older versions and sits on the same machine: it works beside your
backups, never instead of them. The **Backups** area says this in three
sentences, then shows how protected your disks are right now.

Seraphina never runs or schedules SnapRAID herself. Exactly one thing should
start protection runs: your own backup job until the SnapRAID Daemon is in,
then the daemon's own nightly run. Seraphina counts what she can find, and
with two of them she says so and offers no button that would start a run.

### With the SnapRAID Daemon (SnapRAID 15 or newer)

Once the author's SnapRAID Daemon answers on this machine
(`127.0.0.1:7627`), Backups reads it instead of the disks:

- the light: green "Protected up to this morning at 02:21.", amber (waiting
  for you after it stopped before protecting many deletions or changes, a run
  stopped part way, a request whose outcome is unknown, a skip undone by a
  restart, two things starting runs or none, the service not answering or
  reachable from other computers, a run it could not report to Seraphina, a
  disk whose health can't be read), red (it
  accepts commands without a password, a disk failing or missing, hidden
  damage, a SnapRAID too old for it, nothing protected for a week);
- when the next run is, its limits in plain words ("stops a run when 100 or
  more files were deleted, or 100 or more changed, across all disks"; a limit
  of 0 reads "does not stop for deleted files (no limit is set)"), the
  last run's result, a run in progress with how far it got;
- when it **stopped for you** (its Safety Freeze): the counts, **See what
  changed** (the list a page at a time, by disk, kind and name, in the
  cockpit only), and your ways out: bring back the files deleted by mistake,
  **Protect these changes anyway**, skip tonight's run, or change its limits
  yourself.

The buttons, each only when it makes sense and only after your go-ahead:
**Protect the latest changes**, **Skip the next run** (and **Don't skip**),
**Stop safely**, **Bring files back** and **Repair it**. Each press shows what
will happen and one button; Seraphina checks again just before sending that
nothing changed since you looked, sends it once, and tells you the outcome:
done, not done, or "outcome unknown: it may or may not have happened" (when
her link gives no answer she looks again, read-only, a little over a minute
later, once it can no longer send). She never sends anything again by
herself, and the link itself refuses a second send of one go-ahead. A stop
you approved shows as "Stopped safely", never as a failure. Files come back
one disk at a time: the protection service would bring back every chosen
name on every chosen disk. Freezes, failed runs, a service that
stopped answering and a skip a restart undid reach your phone with counts
only, and open tracker items.

**Setting it up.** The daemon has one password and whoever holds it can also
switch its limits off, so Seraphina never holds it: a small program,
Seraphina's **link** (`bin/sf-snapraidd`), which the system starts for each
request, holds it and can make only the calls Seraphina needs. You install it
yourself:

```bash
factory snapraid link install     # stages the files and prints the copy blocks, in order
factory snapraid link check       # the link answers: password on, SnapRAID 15.x, allowing reading
```

The blocks install the link, its settings (reading only), the password (you
type it at a prompt that shows a star for each character: never on a command
line, never in the cockpit, never in the terminal's scrollback), its two systemd
units, the event source that lets the daemon report each run to Seraphina
(`parity.report`, in Messages), and the lines `/etc/snapraidd.conf` needs (a
password, loopback only, both limits, `notify_result` naming the link's
forwarder). Letting Seraphina ask for runs is a later block that changes the
link's settings to allow it, which you read in plain words before moving it
into place; do it only after the real-service lab check (L15) passes, and
only while the agents run in the sandbox (`harness.sandbox` `bwrap`): without
it any agent could reach the link, and the doctor says so as an error.
`factory snapraid link remove` prints the removal blocks.

### Without the daemon: the reader (read-only)

While no daemon is installed, the disk protection reader (`bin/sf-snapraid`)
reads SnapRAID for Seraphina. It **only reads**; the card says "Seraphina is
only watching: nothing is protected by it yet." What it shows:

- a light with its reason in one sentence: green "Protected up to this
  morning at 09:37.", amber (never protected, an unfinished run, a run
  started outside Seraphina, SnapRAID unable to read its own state, a disk
  whose health can't be read or was last read days ago, a part of the disks
  not checked for hidden damage for weeks, a protection disk almost full, a
  disk being taken out of protection, a SnapRAID read with care), red (a disk
  missing or failing, damage recorded, a damaged file list, nothing
  protected for a week while files may have changed). Red and amber stay
  when the facts are old or the reader does not answer, with "Last checked 5
  hours ago." under them: a dying disk stays red. Grey only where it would be
  green;
- each disk, the protection disk too: files, free space, health, its chance
  of failing this year, asleep or awake, and the room left for new data;
- who else runs SnapRAID on this machine (cron, systemd timers, Dockge), and
  runs that started outside Seraphina;
- **Check for changes now**: what changed since the last run, and whether a
  protection run would be held for you to look first.

It reads SnapRAID 13 (also a distribution's package that prints `v0`, when
the package database owns it at 13.0), 14 with care, and refuses two SnapRAID
programs of different versions. It waits while SnapRAID runs, while your
backup job runs or is about to (`snapraid.quiet_units`, with
`quiet_margin_minutes`), and around the power-off. It accepts the file list's
copies in `/var/lib/snapraid/` or at a disk's root, a disk mounted at two
places, and a disk being taken out through an empty placeholder folder (its
settings' `placeholders`), which the card lists under **Not covered**. With the daemon installed it runs no SnapRAID
command at all, and reads the file list's copies only on the system disk,
without reading their contents, so no data disk is woken. A SnapRAID 15 or
newer without the daemon is refused in those words: it needs the daemon.

**Setting it up.** You install it yourself; the harness never runs sudo:

```bash
factory snapraid helper install     # stages the files and prints five copy blocks to run, in order
factory snapraid helper check       # the reader answers: SnapRAID 13.0, a valid config, reading only
```

Each block installs to a `.new` file and checks it; moving it into place is a
second step that runs only when that check passes again. Then turn it on:
`"snapraid": {"enabled": true}` (Settings, or the card's **Turn it on**).
`factory snapraid helper remove` prints the removal blocks. With disk
protection on, keep the agents' sandbox on (`harness.sandbox: bwrap`, the
builtin driver): otherwise your agents could read your file names and call
the reader or the link too; the doctor reports it as an error.

```bash
factory snapraid                    # the card in plain text
factory snapraid check-now diff     # a guard report now: would a run be held?
factory snapraid guard              # its checks and counts (file names stay in the cockpit)
```

Checked here: the reader against a real SnapRAID 13.0 on loop disks; the
daemon route against a mock daemon built from recorded exchanges with
SnapRAID Daemon 2.1 and SnapRAID 15.1. Not yet checked on a server started by
systemd, nor against a real daemon (the link's units, the actions, the
reports): the doctor and the card say so. DESIGN.md §41, and the full design
in [docs/seraphina/BACKUPS_SNAPRAID.md](docs/seraphina/BACKUPS_SNAPRAID.md).

**Known limits.** The cases a real server showed are handled now: Ubuntu's
13.0 package that reports its version as "v0", a copy of SnapRAID's file list
in `/var/lib/snapraid/` or on a disk, a disk being taken out with an empty
stand-in folder, a disk mounted at two places, SnapRAID run by its daemon, and
a hardened factory unit. They are tested against recorded answers and a mock,
not yet on a real server. The sudoers line was checked only with classic sudo,
not with sudo-rs, which newer Ubuntu releases use as `sudo` (DESIGN §41).

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
  dedicated Unix user (for example `ai-dev`) and enable bwrap. Under
  bwrap an agent can commit on its branch but not touch the repository's
  config, hooks or main; each command gets its own PID namespace, so it cannot
  reach them through another process's `/proc/<pid>/root` either. Under bwrap
  MCP servers run in the run's sandbox too; without it they can write what the
  factory's user can, except through the arguments their `write_args` list,
  which are checked (see [MCP](#mcp)).
- **Run it as a user that is not root by another door.** bwrap does not stop
  an account in the `docker`, `lxd` or `incus-admin` group (their sockets start
  root containers), in `sudo` (sudo with the account's password) or in `disk`.
  Under bwrap the harness hides those sockets and agents get no disks, but the
  harness itself keeps the account's rights, and a socket hidden at the start
  of a run shows again if Docker or LXD restarts during it. `factory doctor` checks the
  account it runs as ("account"): a warning, and an error while agents run
  commands outside bwrap. On a shared server, give the harness its own user,
  in none of these groups, and never run it as your own login account.
- **No setuid rights for agents.** Everything an agent starts runs with
  no-new-privileges: bwrap sets it, and outside bwrap (no sandbox, a CLI
  agent, an MCP server) the harness starts the program through `setpriv
  --no-new-privs` (util-linux). The harness's own git starts the same way,
  because a filter or merge driver in a repo's `.git/config` runs with it.
  So `sudo` (sudo-rs or classic sudo) never gives an agent more rights than
  the harness's user, as long as util-linux is installed (`factory doctor`:
  "no new privileges"). The harness itself keeps sudo for its root helpers
  (the disk protection reader): that is why `systemd/factory@.service` has no
  `NoNewPrivileges=yes`. Give its user no sudo grants beyond those helpers'
  read-only commands. Ubuntu 25.10 and
  later have two sudo programs, sudo-rs as `sudo` and classic sudo as
  `sudo.ws`, which read the same sudoers files but not always alike; the
  doctor's "sudo" line names them, so check an account's grants with both.
- **The factory's own git runs no hooks.** Every git command it runs
  disables hooks and `core.fsmonitor`, and status and stash first check that a
  worktree's `.git` file still points into the configured repo. Filter and
  merge drivers set up in the repo's `.git/config` still run (with
  no-new-privileges, so never with sudo), so that file must stay yours: bwrap
  keeps agents from editing it.
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
- **Messages to your phone** carry only what code chose: fixed heads-ups or
  texts built from job numbers and states, never a job's description, what you
  wrote, or one of your sensitive words; Telegram gets heads-ups only, unless
  you set `operational: true` on it. They stay off while agents could read them
  (no bwrap, or a CLI driver); under bwrap, agents' commands cannot read your
  config directory (with `config.json` and the env file) or the message data,
  and cannot reach the SSH and GPG agent sockets at their usual places, the
  harness's systemd credentials or the token files of a custody register or a
  drift source. Text
  typed on a phone is kept without any token in it. Event sources have their own tokens, stored only as digests;
  the master token is refused there. See [Messages on your phone](#messages-on-your-phone).
- **Seraphina runs only on a model on this machine** that you confirmed, with
  no tools (or read-only ones under bwrap), and answers from a fixed menu that
  code checks and applies. What she read from outside is marked untrusted,
  what she read of yours private, and neither reaches a phone as more than a
  fixed heads-up; her memory, journal and thread stay in `<data_dir>/wakes`,
  hidden from agents. See [Seraphina](#seraphina-an-assistant-that-checks-in).
- **An operator persona is not one of the harness's agents.** If you mark one
  (see [An operator persona](#an-operator-persona)), she acts as root with her
  own tools and the harness does not gate her; it only records what she
  reports, read only, labelled as reported. Her receipts, the harness's index
  of them, the system tracker (which holds her unchecked checks) and the GPU
  custody journal are hidden from agents' Bash under bwrap, her items never reach an outside agent's
  token, and what comes from a place the harness's own user may write is
  marked untrusted.
- **Repo content is untrusted.** Agents are told to treat instructions in
  files, specs and reviews as data, and the merge gate is deterministic code: a
  prompt-injected approver cannot merge a protected path, an oversized change,
  a conflict, or anything a reviewer did not pass.
- **No silent fallback.** A busy or down local server makes a call wait or the
  job fail (retryable); the factory never switches to a hosted model on its own.
- **Never pushes** unless `merge.push` is true, and never force-updates a ref.
  A push whose outcome is unknown is never repeated by itself (§40).
  Your checkout is only touched by worktree commands, branch create/delete and
  the final merge.

## CLI reference

```
factory init [--force] [--preset NAME] [--data-dir DIR]   write the config + token
factory serve [--host H] [--port P]                       run the server and workers
factory url                                               login URL (+ SSH tunnel command on stderr)
factory doctor [--probe] [--look-around] [--json]         checks; exit 1 on an error-level failure
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
factory personas [--json] | personas card <name> [--json]  personas (Open WebUI profiles such as Kate); a card says what runs where
factory personas import [FILE] [--model ID] [--snapshot NAME [--snapshot-model KEY]]   a profile card (server)
factory personas check [<name>] [--json]                  the drift check, now (server)
factory personas before-import <name> --attach ID ... [--by operator]   receipts as the changes before import (server)
factory personas ledger <name> [--show SHA | --diff A B | --restore SHA | --erase SHA]   the read ledger of her own memory (server)
factory estimate <repo> [--kind feature|plan] [--profile R] [--preset P] [--json]   tokens, cost, time per step; the judges the independence guard would move
factory snapshot --out DIR [--sudo]                       read-only server inventory (see below)
factory notify status [--json] | notify test [--channel C]   phone channels and their health; a test notice (server)
factory notify event <kind> [--subject S] [--detail TEXT]    report backup.failed, service.up, ... yourself (server)
factory wake pause | resume | status [--json]             stop Seraphina at once (works offline) or restart her
factory wake pause --persona P [--until HH:MM] | resume --persona P   stop or restart one persona
factory wake poke <persona> [--reason TEXT]               ask a persona to look now (works without the server)
factory secret set NAME --stdin                           store a secret from standard input, never echoed
factory activity --agent NAME [--since T] [--until T] [--after CURSOR] [--json]   what an agent or persona did, with evidence
factory operator [--json] | operator receipts [--since T] [--rescan] | operator scan   the operator persona's receipts
factory capture --since WHEN [--until WHEN] [--container NAME ...] --out DIR   logs of a window, read-only
factory drift [--json] | drift check <name>               drift sources: status, or a check now (server)
factory custody [--refresh] [--json]                      GPU custody: residency and leases held (server)
factory snapraid [--json] | snapraid check-now [status|health|diff] | snapraid jobs [J-n] | snapraid guard [G-n]
factory snapraid helper install|check|remove              disk protection: card, checks, copy blocks (server)
factory snapraid link install|check|remove                Seraphina's link to the SnapRAID Daemon: copy blocks (server)
```

- Commands that act on jobs, backlogs and plans talk to the running server
  with the config's token, at the address `factory serve` recorded in
  `<data_dir>/factory.lock` (else `server.host`:`server.port`). `init`, `repo`,
  `run`, `logs`, `doctor`, `agents`, `models`, `personas` (list and `card`),
  `estimate`, `snapshot`, `activity`, `operator` (and `operator receipts`),
  `capture` and `url` work without it (`repo add` goes through the
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
Cockpit, Cosmos, Tailscale and backup tools. Cosmos's own config (its routes)
is read only while Cosmos is installed; once it is gone, the files it left are
listed by name and never read (they include its private key).

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
- Operator toolkit: providers' `lazy_load` and `custody {url, token_file? |
  token_env? | token_credential?, host_header?, ca_file?, provider?, models?,
  timeout?}` (GPU custody, off by default) and `"drift_sources": {name: {url,
  token_file | token_env | token_credential, host_header?, ca_file?,
  schema_versions, interval?, timeout?}}` (see [Operator
  toolkit](#operator-toolkit-gpu-custody-drift-sources-capture)). Their tokens
  are read from a file, the environment or a systemd credential, never stored
  in the config. A drift
  source's state and role manifest live in `<data_dir>/drift-sources/<name>/`.
- Disk protection: `"snapraid": {"enabled": false, "refresh": {"status_minutes":
  60, "health_hours": 24}, "stale_minutes": 180, "light": {...,
  "scrub_target_days": 30}, "daemon": {"metrics_url":
  "http://127.0.0.1:7627/metrics", "link": "/run/seraphina-snapraidd/link.sock",
  "metrics_seconds": 60, "active_seconds": 10, "reads_min_seconds": 60,
  "full_read_minutes": 30, "down_minutes": 30, "approval_minutes": 15},
  "quiet_units": [], "quiet_margin_minutes": 30, "not_schedulers": []}`.
  `scrub_target_days` sets how long a part of the disks may go unchecked for
  hidden damage; `quiet_units` names your backup job's service, which the
  reader waits for; `not_schedulers` lists places you marked "does not start
  runs" (`{place, sha256}`). The old `schedule`, `restore` and `live_folders`
  keys are ignored. The reader's thresholds and disks live in its root-owned
  config (`/etc/seraphina/snapraid-helper.json`); the link's allow classes and
  the daemon's password in its own root-owned files
  (`/etc/seraphina/snapraidd.json`, `/etc/seraphina/snapraidd-basic.cred`).
  The facts and your approvals live in `<data_dir>/snapraid/` (no file
  names).
- Your agents and skills stay in `~/.config/software-factory/agents/` and
  `~/.config/software-factory/skills/`, also when `$FACTORY_CONFIG` moves the config.
- Data: `data_dir` (default `~/.local/share/software-factory`) holds
  `jobs/<repo>/<id>/` (job.json, spec, build, reviews, decision, items, logs,
  sessions), `jobs/<repo>/backlog.md`, `jobs/<repo>/followups.json`, `worktrees/`, `tmp/` (short-lived merge
  worktrees), `chats/`, `plans/`, `output/` (folder deliveries, unless
  `merge.output_dir` moves them), `wakes/` and `notify/` (your messages,
  Seraphina's memory and the audit log: keep them out of any backup that
  leaves the machine) and `factory.lock` (the running server's pid and URL).
- Messages and Seraphina: `"notify"` (phone channels, do-not-disturb time,
  caps, the sensitive-words file), `"wakes"` (time zone, power schedule, event
  sources, personas, chat check-ins) and `"harness": {"instance_name":
  "Seraphina OS Harness"}`. Once `~/.config/software-factory/notice-rules.json`
  exists, quiet hours and the daily limits are changed there (`POST
  /api/notify/rules`), not in the config. See [Messages on your
  phone](#messages-on-your-phone) and [Seraphina](#seraphina-an-assistant-that-checks-in).
- Saving settings (GUI or `POST /api/config`) keeps your file's own keys and
  adds only values that differ from the defaults, so settings you never set
  follow newer defaults.
- The full schema is in [DESIGN.md](DESIGN.md) (§11 config, §18–§25 for
  providers, models, routing, chats, plans, delivery and setup, §26 for Grid
  delivery, §32 for the Housekeeping Tracker, §33 for Open WebUI profiles,
  §34-§35 for Messages and Seraphina, §36 for operator personas, §37-§39
  for GPU custody, drift sources and error capture, §40 for outcomes and §41
  for disk protection);
  presets with
  explanations are in [examples/](examples/README.md). Each preset file also
  carries a one-line `description` (shown in the GUI's preset cards; never
  copied into your config).

### HTTP API

Everything the GUI does goes through a JSON API on the same port (token as
`Authorization: Bearer <token>` or the login cookie; POSTs need
`Content-Type: application/json`). The full table is DESIGN §14 plus the
"HTTP additions" of §19, §22–§25 and §32–§39; the routes you are most likely to script:

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
| GET | `/api/setup` | | `{configured, steps: {providers, models, routing, repos, tools, output}, host}`, each step `{ok, detail}`; host: what listens here, and the last look for model servers you asked for (`model_servers`, `model_servers_at`; `model_servers_error` instead of `model_servers` if that look broke) |
| POST | `/api/setup/look-around` | `{}` | `{at, model_servers, note}`, or `{at, error}` if the look broke: ask each program listening here whether it is a model server, once (no key; the master token only; once in 30 s, 10 an hour, else 429) |
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
| POST | `/api/personas/<name>/before-import` | `{attach?: [ids], detach?: [ids], by?: "owner" \| "operator"}` | `{import}`: the baseline note and the receipts attached as changes before import |
| GET | `/api/personas/<name>/external`, `.../external/versions/<sha256>`, `.../external/diff?from=&to=` | | the read ledger of her own memory: versions per file, one version, a diff |
| POST | `/api/personas/<name>/external/restore` | `{sha256, file?}` | `{restore}`: the earlier text for her tools or you to apply; nothing is written |
| POST | `/api/personas/<name>/external/erase` | `{sha256}` | `{erased}`: one version's copy removed; the ledger keeps only its sha256; her store is not touched |
| GET | `/api/openwebui/models` | `?provider=` | the models Open WebUI lists for the key, workspace models first |
| GET | `/api/messages`, `/api/messages/<id>` | `?unread=1&kind=&channel=` | the inbox: every notice, what each phone got and why |
| POST | `/api/messages/<id>/<action>` | ack, snooze `{minutes?}`, retry, resend | `{message}` |
| GET | `/api/notify`, `/api/notify/channels` | | phone channels, health, do-not-disturb time, today's count, event sources |
| POST | `/api/notify/test`, `/api/notify/pair`, `/api/notify/pair/confirm`, `/api/notify/ntfy/prepare` | `{channel, code?}` | phone setup (DESIGN §34) |
| POST | `/api/notify/events` | `{kind, subject?, detail?}` | `{message}`: report an event yourself |
| POST | `/api/notify/hooks` | `{name, events, subjects}` | `{hook, token, snippets}`: an event source (token shown once) |
| POST | `/api/hooks/event` | `Authorization: Bearer <source token>`; `{kind, subject?}` | `{message}`: never the master token |
| POST | `/api/wakes/control` | `{action: "pause" \| "resume" \| "mute" \| "unmute", persona?, until?}` | stop or restart Seraphina, or one persona |
| GET / POST | `/api/wakes` | POST `{persona, reason?}` | the schedule (queue, reminders, model servers, budgets); POST runs a check-in now |
| GET / POST | `/api/wakes/reminders` | POST `{at, text, persona?, show_on?}` | reminders in your own words, delivered by code |
| POST | `/api/wakes/servers/<provider>/attest` | `{local: true}` | you confirm a model server runs on this machine |
| POST | `/api/personas/<name>/messages` | `{text}` | write to a persona; her answer is in the cockpit |
| GET | `/api/custody` | `?refresh=1` | GPU custody: each provider under custody, its register's residency, the leases held |
| GET | `/api/drift-sources` | | drift sources: status, observation times, findings present and their items |
| POST | `/api/drift-sources/<name>/check` | | check one now: findings opened, reopened, closed |
| GET | `/api/drift-sources/<name>/manifest` | | `{manifest}`: the role manifest generated from its inventory |
| GET | `/api/snapraid` | | disk protection: the card (light, reason, disks, the last guard report's counts) |
| POST | `/api/snapraid/refresh` | `{what: status\|health\|diff, wait?}` | read now (202; `wait: true` answers with the card) |
| GET | `/api/snapraid/jobs`, `/api/snapraid/jobs/<J-n>` | `?log=1&offset=N` | the reader's runs; one run and a page of its output |
| GET | `/api/snapraid/guard/<G-n>` | `?disk=&kind=&dir=&q=&offset=` | a guard report and a page of its change list |
| GET | `/api/snapraid/files` | `?disk=&dir=&q=&offset=` | the last good run's file index, a page at a time |
| GET / POST | `/api/snapraid/setup`, `/api/snapraid/setup/stage`, `/api/snapraid/setup/check`, `/api/snapraid/setup/link-stage`, `/api/snapraid/setup/link-check` | | the copy blocks; stage the files; check the reader or the link |
| POST | `/api/snapraid/actions` | `{action: stop\|hold_off\|maintenance\|accept\|undelete\|heal, paths?, enabled?}` | a preview of an action on the SnapRAID Daemon (201), or why it cannot be offered (400) |
| GET / POST | `/api/snapraid/actions`, `/api/snapraid/actions/<A-n>`, `/api/snapraid/actions/<A-n>/approve`, `/api/snapraid/actions/<A-n>/cancel` | approve `{shown_sha256, typed?}` | your approvals and their outcomes; approve sends it once (409 `{error: "changed"}` when something changed since you looked) |
| GET | `/api/snapraid/review`, `/api/snapraid/fixes`, `/api/snapraid/runs`, `/api/snapraid/runs/<number>` | review `?disk=&change=&q=&offset=` | the last scan's change list and what an undelete brought back, live; the daemon's runs and one step's messages |
| GET / POST | `/api/notify/rules` | the keys to change | the notice rulebook (stricter only) |
| GET / POST | `/api/chats/<id>/wakes` | POST `{allow}` | let a coding chat schedule check-ins |
| POST | `/api/hooks/wake` | `Authorization: Bearer <source token>`; `{persona, reason?}` | ask a persona to look (an event source allowed to wake her) |
| GET | `/api/events` | `?since=<seq>` | the live event stream (SSE) |

## Troubleshooting

- **Start with `factory doctor`** (also the GUI's Doctor page). It checks
  Python (3.10 or newer; one newer than 3.14 is said to be untested), git, the
  config and token, the data dir, the sandbox, repos, agents, every routed
  provider (reachable, key present, and the configured model ids against its
  model list), the providers of builds, overflows, escalation
  rungs and profiles (and that those references resolve), which local servers
  are up and which model a TabbyAPI-style server has loaded, CLI drivers, every MCP
  server an agent uses (defined, its command found and executable; nothing is
  started; a server only Chat agents use, like the analyst's `rea`, is
  optional: "not configured" is advice and a missing command a warning) and
  every `harness.skills_dirs` entry (exists, how many skills).
  `factory doctor --probe` also sends each routed model the two test probes;
  `factory doctor --look-around` also asks each program listening on this
  machine whether it is a model server (one `GET /v1/models`, no key).
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
- **A job's dev-server ports**: job n gets 5100 + 10n (builder), +1 and +2 (the
  UX and UI reviewers). If something on the server already listens on one of
  them when the job starts (a web app on 6080, say), the job gets 15100 + 10n
  instead, or a free block from 25100 up, and its agents are told that port.
- **Port in use**: set `server.port` (or run `factory serve --port 8770` once;
  the other CLI commands find it through the data dir's lock file), and use
  the same port in your SSH tunnel. "another factory is already running"
  means the data dir's lock is held: check `systemctl --user status factory`.
- **"server not running — start it with: factory serve"**: the CLI could not
  connect to the address a running `factory serve` recorded in
  `<data_dir>/factory.lock`, or (with no server) to `server.host`:`server.port`.
- **git older than 2.38** (Ubuntu 22.04 ships 2.34): everything works, but
  merge-conflict checks use a slower trial merge in a temporary worktree.
- **No phone messages, or Seraphina never checks in**: `factory doctor` names
  what is missing (the sandbox, the time zone, a model server you have not
  confirmed as local, a hand edit waiting for review), and `GET /api/wakes`
  says in plain words why each waiting check-in waits ("Not now: someone is
  using the model."). After a boot nothing is sent until the clock is set.
- **The user service stops at logout or a CLI is not found**: run
  `sudo loginctl enable-linger <user>`. The unit records your shell's `PATH`
  at install time; re-run `./install.sh --systemd-user` after installing new
  CLIs, or set `PATH` in the env file. Add no sandboxing options
  (`PrivateTmp=`, `ProtectSystem=`, ...) to the user unit: they make every
  bwrap a sandbox inside a sandbox, which many servers refuse.
- **sudo is refused for a helper** ("NoNewPrivs" in the message): the harness
  runs under a unit with `NoNewPrivileges=yes`, which stops sudo-rs and classic
  sudo alike. The shipped system unit leaves it off; remove it from your copy
  (agents keep no-new-privileges either way).

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
