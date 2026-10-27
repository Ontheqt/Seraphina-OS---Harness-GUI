/* Seraphina OS Harness: placement inside the Software Factory (DESIGN §23 and §15 "Placement"; GPU custody §37).
 * It adds to the Factory's own screens through window.SF: a routing profile picker and the estimate on New feature,
 * New plan, New chat, Hand off, Change routing and the Start review (SF.extend 'form'); where each run went, why
 * (capability, time, stage fit, cost), on which build, overflow, swap or escalation rung, under which GPU custody
 * lease, what the independence guard did and what it cost, on job cards, the job drawer (header and a Runs tab),
 * Live run headers, chats and plans; the placement settings (routing profiles, builds, overflow, prices, families,
 * effort, escalation, reasons, independent alternates, a provider's loaded model, slots and custody register) in
 * Settings; a line under the Doctor's placement checks. Its own page, #/placement, explains placement, estimates a
 * run and shows the GPU custody register. No rail entry and no Home card: it is part of the Software Factory.
 * Every text an agent, a register, a server or a person wrote is shown as text (SF.h), never as HTML. */
'use strict';
(function (SF) {
  const h = SF.h;
  const isObj = SF.isObj;
  const PROFILE_RE = /^[a-z0-9][a-z0-9-]{0,40}$/;
  const EFFORTS = ['minimal', 'low', 'medium', 'high', 'xhigh', 'max'];
  const STAGE_ORDER = ['lead', 'spec', 'build', 'review', 'approve'];
  const LINE = ['spec-writer', 'builder', 'approver'];
  const ST = {
    cfg: null, cfgAt: 0, cfgLoading: null,
    custody: null, custodyAt: 0, custodyLoading: null, custodyError: null, custodyFor: null,
    settings: null,                        // the last Settings render's helpers (to show the custody lines once read)
    open: new Set(),                       // open Placement disclosures in Settings ('route:builder', 'model:local', …)
    pendingReason: new Map(),              // a reason line typed before its kind, by error key (kept across re-renders)
    pendingCustody: new Map(),             // a refused custody field as typed, by error key (kept across re-renders)
    page: { repo: '', kind: 'feature', preset: '' },
  };

  // ================================================================ words: reasons, cost, independence, markers
  /** The owner's four reasons (DESIGN §23): label, icon, what the reason means. */
  const REASONS = {
    capability: ['Capability', 'bulb', 'the stage decides quality: spec, approval, lead, final review'],
    time: ['Time', 'clock', 'a busy local slot would serialise the work; an API runs it at once'],
    'stage-fit': ['Stage fit', 'route', 'another model family than the builder, or sensitive code stays local'],
    cost: ['Cost', 'zap', 'high-volume work on a free or cheap model'],
  };
  function reasonOf(r) {
    if (typeof r === 'string') {
      const i = r.indexOf(':');
      return i >= 0 ? { kind: r.slice(0, i).trim(), text: r.slice(i + 1).trim() } : { kind: r.trim(), text: '' };
    }
    return isObj(r) && typeof r.kind === 'string' ? { kind: r.kind, text: typeof r.text === 'string' ? r.text : '' } : null;
  }
  /** The reason's kind as a badge (its text in the tooltip); withText adds the text beside it. */
  function reasonBadge(r, withText) {
    const x = reasonOf(r);
    if (!x || !x.kind) return null;
    const ui = REASONS[x.kind] || [x.kind, 'info', ''];
    const badge = h('span', { class: 'tag pl-why pl-why-' + (REASONS[x.kind] ? x.kind : 'other'), title: 'Why it runs there: ' + ui[0].toLowerCase() + (x.text ? ' (' + x.text + ')' : ''), dataset: { why: x.kind } },
      SF.icon(ui[1], 'icon-xs'), ui[0]);
    if (!withText || !x.text) return badge;
    return h('span', { class: 'pl-why-line' }, badge, h('span', { class: 'pl-why-text' }, x.text));
  }
  function fmtUsd(n) {
    if (n === null || n === undefined || !isFinite(Number(n))) return '';
    n = Number(n);
    if (n === 0) return '$0';
    if (n < 0.01) return '<$0.01';
    if (n < 100) return '$' + n.toFixed(2);
    return '$' + Math.round(n).toLocaleString('en-US');
  }
  /** A running total {usd, priced, unpriced_runs}: "$4.49", "$4.49 + 1 unpriced run" (short, on a card: "$4.49 + 1
   *  unpriced"; in words, as a phone has no tooltip). */
  function costLabel(c, short) {
    if (!isObj(c)) return '';
    const usd = fmtUsd(Number(c.usd) || 0);
    const n = Number(c.unpriced_runs) || 0;
    if (c.priced !== false || !n) return usd;
    return short ? usd + ' + ' + n + ' unpriced' : usd + ' + ' + SF.plural(n, 'unpriced run');
  }
  function costTitle(c, what) {
    if (!isObj(c)) return '';
    const n = Number(c.unpriced_runs) || 0;
    return (what || 'Cost so far') + ': ' + fmtUsd(Number(c.usd) || 0) + (c.priced === false && n ? '. It leaves out ' + SF.plural(n, 'run') + ' on models without prices.' : '. Every run was priced.');
  }
  function costTag(c, cls) {
    if (!isObj(c)) return null;
    return h('span', { class: 'tag tag-mono pl-cost' + (c.priced === false ? ' is-partial' : '') + (cls ? ' ' + cls : ''), title: costTitle(c) }, costLabel(c, true));
  }
  /** One run's cost from agent.end or a job.runs entry: "$0.42", or "unpriced". */
  function runCostTag(d) {
    if (!isObj(d) || !('priced' in d || 'cost_usd' in d)) return null;
    if (d.priced === false || d.cost_usd === null || d.cost_usd === undefined) {
      return h('span', { class: 'tag tag-outline pl-cost is-unpriced', title: 'This model has no prices: the run is left out of the total (unknown, not free)' }, 'unpriced');
    }
    return h('span', { class: 'tag tag-mono pl-cost', title: 'This run cost ' + fmtUsd(d.cost_usd) }, fmtUsd(d.cost_usd));
  }
  const IND = {
    reduced: ['pl-ind-reduced', 'alert', 'Independence reduced'],
    unknown: ['pl-ind-unknown', 'help', 'Independence unknown'],
    accepted: ['pl-ind-accepted', 'check', 'Shared family (accepted)'],
    alternate: ['pl-ind-alt', 'route', 'Independent alternate'],
  };
  /** "<agent> ran on <model> (<model_id>), the <family> family the builder ran on in round <round> (<builder_model>)". */
  function sharedLine(agent, sm, round) {
    sm = isObj(sm) ? sm : {};
    return String(agent || 'a judge') + ' ran on ' + String(sm.model || '?') + (sm.model_id ? ' (' + sm.model_id + ')' : '') + ', the ' + String(sm.family || '?') +
      ' family the builder ran on' + (round !== null && round !== undefined ? ' in round ' + round : '') + (sm.builder_model ? ' (' + sm.builder_model + ')' : '');
  }
  /** A run's independence as the guard recorded it: reduced (an amber badge, never green), accepted (grey), unknown
   *  (amber outline) or alternate (a quiet chip naming the model it left). */
  function indChip(status, agent, sm, round) {
    const ui = IND[status];
    if (!ui) return null;
    let title = '';
    if (status === 'reduced') title = sharedLine(agent, sm, round);
    else if (status === 'accepted') title = sharedLine(agent, sm, round) + '; accepted by the routing';
    else if (status === 'unknown') title = 'the code was written in a chat whose models were not recorded';
    else title = String(agent || 'The judge') + ' left ' + String((sm && sm.model) || 'its own model') + (sm && sm.family ? ', of the ' + sm.family + ' family the builder ran on,' : '') + ' for its independent alternate';
    const label = status === 'alternate' && isObj(sm) && sm.model ? 'Independent alternate (left ' + sm.model + ')' : ui[2];
    return h('span', { class: 'tag pl-ind ' + ui[0], title, dataset: { independence: status } }, SF.icon(ui[1], 'icon-xs'), label);
  }
  /** The job's own independence (its worst run's), the tooltip listing job.shared_models. */
  function jobIndChip(job) {
    const status = job && job.independence;
    const ui = IND[status];
    if (!ui || status === 'alternate') return null;
    const lines = sharedLines(job);
    return h('span', { class: 'tag pl-ind ' + ui[0], title: lines.join('\n'), dataset: { independence: status } }, SF.icon(ui[1], 'icon-xs'), ui[2]);
  }
  function sharedLines(job) {
    const lines = (Array.isArray(job.shared_models) ? job.shared_models : []).filter(isObj).map((m) => sharedLine(m.agent, m, m.round) + (m.accepted ? ' (accepted)' : ''));
    if (job.independence === 'unknown' && !lines.some((l) => !/\(accepted\)$/.test(l))) lines.push('The code was written in a chat whose models were not recorded, so its first reviews cannot be compared.');
    return lines;
  }
  function mark(cls, ic, label, title, kind) {
    return h('span', { class: 'tag pl-mark' + (cls ? ' ' + cls : ''), title, dataset: { mark: kind } }, SF.icon(ic, 'icon-xs'), label);
  }
  /** What was resident: a residency's {provider, model, vram_mib} rows, or a lease's [provider, model] pairs (the
   *  register's names, custody.Lease.view). */
  function residentText(list) {
    return (Array.isArray(list) ? list : []).map((x) => (Array.isArray(x) ? String(x[0] || '?') + '/' + String(x[1] || '?')
      : isObj(x) ? String(x.provider || '?') + '/' + String(x.model || '?') + (x.vram_mib ? ' (' + SF.fmtNum(x.vram_mib) + ' MiB)' : '') : ''))
      .filter(Boolean).join(', ') || 'nothing';
  }
  /** A run's error that GPU custody caused (DESIGN §39 "Errors and capture"): what happened in a few words, or null.
   *  The specific texts first: every one of them also carries "GPU custody: ". */
  function custodyRefusal(error) {
    const e = String(error || '');
    if (/the lease taken for .* at placement is no longer valid/.test(e)) return 'lease no longer valid';
    if (/the outcome is unknown, so the request was not replayed/.test(e)) return 'cut off under a lease';
    // custody.Claims._wait_out: a HELD answer after the run's one wait was used goes on at once
    if (/GPU custody: .*no wait left \(/.test(e)) return 'GPU custody refused: no wait left';
    if (/GPU custody: /.test(e)) return 'GPU custody refused';
    return null;
  }
  const whenText = (iso) => (isFinite(Date.parse(iso)) ? new Date(Date.parse(iso)).toLocaleString() : String(iso));
  /** Where a run went beside its route's first build: build, overflow, swap, escalation rung, a fresh session, its
   *  lease. Each: [kind, chip class, icon, short label, the whole fact as a sentence]. */
  function placementFacts(d) {
    const out = [];
    if (!isObj(d)) return out;
    if (isObj(d.build)) {
      const n = Number(d.build.index);
      const which = (isFinite(n) ? n + 1 : '?') + ' of ' + (d.build.of || '?');
      out.push(['build', '', 'server', 'build ' + which, 'Ran on build ' + which + ': ' + String(d.build.provider || '?') + '/' + String(d.build.model_id || '?') + '.']);
    }
    if (isObj(d.overflow)) out.push(['overflow', 'pl-mark-warn', 'arrow-right', 'overflow to ' + String(d.overflow.to || '?'), 'Overflow from ' + String(d.overflow.from || '?') + ' to ' + String(d.overflow.to || '?') + (d.overflow.why ? ': ' + d.overflow.why : '') + '.']);
    if (isObj(d.swap)) out.push(['swap', 'pl-mark-warn', 'refresh', 'model swap', String(d.swap.provider || 'The server') + ' loaded ' + String(d.swap.to || '?') + ' in place of ' + String(d.swap.from || '?') + ' (allow_model_swap).']);
    if (isObj(d.escalation)) {
      const e = d.escalation;
      out.push(['escalation', 'pl-mark-acc', 'send', 'rung ' + (e.rung || '?') + ' of ' + (e.of || '?'), 'Escalation rung ' + (e.rung || '?') + ' of ' + (e.of || '?') + ' after ' + SF.plural(Number(e.sendbacks) || 0, 'send-back') + ': ' + String(e.from || '?') + ' → ' + String(e.model || '?') + '.']);
    }
    if (d.handover) out.push(['handover', '', 'handoff', 'fresh session', 'A fresh session on a new model: a hand-over, not a resume (sessions never move between models).']);
    if (isObj(d.custody)) {
      const c = d.custody;
      out.push(['custody', '', 'layers', 'lease ' + String(c.lease_id || '?'), 'GPU custody lease ' + String(c.lease_id || '?') + ' for ' + String(c.model || '?') + ' on ' + String(c.provider || '?') +
        (c.until ? ' until ' + whenText(c.until) : '') + (c.generation !== undefined && c.generation !== null ? ', generation ' + c.generation : '') + '; resident before: ' + residentText(c.resident_before) + '.']);
    }
    // custody.Claims.info: each build the register gave no lease for while the run was placed, in its words (FULL
    // "the card cannot fit this model now", HELD "the card is held by someone else"), beside the build it ran on
    for (const r of (Array.isArray(d.custody_refused) ? d.custody_refused : []).filter(isObj)) {
      const where = String(r.provider || '?') + '/' + String(r.model || '?');
      const words = String(r.words || r.code || 'refused');
      out.push(['no-lease', 'pl-mark-warn', 'layers', 'no lease: ' + where, 'GPU custody gave no lease for ' + where + ': ' + words + '.']);
    }
    return out;
  }
  /** The facts as chips (the sentence in the tooltip; touch, keyboard and screen readers get it from runFacts). */
  function runMarks(d) { return placementFacts(d).map((f) => mark(f[1], f[2], f[3], f[4], f[0])); }
  /** The facts as visible sentences (the Runs tab), or for a screen reader only (a compact header). */
  function runFacts(d, srOnly) {
    const facts = placementFacts(d).filter((f) => f[0] !== 'handover');
    if (!facts.length) return null;
    return h('ul', { class: srOnly ? 'sr-only' : 'pl-run-facts', dataset: { facts: '' } }, facts.map((f) => h('li', { dataset: { fact: f[0] } }, f[4])));
  }
  const localTag = (local) => h('span', { class: 'tag ' + (local ? 'tag-local' : 'tag-api') }, SF.icon(local ? 'server' : 'cloud', 'icon-xs'), local ? 'LOCAL' : 'API');
  const famTag = (fam) => (fam ? h('span', { class: 'tag tag-outline pl-fam', title: 'Model family: the independence guard compares families' }, String(fam) + ' family') : null);
  const secs = (s) => (isFinite(Number(s)) ? SF.fmtDur(Number(s) * 1000) : '');

  // ================================================================ the config (profiles, presets, families)
  function loadCfg(force) {
    if (!force && ST.cfg && Date.now() - ST.cfgAt < 15000) return Promise.resolve(ST.cfg);
    if (ST.cfgLoading) return ST.cfgLoading;
    ST.cfgLoading = SF.api('/api/config').then((c) => { ST.cfg = isObj(c) ? c : {}; ST.cfgAt = Date.now(); return ST.cfg; })
      .finally(() => { ST.cfgLoading = null; });
    return ST.cfgLoading;
  }
  const profilesOf = (cfg) => (cfg && isObj(cfg.profiles) ? Object.keys(cfg.profiles).filter((k) => isObj(cfg.profiles[k])).sort() : []);
  const presetOf = (cfg, name) => (cfg && Array.isArray(cfg.presets) ? cfg.presets : []).find((p) => isObj(p) && p.name === name) || null;
  const profileDesc = (cfg, name) => { const p = cfg && isObj(cfg.profiles) ? cfg.profiles[name] : null; return p && typeof p.description === 'string' ? p.description : ''; };
  /** The profiles a run can name and its default: the saved ones; a routing preset brings its own and makes its own
   *  default (or none), as settings.preset_config merges it. */
  function profileChoices(cfg, routing) {
    const names = new Set(profilesOf(cfg));
    let def = cfg && typeof cfg.default_profile === 'string' && cfg.default_profile ? cfg.default_profile : null;
    let via = null;
    if (typeof routing === 'string') {
      const p = presetOf(cfg, routing);
      if (p && isObj(p.replaces) && Array.isArray(p.replaces.agents) && p.replaces.agents.length) {
        for (const n of Array.isArray(p.replaces.profiles) ? p.replaces.profiles : []) names.add(String(n));
        def = typeof p.default_profile === 'string' && p.default_profile ? p.default_profile : null;
        via = routing;
      }
    }
    return { names: Array.from(names).sort(), def, via };
  }
  /** "builder → local, approver → claude": the steps a profile places on a model. */
  function placesText(cfg, name) {
    const p = cfg && isObj(cfg.profiles) ? cfg.profiles[name] : null;
    const agents = p && isObj(p.agents) ? p.agents : {};
    const parts = Object.keys(agents).filter((a) => isObj(agents[a]) && agents[a].model).map((a) => a + ' → ' + agents[a].model);
    return parts.length ? parts.join(', ') : '';
  }

  // ================================================================ the profile picker (New feature, plan, chat, hand-off, Change routing)
  function profilePicker(o) {
    const id = 'pl-prof-' + o.form;
    const sel = h('select', { class: 'select', id, name: 'profile', dataset: { focus: 'profile' } });
    const hint = h('div', { class: 'field-hint pl-prof-hint' });
    const wrap = h('div', { class: 'field pl-prof', dataset: { field: 'profile' }, hidden: true }, h('label', { class: 'field-label', for: id }, 'Routing profile'), sel, hint);
    let value = '';
    let cfg = null;
    let ch = { names: [], def: null, via: null };
    const keepLabel = () => (o.job ? 'Keep the job’s: ' + (o.job.profile ? o.job.profile : 'no profile') : ch.def ? 'Default: ' + ch.def : 'Default: no profile');
    const current = () => (o.job ? o.job.profile || null : ch.def);
    function describe() {
      SF.clear(hint);
      const name = value === '' ? current() : value === ':none' ? null : value;
      if (!name) {
        SF.add(hint, value === ':none' ? 'No profile: every step runs on its route as saved, without a profile’s placements.' : 'No profile applies: every step runs on its route as saved.');
      } else {
        const desc = profileDesc(cfg, name);
        const places = placesText(cfg, name);
        SF.add(hint, [h('strong', null, name), desc ? ': ' + desc : '', places ? ' Places ' + places + '.' : '', ch.via && value === '' ? ' (the ' + ch.via + ' preset’s default)' : '']);
      }
      if (o.form === 'chat') SF.add(hint, ' A model you pick above goes over the profile’s.');
      if (o.form === 'routing') {
        // Factory.set_routing: naming a profile (or none) re-routes every step from the saved settings with it
        const when = o.job && o.job.stage === 'failed' ? 'Retry' : 'Rework';
        SF.add(hint, value === '' ? ' The job keeps its profile; the new routing applies at the next ' + when + '.'
          : ' Every step is placed again from your saved settings with ' + (value === ':none' ? 'no profile' : 'this profile') + ' (not only the steps you change), at the next ' + when + '. A step you change under Custom runs on what you pick there.');
      }
    }
    function fill(c) {
      cfg = c;
      ch = profileChoices(cfg, o.routing ? o.routing() : undefined);
      SF.clear(sel);
      sel.appendChild(h('option', { value: '' }, keepLabel()));
      if (current()) sel.appendChild(h('option', { value: ':none' }, 'No profile (the routing as saved)'));
      for (const n of ch.names) {
        const d = profileDesc(cfg, n);
        sel.appendChild(h('option', { value: n, title: d }, n + (d ? ' — ' + (d.length > 70 ? d.slice(0, 68).replace(/\s+\S*$/, '') + '…' : d) : '')));
      }
      sel.value = value;
      if (sel.value !== value) { value = ''; sel.value = ''; }
      wrap.hidden = !ch.names.length && !current();
      describe();
    }
    sel.addEventListener('change', () => { value = sel.value; describe(); if (o.onChange) o.onChange(); });
    return {
      el: wrap, select: sel, fill,
      /** undefined (the default, or the job's own), "" (none) or a name. */
      value: () => (wrap.hidden || value === '' ? undefined : value === ':none' ? '' : value),
    };
  }

  // ================================================================ the estimate (GET /api/estimate)
  const EST_CACHE = new Map();   // query → {at, r}
  const EST_TTL = 20000;
  const BASIS = { history: 'on this repository’s past jobs', defaults: 'on the default token table (fewer than 3 past jobs)', mixed: 'partly on past jobs, partly on the default table' };
  function independenceSentences(rows) {
    const out = [];
    for (const r of Array.isArray(rows) ? rows : []) {
      if (!isObj(r) || !isObj(r.builder)) continue;
      const parts = [];
      for (const [judge, alt] of Object.entries(isObj(r.alternate) ? r.alternate : {})) parts.push(judge + ' moves to ' + alt);
      for (const judge of Object.keys(isObj(r.reduced) ? r.reduced : {})) parts.push(judge + ' runs with reduced independence');
      for (const judge of Object.keys(isObj(r.accepted) ? r.accepted : {})) parts.push(judge + ' shares the builder’s family (accepted)');
      if (!parts.length) continue;
      out.push({ via: String(r.builder.via || ''), text: 'If the builder runs on ' + String(r.builder.model || '?') + ' (' + String(r.builder.family || '?') + '): ' + parts.join('; ') + '.', reduced: Object.keys(isObj(r.reduced) ? r.reduced : {}).length > 0 });
    }
    return out;
  }
  function estimateRows(roles) {
    const list = (Array.isArray(roles) ? roles : []).filter(isObj).slice();
    list.sort((a, b) => STAGE_ORDER.indexOf(a.stage) - STAGE_ORDER.indexOf(b.stage));
    return list.map((r) => {
      const model = r.independence === 'alternate' && r.instead_of ? String(r.model) + ' (instead of ' + r.instead_of + ')' : String(r.model || '?');
      const tok = isObj(r.tokens) ? SF.fmtNum(r.tokens.input_tokens) + ' in · ' + SF.fmtNum(r.tokens.output_tokens) + ' out' : '';
      return h('li', { class: 'pl-est-row', dataset: { agent: String(r.agent || '') } },
        h('span', { class: 'pl-est-agent' }, String(r.agent || '?'), r.basis ? h('span', { class: 'pl-est-rbasis', title: tok }, String(r.basis)) : null),
        h('span', { class: 'pl-est-model' }, h('span', { class: 'mono' }, model), localTag(!!r.local),
          r.independence && r.independence !== 'alternate' ? indChip(r.independence, r.agent, null, null) : null),
        h('span', { class: 'pl-est-num', title: tok + (r.tokens_per_second ? ' at ' + r.tokens_per_second + ' tokens/s' : '') + (r.basis ? '; ' + r.basis : '') }, secs(r.seconds)),
        h('span', { class: 'pl-est-num' }, r.priced === false ? h('span', { class: 'tag tag-outline pl-cost is-unpriced', title: 'no prices for ' + r.model + ': left out of the total' }, 'unpriced') : fmtUsd(r.cost_usd)),
        h('span', { class: 'pl-est-why' }, reasonBadge(r.reason, true)));
    });
  }
  /** The estimate panel: the total first (cost, time, what it is based on), the per-step rows and the notes behind
   *  a toggle, what the independence guard would do in the open. refresh() fetches again (debounced). */
  function estimatePanel(o) {
    const el = h('section', { class: 'pl-est', 'aria-label': 'Estimate', dataset: { est: o.kind() } });
    const live = h('div', { class: 'pl-est-live', 'aria-live': 'polite' });
    el.appendChild(live);
    let seq = 0;
    let timer = 0;
    let openRows = false;
    function paint(nodes) { SF.clear(live); SF.add(live, nodes); }
    async function load() {
      const repo = o.repo();
      if (!repo) { paint(h('p', { class: 'pl-est-empty' }, 'Pick a repository to see the estimate.')); return; }
      const routing = o.routing();
      const prof = o.profile();
      const q = 'repo=' + SF.enc(repo) + '&kind=' + o.kind() + (prof !== undefined ? '&profile=' + SF.enc(prof) : '') + (typeof routing === 'string' ? '&routing=' + SF.enc(routing) : '');
      const my = ++seq;
      // A view that re-renders often (the Start review, on each state refresh) shows the answer it had: an
      // estimate changes only as jobs finish, so one answer per query serves EST_TTL.
      const key = q + '|' + ST.cfgAt;   // a config read since (a dialog opening, the page) asks again
      const hit = EST_CACHE.get(key);
      if (hit && Date.now() - hit.at < EST_TTL) { el.classList.remove('is-loading'); paint(render(hit.r, routing)); return; }
      el.classList.add('is-loading');
      if (!live.firstChild) paint(h('p', { class: 'pl-est-empty' }, h('span', { class: 'spinner', 'aria-hidden': 'true' }), 'Estimating…'));
      try {
        const r = await SF.api('/api/estimate?' + q);
        if (EST_CACHE.size > 40) EST_CACHE.clear();
        EST_CACHE.set(key, { at: Date.now(), r });
        if (my !== seq) return;
        paint(render(r, routing));
      } catch (e) {
        if (my !== seq) return;
        if (SF.gone(e)) { el.hidden = true; return; }
        paint(SF.callout('warn', 'alert', 'No estimate:', e && e.message ? e.message : String(e)));
      } finally {
        if (my === seq) el.classList.remove('is-loading');
      }
    }
    function render(r, routing) {
      const t = isObj(r.total) ? r.total : {};
      const unpriced = t.priced === false;
      const head = h('div', { class: 'pl-est-head' },
        h('span', { class: 'pl-est-title' }, SF.icon('gauge', 'icon-sm'), r.kind === 'plan' ? 'Estimate for the plan' : 'Estimate'),
        h('span', { class: 'pl-est-total', dataset: { total: '' } },
          h('strong', { class: 'pl-est-cost' }, '≈ ' + fmtUsd(t.cost_usd) + (unpriced ? ' + unpriced runs' : '')),
          h('span', { class: 'pl-est-sep', 'aria-hidden': 'true' }, '·'),
          h('span', null, 'about ' + secs(t.seconds))),
        r.profile !== undefined ? h('span', { class: 'tag tag-outline', title: 'The profile this run would use' }, r.profile ? 'profile ' + r.profile : 'no profile') : null);
      const notes = Array.isArray(r.notes) ? r.notes.map(String) : [];
      const lines = [];
      lines.push(h('p', { class: 'pl-est-basis' }, 'Based ' + (BASIS[r.basis] || 'on ' + String(r.basis || '?')) + (r.kind === 'plan' && r.jobs ? '; a plan of ' + SF.plural(Number(r.jobs) || 0, 'job') + ', one after another' : '') + '.'));
      const stages = (Array.isArray(r.stages) ? r.stages : []).filter(isObj);
      if (stages.length) {
        const step = (s) => h('span', { class: 'pl-est-stage', dataset: { stage: String(s.stage || '') },
          title: (Array.isArray(s.agents) ? s.agents.join(', ') : '') + (s.stage === 'review' ? ': the lenses start together and share each server’s slots' : '') },
        String(s.stage || '?') + ' ' + secs(s.seconds));
        const lead = stages.filter((s) => s.stage === 'lead');
        const line = stages.filter((s) => s.stage !== 'lead');
        const parts = [];
        lead.forEach((s) => parts.push(step(s), ' → '));
        line.forEach((s, i) => parts.push(step(s), i < line.length - 1 ? ' → ' : ''));
        if (r.kind === 'plan' && Number(r.jobs) > 1) parts.push(' (× ' + r.jobs + ' jobs)');
        lines.push(h('p', { class: 'pl-est-stages', 'aria-label': 'Time per stage' }, SF.icon('clock', 'icon-xs'), h('span', null, parts)));
      }
      if (unpriced) {
        const m = notes.map((n) => /^no prices for (.+?): the cost leaves them out$/.exec(n)).find(Boolean);
        lines.push(h('p', { class: 'pl-est-warn', dataset: { unpriced: '' } }, SF.icon('alert', 'icon-xs'),
          h('span', null, 'The cost leaves out unpriced runs: ' + (m ? 'no prices for ' + m[1] : 'some models have no prices') + ' (unknown, not free).')));
      }
      if (isObj(routing)) lines.push(h('p', { class: 'pl-est-note' }, SF.icon('info', 'icon-xs'), h('span', null, 'Custom per-step routes are not in the estimate: it shows the saved routes with this profile.')));
      const ind = independenceSentences(r.independence);
      const indList = ind.length ? h('ul', { class: 'pl-est-ind', 'aria-label': 'What the independence guard would do' }, ind.map((x) => h('li', { class: x.reduced ? 'is-reduced' : null },
        x.via ? h('span', { class: 'tag tag-outline pl-via' }, x.via) : null, h('span', null, x.text)))) : null;
      const rows = h('details', { class: 'pl-est-more', open: openRows },
        h('summary', null, SF.icon('chevron', 'icon-xs chev'), 'Per step (' + (Array.isArray(r.roles) ? r.roles.length : 0) + ') and notes'),
        h('ol', { class: 'pl-est-rows' }, estimateRows(r.roles)),
        notes.length ? h('ul', { class: 'pl-est-notes' }, notes.map((n) => h('li', null, n))) : null);
      rows.addEventListener('toggle', () => { openRows = rows.open; });
      return [head, lines, indList, rows];
    }
    const refresh = () => { clearTimeout(timer); timer = setTimeout(load, 180); };
    /** ensure(): an estimate is on its way or shown (the first render may come before or after the config). */
    return { el, refresh, now: load, ensure: () => { if (!seq && !timer) refresh(); } };
  }

  // ================================================================ forms of the Software Factory
  SF.extend('form', (f) => {
    if (f.form === 'start') {
      const req = f.request || {};
      if (req.kind !== 'job' && req.kind !== 'plan') return null;
      const body = isObj(req.body) ? req.body : {};
      const est = estimatePanel({ kind: () => (req.kind === 'plan' ? 'plan' : 'feature'), repo: () => body.repo || '', routing: () => body.routing, profile: () => body.profile });
      est.now();
      return est.el;
    }
    const kind = f.form === 'plan' ? 'plan' : 'feature';
    const alts = f.form === 'routing' && f.job ? alternatesField(f) : null;
    let est = null;
    const picker = profilePicker({ form: f.form, job: f.job, routing: f.routing, onChange: () => { if (est) est.refresh(); if (alts) alts.setProfiled(picker.value() !== undefined); } });
    if (f.form === 'feature' || f.form === 'plan') est = estimatePanel({ kind: () => kind, repo: f.repo, routing: f.routing, profile: picker.value });
    loadCfg(true).then((cfg) => picker.fill(cfg)).catch(() => {}).finally(() => { if (est) est.refresh(); });
    f.onChange(() => { if (ST.cfg) picker.fill(ST.cfg); if (est) est.refresh(); if (alts) alts.setProfiled(picker.value() !== undefined); });
    f.contribute((body, action) => {
      const v = picker.value();
      if (action === 'backlog') {
        if (v !== undefined) { picker.select.focus(); throw new Error('Backlog items run with the default profile. Choose “Default”, or use Run now.'); }
        return;
      }
      if (f.form === 'routing' && f.job) {
        if (alts) alts.apply(body, v !== undefined);
        // Without a profile named, the steps Custom leaves alone keep their snapshot; a step it names on its own
        // snapshot model keeps that model's placement too (an explicit model drops agents.<name>'s otherwise).
        if (v === undefined) keepPlacements(f.job, body);
        // Only a profile: the saved settings with it (Factory.set_routing re-routes every step when one is named).
        if (v !== undefined && isObj(body.routing) && !Object.keys(body.routing).length) body.routing = null;
      }
      if (v !== undefined) body.profile = v;
    });
    return [picker.el, alts ? alts.el : null, est ? est.el : null];
  });
  /** The saved model key that still means a snapshot route's model (same provider, model id and base_url), its own
   *  key first; null when the live config no longer has it. */
  function liveKey(cfg, route) {
    const models = cfg && isObj(cfg.models) ? cfg.models : {};
    const provs = cfg && isObj(cfg.providers) ? cfg.providers : {};
    const base = isObj(route.provider_entry) && route.provider_entry.base_url ? String(route.provider_entry.base_url) : null;
    const same = (k) => {
      const m = models[k];
      if (!isObj(m) || String(m.model || '') !== String(route.model_id || '') || String(m.provider || '') !== String(route.provider || '')) return false;
      return !base || (isObj(provs[m.provider]) && String(provs[m.provider].base_url || '') === base);
    };
    if (route.model && same(route.model)) return String(route.model);
    return Object.keys(models).sort().find(same) || null;
  }
  /** Change routing without a new profile: a per-step route that names the step's snapshot model (a new driver, or
   *  only a new alternate) also carries what the snapshot placed there: its reason, its escalation rungs and a
   *  judge's alternate. Factory._resolve_route drops agents.<name>'s for an explicit model. */
  function keepPlacements(job, body) {
    if (!isObj(body.routing)) return;
    const cfg = ST.cfg;
    const models = cfg && isObj(cfg.models) ? cfg.models : {};
    for (const [agent, e] of Object.entries(body.routing)) {
      const r = isObj(job.routing) ? job.routing[agent] : null;
      if (!isObj(e) || !isObj(r) || !e.model || liveKey(cfg, r) !== e.model) continue;
      const why = reasonOf(r.reason);
      if (!('reason' in e) && why && REASONS[why.kind]) e.reason = { kind: why.kind, text: why.text };
      const rungs = (Array.isArray(r.escalate) ? r.escalate : []).filter(isObj).map((x) => String(x.model || ''));
      if (!('escalate' in e) && rungs.length && rungs.every((k) => isObj(models[k]))) e.escalate = rungs;
      if (isJudge(agent) && !('independent_alternate' in e)) {
        if (r.independent_alternate === false) e.independent_alternate = false;
        else if (isObj(r.independent_alternate) && isObj(models[r.independent_alternate.model])) e.independent_alternate = String(r.independent_alternate.model);
      }
    }
  }
  /** Change routing: each judge's independent alternate for this job (DESIGN §15: the same field as in Settings).
   *  Sent inside the per-step routes, so a preset or Saved settings (which re-route every step from a file or the
   *  saved settings) cannot carry it. apply(body, withProfile) adds it; "Keep" also keeps it when Custom moves the
   *  judge to another model (Factory._resolve_route would drop it). */
  function alternatesField(f) {
    const job = f.job;
    const routing = isObj(job.routing) ? job.routing : {};
    const judges = Object.keys(routing).filter((a) => isJudge(a) && isObj(routing[a]));
    if (!judges.length) return null;
    const rows = [];
    const sels = {};
    let det = null;
    const fail = (a, msg) => { det.open = true; sels[a].focus(); throw new Error(msg); };
    let profiled = false;   // a new profile places every judge again, with its alternate
    let lastCfg = null;
    const fill = (cfg) => {
      lastCfg = cfg;
      const fams = cfg && isObj(cfg.model_families) ? cfg.model_families : {};
      const keys = Object.keys(cfg && isObj(cfg.models) ? cfg.models : {}).sort();
      for (const a of judges) {
        const r = routing[a];
        const now = r.independent_alternate === false ? 'accepts the builder’s family' : isObj(r.independent_alternate) ? String(r.independent_alternate.model || '?') : 'none';
        const fam = String(r.family || fams[r.model] || '');
        const sel = sels[a];
        const keep = sel.value;
        SF.clear(sel);
        sel.appendChild(h('option', { value: ':keep', title: profiled ? 'The new profile and your saved settings place this judge, its alternate included' : 'The job’s snapshot: ' + now }, profiled ? 'As the profile places it' : 'Keep: ' + now));
        sel.appendChild(h('option', { value: 'false' }, 'None: accept the builder’s family'));
        const og = h('optgroup', { label: 'Models of another family than ' + String(r.model || '?') + (fam ? ' (' + fam + ')' : '') });
        for (const k of keys.filter((k) => !fam || fams[k] !== fam)) og.appendChild(h('option', { value: k }, k + (fams[k] ? ' · ' + fams[k] + ' family' : '')));
        if (og.firstChild) sel.appendChild(og);
        sel.value = keep && Array.from(sel.options).some((o) => o.value === keep) ? keep : ':keep';
      }
    };
    for (const a of judges) {
      const sel = h('select', { class: 'select select-sm', 'aria-label': a + ' independent alternate', dataset: { focus: 'alt:' + a } }, h('option', { value: ':keep' }, 'Keep'));
      sels[a] = sel;
      rows.push(h('li', { class: 'pl-alt-row' }, h('span', { class: 'pl-step-name' }, a), sel));
    }
    loadCfg().then(fill).catch(() => {});
    function apply(body, withProfile) {
      const cfg = ST.cfg;
      const models = cfg && isObj(cfg.models) ? cfg.models : {};
      const fams = cfg && isObj(cfg.model_families) ? cfg.model_families : {};
      const changed = judges.filter((a) => sels[a].value !== ':keep');
      if (changed.length && body.routing === null) fail(changed[0], 'Saved settings places every step as Settings › Routing says, alternates included: choose Custom to change an independent alternate for this job only.');
      if (changed.length && !isObj(body.routing)) fail(changed[0], 'A preset re-routes every step from its own file: choose Custom to change an independent alternate.');
      if (!isObj(body.routing)) return;
      for (const a of judges) {
        const r = routing[a];
        const e = isObj(body.routing[a]) ? body.routing[a] : null;
        if (sels[a].value === ':keep') {
          // Custom moved the judge to another model: "Keep" keeps its alternate, if it is still of another family.
          if (withProfile || !e || !e.model || liveKey(cfg, r) === e.model || 'independent_alternate' in e) continue;
          if (r.independent_alternate === false) { e.independent_alternate = false; continue; }
          if (!isObj(r.independent_alternate)) continue;
          const key = String(r.independent_alternate.model || '');
          const altFam = String(fams[key] || r.independent_alternate.family || '');
          const newFam = String(fams[e.model] || '');
          if (!isObj(models[key])) fail(a, a + '’s independent alternate ' + key + ' is no longer one of your saved models: pick another alternate for it, or None.');
          if (altFam && altFam === newFam) fail(a, a + '’s independent alternate ' + key + ' is of the ' + altFam + ' family, like its new model ' + e.model + ': pick another alternate for it, or None.');
          e.independent_alternate = key;
          continue;
        }
        let entry = e ? Object.assign({}, e) : null;
        if (!entry && withProfile) entry = {};   // a new profile places the judge; only its alternate is this job's
        if (!entry) {
          const key = liveKey(cfg, r);
          if (!key) fail(a, a + ' runs on ' + String(r.model_id || r.model) + ', which is no longer one of your saved models: pick a model for it under Custom first.');
          entry = { model: key, driver: r.driver || 'builtin' };
          if (isObj(r.extra_body) && Object.keys(r.extra_body).length) entry.extra_body = JSON.parse(JSON.stringify(r.extra_body));
        }
        entry.independent_alternate = sels[a].value === 'false' ? false : sels[a].value;
        body.routing[a] = entry;
      }
    }
    det = h('details', { class: 'pl-alts', dataset: { field: 'alternates' } },
      h('summary', null, SF.icon('chevron', 'icon-xs chev'), 'Independent alternates (' + judges.length + ' judges)'),
      h('p', { class: 'field-hint' }, 'When the builder runs on a judge’s model family, the judge runs on its alternate instead; with none, the run says “Independence reduced”. Keep also keeps it when you move the judge to another model under Custom.'),
      h('ul', { class: 'pl-alt-rows' }, rows));
    return { el: det, apply, setProfiled: (v) => { if (v !== profiled) { profiled = v; if (lastCfg) fill(lastCfg); } } };
  }

  // ================================================================ the job: card, drawer header, run headers
  SF.extend('jobCard', (job) => {
    const out = [jobIndChip(job)];
    const c = job.cost;
    if (isObj(c) && (Number(c.usd) > 0 || Number(c.unpriced_runs) > 0)) out.push(costTag(c));
    return out;
  });
  SF.extend('jobMeta', (job) => [
    'profile' in job ? h('span', { class: 'meta-item pl-meta', dataset: { meta: 'profile' }, title: 'The routing profile this job was submitted with' }, SF.icon('list', 'icon-xs'), job.profile ? 'profile ' + job.profile : 'no profile') : null,
    isObj(job.cost) ? h('span', { class: 'meta-item pl-meta', dataset: { meta: 'cost' }, title: costTitle(job.cost) }, SF.icon('gauge', 'icon-xs'), 'cost ' + costLabel(job.cost)) : null,
  ]);
  SF.extend('jobHead', (job) => {
    const out = [];
    const chip = jobIndChip(job);
    if (chip) {
      const lines = sharedLines(job);
      out.push(h('div', { class: 'pl-ind-block pl-ind-block-' + job.independence, dataset: { independence: job.independence } },
        chip, lines.length ? h('ul', { class: 'pl-ind-lines' }, lines.map((l) => h('li', null, l))) : null,
        job.independence === 'reduced' ? h('p', { class: 'pl-ind-note' }, 'The run went ahead by design: no placement kept this judge off the builder’s model family. Mixing in another family, or an independent alternate, avoids it.') : null));
    }
    if (Array.isArray(job.handoff_models)) {
      out.push(h('div', { class: 'meta-row pl-handoff', dataset: { handoff: '' } }, SF.icon('handoff', 'icon-xs'), h('span', null, 'Written in the chat on: '),
        job.handoff_models.length ? job.handoff_models.filter(isObj).map((m) => h('span', { class: 'tag tag-mono', title: String(m.provider || '') + '/' + String(m.model_id || '') }, String(m.model || '?') + (m.family ? ' · ' + m.family : ''))) : h('span', { class: 'muted' }, 'not recorded')));
    }
    return out;
  });
  SF.extend('runHead', (r) => {
    const d = r.start;
    const out = [];
    if (isObj(d)) {
      if (r.mode === 'chat') {
        if (d.handover) out.push(mark('', 'handoff', 'moved to ' + String(d.provider || '?') + '/' + String(d.model_id || '?') + ', fresh session', 'This turn ran on another model: a fresh session with the conversation so far handed over', 'handover'));
      } else {
        out.push(reasonBadge(d.reason));
        out.push(...runMarks(d));
        out.push(indChip(d.independence, r.agent, d.shared_model, r.round));
        out.push(runFacts(d, true));
      }
    }
    const refused = isObj(r.end) ? custodyRefusal(r.end.error) : null;
    if (refused) out.push(mark('pl-mark-warn', 'layers', refused, String(r.end.error), 'custody-refused'), h('span', { class: 'sr-only' }, refused + ': ' + String(r.end.error)));
    out.push(runCostTag(r.end));
    return out;
  });
  SF.extend('chatHead', (chat) => [
    'profile' in chat ? h('span', { class: 'meta-item pl-meta', dataset: { meta: 'profile' }, title: 'The routing profile this chat started with' + (chat.reason ? '; why its model: ' + (reasonOf(chat.reason) || {}).text : '') }, SF.icon('list', 'icon-xs'), chat.profile ? 'profile ' + chat.profile : 'no profile') : null,
    chat.reason ? reasonBadge(chat.reason) : null,
    isObj(chat.cost) ? h('span', { class: 'meta-item pl-meta', dataset: { meta: 'cost' }, title: costTitle(chat.cost) }, SF.icon('gauge', 'icon-xs'), 'cost ' + costLabel(chat.cost)) : null,
    Array.isArray(chat.earlier_sessions) && chat.earlier_sessions.length
      ? h('span', { class: 'meta-item pl-meta', dataset: { meta: 'sessions' }, title: 'Earlier sessions (a turn on another model starts a fresh one): ' + chat.earlier_sessions.filter(isObj).map((s) => String(s.model || '?') + ' on ' + String(s.provider || '?')).join(', ') },
        SF.icon('handoff', 'icon-xs'), SF.plural(chat.earlier_sessions.length, 'earlier session'))
      : null,
  ]);
  SF.extend('planHead', (plan) => [
    'profile' in plan ? h('span', { class: 'meta-item pl-meta', dataset: { meta: 'profile' }, title: 'The routing profile of the lead and every job of this plan' }, SF.icon('list', 'icon-xs'), plan.profile ? 'profile ' + plan.profile : 'no profile') : null,
    isObj(plan.cost) ? h('span', { class: 'meta-item pl-meta', dataset: { meta: 'cost' }, title: costTitle(plan.cost, 'The lead’s cost so far (its jobs count their own)') }, SF.icon('gauge', 'icon-xs'), 'lead cost ' + costLabel(plan.cost)) : null,
  ]);

  // ================================================================ the job drawer's Runs tab
  function stepOrder(job) {
    const revs = Array.isArray(job.reviewers) ? job.reviewers.map((r) => 'reviewer-' + r) : [];
    return ['spec-writer', 'builder'].concat(revs, ['approver']);
  }
  function modelCell(r) {
    return h('span', { class: 'pl-model' }, h('span', { class: 'mono pl-model-key' }, String(r.model || '?')),
      r.model_id && r.model_id !== r.model ? h('span', { class: 'mono pl-model-id' }, String(r.model_id)) : null,
      h('span', { class: 'pl-model-prov' }, String(r.provider || '?')), famTag(r.family));
  }
  function stepBox(job, agent, run, route) {
    const ran = !!run;
    const r = run || route || {};
    return h('li', { class: 'pl-step' + (ran ? '' : ' is-planned'), dataset: { agent, independence: run && run.independence ? run.independence : null } },
      h('div', { class: 'pl-step-head' }, h('span', { class: 'pl-step-name' }, agent), ran && run.round !== null && run.round !== undefined ? h('span', { class: 'tag tag-outline' }, 'r' + run.round) : null,
        h('span', { class: 'spacer' }), ran ? runCostTag(run) : h('span', { class: 'pl-step-todo' }, 'not run yet')),
      modelCell(r),
      h('div', { class: 'pl-chips' }, reasonBadge(r.reason, false), ran ? runMarks(run) : null, ran ? indChip(run.independence, agent, run.shared_model, run.round) : null),
      ran ? runFacts(run, true) : null,
      reasonOf(r.reason) && reasonOf(r.reason).text ? h('p', { class: 'pl-step-why' }, reasonOf(r.reason).text) : null);
  }
  function runRow(job, run, i) {
    const u = isObj(run.usage) ? run.usage : {};
    const marks = runMarks(run);
    const ind = indChip(run.independence, run.agent, run.shared_model, run.round);
    return h('li', { class: 'pl-run' + (run.ok === false ? ' is-failed' : ''), dataset: { run: String(run.run || i), agent: String(run.agent || ''), independence: run.independence || null } },
      h('span', { class: 'pl-run-who' }, h('span', { class: 'pl-run-n mono', title: run.run ? 'logs/' + run.run + '.jsonl' : '' }, String(run.run || i + 1)),
        h('span', { class: 'pl-run-agent' }, String(run.agent || '?')), run.round !== null && run.round !== undefined ? h('span', { class: 'tag tag-outline' }, 'r' + run.round) : null,
        run.ok === false ? h('span', { class: 'tag tag-err' }, 'failed') : null),
      modelCell(run),
      h('span', { class: 'pl-run-tok', title: (u.cached_tokens ? SF.fmtNum(u.cached_tokens) + ' of the input cached' : '') }, SF.fmtNum(u.input_tokens || 0) + ' in · ' + SF.fmtNum(u.output_tokens || 0) + ' out'),
      h('span', { class: 'pl-run-cost' }, runCostTag(run)),
      h('div', { class: 'pl-run-detail' }, marks.length || ind ? h('span', { class: 'pl-chips pl-run-place' }, marks, ind) : null, h('span', { class: 'pl-run-why' }, reasonBadge(run.reason, true)),
        runFacts(run, false)));
  }
  function perAgent(runs) {
    const by = new Map();
    for (const r of runs) {
      const a = by.get(r.agent) || { agent: r.agent, runs: 0, inp: 0, out: 0, usd: 0, unpriced: 0 };
      a.runs += 1;
      a.inp += Number((r.usage || {}).input_tokens) || 0;
      a.out += Number((r.usage || {}).output_tokens) || 0;
      if (r.priced === false || r.cost_usd === null || r.cost_usd === undefined) a.unpriced += 1; else a.usd += Number(r.cost_usd) || 0;
      by.set(r.agent, a);
    }
    return Array.from(by.values());
  }
  /** One snapshot route: where it can run (builds, overflow, rungs, the alternate) and why. */
  function routeRow(agent, route) {
    const parts = [];
    const name = (x) => (isObj(x) ? String(x.model || '?') + (x.family ? ' (' + x.family + ')' : '') : '?');
    if (Array.isArray(route.builds) && route.builds.length > 1) parts.push(['builds', route.builds.filter(isObj).map((b) => String(b.provider || '?') + '/' + String(b.model_id || b.model || '?')).join(' → ')]);
    if (isObj(route.overflow)) parts.push(['overflow', name(route.overflow)]);
    if (Array.isArray(route.escalate) && route.escalate.length) parts.push(['escalates to', route.escalate.map(name).join(' → ')]);
    if (isObj(route.independent_alternate)) parts.push(['independent alternate', name(route.independent_alternate)]);
    else if (route.independent_alternate === false) parts.push(['independent alternate', 'none: the routing accepts the builder’s family']);
    return h('li', { class: 'pl-route', dataset: { agent } },
      h('div', { class: 'pl-route-head' }, h('span', { class: 'pl-step-name' }, agent), modelCell(route), reasonBadge(route.reason, false)),
      parts.length ? h('dl', { class: 'pl-route-kv' }, parts.map(([k, v]) => [h('dt', null, k), h('dd', null, v)])) : null);
  }
  SF.registerJobTab({
    id: 'runs', label: 'Runs', icon: 'gauge',
    render(panel, job) {
      if (!job) { SF.rebuild(panel, SF.skeleton(5)); return; }
      const runs = (Array.isArray(job.runs) ? job.runs : []).filter(isObj);
      const routing = isObj(job.routing) ? job.routing : {};
      const order = stepOrder(job).filter((a) => routing[a] || runs.some((r) => r.agent === a));
      const last = (a) => { for (let i = runs.length - 1; i >= 0; i--) if (runs[i].agent === a) return runs[i]; return null; };
      const sub = [SF.plural(runs.length, 'finished run')];
      if (isObj(job.cost)) sub.push('cost ' + costLabel(job.cost));
      if ('profile' in job) sub.push(job.profile ? 'profile ' + job.profile : 'no profile');
      if (job.sendbacks) sub.push(SF.plural(Number(job.sendbacks), 'send-back'));
      const nodes = [
        h('div', { class: 'rs-head' }, h('div', null, h('h3', { class: 'rs-title' }, 'Runs and placement'), h('p', { class: 'rs-sub' }, sub.join(' · '))), h('span', { class: 'spacer' }),
          h('a', { class: 'btn btn-sm btn-ghost', href: '#/placement', dataset: { focus: 'placement-page' } }, SF.icon('route', 'icon-sm'), 'How placement works')),
      ];
      if (isObj(job.cost) && job.cost.priced === false) nodes.push(SF.callout('warn', 'alert', 'Partly priced:', 'the cost leaves out ' + SF.plural(Number(job.cost.unpriced_runs) || 0, 'run') + ' on models without prices (unknown, not free). Add price_in and price_out to those models in Settings › Models.'));
      nodes.push(h('h4', { class: 'pl-h' }, 'Steps'), h('p', { class: 'pl-h-sub' }, 'What each step ran on last, and why it went there.'),
        h('ol', { class: 'pl-steps' }, order.map((a) => stepBox(job, a, last(a), routing[a]))));
      if (runs.length) {
        nodes.push(h('h4', { class: 'pl-h' }, 'Every run'), h('ol', { class: 'pl-runs' }, runs.map((r, i) => runRow(job, r, i))));
        const agents = perAgent(runs);
        nodes.push(h('h4', { class: 'pl-h' }, 'Cost per agent'),
          h('div', { class: 'sf-scroll-x' }, h('table', { class: 'table pl-agents' },
            h('thead', null, h('tr', null, ['Agent', 'Runs', 'Tokens', 'Cost'].map((t) => h('th', null, t)))),
            h('tbody', null, agents.map((a) => h('tr', { dataset: { agent: a.agent } }, h('td', { class: 'mono' }, a.agent), h('td', null, String(a.runs)),
              h('td', null, SF.fmtNum(a.inp) + ' in · ' + SF.fmtNum(a.out) + ' out'),
              h('td', null, fmtUsd(a.usd) + (a.unpriced ? ' + ' + SF.plural(a.unpriced, 'unpriced run') : ''))))))));
      } else {
        nodes.push(SF.emptyState('gauge', 'No finished runs yet', 'Each finished run lists the model it ran on, why it went there, and what it cost.', null, true));
      }
      const routes = Object.keys(routing).filter((a) => isObj(routing[a]));
      if (routes.length) {
        const ordered = order.filter((a) => routes.indexOf(a) >= 0).concat(routes.filter((a) => order.indexOf(a) < 0));
        nodes.push(h('h4', { class: 'pl-h' }, 'Where each step can run'), h('p', { class: 'pl-h-sub' }, 'From the routing snapshot the job was submitted with: its builds, overflow, escalation rungs and independent alternates.'),
          h('ul', { class: 'pl-routes' }, ordered.map((a) => routeRow(a, routing[a]))));
      }
      SF.rebuild(panel, h('div', { class: 'pl-runs-tab' }, nodes));
    },
  });

  // ================================================================ Settings: profiles, placements, models, providers
  /** A "Placement" disclosure in a box or row: its button (with what is set) and its body, kept open across renders. */
  function disclosure(key, label, summary, fields) {
    const bodyId = 'pl-' + key.replace(/[^a-z0-9-]/gi, '_');
    const open = ST.open.has(key);
    const set = h('span', { class: 'rt-adv-set pl-adv-set' }, summary || '');
    const b = h('button', { type: 'button', class: 'rt-adv-btn pl-adv-btn' + (summary ? ' is-set' : ''), 'aria-expanded': open ? 'true' : 'false', 'aria-controls': bodyId, dataset: { action: 'placement', focus: 'adv:' + key } },
      SF.icon('chevron', 'icon-xs chev'), h('span', null, label), set);
    const body = h('div', { class: 'rt-adv-body pl-adv-body', id: bodyId, hidden: !open, dataset: { placement: key } }, fields);
    const show = (now) => {
      body.hidden = !now;
      b.setAttribute('aria-expanded', now ? 'true' : 'false');
      if (now) ST.open.add(key); else ST.open.delete(key);
    };
    b.addEventListener('click', () => show(body.hidden));
    return {
      el: [b, body], set: (text) => { set.textContent = text || ''; b.classList.toggle('is-set', !!text); },
      /** Open when a field inside is invalid: its error line stays in sight after a re-render. */
      check: () => { if (body.hidden && body.querySelector('[aria-invalid="true"]')) show(true); },
    };
  }
  /** A labelled field; a control from numField brings its error line (control._err), shown under it. */
  function lbl(text, control, hint) {
    const id = control.id || 'plf-' + Math.random().toString(36).slice(2, 9);
    control.id = id;
    return h('div', { class: 'pl-f' }, h('label', { class: 'pl-f-label', for: id }, text), control, control._err || null, hint ? h('div', { class: 'field-hint' }, hint) : null);
  }
  /** s.errSlot(key), linked to its input (aria-describedby), so the reason is read out and visible without hover. */
  function errLine(s, key, input) {
    const slot = s.errSlot(key);
    slot.id = 'pl-err-' + key.replace(/[^a-z0-9-]/gi, '_');
    const was = (input.getAttribute('aria-describedby') || '').split(/\s+/).filter((x) => x && x !== slot.id);
    input.setAttribute('aria-describedby', was.concat(slot.id).join(' '));
    return slot;
  }
  function famOf(s, key) { const f = s.orig && isObj(s.orig.model_families) ? s.orig.model_families[key] : null; return f ? String(f) : ''; }
  const modelKeysOf = (cfg) => Object.keys(cfg && isObj(cfg.models) ? cfg.models : {}).filter((k) => isObj(cfg.models[k])).sort();
  function modelOptions(s, keys) { return keys.map((k) => [k, k + (famOf(s, k) ? ' · ' + famOf(s, k) + ' family' : '')]); }
  /** The model an agent's route resolves to now (agent_list), for the alternate's family filter. */
  function agentModel(s, name) {
    const a = s.orig && Array.isArray(s.orig.agent_list) ? s.orig.agent_list.find((x) => x && x.name === name) : null;
    return a && a.model ? String(a.model) : '';
  }
  const isJudge = (name) => name === 'approver' || /^reviewer-/.test(name);
  /** The provider type ("anthropic", "openai") of a model key in a config, or null when it does not resolve. */
  function providerTypeOf(cfg, key) {
    const m = cfg && isObj(cfg.models) ? cfg.models[key] : null;
    const p = isObj(m) && isObj(cfg.providers) ? cfg.providers[m.provider] : null;
    return isObj(p) ? String(p.type || 'openai') : null;
  }
  /** The placement fields of one agent (agents.<name> or a profile placement): effort, escalation rungs, reason and,
   *  for a judge, the independent alternate. get() reads the entry; put(key, value) sets it (null removes). */
  function placementFields(s, o) {
    const e = o.get();
    const keys = modelKeysOf(s.draft);
    const tag = o.label;
    const out = [];
    // Claude's output_config.effort has no "minimal" (routing.effort_problem): offer what the model's provider takes,
    // and keep a saved effort it does not take visible, so the server's 400 is not a surprise.
    const own = e.model || o.model || '';
    const ptype = providerTypeOf(s.draft, own);
    const offered = EFFORTS.filter((x) => ptype !== 'anthropic' || x !== 'minimal');
    const effOpts = [['', 'Model default']].concat(offered.map((x) => [x, x]));
    if (e.effort && offered.indexOf(e.effort) < 0) effOpts.push([String(e.effort), String(e.effort) + ' (not taken by ' + own + ')']);
    const eff = SF.selectEl(effOpts, e.effort || '', { class: 'select select-sm', 'aria-label': tag + ' effort' });
    eff.addEventListener('change', () => o.put('effort', eff.value || null));
    out.push(lbl('Effort', eff, ptype === 'anthropic' ? own + ' is an Anthropic-format model: low to max.' : ptype ? own + ' is an OpenAI-format model: minimal to max (reasoning_effort).'
      : 'Anthropic models take low to max; minimal only on OpenAI-format ones.'));
    if (o.agent === 'builder' || (Array.isArray(e.escalate) && e.escalate.length)) out.push(escalateField(s, o, e, keys, tag));
    // The reason: a kind and one line. A line typed before its kind is kept (and the save waits) until a kind is
    // picked or the line is cleared, instead of being dropped without a word.
    const r = reasonOf(e.reason) || { kind: '', text: '' };
    const reasonKey = o.errKey + '.reason';
    const kind = SF.selectEl([['', 'No reason']].concat(Object.keys(REASONS).map((k) => [k, REASONS[k][0]])), r.kind, { class: 'select select-sm', 'aria-label': tag + ' reason' });
    const pending = r.kind ? '' : ST.pendingReason.get(reasonKey) || '';
    const text = h('input', { class: 'input input-sm', type: 'text', maxlength: 200, value: r.text || pending, placeholder: 'one line: why this step goes there', 'aria-label': tag + ' reason text' });
    const loose = () => {
      const t = text.value.trim();
      if (!kind.value && t) ST.pendingReason.set(reasonKey, text.value); else ST.pendingReason.delete(reasonKey);
      s.invalid(reasonKey, !kind.value && t ? 'Pick what kind of reason this is (capability, time, stage fit or cost) to keep this line, or clear it.' : null, text);
    };
    const putReason = () => { loose(); o.put('reason', kind.value ? { kind: kind.value, text: text.value.trim() } : null); };
    kind.addEventListener('change', putReason);
    text.addEventListener('input', () => { if (kind.value) putReason(); else { loose(); s.changed(false); } });
    loose();
    const reasonErr = errLine(s, reasonKey, text);   // after loose(): the line starts with its mark
    const kindHint = h('div', { class: 'field-hint' }, kind.value ? REASONS[kind.value][2] : 'Capability, time, stage fit or cost; shown as a badge on every run.');
    kind.addEventListener('change', () => { kindHint.textContent = kind.value ? REASONS[kind.value][2] : 'Capability, time, stage fit or cost; shown as a badge on every run.'; });
    const rlId = 'pl-rl-' + reasonKey.replace(/[^a-z0-9-]/gi, '_');
    out.push(h('div', { class: 'pl-f' }, h('span', { class: 'pl-f-label', id: rlId }, 'Reason'), h('div', { class: 'pl-f-pair', role: 'group', 'aria-labelledby': rlId }, kind, text), reasonErr, kindHint));
    if (isJudge(o.agent)) {
      const fam = famOf(s, own);
      const others = keys.filter((k) => !fam || famOf(s, k) !== fam);
      const cur = e.independent_alternate === false ? 'false' : e.independent_alternate || '';
      const altKey = o.errKey + '.independent_alternate';
      const altOpts = [['', 'None'], ['false', 'None: accept the builder’s family'], { group: 'Models of another family than ' + (own || 'its own') + (fam ? ' (' + fam + ')' : ''), options: modelOptions(s, others) }];
      // A saved alternate the list leaves out (its family is the judge's own, or it is gone) stays visible as it is,
      // and is flagged before the save (the server refuses it: settings._check_alternates).
      const stale = cur && cur !== 'false' && others.indexOf(cur) < 0;
      if (stale) altOpts.push({ group: 'Saved, but not of another family', options: [[cur, cur + (famOf(s, cur) ? ' · ' + famOf(s, cur) + ' family' : ' (no such model)')]] });
      const alt = SF.selectEl(altOpts, cur,
        { class: 'select select-sm', 'aria-label': tag + ' independent alternate' });
      const flag = () => {
        const v = alt.value;
        const why = v && v !== 'false' && keys.indexOf(v) < 0 ? v + ' is no longer one of your models: pick another alternate, or None.'
          : v && v !== 'false' && fam && famOf(s, v) === fam ? v + ' is of the ' + fam + ' family, like ' + (own || 'the judge’s own model') + ': pick a model of another family, or None.' : null;
        s.invalid(altKey, why, alt);
      };
      flag();
      alt.addEventListener('change', () => { flag(); o.put('independent_alternate', alt.value === '' ? null : alt.value === 'false' ? false : alt.value); });
      alt._err = errLine(s, altKey, alt);
      out.push(lbl('Independent alternate', alt, 'When the builder ran on this judge’s model family, the judge runs here instead. “Accept” records the shared family as the routing’s choice.'));
    }
    return out;
  }
  /** Forget the invalid marks and the pending reason line of a placement that is going away (a step or a profile
   *  removed), so a field nobody can see no longer holds up the save. */
  function forgetPlacement(s, errKey) {
    for (const k of ['.reason', '.independent_alternate', '.escalate']) s.invalid(errKey + k, null);
    ST.pendingReason.delete(errKey + '.reason');
  }
  /** The builder's escalation ladder: model keys, in order (the k-th send-back runs on rung k). */
  function escalateField(s, o, e, keys, tag) {
    const errKey = o.errKey + '.escalate';
    const esc = h('input', { class: 'input input-sm mono-input', type: 'text', value: Array.isArray(e.escalate) ? e.escalate.join(', ') : '', placeholder: 'e.g. ' + (keys[0] || 'claude'), spellcheck: 'false', autocomplete: 'off', 'aria-label': tag + ' escalation rungs' });
    esc.addEventListener('input', () => {
      const list = esc.value.split(/[,\s]+/).map((x) => x.trim()).filter(Boolean);
      const bad = list.filter((k) => keys.indexOf(k) < 0);
      s.invalid(errKey, bad.length ? 'Not a model key: ' + bad.join(', ') : null, esc);
      if (!bad.length) o.put('escalate', list.length ? list : null);
      else s.changed(false);
    });
    esc._err = errLine(s, errKey, esc);
    return lbl('Escalate on send-back', esc, 'Model keys, in order: the k-th send-back runs the builder on rung k, not the same model again.');
  }
  function placementSummary(e) {
    const bits = [];
    const r = reasonOf(e.reason);
    if (r && REASONS[r.kind]) bits.push(REASONS[r.kind][0].toLowerCase());
    if (e.effort) bits.push(String(e.effort));
    if (Array.isArray(e.escalate) && e.escalate.length) bits.push(SF.plural(e.escalate.length, 'rung'));
    if (e.independent_alternate === false) bits.push('accepts family');
    else if (e.independent_alternate) bits.push('alt ' + e.independent_alternate);
    return bits.join(' · ');
  }
  function routeSettings(s) {
    const name = s.name;
    const d = disclosure('route:' + name, 'Placement', placementSummary(s.entry()), []);
    const body = d.el[1];
    // Built again when the box's model changes: the effort choices follow its provider type, the alternates its
    // family, and an alternate of the new model's own family is flagged before the save.
    const paint = () => {
      SF.clear(body);
      SF.add(body, placementFields(s, {
        agent: name, label: name, errKey: 'agents.' + name, model: agentModel(s, name), get: () => s.entry(),
        put: (key, value) => { s.set(key, value); d.set(placementSummary(s.entry())); },
      }));
      d.set(placementSummary(s.entry()));
      d.check();
    };
    paint();
    if (s.onModel) s.onModel(() => { paint(); s.changed(false); });
    return d.el;
  }
  function numField(s, entry, key, label, errKey, opts) {
    const v = entry[key];
    const inp = h('input', { class: 'input input-sm', type: 'number', inputmode: opts.int ? 'numeric' : 'decimal', step: opts.int ? '1' : 'any', min: '0', max: opts.max !== undefined ? String(opts.max) : null, value: v === null || v === undefined ? '' : String(v), placeholder: opts.placeholder || '', 'aria-label': label, title: opts.title || null });
    inp.addEventListener('input', () => {
      const raw = inp.value.trim();
      if (raw === '') { delete entry[key]; s.invalid(errKey, null, inp); s.changed(false); return; }
      const n = Number(raw);
      const bad = !isFinite(n) || n < 0 || (opts.positive && n === 0) ? (opts.int ? 'A whole number above 0' : opts.positive ? 'A number above 0' : 'A number ≥ 0')
        : opts.int && !Number.isInteger(n) ? 'A whole number above 0' : opts.max !== undefined && n > opts.max ? 'At most ' + opts.max : null;
      if (bad) { s.invalid(errKey, bad, inp); s.changed(false); return; }
      entry[key] = n;
      s.invalid(errKey, null, inp);
      s.changed(false);
      if (opts.after) opts.after();
    });
    if (s.isInvalid(errKey)) { inp.classList.add('invalid'); inp.setAttribute('aria-invalid', 'true'); }
    inp._err = errLine(s, errKey, inp);
    return inp;
  }
  function modelSummary(m) {
    const bits = [];
    if (m.family) bits.push(String(m.family));
    if (m.price_in !== undefined && m.price_in !== null) bits.push('$' + m.price_in + '/$' + (m.price_out !== undefined ? m.price_out : '?'));
    if (Array.isArray(m.builds) && m.builds.length) bits.push(SF.plural(m.builds.length, 'build'));
    if (m.overflow) bits.push('overflow ' + m.overflow);
    return bits.join(' · ');
  }
  function modelSettings(s) {
    const key = s.name;
    const m = s.entry;
    const d = disclosure('model:' + key, 'Placement', modelSummary(m), []);
    const upd = () => d.set(modelSummary(m));
    const base = 'models.' + key;
    const fam = h('input', { class: 'input input-sm mono-input', type: 'text', value: typeof m.family === 'string' ? m.family : '', placeholder: famOf(s, key) || 'from the model id', spellcheck: 'false', autocomplete: 'off', 'aria-label': key + ' family' });
    fam.addEventListener('input', () => { const v = fam.value.trim().toLowerCase(); if (v) m.family = v; else delete m.family; s.changed(false); upd(); });
    const provs = Object.keys(isObj(s.draft.providers) ? s.draft.providers : {}).sort();
    const others = modelKeysOf(s.draft).filter((k) => k !== key);
    const over = SF.selectEl([['', 'None (the provider’s, if any)']].concat(modelOptions(s, others)), m.overflow || '', { class: 'select select-sm', 'aria-label': key + ' overflow' });
    over.addEventListener('change', () => { if (over.value) m.overflow = over.value; else delete m.overflow; s.changed(false); upd(); });
    const builds = h('ol', { class: 'pl-builds', 'aria-label': key + ' builds' });
    const list = () => (Array.isArray(m.builds) ? m.builds : []);
    function paintBuilds() {
      SF.clear(builds);
      list().forEach((b, i) => {
        const where = base + '.builds[' + i + ']';
        const p = SF.selectEl(provs.map((x) => [x, x]), b.provider || '', { class: 'select select-sm', 'aria-label': key + ' build ' + (i + 1) + ' provider' });
        p.addEventListener('change', () => { b.provider = p.value; s.changed(false); });
        const id = h('input', { class: 'input input-sm mono-input', type: 'text', value: b.model || '', placeholder: 'model id on that server', spellcheck: 'false', 'aria-label': key + ' build ' + (i + 1) + ' model id' });
        id.addEventListener('input', () => { b.model = id.value.trim(); s.changed(false); });
        const ctx = numField(s, b, 'context_tokens', key + ' build ' + (i + 1) + ' context tokens', where + '.context_tokens', { placeholder: 'context', positive: true, int: true, title: 'Context window on this server (tokens)' });
        const max = numField(s, b, 'max_tokens', key + ' build ' + (i + 1) + ' max tokens', where + '.max_tokens', { placeholder: 'max out', positive: true, int: true, title: 'Most output tokens per call on this server' });
        ctx.classList.add('pl-build-ctx');
        max.classList.add('pl-build-max');
        const rm = h('button', { type: 'button', class: 'btn btn-ghost btn-icon btn-xs pl-build-rm', 'aria-label': 'Remove build ' + (i + 1) + ' of ' + key, title: 'Remove this build' }, SF.icon('trash', 'icon-xs'));
        rm.addEventListener('click', () => { m.builds.splice(i, 1); if (!m.builds.length) delete m.builds; s.changed(false); paintBuilds(); upd(); });
        builds.appendChild(h('li', { class: 'pl-build' }, h('span', { class: 'pl-build-n' }, String(i + 1)), p, id, ctx, max, rm,
          h('div', { class: 'pl-build-errs' }, ctx._err, max._err)));
      });
    }
    paintBuilds();
    const addB = SF.btn('Add a build', { cls: 'btn-xs', icon: 'plus', onClick: () => {
      if (!Array.isArray(m.builds)) m.builds = [{ provider: m.provider || provs[0] || '', model: m.model || '' }];
      m.builds.push({ provider: provs[0] || '', model: '' });
      s.changed(false); paintBuilds(); upd();
    } });
    SF.add(d.el[1], [
      h('div', { class: 'pl-grid' },
        lbl('Family', fam, 'Leave empty to use the one its model id names' + (famOf(s, key) ? ' (' + famOf(s, key) + ')' : '') + '.'),
        lbl('Price in', numField(s, m, 'price_in', key + ' price in', base + '.price_in', { placeholder: 'USD / 1M', after: upd }), 'USD per million input tokens.'),
        lbl('Price out', numField(s, m, 'price_out', key + ' price out', base + '.price_out', { placeholder: 'USD / 1M', after: upd }), 'Set both, or neither (unknown, not free).'),
        lbl('Cached input', numField(s, m, 'price_cached_in', key + ' price cached in', base + '.price_cached_in', { placeholder: 'as price in' }), 'Defaults to the input price.'),
        lbl('Overflow', over, 'Runs here when no build answers or no slot frees in time.')),
      h('div', { class: 'pl-f' }, h('span', { class: 'pl-f-label' }, 'Builds: the same model on other servers, tried in order'), builds, h('div', { class: 'sf-row' }, addB,
        h('span', { class: 'field-hint' }, 'The entry’s own provider and model are always the first build.'))),
    ]);
    return d.el;
  }
  function providerSummary(p) {
    const bits = [];
    if (p.kind) bits.push(String(p.kind));
    if (p.allow_model_swap) bits.push('swaps');
    if (p.lazy_load) bits.push('lazy load');
    if (p.overflow) bits.push('overflow ' + p.overflow);
    if (p.tokens_per_second) bits.push(p.tokens_per_second + ' tok/s');
    if (isObj(p.custody)) bits.push('custody');
    return bits.join(' · ');
  }
  // custody.SETTINGS_KEYS as fields; the token is never typed here: a file, an environment name or a systemd credential
  // name (token_credential), one of the three (plainhttp.check_token_source refuses two)
  const CUSTODY_FIELDS = [['url', 'Register URL', 'http or https, host and port'], ['token_file', 'Token file', '/etc/ai-stack/custody.token'], ['token_env', 'Token name (environment)', 'CUSTODY_TOKEN'],
    ['token_credential', 'Systemd credential name', 'custody-token'],
    ['host_header', 'Host header', 'register.internal'], ['ca_file', 'CA file (https)', '/etc/ai-stack/register-ca.pem'], ['provider', 'Register’s name for this server', 'gpu0']];
  const TOKEN_KEYS = ['token_file', 'token_env', 'token_credential'];
  // plainhttp.CREDENTIAL_NAME_RE (check_credential_name also refuses '..')
  const CREDENTIAL_NAME_RE = /^[A-Za-z0-9_][A-Za-z0-9_.@+-]{0,127}$/;
  // plainhttp.in_credentials_dir: a token file inside a systemd unit's credentials folder is refused by the save
  const CREDENTIALS_DIR_RE = /^\/run\/(credentials|user\/\d+\/credentials)(\/|$)/;
  function providerSettings(s) {
    const name = s.name;
    const p = s.entry;
    const d = disclosure('provider:' + name, 'Placement', providerSummary(p), []);
    const upd = () => d.set(providerSummary(p));
    const base = 'providers.' + name;
    const kind = SF.selectEl([['', 'A plain server'], ['tabby', 'TabbyAPI'], ['openwebui', 'Open WebUI']], p.kind || '', { class: 'select select-sm', 'aria-label': name + ' kind' });
    kind.addEventListener('change', () => { if (kind.value) p.kind = kind.value; else delete p.kind; s.changed(false); upd(); });
    const url = h('input', { class: 'input input-sm mono-input', type: 'text', value: p.loaded_model_url || '', placeholder: 'the server’s model-info address', spellcheck: 'false', 'aria-label': name + ' loaded model URL' });
    url.addEventListener('input', () => { const v = url.value.trim(); if (v) p.loaded_model_url = v; else delete p.loaded_model_url; s.changed(false); });
    // Named by their visible label (the wrapping <label>); the id brings the focus back after a re-render.
    const flag = (key, text) => {
      const inp = h('input', { type: 'checkbox', id: 'pl-flag-' + name.replace(/[^a-z0-9-]/gi, '_') + '-' + key, dataset: { flag: key } });
      inp.checked = !!p[key];
      inp.addEventListener('change', () => { if (inp.checked) p[key] = true; else delete p[key]; s.changed(false); upd(); paintLine(); });
      return h('label', { class: 'check pl-check' }, inp, text);
    };
    const keys = modelKeysOf(s.draft);
    const over = SF.selectEl([['', 'None']].concat(modelOptions(s, keys)), p.overflow || '', { class: 'select select-sm', 'aria-label': name + ' overflow' });
    over.addEventListener('change', () => { if (over.value) p.overflow = over.value; else delete p.overflow; s.changed(false); upd(); });
    const custody = isObj(p.custody) ? p.custody : null;
    const putCustody = (key, v) => {
      const c = isObj(p.custody) ? p.custody : {};
      if (v === undefined || v === null || v === '') delete c[key]; else c[key] = v;
      if (Object.keys(c).length) p.custody = c; else delete p.custody;
      s.changed(false); upd(); paintLine();
    };
    const line = h('div', { class: 'pl-custody-status', 'aria-live': 'polite' });
    const paintLine = () => { SF.clear(line); SF.add(line, custodyLine(name, p)); };
    // custody.parse_settings refuses a register address that is not an http(s) URL, and plainhttp.check_token_source a
    // credential beside a token file or name, a token file in a credentials folder or a credential name that
    // check_credential_name refuses; say so before the save. vals: what the three token fields hold now.
    const custodyBad = (key, v, vals) => key === 'url' && v && !/^https?:\/\/[^/\s]+/i.test(v) ? 'An address that starts with http or https, then :// and the host'
      : key === 'token_file' && CREDENTIALS_DIR_RE.test(v) ? 'The harness never reads a systemd credentials folder: give it its own copy (a LoadCredential= line) and name it under Systemd credential name'
        : key === 'token_credential' && v && (!CREDENTIAL_NAME_RE.test(v) || v.indexOf('..') >= 0) ? 'A systemd credential name such as custody-token: letters, digits and _ . @ + -, not starting with a dot or a dash, no slash and no “..”'
          : TOKEN_KEYS.indexOf(key) >= 0 && v && (key === 'token_credential' ? vals.token_file || vals.token_env : vals.token_credential) ? 'Name one place for the token: a systemd credential, or a file or an environment name, not both'
            : null;
    const inputs = {};
    // One field's value: kept in the settings when good; a refused one stays typed (ST.pendingCustody, across a
    // re-render) and holds the save until it is fixed or cleared.
    const check = (key) => {
      const inp = inputs[key];
      const v = inp.value.trim();
      const vals = {};
      for (const k of TOKEN_KEYS) vals[k] = inputs[k] ? inputs[k].value.trim() : '';
      const errKey = base + '.custody.' + key;
      const bad = custodyBad(key, v, vals);
      s.invalid(errKey, bad, inp);
      if (bad) { ST.pendingCustody.set(errKey, inp.value); s.changed(false); } else { ST.pendingCustody.delete(errKey); putCustody(key, v); }
    };
    const cust = CUSTODY_FIELDS.map(([key, label, ph]) => {
      const errKey = base + '.custody.' + key;
      const saved = custody && custody[key] ? String(custody[key]) : '';
      const typed = s.isInvalid(errKey) && ST.pendingCustody.has(errKey) ? ST.pendingCustody.get(errKey) : saved;
      const inp = h('input', { class: 'input input-sm mono-input', type: 'text', value: typed, placeholder: ph, spellcheck: 'false', autocomplete: 'off', 'aria-label': name + ' custody ' + key });
      inputs[key] = inp;
      if (s.isInvalid(errKey)) { inp.classList.add('invalid'); inp.setAttribute('aria-invalid', 'true'); }
      // A token field re-checks all three: clearing the file clears the conflict shown on the credential, and back
      inp.addEventListener('input', () => { if (TOKEN_KEYS.indexOf(key) >= 0) TOKEN_KEYS.forEach(check); else check(key); });
      if (key === 'url' || TOKEN_KEYS.indexOf(key) >= 0) inp._err = errLine(s, errKey, inp);
      return lbl(label, inp, key === 'token_credential' ? 'Needs a LoadCredential=<name>:<path> line in the harness’s unit.' : null);
    });
    // custody.timeout (seconds, at most 60) and custody.models (model id → the register's name for it)
    const cBox = custody || {};
    const tmo = numField(s, cBox, 'timeout', name + ' custody timeout', base + '.custody.timeout', { placeholder: '5', positive: true, max: 60, after: () => putCustody('timeout', cBox.timeout) });
    tmo.addEventListener('input', () => { if (tmo.value.trim() === '') putCustody('timeout', null); });
    cust.push(lbl('Timeout (s)', tmo, 'How long a register answer may take (default 5).'));
    const namesKey = base + '.custody.models';
    const names = h('textarea', { class: 'input input-sm mono-input pl-names', rows: 2, spellcheck: 'false', placeholder: 'qwen3.8-flash-next = flash-next-exl3', 'aria-label': name + ' custody models' },
      custody && isObj(custody.models) ? Object.keys(custody.models).map((k) => k + ' = ' + custody.models[k]).join('\n') : '');
    names.addEventListener('input', () => {
      const out = {};
      const bad = [];
      for (const line of names.value.split('\n')) {
        if (!line.trim()) continue;
        const m = /^\s*([^=\s][^=]*?)\s*=\s*(\S.*?)\s*$/.exec(line);
        if (m) out[m[1]] = m[2]; else bad.push(line.trim());
      }
      s.invalid(namesKey, bad.length ? 'Write one “model id = the register’s name” per line: ' + bad[0] : null, names);
      if (!bad.length) putCustody('models', Object.keys(out).length ? out : null); else s.changed(false);
    });
    names._err = errLine(s, namesKey, names);
    const namesField = h('div', { class: 'pl-f-wide' }, lbl('The register’s names for models', names, 'One “model id = name” per line, where the register calls a model otherwise; others go by their model id.'));
    paintLine();
    const follows = custodyFollows(name);
    SF.add(d.el[1], [
      h('div', { class: 'pl-grid' },
        lbl('Kind', kind, 'TabbyAPI says which model it has loaded.'),
        lbl('Loaded-model URL', url, 'Where another server reports its loaded model.'),
        lbl('Overflow', over, 'This server’s overflow model, for models without their own.'),
        lbl('Overflow after (s)', numField(s, p, 'overflow_after_seconds', name + ' overflow after seconds', base + '.overflow_after_seconds', { placeholder: 'seconds', positive: true }), 'How long a run waits for a free slot.'),
        lbl('Tokens per second', numField(s, p, 'tokens_per_second', name + ' tokens per second', base + '.tokens_per_second', { placeholder: 'local 60, API 80', positive: true, after: upd }), 'For the estimate.')),
      h('div', { class: 'pl-checks' }, flag('allow_model_swap', 'May load another model on request (allow_model_swap)'), flag('swap_back', 'Load the original model back after a job (swap_back)'),
        flag('lazy_load', 'Loads its model on the first request (lazy_load)')),
      h('fieldset', { class: 'pl-custody-set' }, h('legend', null, 'GPU custody register ', h('span', { class: 'tag tag-warn', title: 'The register’s contract waits for the operator’s confirmation' }, 'Contract proposed')),
        h('p', { class: 'field-hint' }, 'The register is the operator’s: the harness never changes what it holds. It only asks for a lease before a run that may load a model here (lazy_load or allow_model_swap); the register decides and keeps the ledger. Another provider entry on the same server follows it. With custody, a run that gets no lease goes to the next build or the overflow.'),
        h('ul', { class: 'field-hint pl-custody-rules' },
          h('li', null, 'The card is held by someone else (HELD): the run goes at once to another build that is free. Only when none is does it wait, up to this provider’s “Overflow after” seconds, then move on. A run waits once in all, however many builds answer HELD.'),
          h('li', null, 'The card cannot fit the model now (FULL): the run moves on at once, so list another card or a smaller build.'),
          h('li', null, 'The token is never typed here: name a file, an environment name or a systemd credential (one of the three). A credential needs a LoadCredential=<name>:<path> line in the harness’s unit, added by whoever holds root on the server; under bwrap the agents cannot read it.')),
        line, follows && !custody ? null : [h('div', { class: 'pl-grid' }, cust), namesField]),
    ]);
    return d.el;
  }
  /** The provider entry whose custody this one follows (GET /api/custody custody_from), once it was read. */
  function custodyFollows(name) {
    const rows = ST.custody && Array.isArray(ST.custody.providers) ? ST.custody.providers : [];
    const row = rows.find((r) => r && r.provider === name);
    return row && row.custody_from ? String(row.custody_from) : null;
  }
  /** What GET /api/custody says about a provider, once it was read (follows another entry), with the draft's own
   *  custody, lazy_load and allow_model_swap: whether its runs take a lease (custody.needs_lease) as the boxes stand. */
  function custodyLine(name, draft) {
    const rows = ST.custody && Array.isArray(ST.custody.providers) ? ST.custody.providers : null;
    const row = rows ? rows.find((r) => r && r.provider === name) : null;
    const hasDraft = isObj(draft);
    const own = hasDraft ? isObj(draft.custody) : !!(row && !row.custody_from);
    const from = row && row.custody_from && !own ? String(row.custody_from) : null;
    if (!row && !own) return null;
    const under = own || !!from;
    const loads = hasDraft ? !!(draft.lazy_load || draft.allow_model_swap) : !!(row && row.needs_lease);
    const needs = under && loads;
    return h('p', { class: 'pl-custody-line', dataset: { needsLease: needs ? 'true' : 'false' } }, SF.icon('layers', 'icon-xs'),
      from ? 'Follows ' + from + ' (the same server): its custody is edited there. ' : '',
      !under ? 'No register of its own after this change; save to see what applies. '
        : needs ? 'Its runs take a request lease' + (hasDraft && row && !row.needs_lease ? ' (after the save)' : '') + '. '
          : 'No call here takes a lease (neither lazy_load nor allow_model_swap)' + (hasDraft && row && row.needs_lease ? ' after the save' : '') + '. ',
      row ? h('a', { href: '#/placement', dataset: { focus: 'custody-link:' + name } }, 'Register status ›') : null);
  }
  function profilesCard(s) {
    const draft = s.draft;
    const profiles = isObj(draft.profiles) ? draft.profiles : {};
    const names = Object.keys(profiles).filter((k) => isObj(profiles[k])).sort();
    const err = s.saveError();
    const defSel = SF.selectEl([['', 'None: every step on its route as saved']].concat(names.map((n) => [n, n])), typeof draft.default_profile === 'string' ? draft.default_profile : '', { class: 'select', id: 'pl-default-profile', 'aria-label': 'Default profile' });
    defSel.addEventListener('change', () => { draft.default_profile = defSel.value || null; s.changed(false); });
    const cards = names.map((n) => profileEditor(s, n, profiles[n]));
    const addName = h('input', { class: 'input input-sm mono-input', type: 'text', placeholder: 'new-profile', spellcheck: 'false', 'aria-label': 'New profile name' });
    const addErr = h('span', { class: 'field-error', hidden: true });
    const add = SF.btn('Add profile', { cls: 'btn-sm', icon: 'plus', onClick: () => {
      const v = addName.value.trim();
      const msg = !v ? 'Enter a name.' : !PROFILE_RE.test(v) ? 'Use a-z, 0-9 and -, up to 41 characters.' : profiles[v] ? 'A profile with that name exists.' : null;
      if (msg) { addErr.textContent = msg; addErr.hidden = false; addName.focus(); return; }
      if (!isObj(draft.profiles)) draft.profiles = {};
      draft.profiles[v] = { description: '', agents: {} };
      ST.open.add('profile:' + v);
      s.changed(true);
    } });
    addName.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); add.click(); } });
    const body = [
      h('p', { class: 'pl-card-intro' }, 'A profile says, per step, which model runs it and why: ', REASONS.capability[0].toLowerCase(), ', ', REASONS.time[0].toLowerCase(), ', ', REASONS['stage-fit'][0].toLowerCase(), ' or ', REASONS.cost[0].toLowerCase(),
        '. New feature, New plan, New chat and Hand off pick one; the default applies when they name none. A step a profile does not name keeps its route from Routing.'),
      h('div', { class: 'field pl-default' }, h('label', { class: 'field-label', for: 'pl-default-profile' }, 'Default profile'), defSel,
        err && err.section === 'default_profile' ? h('div', { class: 'form-error', role: 'alert' }, SF.icon('alert', 'icon-sm'), h('span', null, err.message)) : null),
      names.length ? h('div', { class: 'pl-profiles' }, cards) : SF.emptyState('list', 'No profiles yet', 'The Local first and NanoGPT value presets bring theirs; or add one below.', null, true),
      h('div', { class: 'add-row' }, addName, add, addErr),
    ];
    return s.card('profiles', 'Profiles', 'Which model runs each step, per job, plan and chat.', body,
      h('a', { class: 'btn btn-sm btn-ghost', href: '#/placement', dataset: { focus: 'placement-overview' } }, SF.icon('route', 'icon-sm'), 'Placement overview'));
  }
  function profileEditor(s, name, prof) {
    const agentsOf = () => (isObj(prof.agents) ? prof.agents : (prof.agents = {}));
    const desc = h('input', { class: 'input input-sm', type: 'text', value: typeof prof.description === 'string' ? prof.description : '', placeholder: 'one sentence: what this profile is for', 'aria-label': name + ' description' });
    desc.addEventListener('input', () => { prof.description = desc.value; s.changed(false); });
    const placed = Object.keys(isObj(prof.agents) ? prof.agents : {}).sort((a, b) => (LINE.indexOf(a) < 0 ? 9 : LINE.indexOf(a)) - (LINE.indexOf(b) < 0 ? 9 : LINE.indexOf(b)) || a.localeCompare(b));
    const errKeyOf = (agent) => 'profiles.' + name + '.agents.' + agent;
    const rm = h('button', { type: 'button', class: 'btn btn-ghost btn-icon btn-sm', 'aria-label': 'Remove profile ' + name, title: 'Remove profile ' + name }, SF.icon('trash', 'icon-sm'));
    rm.addEventListener('click', () => {
      for (const a of placed) forgetPlacement(s, errKeyOf(a));
      delete s.draft.profiles[name]; if (s.draft.default_profile === name) s.draft.default_profile = null; s.changed(true);
    });
    const keys = modelKeysOf(s.draft);
    const sum = h('span', { class: 'pl-profile-sum' });
    const paintSum = () => { sum.textContent = SF.plural(placed.length, 'step') + (placesText(s.draft, name) ? ': ' + placesText(s.draft, name) : ''); };
    paintSum();
    const rows = placed.map((agent) => {
      const e = isObj(agentsOf()[agent]) ? agentsOf()[agent] : (agentsOf()[agent] = {});
      const label = name + ' ' + agent;
      const msel = SF.selectEl([['', 'Keep the step’s model']].concat(modelOptions(s, keys)), e.model || '', { class: 'select select-sm', 'aria-label': label + ' model' });
      const put = (key, value) => { if (value === null || value === undefined || value === '') delete e[key]; else e[key] = value; s.changed(false); };
      const fields = h('div', { class: 'pl-place-fields' });
      // Only this row is built again when its model changes (no page re-render, so moving through the list with the
      // arrow keys keeps the focus): the effort and alternate choices follow the model's provider type and family.
      const paint = () => {
        SF.clear(fields);
        SF.add(fields, placementFields(s, { agent, label, errKey: errKeyOf(agent), model: e.model || agentModel(s, agent), get: () => e, put }));
      };
      paint();
      msel.addEventListener('change', () => { if (msel.value) e.model = msel.value; else delete e.model; paint(); paintSum(); s.changed(false); });
      const del = h('button', { type: 'button', class: 'btn btn-ghost btn-icon btn-xs', 'aria-label': 'Remove ' + agent + ' from ' + name, title: 'Remove this step from the profile' }, SF.icon('x', 'icon-xs'));
      del.addEventListener('click', () => { forgetPlacement(s, errKeyOf(agent)); delete agentsOf()[agent]; s.changed(true); });
      return h('li', { class: 'pl-place', dataset: { agent } },
        h('div', { class: 'pl-place-head' }, h('span', { class: 'pl-step-name' }, agent), h('span', { class: 'spacer' }), del),
        h('div', { class: 'pl-grid' }, lbl('Model', msel), fields));
    });
    const known = new Set(placed);
    const candidates = (s.orig && Array.isArray(s.orig.agent_list) ? s.orig.agent_list.map((a) => a && a.name).filter(Boolean) : []).filter((a) => !known.has(a)).sort();
    // A select and a button (as Add profile): a closed select's arrow keys fire change, so picking alone adds nothing.
    const pick = SF.selectEl([['', 'Choose a step…']].concat(candidates.map((a) => [a, a])), '', { class: 'select select-sm', 'aria-label': 'Step to add to ' + name });
    const addStep = SF.btn('Add step', { cls: 'btn-xs', icon: 'plus', dataset: { action: 'add-step' }, title: 'Place the chosen step in ' + name, aria: 'Add step to ' + name });
    const addErr = h('span', { class: 'field-error', hidden: true });
    addStep.addEventListener('click', () => {
      if (!pick.value) { addErr.textContent = 'Choose a step first.'; addErr.hidden = false; pick.focus(); return; }
      agentsOf()[pick.value] = {};
      ST.open.add('profile:' + name);
      s.changed(true);
    });
    pick.addEventListener('change', () => { addErr.hidden = true; });
    const key = 'profile:' + name;
    const open = ST.open.has(key) || !placed.length;
    const det = h('details', { class: 'pl-profile', open, dataset: { profile: name } },
      h('summary', null, SF.icon('chevron', 'icon-xs chev'), h('span', { class: 'pl-profile-name' }, name),
        s.draft.default_profile === name ? h('span', { class: 'tag tag-accent' }, 'default') : null, sum),
      h('div', { class: 'pl-profile-body' }, h('div', { class: 'sf-row pl-profile-top' }, lbl('Description', desc), rm),
        rows.length ? h('ol', { class: 'pl-places' }, rows) : h('p', { class: 'sf-muted' }, 'No step placed yet: add one.'),
        candidates.length ? h('div', { class: 'add-row pl-add-step' }, pick, addStep, addErr) : null));
    det.addEventListener('toggle', () => { if (det.open) ST.open.add(key); else ST.open.delete(key); });
    if (!det.open && det.querySelector('[aria-invalid="true"]')) { det.open = true; ST.open.add(key); }
    return det;
  }
  SF.extend('settings', (s) => {
    if (s.section === 'page') {
      ST.settings = s;
      // Read the register view once, and again after the config changed (a save in Settings gives a new s.orig):
      // custody's URL, timeout, names or the entry itself may have changed.
      if (!ST.custodyLoading && ((!ST.custody && !ST.custodyError) || (ST.custodyFor && ST.custodyFor !== s.orig))) loadCustody(false);
      return profilesCard(s);
    }
    if (s.section === 'route') return s.name === '*' ? null : routeSettings(s);
    if (s.section === 'model') return modelSettings(s);
    if (s.section === 'provider') return providerSettings(s);
    return null;
  });

  // ================================================================ the Doctor's placement checks
  const DOCTOR = [
    [/^provider \S+ models$/, 'Compares the model ids in your settings with the list this provider serves.', '#/settings/models', 'Models'],
    [/^provider \S+ loaded model$/, 'Which model the server has loaded now. With allow_model_swap off, runs on another model go to their next build or the overflow.', '#/settings/providers', 'Providers'],
    [/^local servers$/, 'Which local model servers answer. A placement falls to the next build or the overflow while one is down.', '#/settings/providers', 'Providers'],
    [/^placement$/, 'Every build, overflow, escalation rung, independent alternate and profile must name a configured model.', '#/settings/profiles', 'Profiles'],
    [/^custody \S+$/, 'The operator’s GPU custody register: the harness asks it for a lease before a run that may load a model.', '#/placement', 'GPU custody'],
    [/^drift sources?( \S+)?$/, 'A read-only drift check outside the harness; its findings become System items in the Housekeeping Tracker.', '#/housekeeping', 'Tracker'],
  ];
  SF.extend('doctorCheck', (c) => {
    const n = String((c && c.name) || '');
    // A systemd credential (token_credential) the doctor names with the line that gives it: that line, to copy
    const line = /LoadCredential=[A-Za-z0-9_@+.-]+:<path to the token file>/.exec(String((c && c.detail) || ''));
    const fix = line ? h('p', { class: 'pl-doc-fix', dataset: { fix: 'load-credential' } }, 'Add to the harness’s unit (whoever holds root on the server): ', h('code', null, line[0]),
      SF.btn('', { cls: 'btn-ghost btn-icon btn-xs', icon: 'copy', aria: 'Copy the line', title: 'Copy the line', onClick: () => SF.copyText(line[0], 'Copied') })) : null;
    const hit = DOCTOR.find((x) => x[0].test(n));
    if (!hit) return fix;
    return [h('p', { class: 'pl-doc-hint', dataset: { hint: n } }, hit[1], ' ', h('a', { href: hit[2] }, hit[3] + ' ›')), fix];
  });

  // ================================================================ GPU custody (GET /api/custody)
  function loadCustody(refresh) {
    // Read now while a plain read is on its way: the residency read follows it, rather than being dropped.
    if (ST.custodyLoading) return refresh && !ST.custodyRefreshing ? ST.custodyLoading.then(() => loadCustody(true)) : ST.custodyLoading;
    ST.custodyRefreshing = !!refresh;
    ST.custodyFor = ST.settings ? ST.settings.orig : null;   // the saved config this answer goes with
    ST.custodyLoading = SF.api('/api/custody' + (refresh ? '?refresh=1' : ''))
      .then((r) => { ST.custody = isObj(r) ? r : { providers: [] }; ST.custodyAt = Date.now(); ST.custodyError = null; })
      .catch((e) => { ST.custodyError = e; })
      .finally(() => {
        ST.custodyLoading = null;
        if (VIEW) VIEW.refresh();
        // Settings › Providers shows each entry's custody line (follows another entry, needs a lease) once it is read
        const r = SF.route();
        if (ST.settings && r && r.view === 'settings' && !ST.settings.saveError()) ST.settings.changed(true);
      });
    return ST.custodyLoading;
  }
  const STATE_TONE = { held: 'tag-ok', lost: 'tag-err', unknown: 'tag-warn', released: 'tag-outline', unreleased: 'tag-warn' };
  function custodyCard(row) {
    const reg = isObj(row.register) ? row.register : {};
    const st = isObj(row.settings) ? row.settings : {};
    const res = isObj(reg.residency) ? reg.residency : null;
    const leases = (Array.isArray(reg.leases) ? reg.leases : []).filter(isObj);
    const hist = (Array.isArray(reg.history) ? reg.history : []).filter(isObj).slice(-8).reverse();
    const fenced = isObj(reg.fenced) ? reg.fenced : null;
    return h('article', { class: 'sf-card pl-cust', dataset: { provider: String(row.provider || '') } },
      h('div', { class: 'pl-cust-head' }, SF.icon('server', 'icon-sm'), h('h3', { class: 'pl-cust-name' }, String(row.provider || '?')),
        row.needs_lease ? h('span', { class: 'tag tag-accent' }, 'needs a lease') : h('span', { class: 'tag tag-outline', title: 'Neither lazy_load nor allow_model_swap: no call here takes a lease' }, 'no lease needed'),
        row.custody_from ? h('span', { class: 'tag tag-outline', dataset: { follows: String(row.custody_from) } }, 'follows ' + row.custody_from + ' (the same server)') : null),
      fenced ? SF.callout('warn', 'alert', 'The register’s generation went back', 'from ' + fenced.seen + ' to ' + fenced.answered + ' (since ' + (isFinite(Date.parse(fenced.since)) ? new Date(Date.parse(fenced.since)).toLocaleString() : String(fenced.since)) + '): its answers are refused until it passes ' + fenced.seen +
        ' or the harness restarts. The register should keep its generation, or send an epoch.') : null,
      h('dl', { class: 'sf-kv' },
        h('dt', null, 'Register'), h('dd', { class: 'mono' }, String(reg.url || st.url || '?') + (st.host_header ? ' (Host ' + st.host_header + ')' : '')),
        st.provider ? [h('dt', null, 'Register’s name for this server'), h('dd', { class: 'mono' }, String(st.provider))] : null,
        isObj(st.models) && Object.keys(st.models).length ? [h('dt', null, 'Model names'), h('dd', { class: 'mono' }, Object.keys(st.models).map((k) => k + ' = ' + st.models[k]).join(', '))] : null,
        st.timeout ? [h('dt', null, 'Answer timeout'), h('dd', null, String(st.timeout) + ' s')] : null,
        h('dt', null, 'Token'), h('dd', null, st.token_credential ? ['the systemd credential ', h('code', null, String(st.token_credential)), h('span', { class: 'sf-muted' }, ' (a LoadCredential= line in the harness’s unit)')]
          : st.token_file ? 'from the file ' + st.token_file : st.token_env ? 'from ' + st.token_env : 'none'),
        row.needs_lease ? [h('dt', null, 'When the card is held'), h('dd', { dataset: { part: 'held-wait' } }, row.held_wait_seconds ? 'a HELD answer waits up to ' + Math.round(Number(row.held_wait_seconds)) + 's, once per run, when no other card is free; FULL never waits'
          : 'a HELD answer does not wait (no Overflow after); FULL never waits')] : null,
        h('dt', null, 'Generation'), h('dd', null, String(reg.generation !== undefined && reg.generation !== null ? reg.generation : '?') + (reg.epoch ? ' · epoch ' + reg.epoch : '')),
        h('dt', null, 'Resident'), h('dd', null, res ? [residentText(res.resident), ' ', h('span', { class: 'sf-muted' }, '(read ' + SF.relTime(res.read_at || res.observed_at) + ')')]
          : h('span', { class: 'sf-muted', dataset: { residency: 'none' } }, 'The register has not been read (or cannot be: the Doctor’s custody ' + row.provider + ' check says why).'))),
      leases.length ? h('div', { class: 'pl-f' }, h('span', { class: 'pl-f-label' }, 'Leases'), h('ul', { class: 'pl-leases' }, leases.map((l) => h('li', { dataset: { lease: String(l.lease_id || '') } },
        h('span', { class: 'tag ' + (STATE_TONE[l.state] || 'tag-outline'), dataset: { state: String(l.state || '') } }, SF.words.lease[l.state] || String(l.state || '?')), h('span', { class: 'mono' }, String(l.lease_id || '?')), ' ', String(l.model || '?'),
        l.until ? h('span', { class: 'sf-muted' }, ' until ' + new Date(Date.parse(l.until)).toLocaleString()) : null,
        h('div', { class: 'pl-lease-purpose' }, String(l.purpose || '')),
        Array.isArray(l.resident_before) ? h('div', { class: 'pl-lease-purpose' }, 'Resident before: ' + residentText(l.resident_before) +
          (Array.isArray(l.resident_after) ? '; after: ' + residentText(l.resident_after) : '')) : null,
        l.error ? h('div', { class: 'pl-lease-err' }, /^the register restarted/.test(String(l.error)) ? 'The register restarted: ' + String(l.error).replace(/^the register restarted:?\s*/, '') : String(l.error)) : null)))) : null,
      hist.length ? h('details', { class: 'pl-hist' }, h('summary', null, SF.icon('chevron', 'icon-xs chev'), 'Recent answers (' + hist.length + ')'),
        h('ul', null, hist.map((x) => h('li', { dataset: { op: String(x.op || ''), outcome: String(x.outcome || '') } }, SF.timeEl(x.at, 'sf-muted'), ' ', h('strong', { class: SF.custodyPlain(x) ? null : 'pl-amber' }, SF.custodyAnswer(x, ST.custody && ST.custody.reason_codes)),
          x.reason && !(x.op === 'claim' && x.outcome === 'denied') && x.op !== 'wait' && !/^not sent: /.test(String(x.reason)) ? ' (' + String(x.reason) + ')' : '',
          x.model ? ' · ' + String(x.model) : '', x.purpose ? h('span', { class: 'sf-muted' }, ' · ' + String(x.purpose)) : null)))) : null);
  }
  function custodySection() {
    const read = SF.btn('Read now', { cls: 'btn-sm', icon: 'refresh', dataset: { focus: 'custody-read', action: 'custody-read' }, title: 'Read each register’s residency now (GET /api/custody?refresh=1)' });
    if (ST.custodyLoading) SF.setBusy(read, true);
    read.addEventListener('click', () => loadCustody(true));
    const rows = ST.custody && Array.isArray(ST.custody.providers) ? ST.custody.providers.filter(isObj) : null;
    let body;
    if (ST.custodyError) body = SF.unavailable(ST.custodyError, 'GPU custody');
    else if (!rows) body = SF.skeleton(3);
    else if (!rows.length) body = SF.emptyState('layers', 'Custody is off', 'No provider names a GPU custody register (the default): the harness loads and swaps models without asking.', null, true);
    else body = h('div', { class: 'sf-cards pl-custs' }, rows.map(custodyCard));
    return h('section', { class: 'pl-sec', id: 'pl-custody', 'aria-labelledby': 'pl-custody-t' },
      h('div', { class: 'pl-sec-head' }, h('h2', { class: 'pl-sec-title', id: 'pl-custody-t' }, 'GPU custody'), h('span', { class: 'tag tag-warn', title: 'The register’s contract waits for the operator’s confirmation' }, 'Contract proposed'),
        h('span', { class: 'spacer' }), read),
      h('p', { class: 'pl-sec-sub' }, 'Which models are resident on your local servers, and who may load one now. The register belongs to the operator; the harness only asks it. Edit custody in Settings › Providers.'),
      body);
  }

  // ================================================================ #/placement: the overview page
  let VIEW = null;
  const PROCEDURE = [
    ['Every step goes where it fits.', 'A step runs on a model for one of four reasons: capability, time, stage fit or cost. Each run shows its reason as a badge.'],
    ['A profile places the steps.', 'New feature, New plan, New chat and Hand off pick a profile; the default applies when they name none.'],
    ['Check the estimate first.', 'Before a run starts you see its cost and time; models without prices are left out, never counted as free.'],
    ['Judges stay independent.', 'A reviewer or the approver on the builder’s model family moves to its independent alternate; with none, the run goes ahead and says “Independence reduced”.'],
    ['A cheap step that fails moves up.', 'After a send-back the builder escalates to a stronger model instead of repeating the same one.'],
  ];
  let pageEst = null;
  let pagePicker = null;
  function pageForm() {
    const repos = SF.repos().map((r) => r.name);
    if (!ST.page.repo || repos.indexOf(ST.page.repo) < 0) ST.page.repo = repos[0] || '';
    const repoSel = SF.selectEl(repos.map((r) => [r, r]), ST.page.repo, { class: 'select', id: 'pl-page-repo', dataset: { focus: 'page-repo' } });
    const kindSel = SF.selectEl([['feature', 'A feature (one job)'], ['plan', 'A plan (lead and jobs)']], ST.page.kind, { class: 'select', id: 'pl-page-kind', dataset: { focus: 'page-kind' }, title: 'A feature is one job; a plan is its lead, then its jobs one after another' });
    const presets = ST.cfg && Array.isArray(ST.cfg.presets) ? ST.cfg.presets.filter((p) => isObj(p) && isObj(p.replaces) && Array.isArray(p.replaces.agents) && p.replaces.agents.length) : [];
    const preSel = SF.selectEl([['', 'Your saved routing']].concat(presets.map((p) => [p.name, 'preset ' + p.name])), ST.page.preset, { class: 'select', id: 'pl-page-preset', dataset: { focus: 'page-preset' } });
    if (!pagePicker) pagePicker = profilePicker({ form: 'page', routing: () => ST.page.preset || undefined, onChange: () => pageEst && pageEst.refresh() });
    if (!pageEst) pageEst = estimatePanel({ kind: () => ST.page.kind, repo: () => ST.page.repo, routing: () => ST.page.preset || undefined, profile: () => pagePicker.value() });
    if (ST.cfg) pagePicker.fill(ST.cfg);
    pageEst.ensure();
    repoSel.addEventListener('change', () => { ST.page.repo = repoSel.value; pageEst.refresh(); });
    kindSel.addEventListener('change', () => { ST.page.kind = kindSel.value; pageEst.refresh(); });
    preSel.addEventListener('change', () => { ST.page.preset = preSel.value; if (ST.cfg) pagePicker.fill(ST.cfg); pageEst.refresh(); });
    return h('section', { class: 'pl-sec', 'aria-labelledby': 'pl-est-t' },
      h('div', { class: 'pl-sec-head' }, h('h2', { class: 'pl-sec-title', id: 'pl-est-t' }, 'Estimate a run')),
      h('p', { class: 'pl-sec-sub' }, 'What a run would cost and how long it would take, before it starts, and what the independence guard would do.'),
      repos.length ? [h('div', { class: 'pl-page-form' }, SF.field('Repository', repoSel), SF.field('What', kindSel), SF.field('Routing', preSel), pagePicker.el), pageEst.el]
        : SF.emptyState('folder', 'No repository yet', 'The estimate reads a repository’s past jobs: add one in Setup.', null, true));
  }
  function profilesSection() {
    const cfg = ST.cfg;
    const names = profilesOf(cfg);
    const def = cfg && cfg.default_profile ? String(cfg.default_profile) : null;
    const cards = names.map((n) => {
      const agents = isObj(cfg.profiles[n].agents) ? cfg.profiles[n].agents : {};
      return h('article', { class: 'sf-card pl-prof-card', dataset: { profile: n } },
        h('div', { class: 'pl-cust-head' }, SF.icon('list', 'icon-sm'), h('h3', { class: 'pl-cust-name' }, n), n === def ? h('span', { class: 'tag tag-accent' }, 'default') : null),
        profileDesc(cfg, n) ? h('p', { class: 'pl-prof-desc' }, profileDesc(cfg, n)) : null,
        h('ul', { class: 'pl-prof-places' }, Object.keys(agents).filter((a) => isObj(agents[a])).map((a) => h('li', null, h('span', { class: 'mono' }, a), agents[a].model ? ' → ' + agents[a].model : ' (its route)', ' ',
          reasonBadge(agents[a].reason, false), agents[a].independent_alternate === false ? h('span', { class: 'tag pl-ind pl-ind-accepted' }, 'accepts family') : agents[a].independent_alternate ? h('span', { class: 'tag tag-outline' }, 'alt ' + agents[a].independent_alternate) : null))));
    });
    return h('section', { class: 'pl-sec', 'aria-labelledby': 'pl-prof-t' },
      h('div', { class: 'pl-sec-head' }, h('h2', { class: 'pl-sec-title', id: 'pl-prof-t' }, 'Routing profiles'), h('span', { class: 'spacer' }),
        h('a', { class: 'btn btn-sm', href: '#/settings/profiles', dataset: { focus: 'edit-profiles' } }, SF.icon('edit', 'icon-sm'), 'Edit in Settings')),
      !cfg ? SF.skeleton(3) : names.length ? h('div', { class: 'sf-cards' }, cards) : SF.emptyState('list', 'No profiles yet', 'Add one in Settings › Profiles, or apply the Local first preset, which brings five.', null, true));
  }
  function reasonsLegend() {
    return h('section', { class: 'pl-sec', 'aria-labelledby': 'pl-why-t' }, h('h2', { class: 'pl-sec-title', id: 'pl-why-t' }, 'The four reasons'),
      h('ul', { class: 'pl-legend' }, Object.keys(REASONS).map((k) => h('li', null, reasonBadge({ kind: k, text: '' }), h('span', null, REASONS[k][2] + '.')))),
      h('ul', { class: 'pl-legend' },
        h('li', null, indChip('reduced', 'a reviewer', { model: 'the builder’s model' }, null), h('span', null, 'a judge ran on the builder’s model family because no placement avoided it; the run went ahead by design.')),
        h('li', null, indChip('accepted', 'a reviewer', {}, null), h('span', null, 'the routing chose the shared family (independent_alternate: false), say a local security lens.')),
        h('li', null, indChip('unknown', 'a reviewer', {}, null), h('span', null, 'the code came from a chat whose models were not recorded.')),
        h('li', null, indChip('alternate', 'a reviewer', {}, null), h('span', null, 'the judge moved to its independent alternate.'))));
  }
  VIEW = SF.registerView({
    id: 'routing',
    title: 'Placement',
    routes: ['placement'],
    nav: null,
    // No card of its own on Home (placement is part of the Software Factory's card); what waits for the owner here
    // goes to "Needs you": a GPU custody register whose generation went back, so that every answer it gives is
    // refused until it passes the generation seen or the harness restarts (custody.CustodyClient, DESIGN §37).
    home: {
      title: 'Placement', card: false, order: 15, href: '#/placement',
      async load() {
        await loadCustody(false);
        const rows = ST.custody && Array.isArray(ST.custody.providers) ? ST.custody.providers.filter(isObj) : [];
        const needs = [];
        const seen = new Set();
        for (const row of rows) {
          const reg = isObj(row.register) ? row.register : {};
          const fenced = isObj(reg.fenced) ? reg.fenced : null;
          const url = String(reg.url || (isObj(row.settings) ? row.settings.url : '') || row.provider);
          if (!fenced || row.custody_from || seen.has(url)) continue;   // a follower shares its register's line
          seen.add(url);
          needs.push({ key: 'custody:fenced:' + row.provider, tone: 'warn', icon: 'alert', href: '#/placement',
            text: 'The harness refuses the GPU register of ' + row.provider,
            sub: 'The register’s generation went back from ' + fenced.seen + ' to ' + fenced.answered + ': its answers are refused until it passes ' + fenced.seen + ' again or the harness restarts.' });
        }
        return { tone: needs.length ? 'warn' : 'ok', text: '', needs };
      },
    },
    enter() {
      loadCfg(true).catch(() => {}).finally(() => { if (VIEW) VIEW.refresh(); if (pageEst) pageEst.refresh(); });
      // Always read again (without refresh=1 it costs the register nothing): custody may have been saved in Settings
      // since. The last answer stays on screen meanwhile.
      loadCustody(false);
    },
    render(root) {
      SF.rebuild(root, [
        SF.viewHead('Placement', 'Which model runs each step of the Software Factory, why it goes there, and what a run costs before it starts.',
          h('a', { class: 'btn btn-sm', href: '#/settings/routing', dataset: { focus: 'settings-routing' } }, SF.icon('settings', 'icon-sm'), 'Settings › Routing')),
        h('section', { class: 'wz-proc-card pl-proc', 'aria-labelledby': 'pl-proc-t' }, h('h2', { class: 'wz-proc-title', id: 'pl-proc-t' }, 'How placement works'), SF.procedureList(PROCEDURE)),
        pageForm(),
        profilesSection(),
        custodySection(),
        reasonsLegend(),
      ]);
    },
  });
  SF.onReconnect(() => { ST.cfgAt = 0; if (VIEW && VIEW.isShown()) { loadCfg(true).catch(() => {}); loadCustody(false); } });
})(window.SF);
