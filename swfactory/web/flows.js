/* Seraphina OS Harness flows (DESIGN §25): the Welcome, Setup and Start flows as plain data, the working
 * procedures of the Grid Card, and the small pure helpers the wizard, the maps and the Grid share.
 *
 * A flow is a list of steps. A step has an id, a title, a numbered procedure written as
 * [bold lead phrase, one or two plain sentences] pairs, a kind (choice | form | review | run) and
 * options ({id, title, does, when, badges, next, ...}) or form groups. `next` is a step id, or a
 * function of the draft; `branches` lists every step a function can lead to, so the maps can draw it.
 *
 * No DOM, no network, no secrets: app.js renders, asks the server and keeps typed keys in memory only.
 * Loaded before app.js; everything is exposed as window.SFFlows. */
'use strict';
(function (root) {
  const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
  const clone = (v) => (v === undefined ? undefined : JSON.parse(JSON.stringify(v)));
  function stable(v) {
    if (Array.isArray(v)) return '[' + v.map(stable).join(',') + ']';
    if (isObj(v)) return '{' + Object.keys(v).sort().map((k) => JSON.stringify(k) + ':' + stable(v[k])).join(',') + '}';
    return JSON.stringify(v === undefined ? null : v);
  }
  const enc = (s) => encodeURIComponent(String(s));
  const B = { rec: 'Recommended', local: 'Local', api: 'API', key: 'Needs a key', setup: 'Needs setup', both: 'Local + API', cli: 'CLI' };

  // ================================================================ vocabulary
  /** Local model servers: provider name, default port, key (if the server checks one), model id hint. */
  const LOCAL_SERVERS = [
    { id: 'tabby', title: 'TabbyAPI', provider: 'tabby', port: 5000, keyEnv: 'TABBY_API_KEY', auth: 'both', model: 'qwen3.8-flash-next', context: 262144, slots: 1,
      does: 'An ExLlama server with an OpenAI-compatible API, fast on NVIDIA GPUs.',
      when: 'You serve EXL2 or EXL3 quants, such as Qwen3.8-Flash-Next.', badges: [B.local],
      tip: 'Set max_seq_len in TabbyAPI’s config.yml to the context below.' },
    { id: 'strata', title: 'Strata', provider: 'strata', port: 18080, keyEnv: 'STRATA_API_KEY', keyFile: '/etc/ai-stack/strata.env', model: 'strata', context: 262144, slots: 1,
      does: 'Serves Qwen3.8-Flash-Next behind Bearer auth, one generation at a time.',
      when: 'Your server runs the Strata stack.', badges: [B.local],
      tip: 'The key can also come from /etc/ai-stack/strata.env; leave the field empty to use that file.',
      tipNoFile: 'Paste the key here, or put STRATA_API_KEY=… in the server’s env file.' },
    { id: 'llamacpp', title: 'llama.cpp', provider: 'llamacpp', port: 8080, model: '', context: 131072, slots: 1,
      does: 'llama-server runs GGUF models on the GPU, the CPU or both.',
      when: 'You run GGUF quants and can start llama-server with --jinja.', badges: [B.local, B.setup],
      tip: 'Start llama-server with --jinja (tool calling needs the chat template), -c matching the context below, -ngl 99 and --parallel 1.' },
    { id: 'ollama', title: 'Ollama', provider: 'ollama', port: 11434, model: 'qwen3-coder:30b', context: 262144, slots: 1,
      does: 'Pulls and serves models with one command.',
      when: 'You want the simplest local setup and can raise its context.', badges: [B.local, B.setup],
      tip: 'Ollama starts models with a small context: set OLLAMA_CONTEXT_LENGTH to the context below.' },
    { id: 'vllm', title: 'vLLM', provider: 'vllm', port: 8000, model: '', context: 131072, slots: 1,
      does: 'High-throughput serving with paged attention and batching.',
      when: 'You serve full-precision or AWQ models and want several slots.', badges: [B.local, B.setup],
      tip: 'Start vllm serve with --max-model-len matching the context, --enable-auto-tool-choice and the model’s --tool-call-parser.' },
    { id: 'lmstudio', title: 'LM Studio', provider: 'lmstudio', port: 1234, model: '', context: 131072, slots: 1,
      does: 'A desktop app that downloads models and runs a local server.',
      when: 'You manage your models in LM Studio on this machine.', badges: [B.local],
      tip: 'Load the model with the context length below, then start LM Studio’s local server.' },
  ];
  /** Hosted APIs. The providers already exist in the default config: only the key (and a model id to test) is asked. */
  const API_PROVIDERS = [
    { id: 'anthropic', title: 'Anthropic (Claude)', provider: 'anthropic', keyEnv: 'ANTHROPIC_API_KEY', model: 'claude-sonnet-5-5',
      does: 'Claude Opus 5.5, Sonnet 5.5 and Haiku 5.5, and Fable 5.1 for the lead.',
      when: 'You want strong, independent reviewers and approver.', badges: [B.api, B.key] },
    { id: 'deepseek', title: 'DeepSeek', provider: 'deepseek', keyEnv: 'DEEPSEEK_API_KEY', model: 'deepseek-v4-flash',
      does: 'DeepSeek V4 Pro and Flash: capable and inexpensive.',
      when: 'You want low-cost API models for any step.', badges: [B.api, B.key] },
    { id: 'nanogpt', title: 'NanoGPT', provider: 'nanogpt', keyEnv: 'NANOGPT_API_KEY', model: 'anthropic/claude-sonnet-5.5',
      does: 'One key for Claude, GPT, Gemini, Qwen and DeepSeek.',
      when: 'You want many vendors on one bill.', badges: [B.api, B.key] },
    { id: 'openrouter', title: 'OpenRouter', provider: 'openrouter', keyEnv: 'OPENROUTER_API_KEY', model: '',
      does: 'A gateway to hosted models from many vendors.',
      when: 'You want to try models from several vendors with one key.', badges: [B.api, B.key] },
    { id: 'openai', title: 'OpenAI', provider: 'openai', keyEnv: 'OPENAI_API_KEY', model: '',
      does: 'OpenAI’s models over their API.',
      when: 'You already have an OpenAI account.', badges: [B.api, B.key] },
  ];
  /** The shipped routing presets (examples/<name>.json), so a card can say in one line what each one does and the
   *  review can list the exact route of every step. providers: {name: api_key_env}; models: {key: 'provider/model'};
   *  agents: {agent: model key}. tests/test_gui_static.py checks this table against the files. A preset the table does
   *  not know is shown from the server's own description. */
  const PRESETS = {
    hybrid: { title: 'Hybrid', kind: 'both',
      does: 'A local model writes specs and code, reviews security and runs Chat; Claude reviews UX, UI and code, approves and leads plans.',
      when: 'You run a strong local model and have an Anthropic key.',
      providers: { tabby: 'TABBY_API_KEY', anthropic: 'ANTHROPIC_API_KEY' },
      models: { local: 'tabby/qwen3.8-flash-next', 'claude-sonnet': 'anthropic/claude-sonnet-5-5', 'claude-opus': 'anthropic/claude-opus-5-5', 'claude-fable': 'anthropic/claude-fable-5-1',
        opus: 'tabby/qwen3.8-flash-next', sonnet: 'anthropic/claude-sonnet-5-5', haiku: 'anthropic/claude-haiku-5-5' },
      agents: { 'spec-writer': 'local', builder: 'local', approver: 'claude-opus', 'reviewer-security': 'local', 'reviewer-ux': 'claude-sonnet', 'reviewer-ui': 'claude-sonnet',
        'reviewer-code': 'claude-sonnet', lead: 'claude-fable', coder: 'local', 'repo-scout': 'local' } },
    anthropic: { title: 'Claude only', kind: 'api',
      does: 'Claude Opus 5.5 writes specs, builds, approves and leads; Sonnet 5.5 reviews UX, UI and code.',
      when: 'You have an Anthropic key and want Claude on every step.',
      providers: { anthropic: 'ANTHROPIC_API_KEY' },
      models: { opus: 'anthropic/claude-opus-5-5', sonnet: 'anthropic/claude-sonnet-5-5', haiku: 'anthropic/claude-haiku-5-5' },
      agents: { 'spec-writer': 'opus', builder: 'opus', approver: 'opus', 'reviewer-security': 'opus', 'reviewer-ux': 'sonnet', 'reviewer-ui': 'sonnet', 'reviewer-code': 'sonnet',
        lead: 'opus', coder: 'opus', 'repo-scout': 'sonnet' } },
    deepseek: { title: 'DeepSeek only', kind: 'api',
      does: 'DeepSeek V4 Pro writes specs, builds, approves and leads; V4 Flash reviews UX, UI and code.',
      when: 'You want capable, low-cost API models on every step (the built-in default).',
      providers: { deepseek: 'DEEPSEEK_API_KEY' },
      models: { opus: 'deepseek/deepseek-v4-pro', sonnet: 'deepseek/deepseek-v4-flash', haiku: 'deepseek/deepseek-v4-flash' },
      agents: { 'spec-writer': 'opus', builder: 'opus', approver: 'opus', 'reviewer-security': 'opus', 'reviewer-ux': 'sonnet', 'reviewer-ui': 'sonnet', 'reviewer-code': 'sonnet',
        lead: 'opus', coder: 'opus', 'repo-scout': 'sonnet' } },
    nanogpt: { title: 'Claude through NanoGPT', kind: 'api',
      does: 'The Claude only split through NanoGPT’s API: one key for many vendors.',
      when: 'You pay for Claude through NanoGPT rather than Anthropic.',
      providers: { nanogpt: 'NANOGPT_API_KEY' },
      models: { opus: 'nanogpt/anthropic/claude-opus-5.5', sonnet: 'nanogpt/anthropic/claude-sonnet-5.5', haiku: 'nanogpt/anthropic/claude-haiku-5.5' },
      agents: { 'spec-writer': 'opus', builder: 'opus', approver: 'opus', 'reviewer-security': 'opus', 'reviewer-ux': 'sonnet', 'reviewer-ui': 'sonnet', 'reviewer-code': 'sonnet',
        lead: 'opus', coder: 'opus', 'repo-scout': 'sonnet' } },
    ollama: { title: 'All local (Ollama)', kind: 'local',
      does: 'Every step on one local model, taking turns on one slot.',
      when: 'Nothing may leave your machine and Ollama serves your model.',
      providers: { ollama: null },
      models: { local: 'ollama/qwen3-coder:30b', opus: 'ollama/qwen3-coder:30b', sonnet: 'ollama/qwen3-coder:30b', haiku: 'ollama/qwen3-coder:30b' },
      agents: { 'spec-writer': 'local', builder: 'local', approver: 'local', 'reviewer-security': 'local', 'reviewer-ux': 'local', 'reviewer-ui': 'local', 'reviewer-code': 'local',
        lead: 'local', coder: 'local', 'repo-scout': 'local' } },
    strata: { title: 'All local (Strata)', kind: 'local',
      does: 'Every step on a Strata server’s Qwen3.8-Flash-Next, one at a time.',
      when: 'Nothing may leave your machine and your server runs Strata.',
      providers: { strata: 'STRATA_API_KEY' },
      models: { local: 'strata/strata', opus: 'strata/strata', sonnet: 'strata/strata', haiku: 'strata/strata' },
      agents: { 'spec-writer': 'local', builder: 'local', approver: 'local', 'reviewer-security': 'local', 'reviewer-ux': 'local', 'reviewer-ui': 'local', 'reviewer-code': 'local',
        lead: 'local', coder: 'local', 'repo-scout': 'local' } },
    mixed: { title: 'Two local GPUs and Claude', kind: 'both',
      does: 'Qwen 3.8 27B builds on one llama.cpp server and Gemma 4 reviews on another; Claude Opus reviews security, approves and leads.',
      when: 'You have two GPUs, one model on each, and an Anthropic key.',
      providers: { 'llamacpp-qwen': null, 'llamacpp-gemma': null, anthropic: 'ANTHROPIC_API_KEY' },
      models: { 'qwen-27b': 'llamacpp-qwen/qwen3.8-27b', 'gemma-4': 'llamacpp-gemma/gemma-4', 'claude-opus': 'anthropic/claude-opus-5-5', opus: 'llamacpp-qwen/qwen3.8-27b',
        sonnet: 'llamacpp-gemma/gemma-4', haiku: 'llamacpp-gemma/gemma-4' },
      agents: { 'spec-writer': 'qwen-27b', builder: 'qwen-27b', approver: 'claude-opus', 'reviewer-security': 'claude-opus', 'reviewer-ux': 'gemma-4', 'reviewer-ui': 'gemma-4',
        'reviewer-code': 'gemma-4', lead: 'claude-opus', coder: 'qwen-27b', 'repo-scout': 'qwen-27b' } },
    'gpt-oss': { title: 'gpt-oss judges, DeepSeek builds', kind: 'both',
      does: 'gpt-oss-120b on llama.cpp writes specs, reviews security and approves; DeepSeek V4 Pro builds and leads, V4 Flash reviews.',
      when: 'You can serve gpt-oss-120b locally and have a DeepSeek key.',
      providers: { llamacpp: null, deepseek: 'DEEPSEEK_API_KEY' },
      models: { 'gpt-oss': 'llamacpp/gpt-oss-120b', 'deepseek-pro': 'deepseek/deepseek-v4-pro', 'deepseek-flash': 'deepseek/deepseek-v4-flash', opus: 'llamacpp/gpt-oss-120b',
        sonnet: 'deepseek/deepseek-v4-flash', haiku: 'deepseek/deepseek-v4-flash' },
      agents: { 'spec-writer': 'gpt-oss', builder: 'deepseek-pro', approver: 'gpt-oss', 'reviewer-security': 'gpt-oss', 'reviewer-ux': 'deepseek-flash', 'reviewer-ui': 'deepseek-flash',
        'reviewer-code': 'deepseek-flash', lead: 'deepseek-pro', coder: 'deepseek-pro', 'repo-scout': 'gpt-oss' } },
    'local-first': { title: 'Local first', kind: 'both',
      does: 'The local Flash-Next builds, codes, scouts and reviews security; Claude Opus writes the spec, approves and leads; GPT-6.1 Sol, Claude Sonnet and Gemini review code, UX and UI.',
      when: 'You run Flash-Next on TabbyAPI or Strata and pay for API models through one NanoGPT key.',
      providers: { tabby: 'TABBY_API_KEY', strata: 'STRATA_API_KEY', llamacpp: null, nanogpt: 'NANOGPT_API_KEY', 'nanogpt-anthropic': 'NANOGPT_API_KEY' },
      models: { 'flash-next': 'tabby/Qwen3.8-Flash-Next-Uncensored-EXL3-2.05bpw', 'swift-27b': 'tabby/Swift-1.5-Qwen3.8-27B-EXL3-5bpw',
        'thinkingcap-27b': 'tabby/ThinkingCap-Qwen3.8-27B-EXL3-5bpw', 'qwen-27b-q8': 'llamacpp/qwen3.8-27b-q8_0', 'gemma-4-31b': 'llamacpp/gemma-4-31b-q8_0',
        'claude-opus': 'nanogpt-anthropic/claude-opus-5-5', 'claude-sonnet': 'nanogpt-anthropic/claude-sonnet-5-5', 'claude-haiku': 'nanogpt-anthropic/claude-haiku-5-5',
        'gpt-sol-high': 'nanogpt/openai/gpt-6.1-sol', 'gpt-sol-xhigh': 'nanogpt/openai/gpt-6.1-sol', 'muse-spark': 'nanogpt/meta/muse-spark-1.3',
        'gemini-argon': 'nanogpt/google/gemini-4-argon', 'qwen-max': 'nanogpt/qwen/qwen3.8-max', mimo: 'nanogpt/xiaomi/mimo-v2.6-pro',
        'claude-opus-max': 'nanogpt-anthropic/claude-opus-5-5', 'claude-fable': 'nanogpt-anthropic/claude-fable-5-1', 'gpt-5.6-sol': 'nanogpt/openai/gpt-5.6-sol',
        'deepseek-flash': 'nanogpt/deepseek/deepseek-v4.1-flash-1017:thinking', opus: 'nanogpt-anthropic/claude-opus-5-5', sonnet: 'nanogpt-anthropic/claude-sonnet-5-5',
        haiku: 'nanogpt-anthropic/claude-haiku-5-5' },
      agents: { 'spec-writer': 'claude-opus', builder: 'flash-next', coder: 'flash-next', 'repo-scout': 'flash-next', 'reviewer-security': 'flash-next',
        'reviewer-code': 'gpt-sol-high', 'reviewer-ux': 'claude-sonnet', 'reviewer-ui': 'gemini-argon', approver: 'claude-opus', lead: 'claude-opus' } },
    'nanogpt-value': { title: 'NanoGPT value (all API)', kind: 'api',
      does: 'Muse Spark builds and codes; four model families review; Claude Opus writes the spec, approves and leads, all through one NanoGPT key.',
      when: 'You have no local GPU and want strong, low-cost API models on every step.',
      providers: { nanogpt: 'NANOGPT_API_KEY', 'nanogpt-anthropic': 'NANOGPT_API_KEY' },
      models: { 'claude-opus': 'nanogpt-anthropic/claude-opus-5-5', 'claude-sonnet': 'nanogpt-anthropic/claude-sonnet-5-5', 'claude-haiku': 'nanogpt-anthropic/claude-haiku-5-5',
        'gpt-sol-high': 'nanogpt/openai/gpt-6.1-sol', 'gpt-sol-xhigh': 'nanogpt/openai/gpt-6.1-sol', 'muse-spark': 'nanogpt/meta/muse-spark-1.3',
        'gemini-argon': 'nanogpt/google/gemini-4-argon', 'qwen-max': 'nanogpt/qwen/qwen3.8-max', mimo: 'nanogpt/xiaomi/mimo-v2.6-pro',
        opus: 'nanogpt-anthropic/claude-opus-5-5', sonnet: 'nanogpt-anthropic/claude-sonnet-5-5', haiku: 'nanogpt-anthropic/claude-haiku-5-5' },
      agents: { 'spec-writer': 'claude-opus', builder: 'muse-spark', coder: 'muse-spark', 'repo-scout': 'mimo', 'reviewer-security': 'qwen-max',
        'reviewer-code': 'gpt-sol-high', 'reviewer-ux': 'claude-sonnet', 'reviewer-ui': 'gemini-argon', approver: 'claude-opus', lead: 'claude-opus' } },
  };
  /** One line per preset for servers that list presets by name only (older servers). */
  const PRESET_TEXT = Object.fromEntries(Object.keys(PRESETS).map((k) => [k, PRESETS[k].does]));
  const PRESET_KIND = Object.fromEntries(Object.keys(PRESETS).map((k) => [k, PRESETS[k].kind]));
  const TIERS = ['opus', 'sonnet', 'haiku'];
  /** Delivery modes (§25): what APPROVE means for a job. */
  const DELIVERY = [
    { id: 'merge', title: 'Merge into the base branch', short: 'merge', icon: 'merge',
      does: 'The gate merges approved work into the base branch; protected paths and large diffs still come to you.',
      when: 'Routine work you trust the line with.', badges: [B.rec] },
    { id: 'branch', title: 'Keep it on its branch', short: 'branch', icon: 'branch',
      does: 'Approved work stays on its job branch for you to merge or open a pull request from.',
      when: 'You review and merge yourself, or push branches to a forge.', badges: [] },
    { id: 'folder', title: 'Export to a folder', short: 'folder', icon: 'folder',
      does: 'Writes changes.patch, the changed files and REPORT.md to the output folder; the branch is kept.',
      when: 'The work goes somewhere else, or you want to read it outside git.', badges: [] },
  ];
  const DELIVERY_LINE = {
    merge: 'APPROVE merges into the base branch (the gate still sends protected paths and large diffs to you).',
    branch: 'APPROVE keeps the work on its branch; you merge it when you are ready.',
    folder: 'APPROVE exports changes.patch, the changed files and REPORT.md to the output folder; the branch is kept.',
  };
  /** The default protected paths (config DEFAULTS escalate_paths), and two profiles around them. */
  const STANDARD_PATHS = ['.github/**', '.gitlab-ci.yml', '.circleci/**', 'Jenkinsfile', '.claude/**', '.qwen/**', '.codex/**', '.agents/**',
    'AGENTS.md', 'CLAUDE.md', 'QWEN.md', '**/Dockerfile', 'docker-compose*.yml', '**/migrations/**', 'package.json', 'package-lock.json',
    'pnpm-lock.yaml', 'yarn.lock', 'requirements*.txt', 'pyproject.toml', 'poetry.lock', 'uv.lock', 'go.mod', 'go.sum', 'Cargo.toml',
    'Cargo.lock', 'Gemfile', 'Gemfile.lock', '**/.env*', '**/*.pem', '**/*.key'];
  const PROTECTED_PROFILES = {
    standard: STANDARD_PATHS,
    strict: STANDARD_PATHS.concat(['**/auth/**', '**/security/**', '**/permissions/**', '**/payments/**', '**/billing/**', 'infra/**', '**/*.tf', '**/*.sql']),
    minimal: ['.github/**', '.gitlab-ci.yml', '.claude/**', '.agents/**', 'AGENTS.md', 'CLAUDE.md', '**/.env*', '**/*.pem', '**/*.key'],
  };
  const LINE_AGENTS = ['spec-writer', 'builder', 'approver'];
  const SIDE_AGENTS = ['lead', 'coder', 'repo-scout'];
  const MCP_AGENTS = ['analyst', 'coder', 'reviewer-security'];

  // ================================================================ draft helpers
  // A draft: {choices: {stepId: optionId | [optionIds]}, values: {stepId: {fieldId: value}}, done: {stepId: true}, at}.
  function emptyDraft() { return { choices: {}, values: {}, done: {}, at: null }; }
  function normDraft(d) {
    const out = emptyDraft();
    if (!isObj(d)) return out;
    if (isObj(d.choices)) out.choices = d.choices;
    if (isObj(d.values)) out.values = d.values;
    if (isObj(d.done)) out.done = d.done;
    if (typeof d.at === 'string') out.at = d.at;
    return out;
  }
  const choiceOf = (draft, stepId) => (draft && draft.choices ? draft.choices[stepId] : undefined);
  function valueOf(draft, stepId, field, dflt) {
    const v = draft && draft.values && isObj(draft.values[stepId]) ? draft.values[stepId][field] : undefined;
    return v === undefined || v === null ? dflt : v;
  }
  function setValue(draft, stepId, field, value) {
    if (!isObj(draft.values[stepId])) draft.values[stepId] = {};
    if (value === undefined) delete draft.values[stepId][field]; else draft.values[stepId][field] = value;
  }
  const listOf = (v) => (Array.isArray(v) ? v : v === undefined || v === null || v === '' ? [] : [v]);
  const linesOf = (v) => (Array.isArray(v) ? v : String(v || '').split('\n')).map((s) => String(s).trim()).filter(Boolean);

  // ================================================================ context helpers (ctx = {cfg, state, setup, secrets, ...})
  const cfgOf = (ctx) => (ctx && isObj(ctx.cfg) ? ctx.cfg : {});
  const sect = (ctx, name) => (isObj(cfgOf(ctx)[name]) ? cfgOf(ctx)[name] : {});
  const reposOf = (ctx) => (ctx && ctx.state && Array.isArray(ctx.state.repos) ? ctx.state.repos.filter((r) => r && r.name) : []);
  /** A registered repository as a compact card: its path under the name and its base branch as a badge. */
  const repoCard = (r, prefix) => ({ id: prefix + r.name, repo: r.name, title: r.name, icon: 'folder', mono: true, sub: String(r.path || ''), compact: true,
    badges: ['base ' + (r.base || 'auto-detect')] });
  function reviewersOf(ctx) {
    const p = ctx && ctx.state && isObj(ctx.state.pipeline) ? ctx.state.pipeline : sect(ctx, 'pipeline');
    if (Array.isArray(p.reviewers) && p.reviewers.length) return p.reviewers.map(String);
    // No pipeline yet: the reviewer agents the server knows (reviewer-<name>), never a fixed table.
    const names = ctx && ctx.state && Array.isArray(ctx.state.agents) ? ctx.state.agents.map((a) => a && a.name).filter(Boolean) : [];
    return names.filter((n) => /^reviewer-/.test(n)).map((n) => n.slice(9));
  }
  function agentNames(ctx) {
    const list = ctx && ctx.state && Array.isArray(ctx.state.agents) ? ctx.state.agents : [];
    return list.map((a) => a && a.name).filter(Boolean);
  }
  const hasAgent = (ctx, name) => agentNames(ctx).indexOf(name) >= 0;
  function deliveryDefault(ctx) {
    const m = sect(ctx, 'merge');
    return DELIVERY.some((d) => d.id === m.delivery) ? m.delivery : 'merge';
  }
  function keySet(ctx, name) {
    const s = ctx && ctx.secrets;
    if (s && Object.prototype.hasOwnProperty.call(s, name)) return !!s[name];
    // Without GET /api/secrets: a provider that reads this env var and has its key present.
    const provs = sect(ctx, 'providers');
    return Object.keys(provs).some((p) => isObj(provs[p]) && provs[p].api_key_env === name && provs[p].key_present === true);
  }
  /** The first sentence of a description, cut at a word (never mid-word) when it is still long. */
  function firstSentence(text, max) {
    const s = String(text || '').replace(/\s+/g, ' ').trim();
    const m = /^(.+?[.!?])(\s|$)/.exec(s);
    let out = m ? m[1] : s;
    const lim = max || 170;
    if (out.length > lim) { out = out.slice(0, lim); out = out.slice(0, Math.max(out.lastIndexOf(' '), 40)).replace(/[\s,;:·-]+$/, '') + '…'; }
    return out;
  }
  const humanName = (name) => String(name).replace(/[-_]+/g, ' ').replace(/^./, (c) => c.toUpperCase());
  /** config_view.presets: names (older servers) or {name, description, ...} objects (§25). Non-routing examples
   *  (an MCP example such as rea-static) are kept apart: `routing` false. */
  function normalizePresets(raw) {
    const out = [];
    for (const p of Array.isArray(raw) ? raw : []) {
      const name = typeof p === 'string' ? p : isObj(p) ? String(p.name || p.id || '') : '';
      if (!name) continue;
      const o = isObj(p) ? p : {};
      const known = PRESETS[name] || null;
      const description = String(o.description || o.summary || o.title || PRESET_TEXT[name] || ('examples/' + name + '.json'));
      const sections = Array.isArray(o.sections) ? o.sections.map(String) : null;
      // settings.preset_summaries: replaces {providers, models, agents: [names]}, merges {section: [names]}.
      const rep = isObj(o.replaces) ? o.replaces : null;
      const routes = (k) => !!rep && Array.isArray(rep[k]) && rep[k].length > 0;
      const routing = rep ? routes('agents') || routes('models') : sections ? sections.indexOf('agents') >= 0 || sections.indexOf('models') >= 0 : !/^rea\b|mcp/i.test(name);
      const kind = o.kind || o.where || PRESET_KIND[name] || (/local|ollama|strata|llama/i.test(name) ? 'local' : 'api');
      const said = /\bNeeds ([A-Z][A-Z0-9_]+)(?:,? (?:and|or) ([A-Z][A-Z0-9_]+))?/.exec(description);
      const keys = Array.isArray(o.keys) ? o.keys.map(String) : Array.isArray(o.needs) ? o.needs.map(String)
        : known ? Object.values(known.providers).filter(Boolean) : said ? [said[1]].concat(said[2] ? [said[2]] : []) : [];
      const generic = kind === 'local' ? 'Everything should stay on your machine.' : kind === 'both' ? 'You run a local model and have an API key.' : 'You use hosted models only.';
      out.push({ name, description, routing, kind: String(kind), keys, sections, known,
        title: known ? known.title : humanName(name), does: known ? known.does : firstSentence(description), when: known ? known.when : generic });
    }
    return out;
  }
  const routingPresets = (ctx) => normalizePresets(cfgOf(ctx).presets).filter((p) => p.routing);
  /** The routing profiles a run can name (DESIGN §23): the saved ones and the default; under a routing preset also the
   *  preset's, with the preset's default (or none), as the server merges a preset. {names, def}. */
  function profileChoices(ctx, preset) {
    const cfg = cfgOf(ctx);
    const names = new Set(isObj(cfg.profiles) ? Object.keys(cfg.profiles).filter((k) => isObj(cfg.profiles[k])) : []);
    let def = typeof cfg.default_profile === 'string' && cfg.default_profile ? cfg.default_profile : null;
    const raw = preset && Array.isArray(cfg.presets) ? cfg.presets.find((p) => isObj(p) && p.name === preset) : null;
    if (raw && isObj(raw.replaces) && Array.isArray(raw.replaces.agents) && raw.replaces.agents.length) {
      for (const n of Array.isArray(raw.replaces.profiles) ? raw.replaces.profiles : []) names.add(String(n));
      def = typeof raw.default_profile === 'string' && raw.default_profile ? raw.default_profile : null;
    }
    return { names: Array.from(names).sort(), def };
  }
  /** The preset the Start draft routes with, if any. */
  const startPreset = (draft) => { const r = choiceOf(draft, 'routing') || 'default'; return typeof r === 'string' && r.indexOf('preset:') === 0 ? r.slice(7) : null; };
  /** The Routing step's profile field: [value, label] options ('' the default, 'none', 'profile:<name>'), none when
   *  there is no profile to pick. */
  function profileFieldOptions(draft, ctx, chat) {
    const c = profileChoices(ctx, chat ? null : startPreset(draft));
    if (!c.names.length) return [];
    const cfg = cfgOf(ctx);
    const desc = (n) => (isObj(cfg.profiles) && isObj(cfg.profiles[n]) && typeof cfg.profiles[n].description === 'string' ? cfg.profiles[n].description : '');
    const out = [['', c.def ? 'The default: ' + c.def : 'No profile (the default)']];
    for (const n of c.names) if (n !== c.def) out.push(['profile:' + n, n + (desc(n) ? ' — ' + firstSentence(desc(n), 70) : '')]);
    if (c.def) out.push(['none', 'No profile (the routing as saved)']);
    return out;
  }
  /** The profile a Start draft sends: undefined (the default), "" (none) or a name it can name with its routing. */
  function startProfile(draft, ctx, chat) {
    const v = String(valueOf(draft, chat ? 'describe' : 'routing', 'profile', '') || '');
    const opts = profileFieldOptions(draft, ctx, chat).map((o) => o[0]);
    if (!v || opts.indexOf(v) < 0) return undefined;
    return v === 'none' ? '' : v.slice(8);
  }
  /** A chat's profile field (New chat takes one too: it places the chat's agent, a model you pick goes over it). */
  function chatProfileField(draft, ctx) {
    const options = profileFieldOptions(draft, ctx, true);
    return options.length ? [{ id: 'profile', label: 'Routing profile', type: 'select', options, default: '', optional: true,
      hint: 'Places the chat’s agent on a model, with its reason. A model you pick above goes over it.' }] : [];
  }
  const reaPresets = (ctx) => normalizePresets(cfgOf(ctx).presets).filter((p) => /^rea\b|rea-/.test(p.name) && !p.routing);
  const kindBadge = (kind) => (kind === 'local' ? B.local : kind === 'both' ? B.both : B.api);
  /** A key's state for badges: set on the server, typed in this setup (not saved yet), or missing. */
  function keyState(ctx, name) {
    if (keySet(ctx, name)) return 'set';
    return ctx && Array.isArray(ctx.typed) && ctx.typed.indexOf(name) >= 0 ? 'typed' : 'missing';
  }
  /** Badges for a key list: "Needs NAME" for the first missing one, else "Key set" / "Key in this setup". */
  function keyBadges(ctx, keys) {
    const list = Array.from(new Set(keys || []));
    if (!list.length) return [];
    const missing = list.filter((k) => keyState(ctx, k) === 'missing');
    if (missing.length) return ['Needs ' + missing[0] + (missing.length > 1 ? ' +' + (missing.length - 1) : '')];
    return list.some((k) => keyState(ctx, k) === 'typed') ? ['Key in this setup'] : [list.length > 1 ? 'Keys set' : 'Key set'];
  }

  // ================================================================ routes: which model each step runs on
  const PROVIDER_TITLE = Object.fromEntries(LOCAL_SERVERS.map((s) => [s.provider, s.title]).concat(API_PROVIDERS.map((p) => [p.provider, p.title.replace(/ \(.*\)$/, '')])));
  const providerTitle = (p) => PROVIDER_TITLE[p] || String(p || '?');
  const agentInfo = (ctx, name) => (ctx && ctx.state && Array.isArray(ctx.state.agents) ? ctx.state.agents.find((a) => a && a.name === name) || null : null);
  const lineSteps = (ctx) => ['spec-writer', 'builder'].concat(reviewersOf(ctx).map((r) => 'reviewer-' + r), ['approver']);
  /** Steps a routing change is shown for: the line, then the lead, Chat agents and the scout the server has. */
  function routedSteps(ctx) {
    const known = agentNames(ctx);
    return lineSteps(ctx).concat(['lead', 'coder', 'analyst', 'repo-scout'].filter((n) => !known.length || known.indexOf(n) >= 0));
  }
  function curModels(ctx) {
    const m = cfgOf(ctx).models;
    return isObj(m) ? m : ctx && ctx.state && isObj(ctx.state.models) ? ctx.state.models : {};
  }
  /** The model key an agent runs on under these routes: its own route, else '*', else its file's tier. */
  function routeKey(agents, ctx, name) {
    const own = isObj(agents) && isObj(agents[name]) && agents[name].model ? agents[name].model : null;
    if (own) return String(own);
    const star = isObj(agents) && isObj(agents['*']) && agents['*'].model ? agents['*'].model : null;
    if (star) return String(star);
    const a = agentInfo(ctx, name);
    return a && a.tier ? String(a.tier) : /^reviewer-(ux|ui|code)$|^repo-scout$/.test(name) ? 'sonnet' : 'opus';
  }
  /** The key an agent runs on now: what the server resolved (state.agents[].model), else the saved routes. */
  function nowKey(ctx, name) {
    const a = agentInfo(ctx, name);
    return a && a.model ? String(a.model) : routeKey(cfgOf(ctx).agents, ctx, name);
  }
  /** "qwen3.8-27b · llama.cpp" for a model key. */
  function modelText(models, key) {
    const e = isObj(models) ? models[key] : null;
    return isObj(e) && e.model ? e.model + ' · ' + providerTitle(e.provider) : key ? key + ' (no such model)' : '(not set)';
  }
  /** One line on the saved routing of the line, such as "deepseek-v4-pro for every step". */
  function routingSummary(ctx) {
    const models = curModels(ctx);
    const steps = lineSteps(ctx);
    const by = new Map();
    for (const s of steps) {
      const a = agentInfo(ctx, s);
      const drv = a && a.driver && a.driver !== 'builtin' ? 'the ' + a.driver + ' CLI' : null;
      const e = models[nowKey(ctx, s)];
      const t = drv || (isObj(e) && e.model ? e.model : nowKey(ctx, s));
      if (!by.has(t)) by.set(t, []);
      by.get(t).push(s);
    }
    const groups = Array.from(by.entries()).sort((x, y) => y[1].length - x[1].length);
    if (groups.length === 1) return groups[0][0] + ' for every step';
    const who = (names) => (names.length === 1 ? names[0] : names.every((n) => /^reviewer-/.test(n)) ? names.length + ' reviewers' : names.length + ' steps');
    return groups.slice(0, 3).map(([t, names]) => t + ' for ' + who(names)).join(', ') + (groups.length > 3 ? ', …' : '');
  }
  const splitModel = (s) => { const i = String(s).indexOf('/'); return { provider: String(s).slice(0, i), model: String(s).slice(i + 1) }; };
  /** Preset model keys the user's first local server takes over: the preset's local entry and every entry that is the
   *  same model (a tier such as opus). */
  const takenOver = (P) => (P && P.models.local ? Object.keys(P.models).filter((k) => P.models[k] === P.models.local) : []);
  /** The providers and models the draft's local servers become (the first is model "local", the others local-<id>). */
  function localEntries(draft, ctx) {
    const providers = {};
    const models = {};
    const w = where(draft);
    if (w !== 'local' && w !== 'hybrid') return { providers, models };
    localPicked(draft).forEach((s, i) => {
      const g = (f, d) => valueOf(draft, 'local-details', s.id + '.' + f, d);
      const cur = isObj(sect(ctx, 'providers')[s.provider]) ? sect(ctx, 'providers')[s.provider] : {};
      const url = String(g('url', cur.base_url || 'http://127.0.0.1:' + s.port + '/v1')).trim();
      const prov = { type: 'openai', base_url: url, api_key_env: s.keyEnv || null, max_concurrency: Number(g('slots', s.slots)) || 1, concurrency_scope: 'run', timeout: 1800 };
      if (s.auth) prov.auth = s.auth;
      if (keyFileOf(ctx, s) && cur.api_key_file === undefined) prov.api_key_file = s.keyFile;
      providers[s.provider] = prov;
      const key = localKey(i, s);
      models[key] = { provider: s.provider, model: String(g('model', s.model)).trim(), max_tokens: 16384, context_tokens: Number(g('context', s.context)) || s.context, stream: true };
      if (!isObj(sect(ctx, 'models')[key])) models[key].extra_body = {};
    });
    return { providers, models };
  }
  /** The env var a provider's key comes from after a change: the preset's own entry, a local server picked here, else
   *  the saved provider. */
  function providerKey(prov, P, local, ctx) {
    if (P && Object.prototype.hasOwnProperty.call(P.providers, prov)) return P.providers[prov];
    if (isObj(local.providers[prov])) return local.providers[prov].api_key_env || null;
    const cur = sect(ctx, 'providers')[prov];
    return isObj(cur) && cur.api_key_env ? cur.api_key_env : null;
  }
  /** What routing ends up as after Apply, with or without a preset: per step before → after, the preset entries a local
   *  server takes over, models the change adds that no step uses, and every key the result needs. */
  function routingOutcome(presetName, draft, ctx) {
    const P = presetName ? PRESETS[presetName] || null : null;
    if (presetName && !P) return null;
    const before = curModels(ctx);
    const models = clone(before) || {};
    const agents = clone(cfgOf(ctx).agents) || {};
    const local = localEntries(draft, ctx);
    let takeover = null;
    if (P) {
      for (const k of Object.keys(P.models)) models[k] = splitModel(P.models[k]);
      for (const a of Object.keys(P.agents)) agents[a] = { model: P.agents[a] };
      agents['*'] = { driver: 'builtin' };
      const first = local.models.local;
      const keys = takenOver(P);
      if (first && keys.length) {
        const from = splitModel(P.models.local);
        takeover = { keys, from: from.model + ' · ' + providerTitle(from.provider), to: first.model + ' · ' + providerTitle(first.provider) };
      }
    }
    for (const k of Object.keys(local.models)) models[k] = local.models[k];
    if (takeover) for (const k of takeover.keys) models[k] = local.models.local;
    const steps = routedSteps(ctx);
    const rows = steps.map((s) => {
      const b = modelText(before, nowKey(ctx, s));
      const a = modelText(models, P ? routeKey(agents, ctx, s) : nowKey(ctx, s));
      return { agent: s, before: b, after: a, changed: b !== a };
    });
    const everyone = Array.from(new Set(steps.concat(agentNames(ctx))));
    const used = new Set(everyone.map((s) => (P ? routeKey(agents, ctx, s) : nowKey(ctx, s))));
    const unused = Object.keys(local.models).filter((k) => !used.has(k) && !(takeover && takeover.keys.some((t) => used.has(t)) && k === 'local'));
    const keyNames = [];
    for (const k of used) {
      const e = models[k];
      const env = isObj(e) ? providerKey(e.provider, P, local, ctx) : null;
      if (env && keyNames.indexOf(env) < 0) keyNames.push(env);
    }
    return { rows, takeover, unused, keys: keyNames.map((n) => ({ name: n, state: keyState(ctx, n) })) };
  }
  /** The keys a routing preset needs with this draft (the table's presets exactly, others from their description). */
  function presetKeys(p, draft, ctx) {
    const o = p && PRESETS[p.name] ? routingOutcome(p.name, draft, ctx) : null;
    return o ? o.keys.map((k) => k.name) : (p && p.keys) || [];
  }

  // ================================================================ flows
  const SETUP_GROUPS = { models: 'Models', providers: 'Providers', routing: 'Routing', tools: 'Tools', safety: 'Safety', repos: 'Repository', output: 'Output', review: 'Review' };
  const localPicked = (draft) => listOf(choiceOf(draft, 'local-servers')).map((id) => LOCAL_SERVERS.find((s) => s.id === id)).filter(Boolean);
  /** What GET /api/setup says about this machine (doctor.setup_status's host): {key_dirs: {folder: exists},
   *  listening: [ports on 127.0.0.1 or every address]}, and only after the owner pressed "Look for model servers"
   *  (POST /api/setup/look-around) model_servers: [{url, port, status, ids}] (those that answered /v1/models like a
   *  model server) with model_servers_at (when), or model_servers_error instead of model_servers when that look
   *  broke; neither when it was not asked. Nothing here looks by itself. */
  const hostOf = (ctx) => (ctx && isObj(ctx.setup) && isObj(ctx.setup.host) ? ctx.setup.host : null);
  /** A local server's key file, unless this machine says its folder is not there (a preset's placeholder). */
  function keyFileOf(ctx, s) {
    if (!s.keyFile) return null;
    const dirs = (hostOf(ctx) || {}).key_dirs;
    return isObj(dirs) && dirs[s.keyFile.replace(/\/[^/]*$/, '')] === false ? null : s.keyFile;
  }
  /** Whether something listens on a local server's usual port here: true, false, or null (not asked). */
  function listensOn(ctx, port) {
    const ports = (hostOf(ctx) || {}).listening;
    return Array.isArray(ports) ? ports.indexOf(port) >= 0 : null;
  }
  /** A model id or address a listener gave, as shown: the program chooses it, so a long one is cut at 60 characters. */
  const idText = (v) => { const t = String(v); return t.length > 60 ? t.slice(0, 59) + '…' : t; };
  /** "lists a, b, c, …" for a model server's first three model ids. */
  const listsText = (ids) => 'lists ' + ids.slice(0, 3).map(idText).join(', ') + (ids.length > 3 ? ', …' : '');
  /** The Address field's hint when nothing listens on the preset's port: the model servers the owner's look found
   *  here (their address to use instead: the presets' ports are only the usual ones), else the ports in use, and
   *  how to tell which of them is a model server (the look-around button above the servers). A look that broke
   *  (model_servers_error) found nothing, so the hint does not say that nothing answered. */
  function listenHint(ctx, s) {
    if (listensOn(ctx, s.port) !== false) return '';
    const host = hostOf(ctx);
    const looked = Array.isArray(host.model_servers);
    const found = (looked ? host.model_servers : []).filter((m) => isObj(m) && m.url && m.port !== s.port).slice(0, 4);
    if (found.length) {
      return 'Nothing listens on 127.0.0.1:' + s.port + ' now. Model servers answer here: ' + found.map((m) => idText(m.url) +
        (Array.isArray(m.ids) && m.ids.length ? ' (' + listsText(m.ids) + ')' : m.status === 200 ? '' : ' (wants a key)')).join('; ') +
        '. If one of them is ' + s.title + ', use its address.';
    }
    const ports = (host.listening || []).filter((p) => p !== s.port).slice(0, 8);
    return 'Nothing listens on 127.0.0.1:' + s.port + ' now.' + (ports.length ? ' Ports in use here: ' + ports.join(', ') + '.' +
      (looked ? ' None of them answered like a model server when you looked.'
        : host.model_servers_error ? ' The last look for model servers did not work: press “Look for model servers” above to try again.'
          : ' Press “Look for model servers” above to see which of them is a model server.') : '');
  }
  const apiPicked = (draft) => listOf(choiceOf(draft, 'api-providers')).map((id) => API_PROVIDERS.find((p) => p.id === id)).filter(Boolean);
  // Unanswered, the question projects its recommended answer (Hybrid), like every other choice on the path.
  const where = (draft) => choiceOf(draft, 'where') || 'hybrid';
  function localKey(i, s) { return i === 0 ? 'local' : 'local-' + s.id; }
  const urlOk = (v) => (/^https?:\/\/[^\s/]+/i.test(String(v || '').trim()) ? null : 'Enter a URL such as http://127.0.0.1:8080/v1.');
  const intOk = (min) => (v) => (v === '' || v === undefined || v === null ? null : Number.isInteger(Number(v)) && Number(v) >= min ? null : 'A whole number of at least ' + min + '.');

  const WELCOME = {
    id: 'welcome', title: 'Welcome', sub: 'What would you like to do?', start: 'what',
    steps: [{
      id: 'what', title: 'What would you like to do?', short: 'Start', kind: 'choice', group: 'welcome',
      procedure: [
        ['Pick the work, not the tool.', 'Each card says what happens next and what it needs. Home links back here.'],
        ['Set up once.', 'Setup connects your model servers or API keys, decides who does what, and registers a repository. Cards that need it say so and take you there first.'],
        ['Watch it happen.', 'Everything you start appears on the board, in Plans or in Chat, with every agent’s work as it runs.'],
      ],
      options: (draft, ctx) => {
        // A factory already in use (jobs, plans or chats) counts as set up even with a key missing: the cards
        // start work, and Welcome points at what needs attention instead.
        const ready = !!(ctx && ctx.setup && (ctx.setup.configured || ctx.used));
        const repos = reposOf(ctx).length > 0;
        const rea = isObj(sect(ctx, 'mcp_servers').rea);
        const needs = !ready || !repos;
        return [
          { id: 'setup', title: 'Set up the harness', icon: 'settings', go: '#/setup',
            does: 'Connect model servers or API keys, choose who does what, add a repository and decide where finished work goes.',
            when: ready ? 'You want to change how the factory runs.' : 'This is a fresh install: start here.', badges: ready ? [] : [B.rec] },
          { id: 'feature', title: 'Build a feature', icon: 'zap', go: needs ? '#/setup' : '#/start/repo', start: { what: 'feature' },
            does: 'Describe one change. A spec-writer, a builder and the reviewers take it to an approved branch.',
            when: 'The change fits one focused pull request.', badges: needs ? [B.setup] : [] },
          { id: 'plan', title: 'Plan a big change', icon: 'plan', go: needs ? '#/setup' : '#/start/repo', start: { what: 'plan' },
            does: 'A lead model splits a large request into ordered jobs, runs them as their dependencies merge, then reviews the whole.',
            when: 'The work is bigger than one pull request: a migration, an API and a UI.', badges: needs ? [B.setup] : [] },
          { id: 'chat', title: 'Chat with an agent', icon: 'chat', go: needs ? '#/setup' : '#/start/repo', start: { what: 'chat' },
            does: 'Work turn by turn with the coder agent on its own branch, then hand the branch to the factory for review.',
            when: 'You want to explore, pair, or steer the work yourself.', badges: needs ? [B.setup] : [] },
          { id: 'analyse', title: 'Analyse a binary (REA)', icon: 'scout', go: !rea ? '#/setup/tools' : needs ? '#/setup' : '#/start/repo', start: { what: 'analyse' },
            does: 'The analyst agent inspects a compiled program with REA over MCP and writes its findings to a notes file.',
            when: 'You need to understand a binary before changing the code around it.', badges: !rea || needs ? [B.setup] : [] },
          { id: 'board', title: 'Open the board', icon: 'factory', go: '#/factory',
            does: 'See every job on the factory line, what each agent is doing and what waits for you.',
            when: 'Work is already running, or you want to look around first.', badges: [] },
          { id: 'home', title: 'Open Home', icon: 'home', go: '#/home',
            does: 'Every section of the harness on one card, with what needs you first and how each section is doing.',
            when: 'You want the whole picture: the tracker, personas, messages and what is planned.', badges: [] },
        ];
      },
    }],
  };

  const SETUP = {
    id: 'setup', title: 'Set up the harness', sub: 'Models, keys, who does what, tools, safety, a repository and where finished work goes. Nothing is saved until you apply, except a key you test.', start: 'where', groups: SETUP_GROUPS,
    steps: [
      { id: 'where', title: 'Where do your models run?', short: 'Models run', kind: 'choice', group: 'models',
        procedure: [
          ['Pick where agents think.', 'Local servers keep code on your machine and cost nothing per token; hosted APIs add independent judgment and run reviewers in parallel.'],
          ['Mix when it helps.', 'Hybrid keeps the long, token-heavy work local and sends review and the final decision to a different model family.'],
          ['Change it later.', 'Every choice becomes ordinary settings: Settings › Providers, Models and Routing show and edit them.'],
        ],
        options: [
          { id: 'local', title: 'Local servers', icon: 'server', next: 'local-servers',
            does: 'Every agent runs on model servers on this machine: no keys, no per-token cost.',
            when: 'You run a capable model with tool calling and a large context.', badges: [B.local] },
          { id: 'api', title: 'Hosted APIs', icon: 'cloud', next: 'api-providers',
            does: 'Every agent runs on hosted models such as Claude or DeepSeek.',
            when: 'You have no GPU server, or want the strongest models on every step.', badges: [B.api, B.key] },
          { id: 'hybrid', title: 'Hybrid', icon: 'route', next: 'local-servers',
            does: 'A local model writes specs and code; Claude reviews and approves.',
            when: 'You have a strong local model and want independent reviewers.', badges: [B.rec, B.both] },
          { id: 'kit', title: 'Claude Code kit', icon: 'terminal', next: 'tools',
            does: 'Every agent runs through the Claude Code CLI on your Claude login (the claude driver).',
            when: 'You already work in Claude Code; kits/claude-code can also run the whole line inside it.', badges: [B.cli, B.setup] },
        ] },
      { id: 'local-servers', title: 'Which local servers do you run?', short: 'Local servers', kind: 'choice', multi: true, min: 1, group: 'models', next: 'local-details',
        procedure: [
          ['Pick each server you run.', 'Every server becomes a provider with its own queue, so two GPUs can work at once.'],
          ['Start servers with tool calling.', 'Agents call tools: start llama.cpp with --jinja and give vLLM a tool-call parser. Test checks it.'],
          ['One slot, one agent.', 'A one-slot server runs one agent at a time and keeps its cache warm; raise slots only if the server runs more.'],
        ],
        options: (draft, ctx) => LOCAL_SERVERS.map((s) => {
          const here = listensOn(ctx, s.port);
          return { id: s.id, title: s.title, icon: 'server', does: s.does, when: s.when, badges: s.badges.slice(),
            sub: '127.0.0.1:' + s.port + (here === true ? ' · something listens there' : here === false ? ' · nothing listens there now' : '') };
        }) },
      { id: 'local-details', title: 'Addresses, context and slots', short: 'Server details', kind: 'form', group: 'models',
        // Not a choice made here: the answer to "Where do your models run?" decides. The maps draw it as a check.
        next: (draft) => (where(draft) === 'hybrid' ? 'api-providers' : 'roles'),
        ask: 'Hosted APIs too?', branches: [{ to: 'api-providers', label: 'Hybrid' }, { to: 'roles', label: 'Local only' }],
        procedure: [
          ['Point at the server.', 'Use the address it listens on, normally 127.0.0.1 with its port and /v1. Not sure which port? Press “Look for model servers” below.'],
          ['Match the context.', 'Set Context to what the server was started with; agents budget their reading from it.'],
          ['Test before you rely on it.', 'Test sends a short reply and a tool call and reports tokens per second. A failed Test keeps your entries; fix the server and test again.'],
        ],
        // The look for model servers comes first, and only when the owner presses its button (app.js lookAroundWidget).
        groups: (draft, ctx) => [{ id: 'look-around', title: 'Model servers on this machine', widget: 'look-around', fields: [] }].concat(localPicked(draft).map((s, i) => {
          const prov = sect(ctx, 'providers')[s.provider];
          const url = isObj(prov) && prov.base_url ? String(prov.base_url) : 'http://127.0.0.1:' + s.port + '/v1';
          const fields = [
            { id: s.id + '.url', label: 'Address', type: 'text', mono: true, default: url, required: true, check: urlOk, hint: listenHint(ctx, s) || undefined },
            { id: s.id + '.model', label: 'Model id', type: 'text', mono: true, default: s.model, required: true, placeholder: 'e.g. qwen3.8-27b', hint: 'The name the server lists under /v1/models.' },
            { id: s.id + '.context', label: 'Context (tokens)', type: 'number', default: s.context, required: true, check: intOk(1024) },
            { id: s.id + '.slots', label: 'Slots', type: 'number', default: s.slots, required: true, check: intOk(1), hint: 'Requests it runs at once.' },
          ];
          const file = keyFileOf(ctx, s);
          if (s.keyEnv) fields.push({ id: s.id + '.key', label: s.keyEnv, type: 'password', secret: s.keyEnv, optional: true, hint: file ? 'Empty: keep the key that is set, or the file ' + file + '.' : 'Empty keeps the key that is set.' });
          return { id: s.id, title: s.title, sub: 'Saved as model `' + localKey(i, s) + '` (provider `' + s.provider + '`)', tip: s.keyFile && !file ? s.tipNoFile : s.tip, fields,
            test: { kind: 'local', server: s.id, provider: s.provider, key: localKey(i, s), keyEnv: s.keyEnv || null } };
        })) },
      { id: 'api-providers', title: 'Which API providers?', short: 'API providers', kind: 'choice', multi: true, min: 1, group: 'providers', next: 'api-keys',
        procedure: [
          ['Pick the providers you have keys for.', 'Each needs one key; NanoGPT and OpenRouter reach many vendors with one.'],
          ['Review with a different family.', 'Reviewers catch more when they are not the same model as the builder.'],
          ['Keys stay on the server.', 'A key goes into the server’s env file, readable only by its user, and never comes back to this page.'],
        ],
        options: (draft, ctx) => API_PROVIDERS.map((p) => {
          const st = keyState(ctx, p.keyEnv);
          return { id: p.id, title: p.title, icon: 'cloud', does: p.does, when: p.when, sub: p.keyEnv,
            badges: [B.api, st === 'set' ? 'Key set' : st === 'typed' ? 'Key in this setup' : B.key] };
        }) },
      { id: 'api-keys', title: 'API keys', short: 'Keys', kind: 'form', group: 'providers', next: 'roles',
        procedure: [
          ['Paste each key once.', 'Saving writes NAME=value into the server’s env file and loads it into the running server.'],
          ['Keep what is set.', 'A key that is already set says so; leave its field empty to keep it.'],
          ['Test each provider.', 'Test saves the key to the env file right away, then sends one short request and says why it failed when it does.'],
        ],
        groups: (draft, ctx) => apiPicked(draft).map((p) => {
          const set = keySet(ctx, p.keyEnv);
          return { id: p.id, title: p.title, sub: p.keyEnv + (set ? ' is set' : ' is not set yet'), status: set ? 'ok' : 'warn',
            fields: [
              { id: p.id + '.key', label: p.keyEnv, type: 'password', secret: p.keyEnv, optional: set, required: !set, hint: set ? 'Leave empty to keep the key that is set.' : 'Paste the key from the provider’s console.' },
              { id: p.id + '.model', label: 'Model id to test', type: 'text', mono: true, default: p.model, optional: true, placeholder: 'a model id from the provider’s list' },
            ],
            test: { kind: 'api', provider: p.provider, keyEnv: p.keyEnv } };
        }) },
      { id: 'roles', title: 'Who does what?', short: 'Who does what', kind: 'choice', group: 'routing',
        next: 'tools', edgeLabels: { tools: 'a preset / keep', 'roles-custom': 'Custom per step' },
        procedure: [
          ['Start from a preset.', 'A preset routes every step at once: the spec-writer, the builder, each reviewer, the approver, the lead and Chat.'],
          ['Local for volume, API for independence.', 'The spec-writer and the builder read and write the most; reviewers and the approver judge work they did not write.'],
          ['Adjust any step.', 'Custom picks a model per step. Running jobs keep the routing they started with.'],
        ],
        options: (draft, ctx) => {
          const w = where(draft);
          const local = localPicked(draft);
          const api = apiPicked(draft).map((p) => p.id);
          let rec = w === 'hybrid' ? 'hybrid' : w === 'local' ? (local.some((s) => s.id === 'ollama') && !local.some((s) => s.id === 'strata') ? 'ollama' : 'strata')
            : api.indexOf('anthropic') >= 0 ? 'anthropic' : api.indexOf('deepseek') >= 0 ? 'deepseek' : api.indexOf('nanogpt') >= 0 ? 'nanogpt' : null;
          const presets = routingPresets(ctx);
          if (rec && !presets.some((p) => p.name === rec)) rec = null;
          // Presets for where your models run come first; the rest sit under "Other presets".
          const want = w === 'hybrid' ? 'both' : w === 'local' ? 'local' : 'api';
          const card = (p, other) => ({ id: 'preset:' + p.name, title: p.title, preset: p.name, icon: 'route', does: p.does, when: p.when,
            details: p.description && p.description !== p.does ? p.description : null, detailsLabel: 'examples/' + p.name + '.json',
            section: other ? 'Other presets' : null,
            badges: (p.name === rec ? [B.rec] : []).concat([kindBadge(p.kind)], keyBadges(ctx, presetKeys(p, draft, ctx))) });
          const match = presets.filter((p) => p.kind === want).sort((a, b) => (a.name === rec ? -1 : b.name === rec ? 1 : 0));
          const rest = presets.filter((p) => p.kind !== want);
          return match.map((p) => card(p, false)).concat([
            { id: 'custom', title: 'Custom per step', icon: 'settings', next: 'roles-custom',
              does: 'Pick the model for each step of the line yourself.', when: 'You know which model suits which step.', badges: [] },
            { id: 'keep', title: 'Keep the current routing', icon: 'check',
              does: 'Every step keeps its route: ' + routingSummary(ctx) + '.',
              when: 'Routing already suits you, or you came here for one setting. Addresses and model ids you entered are still saved.', badges: [] },
          ], rest.map((p) => card(p, true)));
        },
        // A factory that is set up or in use, or a visit that did not start at "Where do your models run?" (a Grid
        // Configure link), keeps its routing: a preset replaces whole sections, so it is applied only when picked.
        default: (draft, ctx) => {
          if ((ctx && ((ctx.setup && ctx.setup.configured) || ctx.used)) || !(draft && draft.done && draft.done.where)) return 'keep';
          const opts = SETUP.steps.find((s) => s.id === 'roles').options(draft, ctx);
          const rec = opts.find((o) => o.badges.indexOf(B.rec) >= 0);
          return rec ? rec.id : 'keep';
        },
        current: () => 'keep', currentText: 'Your routing stays as it is unless you pick a preset or Custom' },
      { id: 'roles-custom', title: 'Pick a model per step', short: 'Per step', kind: 'form', group: 'routing', next: 'tools',
        procedure: [
          ['Go through the line.', 'Each step lists the models you have, local and API, including the ones this setup adds.'],
          ['Leave a step on Default.', 'A step on Default keeps its current route.'],
          ['Keep families apart.', 'Put the approver and at least one reviewer on a different model from the builder.'],
        ],
        groups: (draft, ctx) => [{ id: 'routes', title: 'The factory line', widget: 'routes', steps: LINE_AGENTS.slice(0, 2).concat(reviewersOf(ctx).map((r) => 'reviewer-' + r), ['approver'], SIDE_AGENTS.filter((n) => hasAgent(ctx, n) || n !== 'repo-scout')), fields: [] }] },
      { id: 'tools', title: 'Skills and tools', short: 'Tools', kind: 'form', group: 'tools', next: 'sandbox',
        procedure: [
          ['Give agents your skills.', 'Skill folders hold SKILL.md files; agents see the catalog and load the ones they need.'],
          ['Attach MCP servers deliberately.', 'An MCP server’s tools reach only the agents whose file names it (mcp: rea); expose only the tools they need.'],
          ['Let reviewers look at pages.', 'With the agent-browser CLI installed, the builder and the UX and UI reviewers open pages in a real browser.'],
        ],
        groups: (draft, ctx) => {
          const harness = sect(ctx, 'harness');
          const rea = sect(ctx, 'mcp_servers').rea;
          const examples = reaPresets(ctx);
          // [value, label, one-line hint]; an example's own description sits under the choices, collapsed.
          const reaOpts = [['keep', isObj(rea) ? 'Keep it as it is' : 'Not now', isObj(rea) ? 'The agents keep the REA tools they have.' : 'The analyst works without REA; add it any time.']];
          for (const p of examples) reaOpts.push(['preset:' + p.name, examples.length > 1 ? 'Add REA from ' + p.name : 'Add REA tools for the analyst',
            p.name === 'rea' ? 'The rea and rea-native MCP servers from examples/rea.json, each with a small tool allowlist. Needs rea-agents 6.5 and Node 22.19 or newer.'
              : 'From examples/' + p.name + '.json: ' + firstSentence(p.description, 140)]);
          if (!examples.length || isObj(rea)) reaOpts.push(['form', isObj(rea) ? 'Edit the command' : 'Enter the command yourself', 'For REA installed elsewhere, or started with other arguments.']);
          const reaMode = valueOf(draft, 'tools', 'rea', 'keep');
          return [
            { id: 'skills', title: 'Skills', fields: [
              { id: 'skills_dirs', label: 'Skill folders', type: 'lines', mono: true, default: (Array.isArray(harness.skills_dirs) ? harness.skills_dirs : []).join('\n'), optional: true,
                placeholder: 'one absolute path per line', hint: 'Searched after the default locations (.claude, .qwen and .agents skills folders).' },
            ] },
            { id: 'rea', title: 'REA over MCP', sub: isObj(rea) ? 'Configured: `' + String(rea.command || '?') + '`' : 'Reverse-engineering tools for the analyst agent.',
              about: examples.length === 1 ? { label: 'examples/' + examples[0].name + '.json', text: examples[0].description } : null, fields: [
              { id: 'rea', label: 'REA', type: 'radio', default: 'keep', options: reaOpts },
              { id: 'rea_command', label: 'Command', type: 'text', mono: true, default: isObj(rea) && rea.command ? String(rea.command) : '~/.local/bin/rea-mcp', required: true, showIf: (d) => valueOf(d, 'tools', 'rea', 'keep') === 'form', hint: 'Get the exact command line from rea setup --dry-run.' },
              { id: 'rea_args', label: 'Arguments', type: 'lines', mono: true, default: isObj(rea) && Array.isArray(rea.args) ? rea.args.join('\n') : '', optional: true, showIf: (d) => valueOf(d, 'tools', 'rea', 'keep') === 'form', placeholder: 'one argument per line' },
              { id: 'rea_tools', label: 'Tools allowlist', type: 'text', mono: true, default: isObj(rea) && Array.isArray(rea.tools) ? rea.tools.join(', ') : '', optional: true, showIf: (d) => valueOf(d, 'tools', 'rea', 'keep') === 'form', placeholder: 'empty exposes every tool' },
              { id: 'rea_agents', label: 'Agents that get mcp: rea', type: 'checks', default: MCP_AGENTS.filter((n) => n === 'analyst' && hasAgent(ctx, n)), optional: true,
                options: MCP_AGENTS.filter((n) => hasAgent(ctx, n)).map((n) => [n, n]), showIf: () => reaMode !== 'keep' || isObj(rea),
                hint: 'Adds rea to the mcp: line of each agent file (a shipped agent gets your override copy).' },
            ] },
            { id: 'browser', title: 'agent-browser', widget: 'agent-browser', fields: [] },
          ];
        } },
      { id: 'sandbox', title: 'How are shell commands contained?', short: 'Sandbox', kind: 'choice', group: 'safety', next: 'protected',
        procedure: [
          ['Contain shell commands.', 'With bubblewrap, an agent’s Bash writes only inside its own worktree; reading stays open.'],
          ['Keep the network when tests need it.', 'Package installs and API tests need it; turn it off for repositories you do not trust.'],
          ['Prefer the sandbox on shared machines.', 'Without it, a confused agent can write anywhere your user can.'],
        ],
        options: [
          { id: 'bwrap', title: 'Bubblewrap sandbox', icon: 'layers', does: 'Bash runs in bubblewrap: writes are confined to the job’s worktree.', when: 'bwrap is installed (Doctor checks it); the safe default.', badges: [B.rec] },
          { id: 'none', title: 'No sandbox', icon: 'terminal', does: 'Bash runs as your user, like a terminal you opened yourself.', when: 'bwrap is unavailable, or a repo’s tooling cannot run inside it.', badges: [] },
        ],
        // The saved setting is preselected (passing through changes nothing) and marked Current.
        current: (draft, ctx) => (sect(ctx, 'harness').sandbox === 'none' ? 'none' : sect(ctx, 'harness').sandbox ? 'bwrap' : null),
        default: (draft, ctx) => (sect(ctx, 'harness').sandbox === 'none' ? 'none' : 'bwrap'),
        extra: (draft, ctx) => [{ id: 'net', fields: [
          { id: 'network', label: 'Allow network inside the sandbox', type: 'toggle', default: sect(ctx, 'harness').sandbox_network !== false, showIf: (d) => choiceOf(d, 'sandbox') !== 'none' },
        ] }] },
      { id: 'protected', title: 'Which paths always need you?', short: 'Protected paths', kind: 'choice', group: 'safety', next: 'repo',
        procedure: [
          ['Name what must never change silently.', 'A job that touches a protected path never merges by itself; it waits in Needs human.'],
          ['Start from a profile.', 'Standard covers CI, agent instructions, containers, migrations, dependency files and secrets.'],
          ['Add your own.', 'Each repository can add more, such as content/legal/**.'],
        ],
        options: [
          { id: 'standard', title: 'Standard', icon: 'alert', does: 'CI, agent instructions, containers, migrations, dependency manifests and lockfiles, keys and env files (' + STANDARD_PATHS.length + ' globs).', when: 'Most repositories.', badges: [B.rec] },
          { id: 'strict', title: 'Strict', icon: 'ban', does: 'Standard plus auth, security, permissions, payments, infrastructure and SQL.', when: 'The code handles money, access or production data.', badges: [] },
          { id: 'minimal', title: 'Minimal', icon: 'check', does: 'Only CI, agent instructions, keys and env files.', when: 'A sandbox or toy repository where speed matters more.', badges: [] },
          { id: 'custom', title: 'My own list', icon: 'edit', does: 'Edit the globs yourself, one per line.', when: 'You know exactly what must come to you.', badges: [] },
        ],
        current: (draft, ctx) => {
          const cur = Array.isArray(cfgOf(ctx).escalate_paths) ? cfgOf(ctx).escalate_paths.slice().sort() : null;
          if (!cur) return null;
          for (const k of ['standard', 'strict', 'minimal']) if (stable(PROTECTED_PROFILES[k].slice().sort()) === stable(cur)) return k;
          return 'custom';
        },
        default: (draft, ctx) => SETUP.steps.find((s) => s.id === 'protected').current(draft, ctx) || 'standard',
        extra: (draft, ctx) => [{ id: 'globs', fields: [
          { id: 'globs', label: 'Protected globs', type: 'lines', mono: true, rows: 8, default: (Array.isArray(cfgOf(ctx).escalate_paths) ? cfgOf(ctx).escalate_paths : STANDARD_PATHS).join('\n'), required: true, showIf: (d) => choiceOf(d, 'protected') === 'custom' },
        ] }] },
      { id: 'repo', title: 'Which repository?', short: 'Repository', kind: 'choice', group: 'repos', next: 'output',
        procedure: [
          ['Point at a git checkout.', 'Use the absolute path of the repository’s top folder on the server.'],
          ['Name the base branch.', 'Jobs branch from it and merge back into it; leave it empty to use the checked-out branch.'],
          ['Let the scout write AGENTS.md.', 'Scout reads the repository and drafts how to set it up, run it and test it; every agent reads that file.'],
        ],
        // With no repository yet there is nothing to choose: the step is the form alone (solo).
        solo: true,
        options: (draft, ctx) => reposOf(ctx).map((r) => repoCard(r, 'repo:'))
          .concat([{ id: 'new', title: reposOf(ctx).length ? 'Add another repository' : 'Add a repository', icon: 'plus', does: 'Register another git checkout on this server by its path.', when: 'The code you want to work on is not listed yet.', badges: [] }]),
        default: (draft, ctx) => (reposOf(ctx).length ? 'repo:' + reposOf(ctx)[0].name : 'new'),
        extra: (draft, ctx) => {
          const c = choiceOf(draft, 'repo');
          const existing = typeof c === 'string' && c.indexOf('repo:') === 0 ? reposOf(ctx).find((r) => 'repo:' + r.name === c) : null;
          if (existing) return [{ id: 'existing', fields: [{ id: 'base:' + existing.name, label: 'Base branch', type: 'text', mono: true, default: existing.base || '', optional: true, placeholder: 'auto-detect' }], widget: 'scout', repo: existing.name }];
          const isNew = (d) => choiceOf(d, 'repo') === 'new' || (choiceOf(d, 'repo') === undefined && !reposOf(ctx).length);
          return [{ id: 'new', fields: [
            { id: 'path', label: 'Absolute path', type: 'text', mono: true, required: true, placeholder: '/home/you/code/my-app', check: (v) => (String(v || '').trim().charAt(0) === '/' ? null : 'An absolute path starts with /.'), showIf: isNew },
            { id: 'name', label: 'Name (optional)', type: 'text', mono: true, optional: true, placeholder: 'derived from the folder', check: (v) => (!v || /^[a-z0-9][a-z0-9._-]*$/.test(String(v).trim()) ? null : 'Lowercase letters, digits, . _ -'), showIf: isNew },
            { id: 'base', label: 'Base branch (optional)', type: 'text', mono: true, optional: true, placeholder: 'auto-detect', showIf: isNew },
          ] }];
        } },
      // Output comes before the merge policy: who decides a merge only matters when approved work merges.
      { id: 'output', title: 'Where does finished work go?', short: 'Output', kind: 'choice', group: 'output', next: 'review',
        procedure: [
          ['Choose what approval means.', 'After the approver says APPROVE, the work is merged, kept on its branch, or exported as files.'],
          ['Merge routine work.', 'The gate still sends protected paths and large diffs to you; the next step decides whether the rest merges by itself.'],
          ['Export to read it elsewhere.', 'Folder writes changes.patch, the changed files and REPORT.md under the output folder, per repository and job.'],
        ],
        options: DELIVERY.map((d) => ({ id: d.id, title: d.title, icon: d.icon, does: d.does, when: d.when, badges: d.badges.slice(), next: d.id === 'merge' ? 'merge-policy' : 'review' })),
        edgeLabels: { 'merge-policy': 'merge', review: 'branch · folder' },
        current: (draft, ctx) => (DELIVERY.some((d) => d.id === sect(ctx, 'merge').delivery) ? sect(ctx, 'merge').delivery : null),
        default: (draft, ctx) => deliveryDefault(ctx),
        note: (draft, ctx) => {
          const c = choiceOf(draft, 'output');
          if (c !== 'branch' && c !== 'folder') return null;
          const p = sect(ctx, 'pipeline');
          return 'Nothing merges by itself with this output, so the merge policy step is skipped. A job you start with Merge uses the saved policy: ' +
            (p.auto_merge === false ? 'always ask.' : 'auto-merge up to ' + (p.max_auto_merge_lines || 1500) + ' changed lines.');
        },
        extra: (draft, ctx) => [{ id: 'folder', fields: [
          { id: 'output_dir', label: 'Output folder', type: 'text', mono: true, optional: true, default: sect(ctx, 'merge').output_dir || '',
            placeholder: (cfgOf(ctx).data_dir ? String(cfgOf(ctx).data_dir).replace(/\/$/, '') : '<data dir>') + '/output', hint: 'Empty: the data folder’s output/. Each job lands in <folder>/<repo>/<job-id>/.',
            check: (v) => (!v || /^[/~]/.test(String(v).trim()) ? null : 'An absolute path (or one starting with ~).'), showIf: (d) => choiceOf(d, 'output') === 'folder' },
        ] }] },
      { id: 'merge-policy', title: 'Merge by itself, or ask me first?', short: 'Merge policy', kind: 'choice', group: 'output', next: 'review',
        procedure: [
          ['Let the gate merge routine work.', 'When every reviewer passes and the approver approves, small changes merge by themselves.'],
          ['Or always ask.', 'Always ask stops every approved job in Needs human until you press Approve.'],
          ['Protected paths always ask.', 'Changes to CI, dependencies, migrations or secrets come to you whatever you choose here.'],
        ],
        options: [
          { id: 'auto', title: 'Merge by itself', icon: 'merge', does: 'APPROVE plus a passing gate merges without waiting for you.', when: 'You trust the reviewers with routine changes.', badges: [B.rec] },
          { id: 'ask', title: 'Ask me first', icon: 'user', does: 'Every approved job waits in Needs human for your decision.', when: 'You want to read every change before it lands.', badges: [] },
        ],
        current: (draft, ctx) => (sect(ctx, 'pipeline').auto_merge === false ? 'ask' : sect(ctx, 'pipeline').auto_merge === true ? 'auto' : null),
        default: (draft, ctx) => (sect(ctx, 'pipeline').auto_merge === false ? 'ask' : 'auto'),
        extra: (draft, ctx) => [{ id: 'lines', fields: [
          { id: 'max_lines', label: 'Largest diff that merges by itself (changed lines)', type: 'number', default: sect(ctx, 'pipeline').max_auto_merge_lines || 1500, check: intOk(0), showIf: (d) => choiceOf(d, 'merge-policy') !== 'ask' },
        ] }] },
      { id: 'review', title: 'Review and apply', short: 'Review', kind: 'review', group: 'review', next: null,
        procedure: [
          ['Read every change.', 'Each line shows a setting as it is now and as it will be.'],
          ['Apply in one go.', 'Keys are saved first, then the routing, then the rest; each item reports on its own.'],
          ['Fix and retry.', 'A failed item leaves the others in place; fix it and retry just that one.'],
        ] },
    ],
  };

  const START = {
    id: 'start', title: 'Start work', sub: 'A feature, a plan, a chat or an analysis, from the request to the right view.', start: 'what',
    groups: { what: 'What', repo: 'Repository', describe: 'Request', routing: 'Routing', output: 'Output', review: 'Review' },
    steps: [
      { id: 'what', title: 'What do you want to start?', short: 'What', kind: 'choice', group: 'what', next: 'repo',
        procedure: [
          ['Match the size of the work.', 'One focused change is a feature; work with several parts is a plan.'],
          ['Talk first if you are unsure.', 'Chat explores with an agent on its own branch; hand the branch to the factory when it is ready.'],
          ['Analyse before you change.', 'The analyst studies a binary with REA and writes notes you can build on.'],
        ],
        options: (draft, ctx) => [
          { id: 'feature', title: 'A feature', icon: 'zap', does: 'One change through the whole line: spec, build, reviews, approval.', when: 'It fits one focused pull request.', badges: [B.rec] },
          { id: 'plan', title: 'A plan', icon: 'plan', does: 'The lead splits a large request into ordered jobs and reviews the whole when they merge.', when: 'The work is bigger than one pull request.', badges: [] },
          { id: 'chat', title: 'A chat', icon: 'chat', does: 'Work turn by turn with the coder agent on its own branch.', when: 'You want to explore or steer the work yourself.', badges: [] },
          { id: 'analyse', title: 'An analysis', icon: 'scout', does: 'The analyst agent inspects a binary with REA over MCP and keeps notes.', when: 'You need to understand compiled code first.',
            badges: isObj(sect(ctx, 'mcp_servers').rea) ? [] : [B.setup] },
        ] },
      { id: 'repo', title: 'Which repository?', short: 'Repository', kind: 'choice', group: 'repo', next: 'describe',
        procedure: [
          ['Pick the repository.', 'Work happens on a worktree of it; your own checkout stays untouched.'],
          ['Add one if it is missing.', 'Setup › Repository registers a git checkout by its path.'],
        ],
        options: (draft, ctx) => reposOf(ctx).map((r) => repoCard(r, '')),
        default: (draft, ctx) => (ctx && ctx.lastRepo && reposOf(ctx).some((r) => r.name === ctx.lastRepo) ? ctx.lastRepo : reposOf(ctx)[0] ? reposOf(ctx)[0].name : undefined),
        add: { title: 'Add a repository…', href: '#/setup/repo', text: 'Register another git checkout on this server.' },
        empty: { title: 'No repository yet', text: 'Register a git checkout first: Setup › Repository.', href: '#/setup/repo', action: 'Add a repository' } },
      { id: 'describe', title: (draft) => ({ plan: 'Describe the change', chat: 'Start the conversation', analyse: 'What should the analyst look at?' })[choiceOf(draft, 'what')] || 'Describe the feature',
        short: 'Request', kind: 'form', group: 'describe',
        // The kind of work was chosen at the first step; the maps draw this as a check on that answer.
        next: (draft) => (choiceOf(draft, 'what') === 'chat' || choiceOf(draft, 'what') === 'analyse' ? 'review' : 'routing'),
        ask: 'Feature or plan?', branches: [{ to: 'routing', label: 'feature · plan' }, { to: 'review', label: 'chat · analyse' }],
        procedure: [
          ['Say what, and for whom.', 'Describe the behaviour you want and who it is for, in plain words.'],
          ['Say how you will know.', 'Name the check that proves it works: a test, a page, a command.'],
          ['Keep one job to one change.', 'A plan splits bigger work into jobs for you.'],
        ],
        groups: (draft, ctx) => {
          const w = choiceOf(draft, 'what') || 'feature';
          if (w === 'feature') return [{ id: 'f', fields: [{ id: 'text', label: 'Feature request', type: 'textarea', rows: 7, required: true,
            placeholder: 'What should it do, for whom, and how will you know it works? e.g. Rate limit the signup form to 5 attempts per IP per minute and show a friendly error.', hint: 'The spec-writer receives it verbatim.' }] }];
          if (w === 'plan') return [{ id: 'p', fields: [{ id: 'text', label: 'Request', type: 'textarea', rows: 8, required: true,
            placeholder: 'The whole change, in your words: what it should do, for whom, and what must keep working.', hint: 'The lead may answer with questions first; you answer them in plan.md.' }] }];
          const chatAgents = (ctx && Array.isArray(ctx.chatAgents) && ctx.chatAgents.length ? ctx.chatAgents : ['coder']).map((n) => [n, n]);
          const models = [['', 'Agent default']].concat((ctx && Array.isArray(ctx.modelKeys) ? ctx.modelKeys : []).map((k) => [k, k]));
          if (w === 'chat') return [{ id: 'c', fields: [
            { id: 'agent', label: 'Agent', type: 'select', options: chatAgents, default: 'coder' },
            { id: 'mode', label: 'Where it works', type: 'radio', default: 'worktree', options: [['worktree', 'Its own branch and worktree (can hand off)'], ['repo', 'Directly in the repo checkout']] },
            { id: 'model', label: 'Model', type: 'select', options: models, default: '', optional: true },
          ].concat(chatProfileField(draft, ctx), [
            { id: 'text', label: 'First message (optional)', type: 'textarea', rows: 5, optional: true, placeholder: 'Ask it to explain, change or test something.' },
          ]) }];
          return [{ id: 'a', fields: [
            { id: 'binary', label: 'Binary', type: 'text', mono: true, required: true, placeholder: 'path inside the repository, e.g. vendor/libfoo.so' },
            { id: 'text', label: 'Question', type: 'textarea', rows: 5, required: true, placeholder: 'What do you need to know? e.g. Which functions parse the config file, and what format do they expect?' },
            { id: 'model', label: 'Model', type: 'select', options: models, default: '', optional: true },
          ].concat(chatProfileField(draft, ctx)) }];
        } },
      { id: 'routing', title: 'Which models run it?', short: 'Routing', kind: 'choice', group: 'routing', next: 'output', edgeLabels: { output: 'Default / a preset', 'routing-custom': 'Custom per step' },
        procedure: [
          ['Default uses your saved routing.', 'Settings › Routing decides which model and driver each step runs on.'],
          ['A preset is for this run only.', 'Your saved settings stay as they are; the job keeps a snapshot of what it ran on.'],
          ['Custom changes single steps.', 'Steps you leave alone keep their saved route.'],
        ],
        options: (draft, ctx) => [{ id: 'default', title: 'Your saved routing', icon: 'check', does: 'Each step on its saved route: ' + routingSummary(ctx) + '.', when: 'The saved routing suits this work.', badges: [B.rec] }]
          .concat(routingPresets(ctx).map((p) => ({ id: 'preset:' + p.name, preset: p.name, title: p.title, icon: 'route', does: p.does, when: p.when,
            details: p.description && p.description !== p.does ? p.description : null, detailsLabel: 'examples/' + p.name + '.json',
            badges: [kindBadge(p.kind)].concat(keyBadges(ctx, p.known ? presetKeys(p, emptyDraft(), ctx) : p.keys)) })))
          .concat([{ id: 'custom', title: 'Custom per step', icon: 'settings', next: 'routing-custom', does: 'Pick a model and driver for single steps, for this run only.', when: 'One step needs a different model.', badges: [] }]),
        // The routing profile (DESIGN §23): which model each step runs on and why, over the routing above.
        extra: (draft, ctx) => {
          const options = profileFieldOptions(draft, ctx);
          return options.length ? [{ id: 'profile', title: 'Routing profile', fields: [{ id: 'profile', label: 'Routing profile', type: 'select', options, default: '', optional: true,
            hint: 'Places steps on models over the routing above, each with its reason. Steps it does not name keep their route.' }] }] : [];
        },
        default: 'default' },
      { id: 'routing-custom', title: 'Pick models per step', short: 'Per step', kind: 'form', group: 'routing', next: 'output',
        procedure: [
          ['Change only what matters.', 'Each step starts on its saved route; change the ones this work needs.'],
          ['Keep families apart.', 'A reviewer on a different model from the builder catches more.'],
        ],
        groups: (draft, ctx) => [{ id: 'routes', title: 'Steps of this run', widget: 'run-routes', fields: [],
          steps: (choiceOf(draft, 'what') === 'plan' ? ['lead'] : []).concat(LINE_AGENTS.slice(0, 2), reviewersOf(ctx).map((r) => 'reviewer-' + r), ['approver']) }],
        validate: (draft) => (isObj(valueOf(draft, 'routing-custom', 'routes', null)) && Object.keys(valueOf(draft, 'routing-custom', 'routes', {})).length ? null : 'Custom routing changes no step yet: pick another model or driver for at least one step, or go back and choose Default.') },
      { id: 'output', title: 'Where does finished work go?', short: 'Output', kind: 'choice', group: 'output', next: 'review',
        procedure: [
          ['Choose what approval means.', 'Merge into the base branch, keep the branch, or export a folder.'],
          ['The default comes from Setup.', 'Choosing another mode here changes this run only.'],
        ],
        options: (draft, ctx) => DELIVERY.map((d) => ({ id: d.id, title: d.title, icon: d.icon, does: d.does, when: d.when, badges: d.id === deliveryDefault(ctx) ? ['Default'] : [] })),
        default: (draft, ctx) => deliveryDefault(ctx) },
      { id: 'review', title: 'Review and start', short: 'Start', kind: 'run', group: 'review', next: null,
        procedure: [
          ['Check the request.', 'This is exactly what the server receives.'],
          ['Start it.', 'You land on the job, the plan or the chat, where its work streams in as it happens.'],
        ] },
    ],
  };

  const FLOWS = { welcome: WELCOME, setup: SETUP, start: START };

  // ================================================================ step navigation
  const flowOf = (id) => FLOWS[id] || null;
  const stepOf = (flow, id) => (flow ? flow.steps.find((s) => s.id === id) || null : null);
  const titleOf = (step, draft) => (typeof step.title === 'function' ? step.title(draft || emptyDraft()) : step.title);
  /** A step's options for this draft; the saved setting (step.current) carries a "Current" badge. */
  function optionsOf(step, draft, ctx) {
    const o = typeof step.options === 'function' ? step.options(draft || emptyDraft(), ctx || {}) : step.options;
    const list = Array.isArray(o) ? o : [];
    const cur = currentOf(step, draft, ctx);
    if (!cur || step.currentBadge === false || step.id === 'roles') return list;
    return list.map((x) => (x.id === cur && (x.badges || []).indexOf('Current') < 0 ? Object.assign({}, x, { badges: (x.badges || []).concat(['Current']) }) : x));
  }
  /** The option that matches the saved setting, when the step has one. */
  const currentOf = (step, draft, ctx) => (typeof step.current === 'function' ? step.current(draft || emptyDraft(), ctx || {}) || null : null);
  function groupsOf(step, draft, ctx) {
    const g = typeof step.groups === 'function' ? step.groups(draft || emptyDraft(), ctx || {}) : step.groups;
    const extra = typeof step.extra === 'function' ? step.extra(draft || emptyDraft(), ctx || {}) : step.extra;
    return (Array.isArray(g) ? g : []).concat(Array.isArray(extra) ? extra : []);
  }
  const fieldsOf = (step, draft, ctx) => [].concat.apply([], groupsOf(step, draft, ctx).map((g) => g.fields || []));
  function defaultChoice(step, draft, ctx) {
    if (step.default !== undefined) return typeof step.default === 'function' ? step.default(draft, ctx) : step.default;
    const opts = optionsOf(step, draft, ctx).filter((o) => !o.disabled);
    const rec = opts.find((o) => (o.badges || []).indexOf(B.rec) >= 0);
    return step.multi ? undefined : rec ? rec.id : undefined;
  }
  /** The step after stepId for this draft (a projected default when the step is not answered yet). */
  function nextOf(flow, stepId, draft, ctx) {
    const step = stepOf(flow, stepId);
    if (!step) return null;
    if (typeof step.next === 'function') return step.next(draft, ctx) || null;
    if (step.kind === 'choice' && !step.multi) {
      let c = choiceOf(draft, stepId);
      if (c === undefined) c = defaultChoice(step, draft, ctx);
      const opt = optionsOf(step, draft, ctx).find((o) => o.id === c);
      if (opt && opt.next !== undefined) return opt.next;
    }
    return step.next || null;
  }
  const answered = (draft, step) => !!(draft && draft.done && draft.done[step.id]);
  /** The steps this draft goes through, from the start: [{id, step, done}]. Unanswered choices project their default. */
  function pathOf(flow, draft, ctx) {
    const out = [];
    const seen = new Set();
    let id = flow.start;
    while (id && !seen.has(id) && out.length < 60) {
      seen.add(id);
      const step = stepOf(flow, id);
      if (!step) break;
      out.push({ id, step, done: answered(draft, step) });
      id = nextOf(flow, id, draft, ctx);
    }
    return out;
  }
  /** Every edge of a flow for its map: {from, to, label, opts: [option ids]}; option edges to one step are merged.
   *  A step whose next depends on an earlier answer (step.branches) gives `cond` edges: a check, not a choice. */
  function edgesOf(flow, ctx) {
    const edges = [];
    const draft = emptyDraft();
    for (const step of flow.steps) {
      if (Array.isArray(step.branches)) { for (const b of step.branches) edges.push({ from: step.id, to: b.to, label: b.label || '', opts: [], cond: true }); continue; }
      const opts = step.kind === 'choice' && !step.multi ? optionsOf(step, draft, ctx) : [];
      const byTarget = new Map();
      for (const o of opts) {
        const to = o.next !== undefined ? o.next : step.next;
        if (!to || typeof to !== 'string') continue;
        if (!byTarget.has(to)) byTarget.set(to, []);
        byTarget.get(to).push(o);
      }
      if (byTarget.size > 1) {
        for (const [to, list] of byTarget) edges.push({ from: step.id, to, label: step.edgeLabels && step.edgeLabels[to] ? step.edgeLabels[to] : list.length > 3 ? list.slice(0, 2).map((o) => o.title).join(' / ') + ' / …' : list.map((o) => o.short || o.title).join(' / '), opts: list.map((o) => o.id) });
      } else if (byTarget.size === 1) {
        const [to, list] = Array.from(byTarget)[0];
        edges.push({ from: step.id, to, label: '', opts: list.map((o) => o.id) });
      } else if (typeof step.next === 'string') edges.push({ from: step.id, to: step.next, label: '', opts: [] });
    }
    return edges;
  }
  /** A step branches when the option you pick there leads to more than one step: it is drawn as a decision diamond.
   *  A form whose next step follows from an earlier answer does not: its map draws a separate check after it. */
  function branching(flow, stepId, ctx) {
    return new Set(edgesOf(flow, ctx).filter((e) => e.from === stepId && !e.cond).map((e) => e.to)).size > 1;
  }
  /** Drop what no longer applies after a change: answers of steps off the new path. Returns the dropped titles. */
  function pruneDraft(flow, draft, ctx) {
    const on = new Set(pathOf(flow, draft, ctx).map((p) => p.id));
    const dropped = [];
    for (const step of flow.steps) {
      if (on.has(step.id)) continue;
      const had = draft.choices[step.id] !== undefined || draft.values[step.id] !== undefined || draft.done[step.id];
      if (!had) continue;
      delete draft.choices[step.id];
      delete draft.values[step.id];
      delete draft.done[step.id];
      dropped.push(titleOf(step, draft));
    }
    return dropped;
  }
  function fieldValue(draft, step, f) {
    const v = valueOf(draft, step.id, f.id, undefined);
    return v === undefined ? (f.default === undefined ? '' : f.default) : v;
  }
  function visibleFields(step, draft, ctx) { return fieldsOf(step, draft, ctx).filter((f) => !f.showIf || f.showIf(draft)); }
  /** {ok, message, fields: {fieldId: message}} for one step. Secret fields are checked by app.js (it holds them). */
  function validateStep(flow, stepId, draft, ctx) {
    const step = stepOf(flow, stepId);
    const res = { ok: true, message: null, fields: {} };
    if (!step) return res;
    if (step.kind === 'choice') {
      const c = choiceOf(draft, stepId);
      const opts = optionsOf(step, draft, ctx);
      if (step.multi) {
        const n = listOf(c).filter((id) => opts.some((o) => o.id === id)).length;
        if (n < (step.min || 0)) { res.ok = false; res.message = 'Pick at least ' + (step.min === 1 ? 'one' : step.min) + '.'; }
      } else if (!opts.length && step.empty) {
        res.ok = false; res.message = step.empty.text;
      } else if (c === undefined || !opts.some((o) => o.id === c)) {
        res.ok = false; res.message = 'Pick one of the cards first.';
      }
    }
    for (const f of visibleFields(step, draft, ctx)) {
      if (f.secret || f.type === 'radio' || f.type === 'toggle' || f.type === 'checks') continue;
      const raw = fieldValue(draft, step, f);
      const s = typeof raw === 'string' ? raw.trim() : raw;
      if (f.required && (s === '' || s === null || s === undefined)) { res.fields[f.id] = 'Required.'; continue; }
      if (f.check && s !== '' && s !== null && s !== undefined) { const m = f.check(raw); if (m) res.fields[f.id] = m; }
    }
    if (Object.keys(res.fields).length) { res.ok = false; res.message = res.message || 'Fix the highlighted fields.'; }
    if (res.ok && typeof step.validate === 'function') { const m = step.validate(draft, ctx); if (m) { res.ok = false; res.message = m; } }
    return res;
  }

  // ================================================================ config diffs
  /** {path, before, after} for every setting a merge patch changes (dicts merge, null removes, lists replace). */
  function patchChanges(before, patch, prefix) {
    const out = [];
    for (const k of Object.keys(patch || {})) {
      const path = prefix ? prefix + '.' + k : k;
      const b = isObj(before) ? before[k] : undefined;
      const a = patch[k];
      if (isObj(a) && isObj(b)) { out.push.apply(out, patchChanges(b, a, path)); continue; }
      if (isObj(a) && (b === undefined || b === null)) { out.push({ path, before: undefined, after: a, entry: true }); continue; }
      if (a === null) { if (b !== undefined && b !== null) out.push({ path, before: b, after: undefined }); continue; }
      if (stable(a) !== stable(b)) out.push({ path, before: b, after: a });
    }
    return out;
  }
  /** One row per provider or model entry a patch touches ({section: {key: entry}}), the entry before and after in a few
   *  words, instead of a row per field. */
  function entryChanges(before, patch) {
    const out = [];
    for (const section of Object.keys(patch || {})) {
      const cur = isObj(before) && isObj(before[section]) ? before[section] : {};
      for (const key of Object.keys(patch[section] || {})) {
        const b = cur[key];
        const a = isObj(b) && isObj(patch[section][key]) ? deepMerge(b, patch[section][key]) : patch[section][key];
        if (stable(a) === stable(b)) continue;
        let after = entrySummary(a);
        if (isObj(b) && entrySummary(b) === after) {
          const keys = Object.keys(Object.assign({}, b, a)).filter((k) => stable(a[k]) !== stable(b[k]));
          after += ' (' + keys.join(', ') + ' changed)';
        }
        out.push({ path: section + '.' + key, label: section + '.' + key, before: isObj(b) ? entrySummary(b) : b, after });
      }
    }
    return out;
  }
  /** A new config entry in a few words: a model as provider · model id · context, a provider as its address. */
  function entrySummary(v) {
    if (!isObj(v)) return String(v);
    if (v.provider && v.model) return v.provider + ' · ' + v.model + (v.context_tokens ? ' · ' + v.context_tokens + ' context' : '');
    if (v.base_url) return v.base_url + (v.max_concurrency ? ' · ' + v.max_concurrency + (v.max_concurrency === 1 ? ' slot' : ' slots') : '') + (v.api_key_env ? ' · key ' + v.api_key_env : '');
    if (v.command) return v.command + (Array.isArray(v.args) && v.args.length ? ' ' + v.args.join(' ') : '');
    if (v.model || v.driver) return [v.model ? 'model ' + v.model : '', v.driver ? 'driver ' + v.driver : ''].filter(Boolean).join(' · ');
    return Object.keys(v).filter((k) => v[k] !== null && !isObj(v[k])).slice(0, 4).map((k) => k + ' ' + fmtValue(v[k])).join(' · ');
  }
  function fmtValue(v) {
    if (v === undefined || v === null || v === '') return '(not set)';
    if (isObj(v)) return entrySummary(v);
    if (Array.isArray(v)) return v.length ? (v.length > 4 ? v.slice(0, 3).join(', ') + ' … +' + (v.length - 3) + ' more' : v.join(', ')) : '(empty list)';
    if (typeof v === 'boolean') return v ? 'on' : 'off';
    return String(v);
  }
  const deepMerge = (a, b) => {
    if (!isObj(a) || !isObj(b)) return clone(b);
    const out = clone(a);
    for (const k of Object.keys(b)) { if (b[k] === null) delete out[k]; else out[k] = isObj(out[k]) && isObj(b[k]) ? deepMerge(out[k], b[k]) : clone(b[k]); }
    return out;
  };

  // ================================================================ setup plan (review → apply)
  /** The work Apply does, in order, as items {id, title, icon, requests, changes, after?}. Requests are plain
   *  {method, path, body}; three need app.js: {secret: NAME} (the typed key, kept in memory only), {repoBase:
   *  {name, base}} (repos is a list: read, change one entry, post it back) and {agentMcp: {agent, server}} (read the
   *  agent file, add the server to its mcp: line, save it). typed: names of keys typed on this page (not values). */
  function setupPlan(draft, ctx, typed) {
    const items = [];
    const cfg = cfgOf(ctx);
    const typedSet = new Set(typed || []);
    const w = where(draft);
    // 1. keys
    const keys = [];
    for (const s of w === 'local' || w === 'hybrid' ? localPicked(draft) : []) if (s.keyEnv && typedSet.has(s.keyEnv)) keys.push(s.keyEnv);
    for (const p of w === 'api' || w === 'hybrid' ? apiPicked(draft) : []) if (typedSet.has(p.keyEnv)) keys.push(p.keyEnv);
    const uniq = Array.from(new Set(keys));
    if (uniq.length) {
      items.push({ id: 'keys', title: 'API keys', icon: 'zap', group: 'providers', requests: uniq.map((name) => ({ method: 'POST', path: '/api/secrets', body: { name }, secret: name, label: name })),
        changes: uniq.map((name) => ({ path: name, label: name, before: keySet(ctx, name) ? 'set' : 'not set', after: 'set from this page (env file)' })) });
    }
    // 2. routing preset: one line per step, as it runs now → as it will run
    const role = choiceOf(draft, 'roles');
    const onPath = (id) => pathOf(SETUP, draft, ctx).some((p) => p.id === id);
    const presetName = onPath('roles') && typeof role === 'string' && role.indexOf('preset:') === 0 ? role.slice(7) : null;
    const outcome = presetName ? routingOutcome(presetName, draft, ctx) : null;
    if (presetName) {
      const p = routingPresets(ctx).find((x) => x.name === presetName);
      const changes = [];
      if (outcome) {
        for (const r of outcome.rows) if (r.changed) changes.push({ path: 'agents.' + r.agent, label: r.agent, before: r.before, after: r.after });
        const same = outcome.rows.filter((r) => !r.changed).map((r) => r.agent);
        if (same.length) changes.push({ path: 'agents.same', label: 'Unchanged', note: true, after: same.join(', ') });
        if (outcome.takeover) {
          const also = outcome.takeover.keys.filter((k) => k !== 'local');
          changes.push({ path: 'takeover', label: 'Your local server', note: true, after: outcome.takeover.to + ' takes the place of the preset’s local model (' + outcome.takeover.from + ')' +
            (also.length ? ', also as the ' + also.join(', ') + ' tier' + (also.length > 1 ? 's' : '') : '') + '.' });
        }
      } else changes.push({ path: 'preset', label: 'Routing', before: 'current routes', after: 'preset ' + presetName + (p ? ': ' + p.description : '') });
      changes.push({ path: 'preset.note', label: 'Replaced', note: true, after: 'every provider, model and route the preset defines; everything else is kept' });
      if (outcome && outcome.keys.length) {
        const missing = outcome.keys.filter((k) => k.state === 'missing').map((k) => k.name);
        changes.push({ path: 'preset.keys', label: 'Keys it needs', note: true, warn: missing.length > 0,
          after: outcome.keys.map((k) => k.name + (k.state === 'set' ? ' (set)' : k.state === 'typed' ? ' (saved first, from this page)' : ' (missing)')).join(', ') +
            (missing.length ? '. Steps on a model without its key fail until you add it.' : '') });
      }
      items.push({ id: 'preset', title: 'Routing preset: ' + (p ? p.title : presetName), icon: 'route', group: 'routing', requests: [{ method: 'POST', path: '/api/config/preset', body: { name: presetName } }], changes });
    }
    // 3. local servers → providers + models (the first also takes the place of the preset's local model)
    const local = localEntries(draft, ctx);
    const providers = local.providers;
    const models = local.models;
    if (outcome && outcome.takeover) for (const k of outcome.takeover.keys) if (k !== 'local' && models.local) models[k] = Object.assign(clone(models.local), isObj(sect(ctx, 'models')[k]) ? {} : { extra_body: {} });
    if (Object.keys(providers).length || Object.keys(models).length) {
      const patch = {};
      if (Object.keys(providers).length) patch.providers = providers;
      if (Object.keys(models).length) patch.models = models;
      const changes = entryChanges(cfg, patch);
      if (!changes.length) changes.push({ path: 'models', label: 'models', note: true, after: 'already saved as entered' });
      // A model no step runs on is worth a word (a second server, or a preset that does not route to "local").
      const known = !presetName || !!outcome;
      if (known && w !== 'kit') {
        const o = outcome || routingOutcome(null, draft, ctx);
        const custom = role === 'custom' && onPath('roles-custom') ? valueOf(draft, 'roles-custom', 'routes', {}) : {};
        const usedCustom = new Set(Object.values(isObj(custom) ? custom : {}).map((r) => (isObj(r) ? r.model : null)).filter(Boolean));
        const unused = o.unused.filter((k) => !usedCustom.has(k));
        if (unused.length) changes.push({ path: 'models.unused', label: 'Not used yet', note: true, warn: true,
          after: unused.map((k) => 'models.' + k).join(', ') + (unused.length > 1 ? ': no step runs on them' : ': no step runs on it') + '. Route a step to ' + (unused.length > 1 ? 'them' : 'it') + ' with Custom per step, or later in Settings › Routing.' });
      }
      items.push({ id: 'models', title: 'Local servers and models', icon: 'server', group: 'models', requests: [{ method: 'POST', path: '/api/config', body: patch }], changes });
    }
    // 4. routing: custom routes, or every agent through the claude CLI (the kit)
    if (w === 'kit') {
      const patch = { agents: { '*': { driver: 'claude' } } };
      items.push({ id: 'routing', title: 'Every agent through the Claude Code CLI', icon: 'terminal', group: 'routing', requests: [{ method: 'POST', path: '/api/config', body: patch }],
        changes: patchChanges(cfg, patch).map((c) => Object.assign(c, { label: c.path })) });
    } else if (onPath('roles-custom') && role === 'custom') {
      const routes = valueOf(draft, 'roles-custom', 'routes', {});
      const agents = {};
      for (const name of Object.keys(isObj(routes) ? routes : {})) if (isObj(routes[name])) agents[name] = clone(routes[name]);
      if (Object.keys(agents).length) {
        const patch = { agents };
        items.push({ id: 'routing', title: 'Routing per step', icon: 'route', group: 'routing', requests: [{ method: 'POST', path: '/api/config', body: patch }],
          changes: patchChanges(cfg, patch).map((c) => Object.assign(c, { label: c.path })) });
      }
    }
    // 5. tools: skills folders, REA (only when the step was visited: a setup pass that never reached it changes nothing)
    const touched = (id) => !!(draft.done && draft.done[id]) || choiceOf(draft, id) !== undefined || (isObj(draft.values) && draft.values[id] !== undefined);
    if (onPath('tools') && touched('tools')) {
      const harness = sect(ctx, 'harness');
      const dirs = linesOf(valueOf(draft, 'tools', 'skills_dirs', (harness.skills_dirs || []).join('\n')));
      const patch = {};
      if (stable(dirs) !== stable(Array.isArray(harness.skills_dirs) ? harness.skills_dirs : [])) patch.harness = { skills_dirs: dirs };
      const rea = valueOf(draft, 'tools', 'rea', 'keep');
      const requests = [];
      const changes = [];
      if (rea === 'form') {
        const cur = isObj(sect(ctx, 'mcp_servers').rea) ? sect(ctx, 'mcp_servers').rea : null;
        const toolList = String(valueOf(draft, 'tools', 'rea_tools', cur && cur.tools ? cur.tools.join(', ') : '')).split(/[,\s]+/).filter(Boolean);
        const entry = { command: String(valueOf(draft, 'tools', 'rea_command', '~/.local/bin/rea-mcp')).trim(), args: linesOf(valueOf(draft, 'tools', 'rea_args', '')), tools: toolList.length ? toolList : null };
        if (!cur) { entry.timeout = 120; entry.startup_timeout = 60; }
        patch.mcp_servers = { rea: entry };
      }
      if (Object.keys(patch).length) {
        requests.push({ method: 'POST', path: '/api/config', body: patch });
        changes.push.apply(changes, patchChanges(cfg, patch).map((c) => Object.assign(c, { label: c.path })));
      }
      if (typeof rea === 'string' && rea.indexOf('preset:') === 0) {
        requests.push({ method: 'POST', path: '/api/config/preset', body: { name: rea.slice(7) } });
        changes.push({ path: 'mcp_servers.rea', label: 'mcp_servers.rea', before: isObj(sect(ctx, 'mcp_servers').rea) ? 'configured' : undefined, after: 'from example ' + rea.slice(7) });
      }
      if (requests.length) items.push({ id: 'tools', title: 'Skills and MCP', icon: 'layers', group: 'tools', requests, changes });
      if (rea !== 'keep' || isObj(sect(ctx, 'mcp_servers').rea)) {
        const want = listOf(valueOf(draft, 'tools', 'rea_agents', MCP_AGENTS.filter((n) => n === 'analyst' && hasAgent(ctx, n))));
        const have = ctx && isObj(ctx.agentMcp) ? ctx.agentMcp : {};
        const add = want.filter((n) => !(Array.isArray(have[n]) && have[n].indexOf('rea') >= 0));
        if (add.length) {
          items.push({ id: 'agents', title: 'Agent files', icon: 'agents', group: 'tools', after: ['tools'],
            requests: add.map((agent) => ({ method: 'POST', path: '/api/agents/' + enc(agent), agentMcp: { agent, server: 'rea' }, label: agent })),
            changes: add.map((agent) => ({ path: 'agents/' + agent + '.md', label: agent + '.md · mcp:', before: Array.isArray(have[agent]) && have[agent].length ? have[agent].join(', ') : undefined, after: (Array.isArray(have[agent]) ? have[agent] : []).concat(['rea']).join(', ') })) });
        }
      }
    }
    // 6. safety: sandbox, merge policy, protected paths
    if (onPath('sandbox')) {
      const patch = {};
      const sb = choiceOf(draft, 'sandbox') || defaultChoice(stepOf(SETUP, 'sandbox'), draft, ctx);
      const harness = { sandbox: sb };
      if (sb !== 'none') harness.sandbox_network = !!valueOf(draft, 'sandbox', 'network', sect(ctx, 'harness').sandbox_network !== false);
      patch.harness = harness;
      const pr = choiceOf(draft, 'protected') || defaultChoice(stepOf(SETUP, 'protected'), draft, ctx);
      patch.escalate_paths = pr === 'custom' ? linesOf(valueOf(draft, 'protected', 'globs', (cfg.escalate_paths || STANDARD_PATHS).join('\n'))) : PROTECTED_PROFILES[pr].slice();
      const changes = patchChanges(cfg, patch).map((c) => Object.assign(c, { label: c.path }));
      if (changes.length) {
        const body = {};
        for (const c of changes) { const top = c.path.split('.')[0]; body[top] = patch[top]; }
        items.push({ id: 'safety', title: 'Safety', icon: 'alert', group: 'safety', requests: [{ method: 'POST', path: '/api/config', body }], changes });
      }
    }
    // 7. repository
    if (onPath('repo')) {
      const c = choiceOf(draft, 'repo');
      if (c === 'new') {
        const path = String(valueOf(draft, 'repo', 'path', '')).trim();
        const name = String(valueOf(draft, 'repo', 'name', '')).trim();
        const base = String(valueOf(draft, 'repo', 'base', '')).trim();
        const derived = name || path.replace(/\/+$/, '').split('/').pop().toLowerCase().replace(/[^a-z0-9._-]/g, '-');
        const requests = [{ method: 'POST', path: '/api/repos', body: name ? { path, name } : { path } }];
        if (base) requests.push({ method: 'POST', path: '/api/config', repoBase: { name: derived, base }, label: 'base branch' });
        items.push({ id: 'repo', title: 'Repository ' + derived, icon: 'folder', group: 'repos', requests,
          changes: [{ path: 'repos', label: 'repos', before: undefined, after: derived + ' at ' + path }].concat(base ? [{ path: 'repos.' + derived + '.base', label: derived + ' base branch', before: undefined, after: base }] : []) });
      } else if (typeof c === 'string' && c.indexOf('repo:') === 0) {
        const name = c.slice(5);
        const r = reposOf(ctx).find((x) => x.name === name);
        const base = String(valueOf(draft, 'repo', 'base:' + name, r && r.base ? r.base : '')).trim();
        if (r && base !== String(r.base || '')) {
          items.push({ id: 'repo', title: 'Repository ' + name, icon: 'folder', group: 'repos', requests: [{ method: 'POST', path: '/api/config', repoBase: { name, base: base || null } }],
            changes: [{ path: 'repos.' + name + '.base', label: name + ' base branch', before: r.base || undefined, after: base || undefined }] });
        }
      }
    }
    // 8. output
    if (onPath('output')) {
      const mode = choiceOf(draft, 'output') || deliveryDefault(ctx);
      const merge = { delivery: mode };
      const dir = String(valueOf(draft, 'output', 'output_dir', sect(ctx, 'merge').output_dir || '')).trim();
      if (mode === 'folder') merge.output_dir = dir || null;
      const patch = { merge };
      // The merge policy is asked only when approved work merges (output merge).
      if (onPath('merge-policy')) {
        const mp = choiceOf(draft, 'merge-policy') || defaultChoice(stepOf(SETUP, 'merge-policy'), draft, ctx);
        const pipe = { auto_merge: mp !== 'ask' };
        if (mp !== 'ask') pipe.max_auto_merge_lines = Number(valueOf(draft, 'merge-policy', 'max_lines', sect(ctx, 'pipeline').max_auto_merge_lines || 1500));
        patch.pipeline = pipe;
      }
      const changes = patchChanges(cfg, patch).filter((x) => !(x.path === 'merge.delivery' && x.before === undefined && x.after === 'merge')).map((x) => Object.assign(x, { label: x.path }));
      if (changes.length) {
        const body = {};
        for (const c of changes) { const top = c.path.split('.')[0]; body[top] = patch[top]; }
        items.push({ id: 'output', title: body.pipeline ? 'Output and merge policy' : 'Output', icon: 'merge', group: 'output', requests: [{ method: 'POST', path: '/api/config', body }], changes });
      }
    }
    return { items };
  }

  /** Add `server` to an agent file's frontmatter mcp: line (creating the line before the closing ---). */
  function addMcp(text, server) {
    const src = String(text || '');
    if (!/^---\r?\n/.test(src)) return null;
    const end = src.indexOf('\n---', 3);
    if (end < 0) return null;
    const head = src.slice(0, end);
    const rest = src.slice(end);
    const m = /(^|\n)mcp[ \t]*:[ \t]*([^\n]*)/.exec(head);
    if (m) {
      const items = m[2].replace(/#.*$/, '').replace(/^\[|\]$/g, '').split(',').map((s) => s.trim()).filter(Boolean);
      if (items.indexOf(server) >= 0) return src;
      items.push(server);
      return head.slice(0, m.index) + m[1] + 'mcp: ' + items.join(', ') + head.slice(m.index + m[0].length) + rest;
    }
    return head + '\nmcp: ' + server + rest;
  }

  // ================================================================ start request
  /** What Start sends: {kind, requests: [{method, path, body}], message?, summary: [[label, value]]}. app.js posts
   *  them in order, then opens hashFor(kind, response). */
  function startRequest(draft, ctx) {
    const what = choiceOf(draft, 'what') || 'feature';
    const repo = choiceOf(draft, 'repo') || '';
    const text = String(valueOf(draft, 'describe', 'text', '')).trim();
    const summary = [['What', what], ['Repository', repo]];
    if (what === 'feature' || what === 'plan') {
      const body = what === 'feature' ? { repo, feature: text } : { repo, request: text };
      const r = choiceOf(draft, 'routing') || 'default';
      if (typeof r === 'string' && r.indexOf('preset:') === 0) body.routing = r.slice(7);
      else if (r === 'custom') { const routes = valueOf(draft, 'routing-custom', 'routes', null); if (isObj(routes) && Object.keys(routes).length) body.routing = clone(routes); }
      // Always sent: the job runs with the output the review showed, even if the saved default changes meanwhile.
      const out = choiceOf(draft, 'output') || deliveryDefault(ctx);
      body.delivery = out;
      summary.push(['Routing', body.routing === undefined ? 'saved routing' : typeof body.routing === 'string' ? 'preset ' + body.routing : 'custom: ' + Object.keys(body.routing).join(', ')]);
      // The routing profile: left out for the default, so the server applies it (DESIGN §23).
      const prof = startProfile(draft, ctx);
      if (prof !== undefined) body.profile = prof;
      const pc = profileChoices(ctx, typeof body.routing === 'string' ? body.routing : null);
      if (pc.names.length || pc.def) summary.push(['Profile', body.profile === undefined ? (pc.def ? pc.def + ' (default)' : 'none') : body.profile || 'none']);
      summary.push(['Output', out + (out === deliveryDefault(ctx) ? ' (default)' : '')]);
      return { kind: what === 'feature' ? 'job' : 'plan', requests: [{ method: 'POST', path: what === 'feature' ? '/api/jobs' : '/api/plans', body }], summary };
    }
    const model = String(valueOf(draft, 'describe', 'model', '')).trim();
    const body = { repo, mode: what === 'analyse' ? 'worktree' : String(valueOf(draft, 'describe', 'mode', 'worktree')), agent: what === 'analyse' ? 'analyst' : String(valueOf(draft, 'describe', 'agent', 'coder')) };
    if (model) body.model = model;
    const prof = startProfile(draft, ctx, true);   // left out for the default (DESIGN §23)
    if (prof !== undefined) body.profile = prof;
    let message = text;
    if (what === 'analyse') {
      const bin = String(valueOf(draft, 'describe', 'binary', '')).trim();
      message = 'Analyse ' + bin + '.\n\n' + text;
    }
    summary.push(['Agent', body.agent], ['Mode', body.mode], ['Model', model || 'agent default']);
    const pc = profileChoices(ctx, null);
    if (pc.names.length || pc.def) summary.push(['Profile', body.profile === undefined ? (pc.def ? pc.def + ' (default)' : 'none') : body.profile || 'none']);
    return { kind: 'chat', requests: [{ method: 'POST', path: '/api/chats', body }], message: message || null, summary };
  }
  function hashFor(kind, resp) {
    if (kind === 'job' && resp && resp.job) return '#/job/' + enc(resp.job.repo) + '/' + enc(resp.job.id);
    if (kind === 'plan' && resp && resp.plan) return '#/plans/' + enc(resp.plan.repo) + '/' + enc(resp.plan.id);
    if (kind === 'chat' && resp && resp.chat) return '#/chat/' + enc(resp.chat.id);
    return '#/factory';
  }

  // ================================================================ procedures (owner style) and maps
  /** "1. **Lead.** sentence" lines (or [lead, text] pairs, or {lead, text} objects) → [[lead, text]]. */
  function parseProcedure(src) {
    if (Array.isArray(src)) {
      return src.map((x) => {
        if (Array.isArray(x)) return [String(x[0] || ''), String(x[1] || '')];
        if (isObj(x)) return [String(x.lead || x.title || x.step || x.name || ''), String(x.text || x.detail || x.body || x.description || '')];
        const p = parseProcedure(String(x));
        return p.length ? p[0] : ['', String(x)];
      }).filter((p) => p[0] || p[1]);
    }
    const out = [];
    const text = String(src || '');
    const re = /^\s*\d+[.)]\s+(?:\*\*(.+?)\*\*|__(.+?)__)\s*(.*)$/;
    let cur = null;
    for (const line of text.split('\n')) {
      const m = re.exec(line);
      if (m) { cur = [String(m[1] || m[2]).trim(), String(m[3] || '').trim()]; out.push(cur); continue; }
      const plain = /^\s*\d+[.)]\s+(.*)$/.exec(line);
      if (plain) { cur = ['', plain[1].trim()]; out.push(cur); continue; }
      if (cur && /^\s{2,}\S/.test(line)) cur[1] = (cur[1] + ' ' + line.trim()).trim();
      else if (!line.trim()) cur = null;
    }
    return out;
  }
  /** Paragraphs of a markdown body that are not numbered steps or headings (the booklet's notes). */
  function proseOf(src) {
    if (typeof src !== 'string') return [];
    const out = [];
    let buf = [];
    const flush = () => { if (buf.length) out.push(buf.join(' ')); buf = []; };
    for (const line of src.split('\n')) {
      if (!line.trim() || /^\s*#/.test(line) || /^\s*\d+[.)]\s/.test(line) || /^\s{2,}\S/.test(line)) { flush(); continue; }
      buf.push(line.trim());
    }
    flush();
    return out;
  }
  /** A small subset of Mermaid flowcharts → {dir, nodes: [{id, label, shape}], edges: [{from, to, label}]}. */
  function parseMermaid(src) {
    const nodes = new Map();
    const edges = [];
    let dir = 'TB';
    const clean = (s) => String(s || '').replace(/^["'`]|["'`]$/g, '').replace(/<br\s*\/?>/gi, '\n').replace(/&quot;/g, '"').replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').trim();
    const NODE = /^([A-Za-z0-9_][\w-]*)\s*(\(\[(.*?)\]\)|\[\[(.*?)\]\]|\(\((.*?)\)\)|\{\{(.*?)\}\}|\[(.*?)\]|\((.*?)\)|\{(.*?)\})?/;
    const takeNode = (s) => {
      const m = NODE.exec(s);
      if (!m) return null;
      const id = m[1];
      let label = null;
      let shape = 'box';
      if (m[3] !== undefined) { label = m[3]; shape = 'pill'; }
      else if (m[4] !== undefined) label = m[4];
      else if (m[5] !== undefined) { label = m[5]; shape = 'pill'; }
      else if (m[6] !== undefined) { label = m[6]; shape = 'diamond'; }
      else if (m[7] !== undefined) label = m[7];
      else if (m[8] !== undefined) { label = m[8]; shape = 'pill'; }
      else if (m[9] !== undefined) { label = m[9]; shape = 'diamond'; }
      const cur = nodes.get(id);
      if (!cur) nodes.set(id, { id, label: label !== null ? clean(label) : id, shape });
      else if (label !== null) { cur.label = clean(label); cur.shape = shape; }
      return { id, rest: s.slice(m[0].length) };
    };
    for (let raw of String(src || '').split('\n')) {
      let line = raw.replace(/%%.*$/, '').trim().replace(/;$/, '');
      if (!line) continue;
      const head = /^(flowchart|graph)\s+(TD|TB|LR|RL|BT)\b/i.exec(line);
      if (head) { dir = /LR|RL/i.test(head[2]) ? 'LR' : 'TB'; continue; }
      if (/^(subgraph|end|classDef|class|style|linkStyle|click)\b/.test(line)) continue;
      let first = takeNode(line);
      if (!first) continue;
      let rest = first.rest.trim();
      let from = first.id;
      while (rest) {
        const em = /^(?:--\s*([^->|][^>]*?)\s*-->|==\s*([^=>][^>]*?)\s*==>|-\.\s*([^.]*?)\s*\.->|-->|==>|-\.->|---|--o|--x)\s*(?:\|([^|]*)\|)?\s*/.exec(rest);
        if (!em) break;
        const label = clean(em[1] || em[2] || em[3] || em[4] || '');
        rest = rest.slice(em[0].length);
        const to = takeNode(rest);
        if (!to) break;
        edges.push({ from, to: to.id, label });
        from = to.id;
        rest = to.rest.trim();
        if (/^&/.test(rest)) break;
      }
    }
    return { dir, nodes: Array.from(nodes.values()), edges };
  }
  /** A section's markdown without its numbered items (they are shown as the procedure list). */
  function textWithoutSteps(src) {
    const out = [];
    let inItem = false;
    let fenced = false;
    for (const line of String(src || '').split('\n')) {
      if (/^ {0,3}(```|~~~)/.test(line)) fenced = !fenced;
      if (!fenced && /^ {0,3}\d{1,3}[.)]\s+/.test(line)) { inItem = true; continue; }
      if (!fenced && inItem && (/^\s+\S/.test(line) || !line.trim())) { if (!line.trim()) inItem = false; continue; }
      inItem = false;
      out.push(line);
    }
    return out.join('\n').replace(/\n{3,}/g, '\n\n').trim();
  }
  /** Server map ids → the booklet section they belong to (docs.MAPS has no module field). */
  const MAP_MODULE = { 'factory-line': 'factory-line', 'plan-flow': 'plans', plans: 'plans', 'plan-map': 'plans', 'factory-line-map': 'factory-line' };
  const MAP_SHAPE = { decision: 'diamond', diamond: 'diamond', terminal: 'pill', pill: 'pill', human: 'pill', start: 'pill', end: 'pill' };
  /** GET /api/docs/procedures in any of the shapes a server may send → {title, intro, sections, maps}.
   *  docs.procedures sends {generated_at, agents: [{name, description, model, tier, driver, tools, intro,
   *  procedure: [{title, steps: [{n, lead, text}], text}], markdown}], maps: [{id, title, description,
   *  nodes: [{id, label, kind, col, row}], edges: [{from, to, label, side}]}]}. */
  function normalizeDocs(json) {
    const doc = isObj(json) ? json : {};
    const pick = (o, keys) => { for (const k of keys) if (o[k] !== undefined && o[k] !== null) return o[k]; return undefined; };
    let list = pick(doc, ['sections', 'agents', 'procedures', 'modules']);
    if (isObj(list)) list = Object.keys(list).map((k) => Object.assign({ name: k }, isObj(list[k]) ? list[k] : { procedure: list[k] }));
    const sections = [];
    for (const s of Array.isArray(list) ? list : []) {
      if (!isObj(s)) continue;
      const id = String(pick(s, ['id', 'name', 'agent']) || '').replace(/\.md$/, '');
      if (!id) continue;
      const body = pick(s, ['procedure', 'steps', 'working_procedure', 'text', 'markdown', 'body']);
      // docs.parse_procedure: a list of headed parts, each with its numbered steps and its text.
      const headed = Array.isArray(body) && body.length && body.every((x) => isObj(x) && (Array.isArray(x.steps) || (x.title !== undefined && x.text !== undefined && x.lead === undefined)));
      const parts = headed ? body.map((x) => ({ title: String(x.title || ''), steps: parseProcedure(Array.isArray(x.steps) ? x.steps : []),
        text: Array.isArray(x.steps) && x.steps.length ? textWithoutSteps(x.text) : String(x.text || '').trim() })) : [];
      const meta = [];
      if (s.model) meta.push('model ' + s.model + (s.tier && s.tier !== s.model ? ' (tier ' + s.tier + ')' : ''));
      if (s.driver) meta.push('driver ' + s.driver);
      if (Array.isArray(s.tools) && s.tools.length) meta.push('tools ' + s.tools.map(String).join(', '));
      sections.push({ id, title: String(pick(s, ['title', 'label', 'name']) || id), kind: String(s.kind || 'agent'),
        summary: String(pick(s, ['description', 'summary']) || ''), intro: headed ? String(s.intro || '').trim() : '',
        meta: meta.join(' · '), procedure: headed ? [] : parseProcedure(body), parts,
        agent: { model: s.model ? String(s.model) : '', tier: s.tier ? String(s.tier) : '', driver: s.driver ? String(s.driver) : '', tools: Array.isArray(s.tools) ? s.tools.map(String) : [] },
        notes: Array.isArray(s.notes) ? s.notes.map(String) : typeof s.notes === 'string' ? [s.notes] : headed ? [] : proseOf(body),
        maps: [] });
    }
    const maps = [];
    for (const m of Array.isArray(doc.maps) ? doc.maps : []) {
      if (!isObj(m)) continue;
      let g = null;
      const src = pick(m, ['mermaid', 'source', 'text']);
      if (Array.isArray(m.nodes)) {
        g = { dir: m.dir || 'TB',
          nodes: m.nodes.filter(isObj).map((n) => {
            const kind = String(n.shape || n.kind || 'box');
            return { id: String(n.id), label: String(n.label || n.text || n.id), shape: MAP_SHAPE[kind] || 'box', tone: kind === 'human' ? 'human' : undefined,
              col: typeof n.col === 'number' ? n.col : undefined, row: typeof n.row === 'number' ? n.row : undefined };
          }),
          edges: (Array.isArray(m.edges) ? m.edges : []).filter(isObj).map((e) => ({ from: String(e.from), to: String(e.to), label: String(e.label || '') })) };
      } else if (typeof src === 'string') g = parseMermaid(src);
      if (!g || !g.nodes.length) continue;
      const id = String(m.id || m.title || 'map-' + maps.length);
      maps.push({ id, title: String(m.title || m.heading || 'Map'), description: String(m.description || ''),
        module: String(m.module || m.section || m.agent || MAP_MODULE[id] || ''), graph: g });
    }
    for (const m of maps) { const s = sections.find((x) => x.id === m.module || x.title === m.module); if (s) s.maps.push(m); }
    return { title: String(doc.title || ''), intro: String(doc.intro || doc.description || ''), build: String(doc.build || doc.version || ''), generated: String(doc.generated_at || ''),
      sections, maps: maps.filter((m) => !sections.some((s) => s.maps.indexOf(m) >= 0)), raw: doc };
  }

  /** The factory line as a process map (rounded boxes, decision diamonds, labelled arrows). */
  function factoryMap(reviewers, maxResumes, delivery) {
    const revs = Array.isArray(reviewers) && reviewers.length ? reviewers : ['*'];
    const nodes = [{ id: 'req', label: 'Feature request', shape: 'pill' }, { id: 'spec', label: 'spec-writer\nwrites spec.md' },
      { id: 'build', label: 'builder\ncode, tests, build.md' }];
    const edges = [{ from: 'req', to: 'spec' }, { from: 'spec', to: 'build' }];
    for (const r of revs) { nodes.push({ id: 'rev-' + r, label: r === '*' ? 'reviewers\nreview-<name>.md' : 'reviewer-' + r + '\nreview-' + r + '.md' }); edges.push({ from: 'build', to: 'rev-' + r }); edges.push({ from: 'rev-' + r, to: 'pass' }); }
    nodes.push({ id: 'pass', label: 'Every review\nPASS?', shape: 'diamond' }, { id: 'approver', label: 'approver\ndecision.md' },
      { id: 'decide', label: 'APPROVE?', shape: 'diamond' }, { id: 'mode', label: 'Output mode?', shape: 'diamond' },
      { id: 'gate', label: 'Protected path or\nover the size limit?', shape: 'diamond' }, { id: 'merged', label: 'Merged into\nthe base branch', shape: 'pill' },
      { id: 'branch', label: 'Delivered:\nbranch kept', shape: 'pill' }, { id: 'folder', label: 'Delivered: patch,\nfiles, REPORT.md', shape: 'pill' },
      { id: 'human', label: 'Needs human:\napprove · rework · reject', shape: 'pill' });
    edges.push({ from: 'pass', to: 'build', label: 'any CHANGES → resume builder (max ' + (maxResumes === undefined ? 2 : maxResumes) + ')' },
      { from: 'pass', to: 'approver', label: 'all PASS' }, { from: 'pass', to: 'human', label: 'rounds exhausted' },
      { from: 'approver', to: 'decide' }, { from: 'decide', to: 'mode', label: 'APPROVE' }, { from: 'decide', to: 'human', label: 'ESCALATE' },
      { from: 'mode', to: 'gate', label: 'merge' }, { from: 'mode', to: 'branch', label: 'branch' }, { from: 'mode', to: 'folder', label: 'folder' },
      { from: 'gate', to: 'merged', label: 'No' }, { from: 'gate', to: 'human', label: 'Yes' });
    return { dir: 'TB', nodes, edges, highlight: delivery ? [delivery === 'merge' ? 'merged' : delivery] : [] };
  }
  /** Plans as a process map. */
  function planMap() {
    return { dir: 'TB',
      nodes: [{ id: 'req', label: 'Large request', shape: 'pill' }, { id: 'lead', label: 'lead\nwrites plan.md' }, { id: 'ready', label: 'PLAN: READY?', shape: 'diamond' },
        { id: 'answer', label: 'You answer\nin plan.md' }, { id: 'edit', label: 'You read, edit\nand press Run' }, { id: 'jobs', label: 'Jobs run as their\ndependencies merge' },
        { id: 'stuck', label: 'A job needs you?', shape: 'diamond' }, { id: 'blocked', label: 'Plan blocked\nuntil it merges' }, { id: 'final', label: 'lead\nfinal review' },
        { id: 'verdict', label: 'VERDICT?', shape: 'diamond' }, { id: 'follow', label: 'Follow-up jobs\n(once)' }, { id: 'done', label: 'Done', shape: 'pill' }],
      edges: [{ from: 'req', to: 'lead' }, { from: 'lead', to: 'ready' }, { from: 'ready', to: 'answer', label: 'QUESTIONS' }, { from: 'answer', to: 'lead', label: 'Retry' },
        { from: 'ready', to: 'edit', label: 'READY' }, { from: 'edit', to: 'jobs' }, { from: 'jobs', to: 'stuck' }, { from: 'stuck', to: 'blocked', label: 'Yes' },
        { from: 'blocked', to: 'jobs', label: 'merged' }, { from: 'stuck', to: 'final', label: 'all merged' }, { from: 'final', to: 'verdict' },
        { from: 'verdict', to: 'done', label: 'PASS' }, { from: 'verdict', to: 'follow', label: 'CHANGES' }, { from: 'follow', to: 'jobs' }] };
  }
  /** A flow as a map graph: steps as nodes, a diamond where the option you pick decides the next step, options as
   *  labelled arrows. A step whose next follows from an earlier answer gets a small check diamond after it
   *  (id "if:<step>", labelled with step.ask), so no fork is drawn where nothing is chosen. */
  function flowMap(flow, ctx) {
    const edges = [];
    const nodes = flow.steps.map((s) => ({ id: s.id, label: titleOf(s, emptyDraft()), shape: branching(flow, s.id, ctx) ? 'diamond' : 'box', group: s.group }));
    const conds = new Set();
    for (const e of edgesOf(flow, ctx)) {
      if (!e.cond) { edges.push(e); continue; }
      const id = 'if:' + e.from;
      if (!conds.has(id)) {
        conds.add(id);
        const step = stepOf(flow, e.from);
        nodes.push({ id, label: step.ask || 'Which way?', shape: 'diamond', cond: true, of: e.from, small: true });
        edges.push({ from: e.from, to: id, label: '' });
      }
      edges.push({ from: id, to: e.to, label: e.label, cond: true });
    }
    if (flow.id === 'welcome') {
      const opts = optionsOf(flow.steps[0], emptyDraft(), ctx);
      for (const o of opts) { nodes.push({ id: 'go:' + o.id, label: o.title, shape: 'pill', go: o.go }); edges.push({ from: 'what', to: 'go:' + o.id, label: '' }); }
      nodes[0].shape = 'diamond';
    } else {
      const from = new Set(edges.map((e) => e.from));
      const ends = flow.steps.filter((s) => !from.has(s.id));
      for (const s of ends) { nodes.push({ id: 'end', label: flow.id === 'setup' ? 'Applied: ready to start' : 'Job, plan or chat opens', shape: 'pill' }); edges.push({ from: s.id, to: 'end', label: s.kind === 'review' ? 'Apply' : 'Start' }); }
    }
    return { dir: 'TB', nodes, edges };
  }

  // ================================================================ the Grid Card and working procedures
  /** Working procedures for the factory's own modules, used when the server has no /api/docs/procedures. */
  const AGENT_PROCEDURES = {
    'factory-line': { title: 'Factory line', kind: 'flow', summary: 'One feature request becomes a reviewed, approved change: a spec, a build, parallel reviews and a final decision.', procedure: [
      ['Queue the request.', 'A job waits for a free worker and keeps the routing it was submitted with.'],
      ['Specify, then build.', 'The spec-writer writes testable criteria; the builder implements them on the job branch and proves each one.'],
      ['Review in parallel.', 'Every review lens reads the diff and runs the checks; the line waits for all of them.'],
      ['Loop on findings.', 'Any CHANGES resumes the builder with the reviews, up to the resume limit; then a human decides.'],
      ['Decide and deliver.', 'The approver says APPROVE or ESCALATE; APPROVE merges, keeps the branch or exports a folder.']] },
    plans: { title: 'Plans', kind: 'flow', summary: 'For work bigger than one job: a lead above the line plans, runs and reviews the whole.', procedure: [
      ['Plan.', 'The lead reads the request and the repository and writes plan.md: ordered jobs, or questions for you.'],
      ['Approve the plan.', 'You read and edit plan.md, then press Run.'],
      ['Run in order.', 'Each job starts once the jobs it depends on have merged.'],
      ['Review the whole.', 'The lead reviews the combined change; its findings become follow-up jobs once.']] },
    'spec-writer': { title: 'Spec writer', summary: 'Factory step 1: turns a feature request into spec.md with testable acceptance criteria, grounded in the real code.', procedure: [
      ['Read the repo’s own instructions.', 'AGENTS.md, CLAUDE.md or QWEN.md says how the project is built, run and tested; the spec follows it.'],
      ['Find the real code.', 'Grep and Glob locate the files, functions and components the request touches; nothing is guessed.'],
      ['Write testable criteria.', 'spec.md lists requirements and acceptance criteria, each with the exact check that proves it.'],
      ['Write down what is out of scope.', 'Tempting extras and ambiguous decisions are recorded, so the builder and the approver see them.']] },
    builder: { title: 'Builder', summary: 'Factory step 2: implements the spec on the job branch, tests it, commits and writes build.md. Resumed after each review round.', procedure: [
      ['Confirm the branch.', 'Work starts only on the job branch in its own worktree; a wrong branch stops the run.'],
      ['Make the smallest complete change.', 'Match the surrounding code and meet the spec, with no speculative extras.'],
      ['Prove every criterion.', 'Run each acceptance check and the repo’s tests, lint and build, and record the evidence in build.md.'],
      ['Answer every review.', 'When reviewers ask for changes, the builder resumes with their files and addresses each finding.']] },
    reviewer: { title: 'Reviewers', summary: 'One review lens each (security, UX, UI, code, or any lens you add); they run in parallel and the line waits for all of them.', procedure: [
      ['Read the spec, the build notes and the diff.', 'Only the branch’s own changes count, compared with the base using three dots.'],
      ['Run the checks again.', 'Tests, lint and build are run by the reviewer; build.md’s evidence is not trusted on its own.'],
      ['Judge through one lens.', 'Each reviewer writes one review file that starts with its verdict.'],
      ['Say PASS or CHANGES.', 'CHANGES lists numbered findings the builder can act on; every reviewer must pass before the approver.']] },
    approver: { title: 'Approver', summary: 'The final gate: after a round where every reviewer passed, decides APPROVE or ESCALATE and writes decision.md.', procedure: [
      ['Read everything.', 'The feature, the spec, the build notes, every review and the whole diff.'],
      ['Spot-check a criterion.', 'At least one acceptance check is run again before deciding.'],
      ['Approve only what you would merge.', 'Money, access, data, CI, dependencies and agent configuration are escalated.'],
      ['Escalate with a reason.', 'ESCALATE puts the job in Needs human with the reason in decision.md.']] },
    gate: { title: 'Gate and output', summary: 'Code, not a model: what APPROVE does with the branch.', procedure: [
      ['Check protected paths.', 'A merge that touches a protected glob always goes to a human.'],
      ['Check the size.', 'A diff above the auto-merge limit goes to a human; so does every job while auto-merge is off.'],
      ['Deliver.', 'Merge into the base branch, keep the branch, or export a folder, as the job’s output says.']] },
    human: { title: 'Needs human', summary: 'Where a job waits for your decision.', procedure: [
      ['Read the reason.', 'The card says why the job stopped: an escalation, exhausted review rounds, a protected path or a failure.'],
      ['Decide.', 'Approve merges it, Rework sends a note to the builder, Reject removes the branch; Change routing picks other models first.']] },
    lead: { title: 'Lead', summary: 'Sits above the line: plans a large request as ordered jobs and reviews the combined change once they have merged.', procedure: [
      ['Read the request and the repo.', 'The lead works read-only on a checkout of the base branch.'],
      ['Split into small jobs.', 'Each job is one change a builder can finish and test in one run; dependencies are marked (after: n).'],
      ['Ask instead of guessing.', 'Product decisions become PLAN: QUESTIONS for you to answer in plan.md.'],
      ['Review the whole.', 'After every job has merged, the lead reviews the combined change against the request.']] },
    coder: { title: 'Coder (Chat)', summary: 'The interactive coding agent behind Chat.', procedure: [
      ['Look before changing.', 'Read the repo instructions and the relevant files first; ask one short question when the request is ambiguous.'],
      ['Change the least that works.', 'Small edits in the repo’s own style, verified with its tests.'],
      ['Commit finished work.', 'Committed work on the chat branch can be handed to the factory for review.']] },
    analyst: { title: 'Analyst (Chat)', summary: 'The Chat agent for reverse engineering, with REA over MCP.', procedure: [
      ['Inspect the binary with REA.', 'REA’s tools read structure, symbols and code over MCP.'],
      ['Keep notes.', 'Findings go to the analyst’s notes file only; nothing else in the repository changes.'],
      ['Hand on what you learn.', 'The notes become the context for a feature, a plan or a chat.']] },
    handoff: { title: 'Hand-off', summary: 'A chat’s branch enters the factory line.', procedure: [
      ['Commit on the chat branch.', 'The branch needs commits ahead of its base and a clean tree.'],
      ['Hand off with one line.', 'The spec-writer describes what the branch does; review starts without a fresh build.']] },
    'repo-scout': { title: 'Repo scout', summary: 'Drafts the AGENTS.md every agent relies on.', procedure: [
      ['Inspect once.', 'Manifests, scripts, CI steps and docs show the stack and its commands.'],
      ['Write only what has evidence.', 'Every command in the draft comes from the repository itself.'],
      ['Draft AGENTS.md.', 'You review the draft and save it as AGENTS.md in the repository.']] },
    tracker: { title: 'Housekeeping Tracker', summary: 'Everything the agents promised, found or were asked to do, kept until it is proven done.', procedure: [
      ['Collect every promise.', 'Acceptance criteria, review findings, product decisions and what an agent says it will do later become items of their job, chat or plan.'],
      ['Close only with proof.', 'An item is proven by someone other than its owner; a claimed command is never re-run, and only a person answers a product decision.'],
      ['Carry what is left.', 'When a job, chat or plan ends, its open items move to the repository’s follow-ups; Make a job turns a follow-up into a new job.']] },
    queue: { title: 'Queue', summary: 'Jobs wait here for a free worker.', procedure: [
      ['Wait for a slot.', 'Up to the parallel-job limit run at once; the rest wait in order.'],
      ['Snapshot the routing.', 'Each job keeps the routing it was submitted with, whatever changes later.']] },
  };
  /** The harness on one page. Lanes left → right: Set up, the factory line, Plans, Chat; rows top → bottom. */
  const GRID = {
    lanes: [{ id: 'setup', title: 'Set up' }, { id: 'line', title: 'Factory line' }, { id: 'plans', title: 'Plans' }, { id: 'chat', title: 'Chat' },
      { id: 'housekeeping', title: 'Housekeeping' }],   // drawn under Chat, beside the line's outputs
    nodes: [
      { id: 'welcome', label: 'Welcome', lane: 'setup', row: 0, shape: 'pill', open: '#/welcome', proc: { flow: 'welcome', step: 'what' } },
      { id: 'providers', label: 'Providers', lane: 'setup', row: 1, status: 'providers', configure: 'api-providers', proc: { flow: 'setup', step: 'api-keys' } },
      { id: 'models', label: 'Models', lane: 'setup', row: 2, status: 'models', configure: 'where', proc: { flow: 'setup', step: 'where' } },
      { id: 'routing', label: 'Routing', lane: 'setup', row: 3, status: 'routing', configure: 'roles', open: '#/settings/routing', proc: { flow: 'setup', step: 'roles' } },
      { id: 'tools', label: 'Tools', lane: 'setup', row: 4, status: 'tools', configure: 'tools', proc: { flow: 'setup', step: 'tools' } },
      { id: 'safety', label: 'Safety', lane: 'setup', row: 5, status: 'safety', configure: 'sandbox', proc: { flow: 'setup', step: 'sandbox' } },
      { id: 'repos', label: 'Repos', lane: 'setup', row: 6, status: 'repos', configure: 'repo', open: '#/settings/repos', proc: { flow: 'setup', step: 'repo' } },
      { id: 'output', label: 'Output', lane: 'setup', row: 7, status: 'output', configure: 'output', proc: { flow: 'setup', step: 'output' } },
      { id: 'start', label: 'Start: what?', lane: 'line', row: 1, shape: 'diamond', open: '#/start', proc: { flow: 'start', step: 'what' } },
      { id: 'queue', label: 'Queued', lane: 'line', row: 2, stages: ['queued'], open: '#/factory', proc: { agent: 'queue' } },
      { id: 'spec', label: 'spec-writer', lane: 'line', row: 3, agent: 'spec-writer', stages: ['spec'], open: '#/agents/spec-writer', proc: { agent: 'spec-writer' } },
      { id: 'builder', label: 'builder', lane: 'line', row: 4, agent: 'builder', stages: ['build'], open: '#/agents/builder', proc: { agent: 'builder' } },
      { id: 'reviewers', label: 'reviewers', lane: 'line', row: 5, kind: 'reviewers', stages: ['review'], proc: { agent: 'reviewer' } },
      { id: 'wait', label: 'wait for all', lane: 'line', row: 6, shape: 'small', proc: { agent: 'reviewer' } },
      { id: 'approver', label: 'approver', lane: 'line', row: 7, agent: 'approver', stages: ['approve'], open: '#/agents/approver', proc: { agent: 'approver' } },
      { id: 'human', label: 'Needs human', lane: 'line', row: 7, side: 'left', stages: ['needs-human'], open: '#/factory', proc: { agent: 'human' } },
      { id: 'gate', label: 'Gate', lane: 'line', row: 8, shape: 'diamond', stages: ['merging'], proc: { agent: 'gate' } },
      { id: 'out-merge', label: 'Base branch', lane: 'line', row: 9, col: 0, kind: 'output', mode: 'merge', stages: ['merged'], proc: { agent: 'gate' } },
      { id: 'out-branch', label: 'Job branch', lane: 'line', row: 9, col: 1, kind: 'output', mode: 'branch', stages: ['delivered:branch'], proc: { agent: 'gate' } },
      { id: 'out-folder', label: 'Output folder', lane: 'line', row: 9, col: 2, kind: 'output', mode: 'folder', stages: ['delivered:folder'], proc: { agent: 'gate' } },
      { id: 'lead', label: 'lead', lane: 'plans', row: 3, agent: 'lead', plans: ['planning'], open: '#/plans', proc: { agent: 'lead' } },
      { id: 'plan-ready', label: 'You read and Run', lane: 'plans', row: 4, plans: ['ready', 'needs-human'], open: '#/plans', proc: { agent: 'lead' } },
      { id: 'plan-jobs', label: 'Jobs in order', lane: 'plans', row: 5, plans: ['running', 'blocked'], open: '#/plans', proc: { agent: 'lead' } },
      { id: 'final', label: 'Final review', lane: 'plans', row: 6, plans: ['final-review'], open: '#/plans', proc: { agent: 'lead' } },
      { id: 'plan-done', label: 'Done', lane: 'plans', row: 7, shape: 'pill', plans: ['done'], open: '#/plans', proc: { agent: 'lead' } },
      { id: 'chat-agent', label: 'coder · analyst', lane: 'chat', row: 3, chats: true, open: '#/chat', proc: { agent: 'coder' } },
      { id: 'handoff', label: 'Hand off', lane: 'chat', row: 4, open: '#/chat', proc: { agent: 'handoff' } },
      // A view module's node (views/tracker.js): drawn only while that view is registered; its status is the view's badge.
      { id: 'tracker', label: 'Housekeeping Tracker', lane: 'housekeeping', row: 9, view: 'tracker', open: '#/housekeeping', proc: { agent: 'tracker' } },
    ],
    edges: [
      { from: 'welcome', to: 'providers', label: 'first run' },
      { from: 'providers', to: 'models' }, { from: 'models', to: 'routing' }, { from: 'routing', to: 'tools' }, { from: 'tools', to: 'safety' },
      { from: 'safety', to: 'repos' }, { from: 'repos', to: 'output' },
      { from: 'output', to: 'start', label: 'ready', route: 'up-left' },
      { from: 'welcome', to: 'start', label: 'set up already', route: 'over' },
      { from: 'start', to: 'queue', label: 'feature' },
      { from: 'start', to: 'lead', label: 'plan', route: 'right-down' },
      { from: 'start', to: 'chat-agent', label: 'chat · analyse', route: 'right-down' },
      { from: 'queue', to: 'spec' }, { from: 'spec', to: 'builder', label: 'spec.md' },
      { from: 'builder', to: 'reviewers', label: 'build.md' }, { from: 'reviewers', to: 'wait' },
      { from: 'wait', to: 'builder', label: 'loop', route: 'loop' },
      { from: 'wait', to: 'approver', label: 'all PASS' },
      { from: 'approver', to: 'human', label: 'ESCALATE', route: 'side' },
      { from: 'approver', to: 'gate', label: 'APPROVE' },
      { from: 'gate', to: 'human', label: 'protected · too big', route: 'gate-human' },
      { from: 'gate', to: 'out-merge', label: 'merge' }, { from: 'gate', to: 'out-branch', label: 'branch' }, { from: 'gate', to: 'out-folder', label: 'folder' },
      { from: 'lead', to: 'plan-ready', label: 'plan.md' }, { from: 'plan-ready', to: 'plan-jobs', label: 'Run' },
      { from: 'plan-jobs', to: 'queue', label: 'each job', route: 'into-queue-low' },
      { from: 'plan-jobs', to: 'final', label: 'all merged' }, { from: 'final', to: 'plan-done', label: 'PASS' },
      { from: 'chat-agent', to: 'handoff', label: 'branch ready' },
      { from: 'handoff', to: 'queue', label: 'branch as round 1', route: 'into-queue-high' },
      { from: 'out-folder', to: 'tracker', label: 'open items when a job ends' },
      { from: 'tracker', to: 'queue', label: 'make a job', route: 'into-queue-high' },
    ],
  };

  root.SFFlows = Object.freeze({
    FLOWS, LOCAL_SERVERS, API_PROVIDERS, DELIVERY, DELIVERY_LINE, PRESETS, PRESET_TEXT, PROTECTED_PROFILES, STANDARD_PATHS, SETUP_GROUPS,
    AGENT_PROCEDURES, GRID, BADGE: B, MCP_AGENTS, TIERS,
    emptyDraft, normDraft, choiceOf, valueOf, setValue, listOf, linesOf,
    flowOf, stepOf, titleOf, optionsOf, currentOf, groupsOf, fieldsOf, visibleFields, fieldValue, defaultChoice, nextOf, pathOf, edgesOf, branching,
    pruneDraft, validateStep, patchChanges, fmtValue, deepMerge, setupPlan, addMcp, startRequest, hashFor,
    normalizePresets, routingPresets, routingOutcome, routingSummary, keyBadges, keyState, firstSentence, modelText, providerTitle, profileChoices,
    deliveryDefault, keySet, reviewersOf, textWithoutSteps, idText, listsText,
    parseProcedure, parseMermaid, normalizeDocs, factoryMap, planMap, flowMap, stable,
  });
})(typeof window !== 'undefined' ? window : globalThis);
