/* Seraphina OS Harness: the Housekeeping Tracker (DESIGN §32 and §15 "Housekeeping Tracker"; receipt items §36;
 * drift sources §38 and §39's GUI notes; Grid delivery receipts and Mermaid maps §26; product decisions §7).
 * Routes: #/housekeeping?scope=&status=&owner=&kind=&source=&must=1&overdue=1&q= (the list) and
 * #/housekeeping/<item ref> (its panel over the list; closing it goes back to the list); #/housekeeping/agents
 * ("Connect your own agents": agent tokens). Rail: "Tracker" after Plans, its badge counts.waiting. Home: the
 * tracker's card, and product decisions and other items that wait for a person in "Needs you".
 * In the Software Factory: the job drawer's Items tab (#/job/<repo>/<id>/items: items, product decisions to confirm,
 * unparsed lines, notices, the log), card chips (open, must-do, suggested, to answer), an "A person must answer
 * this" block in the drawer head, the must-do items an approve moves on in the Approve dialog (the 'form'
 * extension, form 'approve': carry_items), the Grid Card receipt on each factory-line run row (the 'runHead'
 * extension) and Copy / Download .mmd on the process maps in Procedures (the 'map' extension). In Chat: a chat's
 * suggestions under the reply that made them, Track it / Not a promise ('chatReply'), and an Items chip in its head
 * that opens every item with + Item ('chatHead'). In Plans: the roll-up chip in a plan's detail ('planHead'). On the
 * Grid Card: the Housekeeping Tracker node (flows.js GRID, drawn while this module is loaded; its badge is the count).
 * A view module: it reaches the page only through window.SF. Item titles, details, reasons, notes, evidence,
 * receipts and drift findings are raw text: SF.h() makes text nodes, never HTML, and none of it is Markdown here.
 * The buttons are a person's only (the server's transition rules, items.refusal, decide what a person may do). */
'use strict';
(function (SF) {
  const h = SF.h;

  // ---------------------------------------------------------------- words (items.WORDS and friends)
  const WORDS = SF.words.items;   // items.WORDS, shared with every section (app.js)
  const CLOSED = new Set(['verified', 'dropped', 'deferred']);
  const TERMINAL = new Set(['verified', 'dropped']);
  const ENDED = new Set(['merged', 'delivered', 'rejected', 'cancelled']);          // orchestrator.END_STAGES
  const CONFIRM_CLOSED = new Set(['merged', 'rejected', 'cancelled']);              // Factory.confirm refuses these
  const ACTIVE = new Set(['queued', 'spec', 'build', 'review', 'approve', 'merging']);
  const KINDS = { criterion: 'Acceptance criterion', finding: 'Review finding', note: 'Review note', decision: 'Product decision', rework: 'Rework note',
    commitment: 'Promise', gap: 'Unfinished check', risk: 'Risk', followup: 'Follow-up' };
  const WAITING = { overdue: 'Overdue', blocking: 'Must-do on a stopped job', suggested: 'Suggestion to accept or dismiss', ended: 'Left open in a job that ended', yours: 'Yours to do or check' };
  const SCOPES = [['', 'All'], ['job', 'Jobs'], ['chat', 'Chats'], ['plan', 'Plans'], ['followups', 'Follow-ups'], ['system', 'System']];
  const STATUS_FILTERS = [['', 'Not closed'], ['waiting', 'Waiting for you'], ['proposed', WORDS.proposed], ['active', WORDS.open + ' or ' + WORDS.doing.toLowerCase()],
    ['open', WORDS.open], ['doing', WORDS.doing], ['done', WORDS.done], ['deferred', WORDS.deferred], ['verified', WORDS.verified], ['dropped', WORDS.dropped], ['all', 'Everything']];
  const SOURCES = { spec: 'The spec', review: 'Reviews', rework: 'Rework notes', sweep: 'Found in an agent’s words', carry: 'Carried from elsewhere',
    agent: 'Added by an agent', added: 'Added by a person or an outside agent', 'plan-review': 'Plan review', overflow: 'Folded suggestions',
    persona: 'A persona', wake: 'A wake', receipt: 'Reported by the operator', 'drift-source': 'Drift sources (all)', drift: 'Persona drift (Open WebUI)' };
  const DRIFT_WORDS = SF.words.drift;
  const DRIFT_TONE = SF.words.driftTone;
  const T = { counts: null, list: null, listKey: '', loading: null, error: null, history: new Map(), drift: null, driftLoading: null, driftError: null,
    driftResults: new Map(), filtersOpen: null, lastQ: null, tokens: null, maps: null, mapsAt: 0, mapsLoading: null, mapsError: null, lastList: '#/housekeeping', panelRef: null, handle: null,
    personas: null, personasLoading: null };

  // ---------------------------------------------------------------- small helpers
  const isTok = (s, p) => typeof s === 'string' && s.indexOf(p) === 0;
  /** "you", "agent qwen-code", "persona kate", "the factory", an agent's name. */
  function who(o) {
    if (!o) return '—';
    if (o === 'human') return 'you';
    if (o === 'factory') return 'the factory';
    if (o === 'code') return 'code (a check)';
    if (isTok(o, 'agent:')) return 'agent ' + o.slice(6);
    if (isTok(o, 'persona:')) return 'persona ' + o.slice(8);
    return String(o);
  }
  function statusTone(row) {
    const st = row.status;
    if (st === 'verified') return 'ok';
    if (st === 'deferred' || st === 'dropped') return 'muted';
    if (st === 'done') return row.verifier === 'human' ? 'human' : 'accent';
    if (st === 'proposed' || st === 'doing') return 'accent';
    return 'neutral';
  }
  const pill = (row) => h('span', { class: 'pill tr-pill tone-' + statusTone(row), title: 'Status: ' + (WORDS[row.status] || row.status) }, WORDS[row.status] || String(row.status));
  const tag = (text, cls, title) => h('span', { class: 'tag ' + (cls || ''), title: title || null }, text);
  const scopeOf = (row) => (SF.isObj(row && row.scope) ? row.scope : {});
  const isAlive = (row) => !CLOSED.has(row.status) || (row.status === 'deferred' && /^(followups|system)$/.test(scopeOf(row).kind));
  /** items.holds_gate: what a job's gate (and approve) waits for. */
  const holdsGate = (row) => !!row.blocking && (!CLOSED.has(row.status) || (row.kind === 'decision' && row.status === 'verified' && ['human', 'factory', 'code'].indexOf(row.verified_by) < 0));
  /** items.persona_written: never given to an outside agent (nor anything a private turn wrote). */
  const personaWritten = (row) => isTok(String(row.owner || ''), 'persona:') || (row.source || {}).type === 'persona' || isTok(String((row.source || {}).agent || ''), 'persona:');
  function whereOf(sc) {
    if (sc.kind === 'job') return 'job ' + sc.id + (sc.repo ? ' (' + sc.repo + ')' : '');
    if (sc.kind === 'chat') return 'chat ' + sc.id;
    if (sc.kind === 'plan') return 'plan ' + sc.id + (sc.repo ? ' (' + sc.repo + ')' : '');
    if (sc.kind === 'followups') return sc.repo + ' follow-ups';
    if (sc.kind === 'system') return 'system';
    return String(sc.ref || '');
  }
  function scopeHref(sc) {
    if (sc.kind === 'job') return '#/job/' + SF.enc(sc.repo) + '/' + SF.enc(sc.id) + '/items';
    if (sc.kind === 'chat') return '#/chat/' + SF.enc(sc.id);
    if (sc.kind === 'plan') return '#/plans/' + SF.enc(sc.repo) + '/' + SF.enc(sc.id);
    return '#/housekeeping?scope=' + SF.enc(sc.kind || '');
  }
  const itemHash = SF.itemHref;
  /** A ref's scope from its text (items.parse_ref, local host): {kind, repo, id}. */
  function refScope(ref) {
    const m = /^(?:host:[^/]+\/)?(job|plan):([^/]+)\/([^/]+)\/[ICFS]-\d+$/.exec(ref) || /^(?:host:[^/]+\/)?(chat):([^/]+)\/[ICFS]-\d+$/.exec(ref)
      || /^(?:host:[^/]+\/)?repo:([^/]+)\/F-\d+$/.exec(ref);
    if (!m) return /^(?:host:[^/]+\/)?system:S-\d+$/.test(ref) ? { kind: 'system' } : null;
    if (m[1] === 'job' || m[1] === 'plan') return { kind: m[1], repo: m[2], id: m[3] };
    if (m[1] === 'chat') return { kind: 'chat', id: m[2] };
    return { kind: 'followups', repo: m[1] };
  }
  /** An item ref's scope ref, as items.updated names it: job:<repo>/<id>, chat:<id>, plan:<repo>/<id>, repo:<repo>, system. */
  function scopeRefOf(ref) {
    const m = /^(.*)[/:][ICFS]-\d+$/.exec(String(ref || ''));
    return m ? m[1] : null;
  }
  /** Keyboard focus inside a node a hook gives again after its parent was rebuilt (a chat head, a turn's foot): the
   *  control that had it gets it back once the node is in the page again. A click elsewhere forgets it. */
  let lastFocus = null;
  document.addEventListener('focusin', (e) => { lastFocus = e.target; });
  document.addEventListener('pointerdown', () => { lastFocus = null; }, true);
  function keepFocusIn(node) {
    const el = lastFocus;
    const act = document.activeElement;
    if (!el || !node.contains(el) || (act && act !== document.body)) return;
    setTimeout(() => { const now = document.activeElement; if (el.isConnected && (!now || now === document.body)) el.focus({ preventScroll: true }); }, 0);
  }
  /** The control that had focus (data-focus key) in root, or the one that had it until its busy button was disabled. */
  function focusKey(root) {
    let act = document.activeElement;
    if ((!act || act === document.body) && lastFocus && root && root.contains(lastFocus)) act = lastFocus;
    return act && act !== document.body && root && root.contains(act) && act.dataset ? act.dataset.focus || '' : '';
  }
  /** After an inline row's action (key "<prefix>:<ref>:<action>") took its button away: that row's first action, else
   *  the row itself. */
  function rowFallback(root, key, prefix) {
    if (!key || key.indexOf(prefix + ':') !== 0) return null;
    const ref = key.slice(prefix.length + 1, key.lastIndexOf(':'));
    const esc = (x) => CSS.escape(x);
    const el = root.querySelector('.tr-actions [data-focus^="' + esc(prefix + ':' + ref + ':') + '"]:not([disabled])') || root.querySelector('[data-focus="' + esc(prefix + 'row:' + ref) + '"]');
    // The item may have moved into the folded Closed group (proven, dropped, moved on): unfold it, so it shows.
    const fold = el && el.closest('details:not([open])');
    if (fold) fold.open = true;
    return el;
  }
  /** SF.drawer.open, keeping keyboard focus: on the same control again (data-focus), else, when it went away (an action
   *  changed the item), on first (a selector, or fn(key) → element), else on the panel's title. */
  function openKept(opts, firstSel) {
    const drawer = document.getElementById('drawer');
    let act = document.activeElement;
    // A busy button is disabled while its action runs, which drops focus to the page: it still counts as here.
    if ((!act || act === document.body) && lastFocus && drawer && drawer.contains(lastFocus)) act = lastFocus;
    const inside = !!(act && act !== document.body && drawer && drawer.contains(act) && SF.drawer.isOpen(opts.key));
    const key = inside && act.dataset ? act.dataset.focus || '' : '';
    const ctl = SF.drawer.open(opts);
    if (inside && !drawer.contains(document.activeElement)) {
      const twin = key ? drawer.querySelector('[data-focus="' + CSS.escape(key) + '"]') : null;
      const first = typeof firstSel === 'function' ? firstSel(key) : firstSel ? drawer.querySelector(firstSel) : null;
      const target = twin || first || document.getElementById('drawer-title');
      if (target) target.focus({ preventScroll: true });
    }
    return ctl;
  }
  function sourceLabel(row) {
    const s = SF.isObj(row.source) ? row.source : {};
    const t = String(s.type || '');
    if (row.kind === 'criterion' && t === 'spec') return 'spec criterion ' + (s.n || '');
    if (row.kind === 'decision') return 'spec assumption (product)';
    if (t === 'review') return (s.agent || 'review') + ' round ' + s.round + (row.kind === 'finding' && s.n ? ', finding ' + s.n : ', note');
    if (t === 'rework') return 'rework note ' + (s.k || '');
    if (t === 'sweep') return 'found in ' + who(s.agent) + '’s ' + (s.file || 'reply') + (s.turn ? ' (turn ' + s.turn + ')' : '');
    if (t === 'carry') return 'carried from ' + (s.ref || 'another scope');
    if (t === 'agent' || t === 'added') { const by = s.agent || s.by; return by === 'human' ? 'added by you' : 'added by ' + who(by); }
    if (t === 'receipt') return 'receipt ' + (s.receipt || '') + ', reported by the operator';
    if (isTok(t, 'drift:')) return 'drift source ' + t.slice(6) + (s.key ? ', finding ' + s.key : '');
    if (t === 'drift') return (s.model || 'a persona') + ' changed in Open WebUI';
    if (t === 'overflow') return 'suggestions folded together';
    if (t === 'plan-review') return 'the plan’s final review';
    if (t === 'push') return 'the push after job ' + (s.job || '');
    if (t === 'persona' || t === 'wake') return who(s.agent) + (t === 'wake' ? '’s wake' : '');
    return t || String(row.kind || '');
  }
  /** An item about an outside call whose outcome is unknown (outcomes.UNKNOWN_TITLE starts its title, DESIGN §40). */
  const unknownOutcome = (row) => String(row.title || '').indexOf(SF.words.unknownTitle) === 0;
  const sourceKey = (row) => { const t = String((row.source || {}).type || ''); return isTok(t, 'drift:') ? 'drift-source' : t; };
  function lines(text) { return String(text || '').split('\n'); }
  /** Raw text with its line breaks kept (detail ≤ 2000 characters): text only. */
  const textBlock = (text, cls) => h('div', { class: 'tr-text ' + (cls || '') }, String(text || ''));
  const dateOnly = (d) => (d ? String(d).slice(0, 10) : '');
  function plainHref(href, label, cls) { return h('a', { href, class: cls || null }, label); }
  /** A time that may be in the future (a next check): "in 22m", else SF.timeEl's "3h ago". */
  function whenEl(ts) {
    const t = Date.parse(ts || '');
    if (!isFinite(t) || t <= Date.now() + 5000) return SF.timeEl(ts, 'rel');
    const m = Math.round((t - Date.now()) / 60000);
    const text = m < 1 ? 'in under a minute' : m < 60 ? 'in ' + m + 'm' : m < 48 * 60 ? 'in ' + Math.round(m / 60) + 'h' : 'in ' + Math.round(m / 1440) + 'd';
    return h('time', { class: 'rel', datetime: ts, title: new Date(t).toLocaleString() }, text);
  }

  // ---------------------------------------------------------------- the server
  async function fetchList(key, q) {
    const params = ['limit=1000'];
    if (q.scope) params.push('scope=' + SF.enc(q.scope));
    if (q.repo && q.scope !== 'system') params.push('repo=' + SF.enc(q.repo));
    if (q.status === 'waiting') params.push('waiting=1');
    else if (q.status === 'all') { /* everything */ }
    else if (q.status === 'active') params.push('status=open,doing');
    else if (q.status) params.push('status=' + SF.enc(q.status));
    else params.push('status=proposed,open,doing,done,deferred');
    const r = await SF.api('/api/items?' + params.join('&'));
    if (key !== T.listKey) return;
    T.list = { key, items: Array.isArray(r && r.items) ? r.items : [], counts: SF.isObj(r && r.counts) ? r.counts : {}, total: Number(r && r.total) || 0, at: Date.now() };
  }
  async function loadCounts() {
    const r = await SF.api('/api/items?waiting=1&limit=200');
    T.counts = SF.isObj(r && r.counts) ? r.counts : null;
    return { items: Array.isArray(r && r.items) ? r.items : [], counts: T.counts || {} };
  }
  function badge(counts) {
    if (!T.handle || !counts) return;
    T.handle.badge(counts.waiting, SF.plural(counts.waiting || 0, 'item') + ' waiting for you');
  }
  async function act(action, body, opts) {
    return SF.api('/api/items/' + action, Object.assign({ method: 'POST', body, retry409: true }, opts || {}));
  }
  /** An item panel's data, kept while it is read again (so the panel never flashes "Loading…"). */
  function staleHistory(scopeRef) {
    for (const [ref, e] of T.history) if (scopeRef === undefined || scopeRefOf(ref) === scopeRef) { e.stale = true; e.gen++; }
  }
  /** After any change: the list, the panel, the job tabs, chats and plans, Home's card and the badge. */
  function changed() {
    T.listKey = '';
    staleHistory();
    for (const st of JT.values()) st.stale = true;
    for (const k of CS.keys()) csChanged(k);
    if (T.handle) { T.handle.refresh(); T.handle.refreshHome(); }
    rerenderJobTab();
    loadCounts().then((r) => badge(r.counts)).catch(() => {});
  }
  async function loadTokens() {
    if (T.tokens) return T.tokens;
    try { const r = await SF.api('/api/agent-tokens'); T.tokens = Array.isArray(r && r.tokens) ? r.tokens.map((t) => String(t.name)) : []; }
    catch (e) { T.tokens = []; }
    return T.tokens;
  }

  // ---------------------------------------------------------------- a person's actions (items.refusal for by = human)
  /** The buttons a person has on this row, in order: [{id, label, icon, primary?, danger?}]. */
  function personActions(row) {
    const st = row.status;
    const sc = scopeOf(row);
    const ended = sc.kind === 'job' && ENDED.has(sc.stage);
    const followupsLike = sc.kind === 'followups' || sc.kind === 'system';
    const noLater = sc.kind === 'chat' && !sc.repo;          // a chat without a repository has no follow-ups
    const decision = row.kind === 'decision';
    const sweep = (row.source || {}).type === 'sweep';
    const receipt = (row.source || {}).type === 'receipt';
    const overflow = (row.source || {}).type === 'overflow';
    const links = row.links || {};
    const out = [];
    const add = (id, label, ic, extra) => out.push(Object.assign({ id, label, icon: ic }, extra || {}));
    if (overflow && st === 'proposed') {
      add('accept', 'Accept the group', 'check');   // Dismiss all is in the group itself
    } else if (st === 'proposed') {
      add('accept', sweep ? 'Track it' : 'Accept', 'check', { primary: true });
      add('dismiss', sweep ? 'Not a promise' : 'Dismiss…', 'x');
      add('verify', 'I checked it myself…', 'check-circle');
    } else if (st === 'open' || st === 'doing') {
      if (decision) add('verify', 'Yes: confirm it', 'check', { primary: true });
      else add('verify', 'I checked it myself…', 'check-circle', { primary: !ended });
      if (decision) add('drop', 'No…', 'ban');
      if (!noLater && !decision) add('defer', followupsLike ? 'Later…' : 'Move to follow-ups…', 'clock');
      if (!decision) add('drop', 'Drop…', 'ban', { danger: true });
    } else if (st === 'done') {
      add('verify', receipt ? 'Confirm her check…' : 'I checked it myself…', 'check-circle', { primary: true });
      if (!ended) add('notmet', receipt ? 'Reopen…' : 'Not met…', 'rework');
      add('drop', 'Drop…', 'ban', { danger: true });
    } else if (st === 'deferred' && followupsLike) {
      add('date', 'Change the date…', 'clock');
      add('reopen', 'Reopen…', 'rework');
    } else if (st === 'deferred' && links.followup) {
      // Moved on: its follow-up is the live copy (reopening this one would make a second); work it there.
      add('open-followup', 'Open the follow-up', 'arrow-right', { primary: true, href: itemHash(links.followup), title: 'Work it as ' + links.followup });
    } else if (st === 'deferred' && sc.kind === 'chat' && links.job) {
      const repo = sc.repo || '';
      add('open-job', 'Open job ' + links.job, 'arrow-right', { primary: true, href: repo ? '#/job/' + SF.enc(repo) + '/' + SF.enc(links.job) + '/items' : null, title: 'Handed to job ' + links.job + ': work it there' });
    } else if (!ended) {
      add('reopen', 'Reopen…', 'rework');   // a Later item that did not move on yet (items.unmoved_later), a proven or dropped one
    }
    if (!TERMINAL.has(st) && !(st === 'deferred' && !followupsLike)) {
      if (!decision && /^(job|chat|plan)$/.test(sc.kind)) add(row.blocking ? 'unblock' : 'block', row.blocking ? 'Not must-do' : 'Make it must-do', 'alert');
      if (!decision) add('assign', 'Change owner…', 'user');
    }
    if (sc.kind === 'followups' && !overflow && (st === 'open' || st === 'doing' || st === 'proposed')) {
      const linked = (row.links || {}).job;
      const live = linked && (() => { const j = SF.job(sc.repo, linked); return !!j && !ENDED.has(j.stage); })();
      add('make-job', live ? 'A job works on it' : 'Make a job', 'factory', { disabled: !!(st === 'doing' && live), title: live ? 'Job ' + linked + ' is working on it' : 'Submit a job for this follow-up' });
    }
    return out;
  }

  /** Ask what the action needs (a reason, a note, a date, an owner) and send it. */
  async function runAction(a, row, button, after) {
    const ref = row.ref;
    const title = String(row.title || ref);
    const done = (msg) => { SF.toast(msg, { kind: 'ok' }); changed(); if (after) after(); };
    if (a.id === 'accept' || a.id === 'block' || a.id === 'unblock') {
      SF.setBusy(button, true);
      try {
        await act(a.id, { ref });
        done(a.id === 'accept' ? 'Tracked: ' + ref + ' is To do.' : a.id === 'block' ? ref + ' must be done before merge.' : ref + ' is no longer must-do.');
      } catch (e) { SF.toastError('Not done', e); }
      finally { SF.setBusy(button, false); }
      return;
    }
    if (a.id === 'make-job') {
      SF.setBusy(button, true);
      try {
        const r = await act('make-job', { ref });
        done('Job ' + (r && r.job ? r.job.id : '') + ' works on ' + ref + '.');
      } catch (e) { SF.toastError('No job made', e); }
      finally { SF.setBusy(button, false); }
      return;
    }
    const body = [];
    const sc = scopeOf(row);
    let reason = null; let note = null; let due = null; let owner = null;
    const want = { dismiss: 'reason?', verify: 'note', drop: 'reason', reopen: 'reason', notmet: 'reason', defer: 'reason', date: 'due', assign: 'owner' }[a.id];
    if (want === 'reason' || want === 'reason?') {
      reason = h('textarea', { class: 'textarea', rows: 3, maxlength: 300, required: want === 'reason', placeholder: a.id === 'dismiss' ? 'not a promise' : 'Why? It is kept in the item’s history.' });
      body.push(SF.field(want === 'reason' ? 'Reason (needed)' : 'Reason (optional)', reason, a.id === 'dismiss' ? 'Left empty: “not a promise”. A dismissed sweep suggestion is not suggested again in this repository.' : null));
    }
    if (want === 'note') {
      note = h('textarea', { class: 'textarea', rows: 3, maxlength: 300, placeholder: 'How did you check it? (optional)' });
      body.push(SF.field('How you checked it', note, row.kind === 'decision' ? 'Proving a product decision confirms its line in the spec for the approver.' : 'Kept as your evidence (“checked by a person” when empty).'));
    }
    if (a.id === 'defer' || a.id === 'date') {
      due = h('input', { class: 'input', type: 'date', value: dateOnly(row.due) });
      body.push(SF.field('Due date' + (a.id === 'date' ? '' : ' (optional)'), due, sc.kind === 'followups' || sc.kind === 'system' ? 'It stays Later here until then.'
        : 'The copy in ' + (sc.repo || 'the repository') + '’s follow-ups is due then; left empty, the usual follow-up time (14 days unless Settings › housekeeping says otherwise).'));
    }
    if (want === 'owner') {
      const opts = [['human', 'you (a person)']];
      if (sc.kind === 'job') opts.push(['builder', 'builder']);
      if (sc.kind === 'plan') opts.push(['lead', 'lead'], ['builder', 'builder']);
      // An outside agent's token reads and updates only its own items of the system scope and the follow-ups
      // (Factory.items for_agent): one given a job's, chat's or plan's item could never see it.
      const agentScope = sc.kind === 'system' || sc.kind === 'followups';
      const privateRow = personaWritten(row) || (row.taint || []).indexOf('private') >= 0;
      const tokens = agentScope && !privateRow ? await loadTokens() : [];
      for (const t of tokens) opts.push(['agent:' + t, 'agent ' + t + ' (an outside agent)']);
      if (opts.every((o) => o[0] !== row.owner)) opts.push([row.owner, who(row.owner) + ' (now)']);
      owner = SF.selectEl(opts, row.owner, { 'aria-label': 'New owner' });
      body.push(SF.field('Owner', owner, privateRow
        ? 'A persona or a private turn wrote this item: it is never given to an outside agent (they may run on hosted models).'
        : !agentScope ? (sc.kind === 'chat' && !sc.repo ? 'An outside agent works only on System items and follow-ups, and a chat without a repository has none: this item stays with the chat.'
          : 'An outside agent works only on System items and follow-ups: move this item to the follow-ups first (Move to follow-ups…), then give it to one there.')
          : tokens.length ? 'An outside agent’s token reads only the items it owns here: what it added and what you give it.'
            : 'No outside agent has a token yet: make one in Outside agents (Housekeeping Tracker) to give it items.'));
    }
    const titles = { dismiss: 'Dismiss this suggestion?', verify: row.kind === 'decision' ? 'Answer yes to this product decision?' : 'Prove it: you checked it yourself', drop: row.kind === 'decision' ? 'Answer no to this product decision?' : 'Drop this item?',
      reopen: 'Reopen this item?', notmet: 'Not met: back to To do?', defer: sc.kind === 'followups' || sc.kind === 'system' ? 'Move it to later?' : 'Move it to the follow-ups?', date: 'Change the date', assign: 'Give it to someone else' };
    const confirm = { dismiss: 'Dismiss', verify: row.kind === 'decision' ? 'Yes, confirm it' : 'Prove it', drop: row.kind === 'decision' ? 'No: drop it' : 'Drop it', reopen: 'Reopen', notmet: 'Not met', defer: sc.kind === 'followups' || sc.kind === 'system' ? 'Later' : 'Move it', date: 'Save the date', assign: 'Change owner' };
    const descs = {
      defer: sc.kind === 'followups' || sc.kind === 'system' ? 'Later keeps it here, alive, with its date.' : 'Later moves it to the repository’s follow-ups: the copy keeps its words and history link, and this item says where it went.',
      dismiss: 'A suggestion nobody tracks is dropped with your reason.',
      drop: 'Dropped items stay in the history; a person can reopen them.',
    };
    await SF.openModal({
      title: titles[a.id] || a.label,
      description: [h('span', { class: 'tr-modal-ref' }, ref), ' ', h('span', { class: 'tr-modal-title' }, title), descs[a.id] ? h('span', { class: 'tr-modal-desc' }, descs[a.id]) : null],
      body: body.length ? body : null,
      dismissable: false,
      actions: [{ label: 'Cancel', value: 'cancel' }, { label: confirm[a.id] || a.label, value: 'ok', cls: a.danger ? 'btn-danger-solid' : 'btn-primary', icon: a.icon }],
      onSubmit: async () => {
        const send = { ref };
        const r = reason ? reason.value.trim() : '';
        if (want === 'reason' && !r) { reason.focus(); throw new Error('Write a reason: it stays in the item’s history.'); }
        if (r) send.reason = r;
        if (note && note.value.trim()) send.note = note.value.trim();
        if (due && due.value) send.due = due.value;
        if (a.id === 'date' && !send.due) { due.focus(); throw new Error('Pick a date.'); }
        if (a.id === 'date') send.reason = 'new date ' + send.due;
        if (owner) send.owner = owner.value;
        const action = a.id === 'notmet' ? 'reopen' : a.id === 'date' ? 'defer' : a.id;
        const res = await act(action, send);
        const it = res && res.item ? res.item : null;
        const moved = it && it.links && it.links.followup && a.id === 'defer' ? ': moved to ' + it.links.followup : '';
        done((WORDS[it && it.status] ? ref + ' is ' + WORDS[it.status] : 'Done') + moved + '.');
      },
    });
  }
  function actionButtons(row, after, prefix) {
    const acts = personActions(row);
    if (!acts.length) return null;
    return h('div', { class: 'tr-actions', role: 'group', 'aria-label': 'What you can do with ' + row.ref }, acts.map((a) => {
      const cls = 'btn-sm ' + (a.primary ? 'btn-primary' : a.danger ? 'btn-danger' : '');
      const dataset = { action: a.id, focus: (prefix || 'act') + ':' + row.ref + ':' + a.id };
      if (a.href !== undefined) {   // where it goes on, not a change: a link
        if (!a.href) return null;
        return h('a', { class: 'btn ' + cls, href: a.href, title: a.title || null, dataset }, SF.icon(a.icon, 'icon-sm'), a.label);
      }
      const b = SF.btn(a.label, { cls, icon: a.icon, disabled: !!a.disabled, title: a.title || null, dataset });
      b.addEventListener('click', () => runAction(a, row, b, after));
      return b;
    }));
  }

  // ---------------------------------------------------------------- one item: chips, evidence, the whole body
  function chips(row, opts) {
    const out = [];
    const s = row.source || {};
    // "Must-do on a stopped job" says must-do already: one chip, not two.
    const mustWait = row.waiting === 'blocking';
    if (row.blocking && !TERMINAL.has(row.status) && !mustWait) out.push(tag('must-do', 'tag-warn tr-must', 'Must be done before merge'));
    if (row.waiting) out.push(tag(WAITING[row.waiting] || row.waiting, (row.waiting === 'overdue' || mustWait ? 'tag-err' : 'tr-tag-human') + (mustWait && row.blocking ? ' tr-must' : ''), 'Waiting for you: ' + (WAITING[row.waiting] || row.waiting)));
    else if (row.overdue) out.push(tag('overdue', 'tag-err'));
    if (row.stale) out.push(tag('stale ' + (row.stale_rounds || '') + ' rounds', 'tag-warn', 'Its owner has not touched it for ' + (row.stale_rounds || 'several') + ' rounds'));
    if (row.met_claimed) out.push(tag('met claimed, no evidence', 'tag-warn', 'The approver said met, but the builder reported no evidence'));
    if (unknownOutcome(row)) out.push(tag('Outcome unknown: check before retrying', 'tag-warn tr-unknown', 'It may or may not have happened: look, read only, before anyone tries it again'));
    if (s.type === 'receipt') out.push(tag('Reported by the operator', 'tag-outline tr-receipt', 'From receipt ' + (s.receipt || '') + ': what she reported, never re-checked'));
    if (isTok(String(s.type || ''), 'drift:')) out.push(tag('drift: ' + String(s.type).slice(6), 'tag-outline', 'A drift source’s finding' + (s.key ? ' (' + s.key + ')' : '')));
    if (s.type === 'drift') out.push(tag('persona drift', 'tag-outline', (s.model || '') + ' changed in Open WebUI'));
    if (s.type === 'overflow' || Number(s.overflow) > 0) out.push(tag('group of ' + (Number(s.count || s.overflow) || ''), 'tag-outline', 'Several suggestions kept as one item'));
    for (const t of row.taint || []) {
      if (t === 'untrusted') out.push(tag(s.type === 'receipt' ? 'May not be hers' : 'untrusted', 'tag-warn', s.type === 'receipt' ? 'It came from a place the harness itself could write' : 'Written by a turn that read something untrusted'));
      else if (t === 'private') out.push(tag('private', 'tag-outline', 'Written by a turn that read something private: never given to an outside agent'));
    }
    if (opts && opts.due && row.due && !row.overdue) out.push(tag('due ' + dateOnly(row.due), 'tag-outline'));
    return out;
  }
  function checkMark(ev) {
    if (ev.check === 'ok') return tag('✓ checked', 'tag-ok', ev.check_detail || 'Checked by the factory');
    if (ev.check === 'failed') return tag('✗ check failed', 'tag-err', ev.check_detail || 'The factory’s check failed');
    if (ev.check === 'pending') return tag('not checked yet', 'tag-outline', 'The factory checks commits and files after the run');
    return null;
  }
  function evidenceRow(ev) {
    const k = String(ev.kind || '');
    const by = ev.by ? who(ev.by) : '';
    const meta = h('span', { class: 'tr-ev-meta' }, by ? 'by ' + by : null, ev.at ? [' · ', SF.timeEl(ev.at, 'rel')] : null);
    let main;
    if (k === 'commit') main = [h('span', { class: 'tr-ev-kind' }, 'commit'), h('code', { class: 'tr-mono' }, String(ev.ref || '').slice(0, 12)), checkMark(ev)];
    else if (k === 'file') main = [h('span', { class: 'tr-ev-kind' }, 'file'), h('code', { class: 'tr-mono' }, String(ev.ref || '') + (ev.line ? ':' + ev.line : '')), checkMark(ev)];
    else if (k === 'command') main = [h('span', { class: 'tr-ev-kind' }, 'command'), h('code', { class: 'tr-mono' }, String(ev.ref || '')), tag('claimed, not re-run', 'tag-outline tr-claimed', 'The agent says it ran this; nobody ran it again')];
    else if (k === 'receipt') main = [h('span', { class: 'tr-ev-kind' }, 'receipt'), h('code', { class: 'tr-mono' }, String(ev.ref || '')), tag('reported', 'tag-outline', 'What the operator reported: never re-checked by the harness')];
    else if (k === 'job' || k === 'wake' || k === 'drift') main = [h('span', { class: 'tr-ev-kind' }, k), h('code', { class: 'tr-mono' }, String(ev.ref || '')), tag('written by code', 'tag-outline')];
    else main = [h('span', { class: 'tr-ev-kind' }, k === 'human' ? 'a person’s check' : k === 'review' ? 'a reviewer’s check' : k)];
    const detail = ev.detail ? h('div', { class: 'tr-ev-detail' }, String(ev.detail)) : null;
    const checkDetail = ev.check_detail && (ev.check === 'failed' || ev.check === 'ok') ? h('div', { class: 'tr-ev-detail tr-ev-check' + (ev.check === 'failed' ? ' is-failed' : '') }, String(ev.check_detail)) : null;
    return h('li', { class: 'tr-ev', dataset: { kind: k } }, h('div', { class: 'tr-ev-main' }, main, meta), detail, checkDetail);
  }
  /** §36: what the operator reported, Not verified above Verified, with the same weight; never a check mark. */
  function receiptBlock(row) {
    const s = row.source || {};
    const verified = (row.evidence || []).filter((e) => e.kind === 'receipt');
    const detail = String(row.detail || '').trim();
    return h('section', { class: 'tr-receipt-box', 'aria-label': 'What the operator reported' },
      h('p', { class: 'tr-receipt-head' }, 'From receipt ', h('code', { class: 'tr-mono' }, String(s.receipt || '')), ', reported by the operator',
        (row.links || {}).receipt && (row.links || {}).receipt !== s.receipt ? [' (first in ', h('code', { class: 'tr-mono' }, String(row.links.receipt)), ')'] : null),
      h('div', { class: 'tr-receipt-cols' },
        h('div', { class: 'tr-receipt-col' }, h('h4', null, 'Not verified (1)'), h('p', { class: 'tr-text' }, String(row.title || '')), detail ? h('p', { class: 'tr-ev-detail' }, detail) : null),
        h('div', { class: 'tr-receipt-col' }, h('h4', null, 'Verified (' + verified.length + ')'),
          verified.length ? h('ul', { class: 'tr-plain' }, verified.map((e) => h('li', { class: 'tr-text' }, String(e.detail || e.ref)))) : h('p', { class: 'sf-muted' }, 'Nothing verified'))),
      row.status === 'done' ? SF.callout('human', 'user', 'She reports this checked:', 'confirm it or reopen it. Her report is not proof; your check is.') : null,
      (row.taint || []).indexOf('untrusted') >= 0 ? SF.callout('warn', 'alert', 'May not be hers:', 'it came from a place the harness’s own user can write, so a file put there would read as hers.') : null);
  }
  function factsKv(row) {
    const sc = scopeOf(row);
    const links = row.links || {};
    const rows = [];
    const add = (k, v) => { if (v !== null && v !== undefined && v !== '' && !(Array.isArray(v) && !v.length)) rows.push(h('dt', null, k), h('dd', null, v)); };
    add('Where', plainHref(scopeHref(sc), whereOf(sc)));
    add('Kind', KINDS[row.kind] || row.kind);
    add('Owner', who(row.owner));
    add('Checked by', who(row.verifier));
    add('Must-do', row.blocking ? 'yes: it must be done before merge' : 'no');
    add('Came from', sourceLabel(row));
    add('Opened', [SF.timeEl(row.opened_at, 'rel'), row.opened_round ? ' · round ' + row.opened_round : null]);
    add('Changed', SF.timeEl(row.updated_at, 'rel'));
    add('Due', row.due ? dateOnly(row.due) + (row.overdue ? ' (overdue)' : '') : null);
    add('Done by', row.done_by ? who(row.done_by) : null);
    add('Proven by', row.verified_by ? who(row.verified_by) : null);
    add('Reason', row.reason ? h('span', { class: 'tr-text' }, String(row.reason)) : null);
    add('Note', row.note ? h('span', { class: 'tr-text' }, String(row.note)) : null);
    const linkNodes = [];
    if (links.followup) linkNodes.push(h('span', null, 'follow-up ', plainHref(itemHash(links.followup), String(links.followup), 'tr-mono')));
    if (links.job) {
      const repo = sc.repo || (refScope(String(links.followup || '')) || {}).repo;
      linkNodes.push(h('span', null, 'job ', repo ? plainHref('#/job/' + SF.enc(repo) + '/' + SF.enc(links.job), String(links.job), 'tr-mono') : h('code', { class: 'tr-mono' }, String(links.job))));
    }
    if (links.parent) linkNodes.push(h('span', null, 'part of ', h('code', { class: 'tr-mono' }, String(links.parent))));
    if (links.plan) linkNodes.push(h('span', null, 'plan ', h('code', { class: 'tr-mono' }, String(links.plan))));
    if (links.chat) linkNodes.push(h('span', null, 'chat ', plainHref('#/chat/' + SF.enc(links.chat), String(links.chat), 'tr-mono')));
    if (links.wake) linkNodes.push(h('span', null, 'wake ', h('code', { class: 'tr-mono' }, String(links.wake))));
    if (links.receipt) linkNodes.push(h('span', null, 'receipt ', h('code', { class: 'tr-mono' }, String(links.receipt))));
    add('Links', linkNodes.length ? h('span', { class: 'tr-links' }, linkNodes) : null);
    if (Array.isArray(row.also) && row.also.length) add('Raised again', row.also.length + ' time(s) by a reviewer');
    return h('dl', { class: 'sf-kv tr-kv' }, rows);
  }
  /** Persona drift (personas._open_drift_item): source.model is the Open WebUI model id, not a persona's name (one
   *  model can back several personas). The personas bound to it (GET /api/personas: binding.model, or drift.item this
   *  item), each a link to its page; Personas while they are read, or when none is bound to it now. */
  function loadPersonas() {
    if (T.personas && Date.now() - T.personas.at < 60000) return Promise.resolve(T.personas.rows);
    if (T.personasLoading) return T.personasLoading;
    T.personasLoading = SF.api('/api/personas').then((r) => { T.personas = { rows: Array.isArray(r && r.personas) ? r.personas : [], at: Date.now() }; return T.personas.rows; })
      .catch(() => { T.personas = { rows: [], at: Date.now() }; return []; }).finally(() => { T.personasLoading = null; });
    return T.personasLoading;
  }
  function personaLinks(row) {
    const model = String((row.source || {}).model || '');
    const box = h('span', { class: 'tr-persona-links' }, plainHref('#/personas', 'Open Personas'), '.');
    loadPersonas().then((rows) => {
      const bound = rows.filter((p) => p && p.name && ((SF.isObj(p.binding) && p.binding.model === model) || (SF.isObj(p.drift) && p.drift.item === row.ref)));
      SF.clear(box);
      if (!bound.length) { SF.add(box, ['No persona is bound to it now: ', plainHref('#/personas', 'open Personas'), '.']); return; }
      SF.add(box, ['Bound to it: ', bound.map((p, i) => [i ? ', ' : null, plainHref('#/personas/' + SF.enc(p.name), String(p.label || p.name) + (p.label && p.label !== p.name ? ' (' + p.name + ')' : ''))]),
        bound.length === 1 ? ': open its profile card.' : ': open their profile cards.']);
    });
    return box;
  }
  /** The banners that say what is special about an item, before its facts. */
  function banners(row) {
    const out = [];
    const sc = scopeOf(row);
    const s = row.source || {};
    const links = row.links || {};
    if (unknownOutcome(row) && !TERMINAL.has(row.status)) {
      // DESIGN §40: nothing whose outcome is unknown is tried again by itself; a person looks first, then closes it
      out.push(SF.callout('warn', 'alert', 'Outcome unknown: it may or may not have happened.', (s.type === 'push' ? 'Look at the remote (git ls-remote, or its web page) before anyone pushes again.'
        : s.type === 'receipt' ? 'Her receipt says so: look, read only, before she or anyone tries it again.' : 'Look, read only, before anyone tries it again.') +
        (row.status === 'done' ? ' A later report is not a check: prove it yourself, or reopen it.' : ' It stays open until a person checks it.')));
    }
    if (row.kind === 'decision' && !TERMINAL.has(row.status)) out.push(SF.callout('human', 'user', 'A person must answer this.', 'Yes proves it and confirms its line in the spec for the approver; No drops it with your reason. No agent can answer a product decision.'));
    if (row.met_claimed) out.push(SF.callout('warn', 'alert', 'The approver said met,', 'but the builder reported no evidence: it stays To do until the builder marks it done with evidence or you check it yourself.'));
    if (row.stale) out.push(SF.callout('warn', 'clock', 'Stale:', 'its owner has not touched it for ' + (row.stale_rounds || 'several') + ' rounds.'));
    if (sc.kind === 'job' && ENDED.has(sc.stage) && !TERMINAL.has(row.status)) out.push(SF.callout('info', 'info', 'This job has ended (' + sc.stage + ').', 'Prove it, drop it or move it to the follow-ups; it cannot be reopened or added to here.'));
    if (row.status === 'deferred' && links.followup) out.push(h('p', { class: 'tr-later' }, SF.icon('clock', 'icon-sm'), 'Later: moved to ', plainHref(itemHash(links.followup), String(links.followup), 'tr-mono'), '.'));
    else if (row.status === 'deferred' && sc.kind === 'chat' && links.job) out.push(h('p', { class: 'tr-later' }, SF.icon('handoff', 'icon-sm'), 'Handed to job ', sc.repo ? plainHref('#/job/' + SF.enc(sc.repo) + '/' + SF.enc(links.job), String(links.job), 'tr-mono') : String(links.job), '.'));
    else if (row.status === 'deferred' && /^(followups|system)$/.test(sc.kind)) out.push(h('p', { class: 'tr-later' }, SF.icon('clock', 'icon-sm'), 'Later: alive here' + (row.due ? ', due ' + dateOnly(row.due) : '') + '.'));
    else if (row.status === 'deferred' && sc.kind === 'job') {   // items.unmoved_later: the follow-ups could not be written then
      out.push(SF.callout('warn', 'alert', 'Later, but not in the follow-ups yet:', ENDED.has(sc.stage)
        ? 'moving it failed when the job ended (the job’s log says why). It moves with the job’s other open items the next time you change one of them; nothing is lost.'
        : 'it moves to the follow-ups with the job’s open items when the job ends. You can reopen it here meanwhile.'));
    }
    if (isTok(String(s.type || ''), 'drift:')) {
      out.push(h('p', { class: 'tr-drift-note' }, SF.icon('eye', 'icon-sm'), 'Drift source ', h('strong', null, String(s.type).slice(6)), s.key ? [' · finding ', h('code', { class: 'tr-mono' }, String(s.key))] : null,
        s.severity ? ' · severity ' + s.severity : null, s.observed_at ? [' · observed ', SF.timeEl(s.observed_at, 'rel')] : null,
        '. It closes on its own, with the observation as evidence, once a fresh check no longer reports it.'));
    }
    if (s.type === 'drift') {
      out.push(h('p', { class: 'tr-drift-note' }, SF.icon('persona', 'icon-sm'), 'Open WebUI’s workspace model ', h('code', { class: 'tr-mono' }, String(s.model || '?')),
        ' changed (' + (s.change || 'unknown') + ')', s.from || s.to ? ': version ' + (s.from || '?') + ' → ' + (s.to || '?') : '', '. ', personaLinks(row)));
    }
    if (isTok(String(row.owner || ''), 'agent:')) {
      out.push(h('p', { class: 'tr-drift-note' }, SF.icon('agents', 'icon-sm'), /^(system|followups)$/.test(sc.kind)
        ? 'An outside agent owns it: its token reads only its own items (what it added and what you gave it).'
        : 'An outside agent owns it, but its token reads only System items and follow-ups, so it cannot see this one: move it to the follow-ups, or give it to someone else.'));
    }
    if ((links.wake || row.verifier === 'code') && !TERMINAL.has(row.status)) out.push(h('p', { class: 'tr-drift-note' }, SF.icon('bell', 'icon-sm'), 'A promised check-in: code closes it when its wake runs; it waits for you only once it is overdue.'));
    return out;
  }
  /** The overflow item (source.type overflow): a group of folded suggestions, with Dismiss all and Show all. */
  function overflowBlock(row, after) {
    const s = row.source || {};
    const folded = lines(row.detail).filter((l) => l.trim()).map((l) => l.replace(/^- /, ''));
    const full = folded.length < (Number(s.count) || 0);
    const list = h('ul', { class: 'tr-folded' }, folded.map((l) => h('li', { class: 'tr-text' }, l)));
    const box = h('section', { class: 'tr-overflow', 'aria-label': 'Folded suggestions' },
      h('p', null, h('strong', null, SF.plural(Number(s.count) || folded.length, 'suggestion')), ' folded together: this scope already holds as many suggestions as it keeps. Dismiss them all, or accept the group and work them one by one.'),
      list);
    const acts = h('div', { class: 'tr-actions' });
    if (row.status === 'proposed') {
      const b = SF.btn('Dismiss all', { cls: 'btn-sm btn-danger', icon: 'x', dataset: { action: 'dismiss-all', focus: 'dismiss-all:' + row.ref } });
      b.addEventListener('click', () => bulkDismiss([row.ref], 'the ' + SF.plural(Number(s.count) || folded.length, 'folded suggestion'), after));
      acts.appendChild(b);
    }
    if (full) {
      const more = SF.btn('Show all ' + s.count, { cls: 'btn-sm', icon: 'list', dataset: { action: 'show-folded', focus: 'show-folded:' + row.ref } });
      more.addEventListener('click', async () => {
        SF.setBusy(more, true);
        try {
          const hist = await SF.api('/api/items/history?ref=' + SF.enc(row.ref));
          const texts = (hist.log || []).map((e) => String(e.note || '')).filter((n) => n.indexOf('folded in: ') === 0).map((n) => n.slice(11));
          SF.clear(list);
          SF.add(list, texts.map((t) => h('li', { class: 'tr-text' }, t)));
          more.remove();
        } catch (e) { SF.toastError('Could not load them', e); SF.setBusy(more, false); }
      });
      acts.appendChild(more);
    }
    box.appendChild(acts);
    return box;
  }
  async function bulkDismiss(refs, what, after) {
    const reason = h('input', { class: 'input', type: 'text', maxlength: 300, value: 'not needed' });
    await SF.openModal({
      title: 'Dismiss ' + what + '?',
      description: 'Each one is dismissed with this reason and stays in the history. A sweep suggestion is not suggested again in its repository.',
      body: SF.field('One reason for all', reason),
      actions: [{ label: 'Cancel', value: 'cancel' }, { label: 'Dismiss all', value: 'ok', cls: 'btn-danger-solid', icon: 'x' }],
      onSubmit: async () => {
        if (!reason.value.trim()) throw new Error('Write a reason.');
        const r = await act('dismiss', { refs, reason: reason.value.trim() });
        const refused = Array.isArray(r && r.refused) ? r.refused : [];
        SF.toast(SF.plural((r && r.items || []).length, 'suggestion') + ' dismissed' + (refused.length ? '; ' + refused.length + ' refused: ' + refused.map((x) => x.ref + ' (' + x.error + ')').join('; ') : '.'), { kind: refused.length ? 'error' : 'ok' });
        changed();
        if (after) after();
      },
    });
  }
  function historyList(log) {
    if (!log.length) return h('p', { class: 'sf-muted' }, 'No changes recorded yet.');
    return h('ol', { class: 'tr-history' }, log.map((e) => h('li', null,
      h('div', { class: 'tr-h-top' }, SF.timeEl(e.at, 'rel'), h('span', { class: 'tr-h-by' }, who(e.by)),
        h('span', { class: 'tr-h-move' }, e.from ? (WORDS[e.from] || e.from) + (e.from !== e.to ? ' → ' + (WORDS[e.to] || e.to) : '') : 'added as ' + (WORDS[e.to] || e.to))),
      // items.add records "added" as the first entry's note when it has none of its own: "added as …" says it.
      e.note && !(!e.from && String(e.note) === 'added') ? h('div', { class: 'tr-text tr-h-note' }, String(e.note)) : null,
      Array.isArray(e.evidence) && e.evidence.length ? h('div', { class: 'tr-h-ev' }, e.evidence.map((x) => String(x.kind) + ' ' + String(x.ref || '').slice(0, 40) + (x.check === 'ok' ? ' ✓' : x.check === 'failed' ? ' ✗' : x.kind === 'command' ? ' (claimed)' : '')).join(' · ')) : null)));
  }
  /** The whole item: banners, the text, the facts, evidence, a person's actions, and (when given) its history. */
  function itemBody(row, ctx) {
    const s = row.source || {};
    const ev = (row.evidence || []).filter((e) => s.type !== 'receipt' || e.kind !== 'receipt');
    const parts = [h('div', { class: 'tr-chips' }, chips(row, { due: true }))].concat(banners(row));
    if (s.type === 'overflow') parts.push(overflowBlock(row, ctx && ctx.after));
    else if (s.type === 'receipt') parts.push(receiptBlock(row));
    else if (Number(s.overflow) > 0) {   // a run's own "N more suggestions from <agent>": accepted or dismissed as one
      const folded = lines(row.detail).filter((l) => l.trim()).map((l) => l.replace(/^- /, ''));
      parts.push(h('section', { class: 'tr-overflow', 'aria-label': 'Suggestions in this group' },
        h('p', null, h('strong', null, SF.plural(Number(s.overflow), 'suggestion')), ' from one run, past its limit of ten, kept together: track or dismiss them as one.'),
        h('ul', { class: 'tr-folded' }, folded.map((l) => h('li', { class: 'tr-text' }, l)))));
    }
    else if (row.detail) parts.push(h('section', { class: 'tr-sec', 'aria-label': 'Detail' }, h('h3', { class: 'tr-h3' }, 'Detail'), textBlock(row.detail)));
    if (s.quote && s.quote !== row.title && s.type !== 'receipt') parts.push(h('section', { class: 'tr-sec' }, h('h3', { class: 'tr-h3' }, 'As written in ' + (s.file || 'the source') + (s.line ? ':' + s.line : '')), h('blockquote', { class: 'tr-quote tr-text' }, String(s.quote))));
    if (ev.length) parts.push(h('section', { class: 'tr-sec', 'aria-label': 'Evidence' }, h('h3', { class: 'tr-h3' }, 'Evidence (' + ev.length + ')'), h('ul', { class: 'tr-evidence' }, ev.map(evidenceRow))));
    else if (row.status === 'done' && s.type !== 'receipt') parts.push(h('p', { class: 'sf-muted' }, 'Marked done without evidence on record.'));
    const actions = actionButtons(row, ctx && ctx.after, ctx && ctx.prefix);
    if (actions) parts.push(h('section', { class: 'tr-sec tr-act-sec', 'aria-label': 'Actions' }, actions));
    else parts.push(h('p', { class: 'sf-muted tr-noact' }, row.status === 'deferred' ? 'Work it where it moved.' : 'Nothing for you to do here.'));
    const facts = h('details', { class: 'tr-facts', open: ctx && ctx.factsOpen }, h('summary', { dataset: { focus: (ctx && ctx.prefix || 'x') + ':facts:' + row.ref } }, SF.icon('chevron', 'icon-xs chev'), 'Facts'), factsKv(row));
    if (ctx && ctx.onFacts) facts.addEventListener('toggle', () => ctx.onFacts(facts.open));
    parts.push(facts);
    if (ctx && ctx.history) parts.push(h('section', { class: 'tr-sec', 'aria-label': 'History' }, h('h3', { class: 'tr-h3' }, 'History, oldest first'), ctx.history));
    return parts;
  }

  // ---------------------------------------------------------------- the list
  function rowEl(row, opts) {
    const sc = scopeOf(row);
    const href = itemHash(row.ref);
    const overflow = (row.source || {}).type === 'overflow';
    const a = h('a', { class: 'tr-row' + (row.waiting ? ' is-waiting' : '') + (overflow ? ' is-group' : '') + (TERMINAL.has(row.status) ? ' is-closed' : ''), href,
      dataset: { ref: row.ref, focus: 'row:' + row.ref, status: row.status } },
    h('span', { class: 'tr-row-top' }, pill(row), h('span', { class: 'tr-row-title' }, String(row.title || row.ref))),
    h('span', { class: 'tr-row-chips' }, chips(row, { due: true })),
    h('span', { class: 'tr-row-meta' }, h('span', { class: 'tr-mono tr-row-ref' }, row.ref), h('span', null, KINDS[row.kind] || row.kind), h('span', null, 'owner ' + who(row.owner)),
      opts && opts.where === false ? null : h('span', null, whereOf(sc)), h('span', null, sourceLabel(row)), SF.timeEl(row.updated_at, 'rel')));
    return h('li', { class: 'tr-li' }, a);
  }
  function listQuery(route) {
    const q = route && route.query ? route.query : {};
    return { scope: SCOPES.some((s) => s[0] === q.scope) ? q.scope || '' : '', status: STATUS_FILTERS.some((s) => s[0] === q.status) ? q.status || '' : '',
      owner: q.owner || '', kind: KINDS[q.kind] ? q.kind : '', source: q.source || '', must: q.must === '1', overdue: q.overdue === '1', q: q.q || '', repo: SF.filterRepo() || '' };
  }
  function listHash(q, patch) {
    const n = Object.assign({}, q, patch || {});
    const parts = [];
    for (const k of ['scope', 'status', 'owner', 'kind', 'source', 'q']) if (n[k]) parts.push(k + '=' + SF.enc(n[k]));
    if (n.must) parts.push('must=1');
    if (n.overdue) parts.push('overdue=1');
    return '#/housekeeping' + (parts.length ? '?' + parts.join('&') : '');
  }
  function filtered(items, q) {
    const text = q.q.trim().toLowerCase();
    return items.filter((r) => {
      if (!q.status && r.status === 'deferred' && !r.waiting && !/^(followups|system)$/.test(scopeOf(r).kind)) return false;   // moved on: shown where it went
      if (q.owner && r.owner !== q.owner) return false;
      if (q.kind && r.kind !== q.kind) return false;
      if (q.source) {
        const t = String((r.source || {}).type || '');
        if (q.source === 'drift-source' ? !isTok(t, 'drift:') : isTok(q.source, 'drift:') ? t !== q.source : t !== q.source) return false;
      }
      if (q.must && !(r.blocking && !TERMINAL.has(r.status))) return false;
      if (q.overdue && !r.overdue) return false;
      if (text && String(r.title || '').toLowerCase().indexOf(text) < 0 && String(r.ref).toLowerCase().indexOf(text) < 0) return false;
      return true;
    });
  }
  function countsBar(c, q) {
    const items = [['waiting', 'waiting for you', { status: 'waiting' }, c.waiting ? 'human' : null], ['open', 'to do or working on it', { status: 'active' }, null],
      ['blocking', 'must-do', { must: true, status: '' }, c.blocking ? 'warn' : null], ['proposed', 'suggested', { status: 'proposed' }, null],
      ['done', 'waiting for a check', { status: 'done' }, c.done ? 'human' : null], ['overdue', 'overdue', { overdue: true, status: '' }, c.overdue ? 'err' : null],
      ['stale', 'stale', null, c.stale ? 'warn' : null], ['closed_7d', 'proven or dropped this week', null, null]];
    return h('ul', { class: 'tr-counts', 'aria-label': 'Counts' }, items.map(([k, label, patch, tone]) => h('li', null,
      h(patch ? 'a' : 'span', { class: 'tr-count' + (tone && c[k] ? ' tone-' + tone : ''), href: patch ? listHash(q, Object.assign({ owner: '', kind: '', source: '', q: '', must: false, overdue: false }, patch)) : null, dataset: { count: k, focus: 'count:' + k } },
        h('strong', null, SF.fmtNum(c[k] || 0)), ' ', label))));
  }
  function scopeTabs(q) {
    return h('nav', { class: 'tr-scopes sf-scroll-x', 'aria-label': 'Scopes' }, SCOPES.map(([id, label]) =>
      h('a', { class: 'tr-scope' + (q.scope === id ? ' is-on' : ''), href: listHash(q, { scope: id }), 'aria-current': q.scope === id ? 'page' : null, dataset: { scope: id || 'all', focus: 'scope:' + (id || 'all') } }, label)));
  }
  function filterBar(q, items) {
    const go = (patch) => SF.go(listHash(q, patch));
    const owners = Array.from(new Set(items.map((r) => r.owner).filter(Boolean).concat(q.owner ? [q.owner] : []))).sort();
    const status = SF.selectEl(STATUS_FILTERS, q.status, { 'aria-label': 'Status', dataset: { filter: 'status', focus: 'f:status' } });
    status.addEventListener('change', () => go({ status: status.value }));
    const owner = SF.selectEl([['', 'Anyone']].concat(owners.map((o) => [o, who(o)])), q.owner, { 'aria-label': 'Owner', dataset: { filter: 'owner', focus: 'f:owner' } });
    owner.addEventListener('change', () => go({ owner: owner.value }));
    const kind = SF.selectEl([['', 'Any kind']].concat(Object.keys(KINDS).map((k) => [k, KINDS[k]])), q.kind, { 'aria-label': 'Kind', dataset: { filter: 'kind', focus: 'f:kind' } });
    kind.addEventListener('change', () => go({ kind: kind.value }));
    const drifts = Array.from(new Set(items.map((r) => String((r.source || {}).type || '')).filter((t) => isTok(t, 'drift:')).concat(isTok(q.source, 'drift:') ? [q.source] : []))).sort();
    const srcOpts = [['', 'Any source']].concat(Object.keys(SOURCES).map((k) => [k, SOURCES[k]]), drifts.map((t) => [t, 'Drift source ' + t.slice(6)]));
    const source = SF.selectEl(srcOpts, q.source, { 'aria-label': 'Source', dataset: { filter: 'source', focus: 'f:source' } });
    source.addEventListener('change', () => go({ source: source.value }));
    const must = h('input', { type: 'checkbox', checked: q.must, dataset: { filter: 'must', focus: 'f:must' } });
    must.addEventListener('change', () => go({ must: must.checked }));
    const overdue = h('input', { type: 'checkbox', checked: q.overdue, dataset: { filter: 'overdue', focus: 'f:overdue' } });
    overdue.addEventListener('change', () => go({ overdue: overdue.checked }));
    const search = h('input', { class: 'input', type: 'search', value: q.q, placeholder: 'Title or reference', 'aria-label': 'Search items', dataset: { filter: 'q', focus: 'f:q' } });
    let timer = 0;
    search.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(() => go({ q: search.value }), 350); });
    const clearAll = q.owner || q.kind || q.source || q.must || q.overdue || q.q || q.status
      ? h('a', { class: 'btn btn-sm btn-ghost', href: listHash({ scope: q.scope }), dataset: { focus: 'f:clear' } }, SF.icon('x', 'icon-sm'), 'Clear filters') : null;
    const active = [q.status, q.owner, q.kind, q.source, q.must, q.overdue, q.q].filter(Boolean).length;
    if (T.filtersOpen === null) T.filtersOpen = !SF.isNarrow() || !!active;
    const box = h('details', { class: 'tr-filter-box', open: T.filtersOpen },
      h('summary', { dataset: { focus: 'f:toggle' } }, SF.icon('chevron', 'icon-xs chev'), 'Filters', active ? h('span', { class: 'count-pill has' }, String(active)) : null),
      h('div', { class: 'tr-filters', role: 'group', 'aria-label': 'Filters' },
        SF.field('Status', status), SF.field('Owner', owner), SF.field('Kind', kind), SF.field('Source', source), SF.field('Search', search),
        h('div', { class: 'tr-checks' }, h('label', { class: 'check' }, must, 'Must-do only'), h('label', { class: 'check' }, overdue, 'Overdue only'), clearAll)));
    box.addEventListener('toggle', () => { T.filtersOpen = box.open; });
    return box;
  }
  function bulkBar(rows) {
    const sugg = rows.filter((r) => r.status === 'proposed' && !r.blocking);
    if (sugg.length < 2) return null;
    const b = SF.btn('Dismiss all ' + sugg.length + ' suggestions shown…', { cls: 'btn-sm', icon: 'x', dataset: { action: 'dismiss-shown', focus: 'dismiss-shown' } });
    b.addEventListener('click', () => bulkDismiss(sugg.slice(0, 300).map((r) => r.ref), SF.plural(Math.min(300, sugg.length), 'suggestion')));
    return h('div', { class: 'tr-bulk' }, h('span', { class: 'sf-muted' }, 'Suggestions are an agent’s words nobody has tracked yet.'), b);
  }
  function addButton(q) {
    const b = SF.btn('Item', { cls: 'btn-sm btn-primary', icon: 'plus', dataset: { action: 'add-item', focus: 'add-item' }, title: 'Add an item to the system scope or a repository’s follow-ups' });
    b.addEventListener('click', () => addItemModal(q.scope === 'followups' ? 'followups' : q.scope === 'system' ? 'system' : ''));
    return b;
  }
  const agentsLink = () => h('a', { class: 'btn btn-sm', href: AGENTS_HASH, dataset: { action: 'outside-agents', focus: 'outside-agents' }, title: 'Tokens for agents you run yourself (Claude Code, Qwen Code…)' },
    SF.icon('agents', 'icon-sm'), 'Outside agents');
  async function addItemModal(prefer, repoHint) {
    const repos = SF.repos().map((r) => (typeof r === 'string' ? r : r && r.name)).filter(Boolean);
    const where = SF.selectEl([['system', 'System (belongs to no repository)']].concat(repos.map((r) => ['repo:' + r, r + ' follow-ups'])),
      repoHint ? 'repo:' + repoHint : prefer === 'followups' && repos.length ? 'repo:' + (SF.filterRepo() || repos[0]) : 'system', { 'aria-label': 'Where' });
    const title = h('input', { class: 'input', type: 'text', maxlength: 200, required: true, placeholder: 'What has to happen, in a few words' });
    const detail = h('textarea', { class: 'textarea', rows: 4, maxlength: 2000, placeholder: 'Optional: details, links as text, how to check it' });
    const due = h('input', { class: 'input', type: 'date' });
    const must = h('input', { type: 'checkbox' });
    const mustRow = h('label', { class: 'check' }, must, 'Must-do (system items)');
    const sync = () => { mustRow.hidden = where.value !== 'system'; };
    where.addEventListener('change', sync);
    sync();
    await SF.openModal({
      title: 'Add an item',
      description: 'You own it and you prove it. A follow-up is due in 14 days unless you pick a date; a system item has no date unless you pick one.',
      body: [SF.field('Where', where), SF.field('Title', title), SF.field('Detail', detail), SF.field('Due date (optional)', due), mustRow],
      dismissable: false,
      actions: [{ label: 'Cancel', value: 'cancel' }, { label: 'Add it', value: 'ok', primary: true, icon: 'plus' }],
      onSubmit: async () => {
        if (!title.value.trim()) { title.focus(); throw new Error('Give it a title.'); }
        const body = { title: title.value.trim() };
        if (detail.value.trim()) body.detail = detail.value.trim();
        if (due.value) body.due = due.value;
        let r;
        if (where.value === 'system') { if (must.checked) body.blocking = true; r = await SF.api('/api/system/items', { method: 'POST', body, retry409: true }); }
        else r = await SF.api('/api/repos/' + SF.enc(where.value.slice(5)) + '/followups', { method: 'POST', body, retry409: true });
        const ref = r && r.item ? r.item.ref : '';
        SF.toast('Added ' + ref + '.', { kind: 'ok', action: ref ? { label: 'Open it', href: itemHash(ref) } : null });
        changed();
      },
    });
  }

  // ---------------------------------------------------------------- drift sources (System tab, §38 and §39's notes)
  async function loadDrift() {
    if (T.driftLoading) return T.driftLoading;
    T.driftLoading = SF.api('/api/drift-sources').then((r) => { T.drift = Array.isArray(r && r.sources) ? r.sources : []; T.driftError = null; })
      .catch((e) => { T.drift = null; T.driftError = e; })
      .finally(() => { T.driftLoading = null; if (T.handle) T.handle.refresh(); });
    return T.driftLoading;
  }
  function refLinks(refs) {
    return (refs || []).map((ref, i) => [i ? ', ' : null, plainHref(itemHash(ref), String(ref), 'tr-mono')]);
  }
  function resultLine(res, lead) {
    if (!SF.isObj(res)) return null;
    const out = [];
    for (const k of ['opened', 'reopened', 'closed']) {
      if (!Array.isArray(res[k])) continue;   // findings "unchanged" or none: nothing was acted on
      const list = res[k];
      out.push(h('span', { class: 'tr-res', dataset: { res: k } }, k + ' ' + list.length, list.length ? [' (', refLinks(list), ')'] : null));
    }
    if (res.findings === null) out.push(h('span', { class: 'tr-res' }, 'inventory only: it says nothing about drift'));
    else if (res.findings === 'unchanged') out.push(h('span', { class: 'tr-res' }, 'nothing new since the last observation'));
    if (res.profiles === 'unchanged') out.push(h('span', { class: 'tr-res' }, 'the role manifest is newer than this observation: kept'));
    else if (typeof res.profiles === 'number') out.push(h('span', { class: 'tr-res' }, 'role manifest written (' + SF.plural(res.profiles, 'role') + ')'));
    if (!out.length) return null;
    return h('p', { class: 'tr-drift-result' }, h('strong', null, lead || 'Last observation acted on: '), out.map((n, i) => [i ? ' · ' : null, n]));
  }
  function driftCard(src) {
    const st = String(src.status || 'never');
    const present = Array.isArray(src.present) ? src.present : [];
    // An ok check of an observation without findings (an inventory only) leaves findings, present and last_result as
    // they were (driftsources._act): only a stored last_result says an observation with findings was acted on, so
    // "no drift" needs it; this browser's own check says whether the newest observation was an inventory only.
    const own = T.driftResults.has(src.name) ? T.driftResults.get(src.name) : null;
    const acted = SF.isObj(src.last_result);
    const inventoryNow = !!own && own.findings === null;
    let verdict;
    let ok = false;
    if (st !== 'ok') verdict = 'Drift unknown: ' + (DRIFT_WORDS[st] || st).toLowerCase() + (st === 'never' ? '' : ', so nothing is known about drift now');
    else if (!acted) verdict = 'Inventory only so far: this source has not reported findings yet, so nothing is known about drift';
    else if (present.length) verdict = SF.plural(present.length, 'finding') + ' reported in the last observation with findings' + (inventoryNow ? ' (the newest was an inventory only)' : '');
    else if (inventoryNow) verdict = 'The newest observation was an inventory only; the last one with findings reported no drift';
    else { verdict = 'No drift reported in the last observation with findings'; ok = true; }
    const check = SF.btn('Check now', { cls: 'btn-sm', icon: 'refresh', dataset: { action: 'drift-check', focus: 'drift-check:' + src.name } });
    check.addEventListener('click', async () => {
      SF.setBusy(check, true);
      try {
        const r = await SF.api('/api/drift-sources/' + SF.enc(src.name) + '/check', { method: 'POST', body: {} });
        if (r && r.status === 'ok' && SF.isObj(r.result)) T.driftResults.set(src.name, r.result);   // a check that read nothing acts on nothing
        else T.driftResults.delete(src.name);
        SF.toast(src.name + ': ' + (DRIFT_WORDS[r && r.status] || (r && r.status) || 'checked') + '.', { kind: r && r.status === 'ok' ? 'ok' : 'info' });
        await loadDrift();
        changed();
      } catch (e) { SF.toastError('Check failed', e); }
      finally { SF.setBusy(check, false); }
    });
    const actions = [check];
    if (src.manifest) {
      const man = SF.btn('Role manifest', { cls: 'btn-sm', icon: 'list', dataset: { action: 'drift-manifest', focus: 'drift-manifest:' + src.name } });
      man.addEventListener('click', () => showManifest(src.name, man));
      actions.push(man);
    }
    const res = own || src.last_result;
    return h('article', { class: 'sf-card tr-drift', dataset: { source: src.name, status: st } },
      h('div', { class: 'tr-drift-head' }, h('h3', { class: 'tr-h3' }, String(src.name)), h('span', { class: 'pill tone-' + (DRIFT_TONE[st] || 'muted') }, DRIFT_WORDS[st] || st)),
      src.detail ? h('p', { class: 'tr-text tr-drift-detail' }, String(src.detail)) : null,
      h('p', { class: 'tr-drift-verdict' + (ok ? ' is-ok' : '') }, verdict),
      h('dl', { class: 'sf-kv' },
        h('dt', null, 'Observed'), h('dd', null, src.observed_at ? SF.timeEl(src.observed_at, 'rel') : '—'),
        h('dt', null, 'Next check'), h('dd', null, src.next_at ? whenEl(src.next_at) : '—'),
        h('dt', null, 'Findings'), h('dd', null, present.length ? present.map((k, i) => [i ? ', ' : null, h('code', { class: 'tr-mono' }, String(k))]) : 'none present'),
        h('dt', null, 'Items'), h('dd', null, (src.items || []).length ? refLinks(src.items) : 'none'),
        h('dt', null, 'Source'), h('dd', { class: 'tr-mono' }, String(src.url || '') + (src.host_header ? ' (Host ' + src.host_header + ')' : ''))),
      res ? resultLine(res, own ? 'Your check: ' : 'Last observation acted on: ') : null,
      h('div', { class: 'tr-actions' }, actions));
  }
  async function showManifest(name, button) {
    SF.setBusy(button, true);
    let r = null;
    try { r = await SF.api('/api/drift-sources/' + SF.enc(name) + '/manifest'); }
    catch (e) { SF.toastError('Could not load the role manifest', e); return; }
    finally { SF.setBusy(button, false); }
    const m = r && r.manifest;
    const roles = m && Array.isArray(m.roles) ? m.roles : [];
    const cols = ['base_model_id', 'provider_kind', 'provider_model_id', 'route_kind', 'version_id', 'tool_packages', 'filter_packages', 'limitations'];
    const table = h('div', { class: 'sf-scroll-x' }, h('table', { class: 'table tr-table' },
      h('thead', null, h('tr', null, ['Base model', 'Provider kind', 'Provider model', 'Route', 'Version', 'Tools', 'Filters', 'Limitations'].map((t) => h('th', null, t)))),
      h('tbody', null, roles.map((role) => h('tr', null, cols.map((c) => h('td', { class: c === 'limitations' ? '' : 'tr-mono' },
        Array.isArray(role[c]) ? role[c].join('; ') : role[c] === null || role[c] === undefined ? '—' : String(role[c]))))))));
    await SF.openModal({
      title: 'Role manifest of ' + name, wide: true,
      description: m ? 'Generated from the drift source’s inventory, never applied: read-only. Observed ' + (m.observed_at || '?') + '.' : 'No role manifest yet.',
      body: m ? [table, m.note ? h('p', { class: 'sf-muted tr-text' }, String(m.note)) : null] : null,
      actions: [{ label: 'Close', value: 'cancel' }],
    });
  }
  function driftPanel() {
    if (!T.drift && !T.driftError) { if (!T.driftLoading) loadDrift(); return h('section', { class: 'tr-drift-sec' }, SF.skeleton(3)); }
    if (T.driftError) return h('section', { class: 'tr-drift-sec' }, SF.unavailable(T.driftError, 'Drift sources'));
    return h('section', { class: 'tr-drift-sec', 'aria-labelledby': 'tr-drift-t' },
      h('h2', { class: 'tr-h2', id: 'tr-drift-t' }, 'Drift sources', h('span', { class: 'tag tag-warn', title: 'The source contract (DESIGN §38) is a proposal: its fields may still change' }, 'Contract proposed')),
      h('p', { class: 'sf-muted' }, 'Outside, read-only checks of what is deployed against what should be. Their findings become System items, and close only when a fresh, readable observation no longer reports them.'),
      T.drift.length ? h('div', { class: 'sf-cards' }, T.drift.map(driftCard)) : h('p', { class: 'sf-muted' }, 'None configured (Settings: drift_sources).'));
  }

  // ---------------------------------------------------------------- the view
  function viewProcedure() {
    return h('section', { class: 'wz-proc-card tr-proc', 'aria-labelledby': 'tr-proc-t' }, h('h2', { class: 'wz-proc-title', id: 'tr-proc-t' }, 'How the tracker works'),
      SF.procedureList([
        ['Start with Waiting for you.', 'Suggestions to accept or dismiss, must-do items on a stopped job, checks only you can make and anything overdue come first.'],
        ['Open an item to act on it.', 'Each item shows where it came from, its evidence (a claimed command is never re-run) and only the buttons the rules give a person.'],
        ['Prove it or move it on.', 'An item closes only with a check by someone other than its owner; Later moves a job’s item to the repository’s follow-ups, so nothing stays inside a finished job.'],
      ]));
  }
  function renderList(root, route, handle) {
    // Behind an item's panel the list stays the one it was opened from (and closing the panel goes back to it).
    const onItem = !!(route.parts && route.parts.length);
    const q = onItem && T.lastQ ? Object.assign({}, T.lastQ, { repo: SF.filterRepo() || '' }) : listQuery(route);
    const key = [q.scope, q.status, q.repo].join('|');
    if (T.listKey !== key) {
      T.listKey = key;
      T.error = null;
      T.loading = fetchList(key, q).catch((e) => { if (T.listKey === key) T.error = e; })
        .finally(() => { if (T.listKey === key) { T.loading = null; handle.refresh(); } });
    }
    const head = SF.viewHead('Housekeeping Tracker', 'Everything the agents promised, found or were asked to do, until it is proven done.', h('div', { class: 'sf-row' }, agentsLink(), addButton(q)));
    const nodes = [head];
    if (q.repo && q.scope !== 'system') nodes.push(h('p', { class: 'tr-repo-note' }, SF.icon('info', 'icon-sm'), 'Showing ' + q.repo + ' only (the repository filter at the top); System items belong to no repository.'));
    nodes.push(scopeTabs(q));
    if (T.list && T.list.key === key) nodes.push(countsBar(T.list.counts, q));   // counts of this scope (and repo), before the filters
    if (q.scope === 'system') nodes.push(driftPanel());
    const items = T.list && T.list.key === key ? T.list.items : [];
    nodes.push(filterBar(q, items));
    if (T.error && !T.list) nodes.push(SF.unavailable(T.error, 'The tracker'));
    else if (!T.list || T.list.key !== key) nodes.push(SF.skeleton(8));
    else {
      const rows = filtered(items, q);
      const waiting = rows.filter((r) => r.waiting);
      const rest = rows.filter((r) => !r.waiting);
      nodes.push(h('p', { class: 'tr-shown', role: 'status' }, 'Showing ' + SF.plural(rows.length, 'item') + (T.list.total > items.length ? ' of the first ' + items.length + ' (' + T.list.total + ' match)' : '') + '.'));
      const bulk = bulkBar(rows);
      if (bulk) nodes.push(bulk);
      const filtering = !!(q.status || q.owner || q.kind || q.source || q.must || q.overdue || q.q);
      if (!rows.length && filtering) nodes.push(SF.emptyState('checklist', 'Nothing here', q.status === 'waiting' ? 'Nothing waits for you right now.' : 'No item matches these filters.', [h('a', { class: 'btn btn-sm', href: listHash({ scope: q.scope }) }, 'Clear filters')], true));
      else if (!rows.length) nodes.push(SF.emptyState('checklist', 'No open items yet', 'Acceptance criteria, review findings and the agents’ promises appear here as jobs, chats and plans run; add your own with + Item.', null, true));
      if (waiting.length) nodes.push(h('section', { class: 'tr-group', 'aria-labelledby': 'tr-g-wait' }, h('h2', { class: 'tr-h2', id: 'tr-g-wait' }, 'Waiting for you (' + waiting.length + ')'), h('ul', { class: 'tr-list' }, waiting.map((r) => rowEl(r)))));
      if (rest.length) nodes.push(h('section', { class: 'tr-group', 'aria-labelledby': 'tr-g-rest' }, h('h2', { class: 'tr-h2', id: 'tr-g-rest' }, waiting.length ? 'Everything else (' + rest.length + ')' : 'Items (' + rest.length + ')'), h('ul', { class: 'tr-list' }, rest.map((r) => rowEl(r)))));
    }
    nodes.push(viewProcedure());
    SF.rebuild(root, nodes);
    const tabs = root.querySelector('.tr-scopes');
    if (tabs) SF.trackFades(tabs);
    const search = root.querySelector('[data-focus="f:q"]');
    if (search && root.ownerDocument.activeElement === search) search.setSelectionRange(search.value.length, search.value.length);
    if (!onItem) { T.lastList = listHash(q); T.lastQ = q; }
  }
  /** #/housekeeping/<ref>: the item's panel over the list. The data shown stays while it is read again (after an
   *  action, or an items.updated of its own scope only), and the panel is drawn again only when what it shows
   *  changed, keeping keyboard focus (openKept). */
  const PANEL = { ref: null, sig: '', factsOpen: null };
  const PANEL_FIRST = '#drawer .tr-act-sec .tr-actions button:not([disabled]), #drawer .tr-act-sec .tr-actions a';
  function loadHistory(ref) {
    let e = T.history.get(ref);
    if (!e) { e = { data: null, error: null, stale: true, gen: 0, loading: false, again: false }; T.history.set(ref, e); }
    if (e.loading) { e.again = true; return; }
    e.loading = true;
    e.stale = false;
    const gen = e.gen;
    SF.api('/api/items/history?ref=' + SF.enc(ref)).then((d) => { e.data = d; e.error = null; if (e.gen !== gen) e.stale = true; })
      .catch((err) => { e.error = err; if (err && err.status === 404) e.data = null; })
      .finally(() => {
        e.loading = false;
        if (T.panelRef !== ref) return;
        if ((e.stale || e.again) && !e.error) { e.again = false; loadHistory(ref); }
        drawItemPanel(ref);
      });
  }
  function drawItemPanel(ref) {
    const now = SF.route();   // an answer that comes back after the route moved on opens nothing
    if (T.panelRef !== ref || now.view !== 'tracker' || (now.parts || []).join('/') !== ref) return;
    const e = T.history.get(ref) || {};
    const data = e.data || null;
    const err = data ? null : e.error || null;
    const row = data && data.item;
    const linked = row && (row.links || {}).job && scopeOf(row).repo ? SF.job(scopeOf(row).repo, row.links.job) : null;
    const sig = [err ? 'e' + err.status + ':' + err.message : data ? JSON.stringify(data) : '…', SF.isNarrow(), linked ? linked.stage : ''].join('|');
    if (sig === PANEL.sig && SF.drawer.isOpen('item:' + ref)) return;   // nothing new: the panel stays as it is
    PANEL.sig = sig;
    const back = T.lastList || '#/housekeeping';
    const copy = h('button', { type: 'button', class: 'btn btn-ghost btn-icon btn-xs', 'aria-label': 'Copy the reference', title: 'Copy ' + ref, dataset: { focus: 'copy-ref' } }, SF.icon('copy', 'icon-xs'));
    copy.addEventListener('click', () => SF.copyText(ref, 'Copied ' + ref));
    const eyebrow = [row ? pill(row) : null, h('code', { class: 'tr-mono tr-eyebrow-ref' }, ref), copy];
    const after = () => { const c = T.history.get(ref); if (c) { c.stale = true; c.gen++; } loadHistory(ref); };
    const factsOpen = PANEL.factsOpen === null ? !SF.isNarrow() : PANEL.factsOpen;
    const body = err ? [err.status === 404 ? SF.emptyState('alert', 'No such item here', String(err.message || ''), null, true) : SF.unavailable(err, 'This item')] : !row ? [SF.skeleton(6)]
      : itemBody(row, { history: historyList(data.log || []), after, factsOpen, prefix: 'p', onFacts: (open) => { PANEL.factsOpen = open; } });
    const sc = row ? scopeOf(row) : {};
    openKept({ key: 'item:' + ref, title: row ? String(row.title || ref) : err ? 'Item not found' : 'Loading…', eyebrow,
      sub: row ? (KINDS[row.kind] || row.kind) + ' · ' + whereOf(sc) + ' · owner ' + who(row.owner) : null, body, back, wide: false }, PANEL_FIRST);
  }
  function openItemPanel(route) {
    if (!route || route.view !== 'tracker' || !route.parts || !route.parts.length) return;
    if (route.parts.length === 1 && route.parts[0] === 'agents') { T.panelRef = null; openAgentsPanel(SF.drawer.isOpen('tracker:agents')); return; }
    const ref = route.parts.join('/');
    if (PANEL.ref !== ref) { PANEL.ref = ref; PANEL.sig = ''; PANEL.factsOpen = null; }
    T.panelRef = ref;
    const e = T.history.get(ref);
    drawItemPanel(ref);
    if (!e || e.stale || (!e.data && !e.loading)) loadHistory(ref);
  }

  // ---------------------------------------------------------------- outside agents: tokens and "Connect your own agents" (§32)
  const AGENTS_HASH = '#/housekeeping/agents';
  const TOKEN_NAME_RE = /^[a-z0-9][a-z0-9_-]{0,31}$/;   // housekeeping.TOKEN_NAME_RE
  const AG = { data: null, error: null, loading: null, made: null, busy: false };
  function loadAgents() {
    if (AG.loading) return AG.loading;
    AG.loading = SF.api('/api/agent-tokens').then((r) => { AG.data = { tokens: Array.isArray(r && r.tokens) ? r.tokens : [], connect: SF.isObj(r && r.connect) ? r.connect : {} }; AG.error = null; })
      .catch((e) => { AG.error = e; }).finally(() => { AG.loading = null; drawAgents(); });
    return AG.loading;
  }
  /** The MCP server entry for Claude Code or Qwen Code (.mcp.json / settings.json), built from `connect`. */
  function mcpEntry(connect, token) {
    const env = {};
    env[connect.token_env || 'SF_ITEMS_TOKEN'] = token;
    env[connect.url_env || 'SF_ITEMS_URL'] = String(connect.url || '');
    return JSON.stringify({ mcpServers: { 'sf-items': { command: String(connect.sf_items || 'sf-items'), args: Array.isArray(connect.mcp_args) ? connect.mcp_args.map(String) : ['mcp'], env } } }, null, 2);
  }
  const AGENTS_LINE = 'Record anything you leave for later with `sf-items add`; mark it done with evidence (`sf-items done <ref> --commit <sha>` or `--command <cmd> --result <what it printed>`).';
  function copyBlock(label, text, key) {
    const b = SF.btn('Copy', { cls: 'btn-xs', icon: 'copy', dataset: { action: 'copy-' + key, focus: 'copy-' + key } });
    b.addEventListener('click', () => SF.copyText(text, label + ' copied'));
    return h('div', { class: 'tr-copy', dataset: { block: key } }, h('div', { class: 'tr-copy-head' }, h('span', null, label), b), h('pre', { class: 'code-block tr-copy-pre' }, text));
  }
  function agentsBody() {
    const intro = [
      h('p', { class: 'tr-agents-lead' }, 'Agents you run yourself (Claude Code, Qwen Code, a script) can keep their promises here with ', h('code', null, 'sf-items'), ', so nothing they leave for later is forgotten when their context moves on.'),
      SF.callout('info', 'eye', 'What a token reads:', 'only its own items among the System items and the follow-ups: what it added and what you gave it. Never your other items, nor a job’s, chat’s or plan’s, nor anything a persona or a private turn wrote, since outside agents may run on hosted models. It can add items and mark its own done with evidence; it can never prove, drop or unblock anything.'),
      SF.procedureList([
        ['Make a token.', 'Name it after the agent (qwen-code, claude-laptop). The token is shown once; only its fingerprint is kept here.'],
        ['Put it in the agent’s setup.', 'Copy the MCP server entry into the agent’s settings and the line into its AGENTS.md or QWEN.md.'],
        ['Give it items.', 'On a System item or a follow-up, Change owner… → agent <name>; its token then lists that item too. A job’s, chat’s or plan’s item moves to the follow-ups first (Move to follow-ups…).'],
      ]),
    ];
    const parts = [intro];
    if (AG.made) {
      const c = AG.data ? AG.data.connect : {};
      parts.push(h('section', { class: 'tr-made', 'aria-labelledby': 'tr-made-t' },
        h('h3', { class: 'tr-h3', id: 'tr-made-t' }, SF.icon('shield', 'icon-sm'), 'Token for ' + AG.made.name),
        SF.callout('warn', 'alert', 'Copy it now:', 'it is never shown again. If it is lost, revoke it and make a new one.'),
        copyBlock('The token', AG.made.token, 'token'),
        copyBlock('MCP server entry (Claude Code .mcp.json, Qwen Code settings.json)', mcpEntry(c, AG.made.token), 'mcp'),
        copyBlock('A line for its AGENTS.md or QWEN.md', AGENTS_LINE, 'line'),
        copyBlock('Or in a shell, for the sf-items command', 'export ' + (c.token_env || 'SF_ITEMS_TOKEN') + '=' + AG.made.token + '\nexport ' + (c.url_env || 'SF_ITEMS_URL') + '=' + String(c.url || ''), 'shell'),
        h('div', { class: 'tr-actions' }, SF.btn('I have copied it', { cls: 'btn-sm', icon: 'check', dataset: { action: 'token-done', focus: 'token-done' }, onClick: () => { AG.made = null; drawAgents(); } }))));
    }
    if (AG.error && !AG.data) parts.push(SF.unavailable(AG.error, 'Agent tokens'));
    else if (!AG.data) parts.push(SF.skeleton(4));
    else {
      const tokens = AG.data.tokens;
      parts.push(h('section', { class: 'tr-sec', 'aria-labelledby': 'tr-tokens-t' }, h('h3', { class: 'tr-h3', id: 'tr-tokens-t' }, 'Tokens (' + tokens.length + ')'),
        tokens.length ? h('ul', { class: 'tr-list tr-tokens' }, tokens.map((t) => {
          const name = String(t.name || '');
          const rv = SF.btn('Revoke…', { cls: 'btn-xs btn-danger', icon: 'ban', dataset: { action: 'revoke-token', focus: 'revoke:' + name } });
          rv.addEventListener('click', () => revokeToken(name));
          return h('li', { class: 'tr-li tr-token', dataset: { token: name } },
            h('div', { class: 'tr-token-main' }, h('span', { class: 'tr-token-name' }, 'agent ' + name),
              h('span', { class: 'tr-row-meta' }, h('span', null, 'made ', SF.timeEl(t.created, 'rel')), h('span', null, t.last_used ? ['last used ', SF.timeEl(t.last_used, 'rel')] : 'never used'),
                h('a', { href: '#/housekeeping?owner=' + SF.enc('agent:' + name) + '&status=all' }, 'its items'))),
            rv);
        })) : h('p', { class: 'sf-muted' }, 'No outside agent has a token yet.')));
      const name = h('input', { class: 'input', type: 'text', maxlength: 32, placeholder: 'qwen-code', autocomplete: 'off', spellcheck: 'false', 'aria-describedby': 'tr-token-hint', dataset: { focus: 'token-name' } });
      const err = h('p', { class: 'form-error', role: 'alert', hidden: true });
      const make = SF.btn('Make a token', { cls: 'btn-sm btn-primary', icon: 'plus', dataset: { action: 'make-token', focus: 'make-token' } });
      const submit = async () => {
        const v = name.value.trim();
        err.hidden = true;
        if (!TOKEN_NAME_RE.test(v)) { err.textContent = 'A token name is 1-32 lower-case letters, digits, “-” or “_”, starting with a letter or digit (e.g. qwen-code).'; err.hidden = false; name.focus(); return; }
        SF.setBusy(make, true);
        try {
          const r = await SF.api('/api/agent-tokens', { method: 'POST', body: { name: v } });
          AG.made = { name: String(r.name || v), token: String(r.token || '') };
          if (AG.data && SF.isObj(r.connect)) AG.data.connect = r.connect;
          T.tokens = null;
          SF.toast('Token made for ' + AG.made.name + ': copy it now.', { kind: 'ok' });
          await loadAgents();
          const first = document.querySelector('#drawer [data-focus="copy-token"]');
          if (first) first.focus();
        } catch (e) { err.textContent = String(e && e.message ? e.message : e); err.hidden = false; SF.setBusy(make, false); }
      };
      make.addEventListener('click', submit);
      name.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); submit(); } });
      parts.push(h('section', { class: 'tr-sec tr-make', 'aria-labelledby': 'tr-make-t' }, h('h3', { class: 'tr-h3', id: 'tr-make-t' }, 'Make a token'),
        h('div', { class: 'tr-make-row' }, SF.field('Agent name', name, null), make),
        h('p', { class: 'sf-muted', id: 'tr-token-hint' }, 'Lower-case letters, digits, “-” and “_”. Its items show their owner as “agent <name>”.'), err));
    }
    return parts;
  }
  async function revokeToken(name) {
    await SF.confirmModal({
      title: 'Revoke the token of ' + name + '?',
      description: 'The agent can no longer reach the tracker with it. Its items stay, still owned by agent ' + name + ': give them to someone else, or make a new token with the same name.',
      confirm: 'Revoke', cls: 'btn-danger-solid', icon: 'ban',
      onSubmit: async () => {
        const r = await SF.api('/api/agent-tokens/' + SF.enc(name) + '/revoke', { method: 'POST', body: {} });
        AG.data = { tokens: Array.isArray(r && r.tokens) ? r.tokens : [], connect: SF.isObj(r && r.connect) ? r.connect : (AG.data ? AG.data.connect : {}) };
        if (AG.made && AG.made.name === name) AG.made = null;
        T.tokens = null;
        SF.toast('Revoked the token of ' + name + '.', { kind: 'ok' });
        drawAgents();
      },
    });
  }
  function drawAgents() {
    if (!SF.drawer.isOpen('tracker:agents')) return;
    openAgentsPanel(true);
  }
  function openAgentsPanel(again) {
    if (!again && !AG.loading) loadAgents();
    SF.drawer.open({ key: 'tracker:agents', title: 'Connect your own agents', eyebrow: [h('span', { class: 'tag tag-outline' }, SF.icon('agents', 'icon-xs'), 'Outside agents')],
      sub: 'Agent tokens for sf-items and its MCP server', body: agentsBody(), back: T.lastList || '#/housekeeping',
      onClose: () => { AG.made = null; } });
  }

  // ---------------------------------------------------------------- the job drawer's Items tab
  const JT = new Map();   // job key → {data, error, loading, stale, asm, asmSig, open: Set of refs}
  const JTAB = { panel: null, ref: null };
  function jobState(key) {
    let st = JT.get(key);
    if (!st) { st = { data: null, error: null, loading: null, stale: true, asm: null, asmErr: null, asmSig: '', asmLoading: null, open: new Set(), hist: new Map(), sel: new Set(), closedOpen: false }; JT.set(key, st); }
    return st;
  }
  function loadJobItems(repo, id) {
    const key = repo + '/' + id;
    const st = jobState(key);
    if (st.loading) return st.loading;
    st.stale = false;
    st.loading = SF.api('/api/jobs/' + SF.enc(repo) + '/' + SF.enc(id) + '/items').then((r) => { st.data = r; st.error = null; st.hist.clear(); })
      .catch((e) => { st.error = e; })
      .finally(() => { st.loading = null; rerenderJobTab(key); SF.refreshJobs(); });
    return st.loading;
  }
  /** GET .../assumptions: fetched when the drawer opens, after the spec stage, and when job.confirmed changes. */
  function asmSig(job) { return job ? [job.stage, job.confirmed && job.confirmed.at, job.items && job.items.v].join('|') : ''; }
  function loadAssumptions(job, force) {
    const key = job.repo + '/' + job.id;
    if (!SF.job(job.repo, job.id)) return null;   // gone from the board (an event replayed after its removal)
    const st = jobState(key);
    const sig = asmSig(job);
    if (st.asmLoading || (!force && st.asmSig === sig)) return st.asmLoading;
    st.asmSig = sig;
    st.asmLoading = SF.api('/api/jobs/' + SF.enc(job.repo) + '/' + SF.enc(job.id) + '/assumptions').then((r) => { st.asm = r; st.asmErr = null; })
      .catch((e) => { st.asm = null; st.asmErr = e; })
      .finally(() => { st.asmLoading = null; rerenderJobTab(key); SF.refreshJobs(); });
    return st.asmLoading;
  }
  /** Draw the open Items tab again (only when `key`, if given, is its job). */
  function rerenderJobTab(key) {
    if (!JTAB.panel || !JTAB.panel.isConnected || !JTAB.ref) return;
    if (key && key !== JTAB.ref.repo + '/' + JTAB.ref.id) return;
    renderJobTab(JTAB.panel, SF.job(JTAB.ref.repo, JTAB.ref.id), JTAB.ref);
  }
  /** assumptions.normalize (ASCII spacing, as its re.ASCII patterns): no heading mark, list marker, [x] box or bold. */
  function normLine(s) {
    const W = '[ \\t\\n\\r\\f\\v]';
    return String(s || '').replace(/[\r\n]+$/, '').replace(new RegExp('^' + W + '*#+' + W + '+'), '').replace(new RegExp('^' + W + '*(?:[-*+]|[0-9]+[.)])' + W + '+'), '')
      .replace(new RegExp('^\\[[xX ]\\]' + W + '+'), '').replace(/\*\*/g, '').replace(new RegExp(W + '+', 'g'), ' ').replace(new RegExp('^' + W + '+|' + W + '+$', 'g'), '');
  }
  /** assumptions.answers: the marked line (normalized) is the decision item's quote, or held in it. */
  const answers = (line, quote) => { const t = normLine(quote); return !!line && (t === line || t.indexOf(line) >= 0); };
  /** The decision item a marked line answers: a live one first, else a dropped one (a person's No). */
  function decisionFor(line, items) {
    const all = items.filter((r) => r.kind === 'decision' && (r.source || {}).type === 'spec' && answers(line, (r.source || {}).quote || r.title));
    return all.find((r) => r.status !== 'dropped') || all[0] || null;
  }
  /** The marked lines still waiting for an answer: not confirmed, and not answered No (their decision item dropped).
   *  Factory.confirm(all) would confirm a refused line too (_answer_decisions skips closed items), so a No is kept
   *  out of Confirm all and of the counts. null until both the assumptions and the items are read. */
  function unansweredLines(st) {
    if (!st || !st.asm || !Array.isArray(st.asm.unconfirmed) || !st.data || !Array.isArray(st.data.items)) return null;
    return st.asm.unconfirmed.filter((line) => { const it = decisionFor(line, st.data.items); return !(it && it.status === 'dropped'); });
  }
  function decisionsPanel(job, st, items) {
    const asm = st.asm;
    if (!asm || !Array.isArray(asm.marked) || !asm.marked.length) return null;
    const open = new Set(Array.isArray(asm.unconfirmed) ? asm.unconfirmed : asm.marked);   // the server's own comparison
    const allowed = !CONFIRM_CLOSED.has(job.stage);
    const boxes = [];
    let refusedN = 0;
    const rows = asm.marked.map((line, i) => {
      const done = !open.has(line);
      const item = decisionFor(line, items);
      const refused = !done && !!item && item.status === 'dropped';
      if (refused) refusedN++;
      const lineEl = h('span', { class: 'tr-text tr-line' }, String(line));
      let control;
      if (done) control = h('span', { class: 'tr-confirmed', title: 'Confirmed by a person' }, SF.icon('check', 'icon-sm'), h('span', { class: 'sr-only' }, 'Confirmed: '));
      else if (refused) control = h('span', { class: 'tr-refused', title: 'Answered no by a person' }, SF.icon('ban', 'icon-sm'), h('span', { class: 'sr-only' }, 'Answered no: '));
      else {
        control = h('input', { type: 'checkbox', disabled: !allowed, checked: st.sel.has(line), 'aria-label': 'Confirm: ' + line, dataset: { line: String(i), focus: 'confirm-line:' + i } });
        boxes.push([control, line]);
      }
      const extra = [];
      if (item) {
        const show = h('button', { type: 'button', class: 'btn btn-ghost btn-xs tr-mono tr-line-ref', title: 'Show ' + item.id + ' below', dataset: { action: 'show-decision', focus: 'show-decision:' + i } }, item.id);
        show.addEventListener('click', () => {
          st.open.add(item.ref);
          rerenderJobTab();
          const el = JTAB.panel && JTAB.panel.querySelector('[data-focus="jrow:' + item.ref + '"]');
          if (el) { el.focus(); el.scrollIntoView({ block: 'nearest' }); }
        });
        extra.push(show);
      }
      else extra.push(h('span', { class: 'sf-muted tr-line-ref', title: 'A heading-style mark: confirmable, but no decision item' }, 'no item'));
      if (refused) extra.push(h('span', { class: 'tr-refused-word' }, 'Answered no' + (item.reason ? ': ' : ''), item.reason ? h('span', { class: 'tr-text' }, String(item.reason)) : null));
      if (((done && item && item.status === 'verified') || refused) && allowed) {
        const re = SF.btn(refused ? 'Reopen to change…' : 'Reopen…', { cls: 'btn-xs', icon: 'rework', dataset: { action: 'reopen-decision', focus: 'reopen-decision:' + i } });
        re.addEventListener('click', () => runAction({ id: 'reopen', label: 'Reopen…', icon: 'rework' }, item, re, () => loadAssumptions(job, true)));
        extra.push(re);
      }
      const settled = done || refused;
      return h('li', { class: 'tr-decision' + (done ? ' is-done' : '') + (refused ? ' is-refused' : ''), dataset: { line: String(i) } }, settled ? control : h('label', { class: 'tr-decision-label' }, control, lineEl), settled ? lineEl : null, h('span', { class: 'tr-decision-extra' }, extra));
    });
    const sel = SF.btn('Confirm selected', { cls: 'btn-sm btn-primary', icon: 'check', disabled: true, dataset: { action: 'confirm-selected', focus: 'confirm-selected' } });
    const all = SF.btn('Confirm all (' + boxes.length + ')', { cls: 'btn-sm', icon: 'check-circle', disabled: !boxes.length || !allowed, dataset: { action: 'confirm-all', focus: 'confirm-all' } });
    const sync = () => { sel.disabled = !allowed || !boxes.some(([b]) => b.checked); };
    for (const [b, line] of boxes) b.addEventListener('change', () => { if (b.checked) st.sel.add(line); else st.sel.delete(line); sync(); });
    for (const line of Array.from(st.sel)) if (!boxes.some(([, l]) => l === line)) st.sel.delete(line);   // confirmed meanwhile
    sync();
    const send = async (body, button) => {
      SF.setBusy(button, true);
      try {
        const r = await SF.api('/api/jobs/' + SF.enc(job.repo) + '/' + SF.enc(job.id) + '/confirm', { method: 'POST', body, retry409: true });
        st.asm = r && r.assumptions ? r.assumptions : st.asm;
        st.sel.clear();
        SF.toast('Confirmed. The approver reads your answer; the matching decision items are proven.', { kind: 'ok' });
        changed();
        loadJobItems(job.repo, job.id);
      } catch (e) { SF.toastError('Not confirmed', e); SF.setBusy(button, false); }
    };
    sel.addEventListener('click', () => send({ lines: boxes.filter(([b]) => b.checked).map(([, l]) => l) }, sel));
    // {all: true} would confirm a line answered No too: then only the lines still waiting are named.
    all.addEventListener('click', () => send(refusedN ? { lines: boxes.map(([, l]) => l) } : { all: true }, all));
    const waiting = boxes.length;
    return h('section', { class: 'tr-decisions', 'aria-labelledby': 'tr-dec-t', dataset: { unconfirmed: String(waiting) } },
      h('h3', { class: 'tr-h3', id: 'tr-dec-t' }, SF.icon('user', 'icon-sm'), 'Product decisions', h('span', { class: 'count-pill' + (waiting ? ' has' : '') }, String(waiting))),
      h('p', { class: 'sf-muted' }, waiting ? 'A person must answer these: the spec marks them (product). Confirming a line proves its decision item; No on the item drops it; reopening it withdraws either answer.'
        : refusedN ? 'Every marked line is answered: confirmed, or answered no (reopen a No to change it).' : 'Every marked line is confirmed.'),
      allowed ? null : h('p', { class: 'sf-muted' }, 'This job is ' + job.stage + ': its answers can no longer change.'),
      h('ul', { class: 'tr-decision-list' }, rows),
      boxes.length && allowed ? h('div', { class: 'tr-actions' }, sel, all) : null);
  }
  /** Must-do items that hold the gate on a job a person may approve: the drawer's Approve lists them and moves them,
   *  unproven, to the follow-ups once the box is ticked (approveField). */
  function carryPanel(job, st, items) {
    const stage = job.stage;
    const canApprove = stage === 'needs-human' || stage === 'delivered' || (stage === 'failed' && job.failed_step === 'merge');
    const holding = items.filter(holdsGate);
    if (!canApprove || !holding.length) return null;
    return h('section', { class: 'tr-carry' }, SF.callout('human', 'alert', SF.plural(holding.length, 'must-do item') + (holding.length === 1 ? ' is' : ' are') + ' not proven.',
      'Prove ' + (holding.length === 1 ? 'it' : 'them') + ' below, or approve anyway: Approve, at the top, lists ' + (holding.length === 1 ? 'it' : 'them') + ' and moves ' + (holding.length === 1 ? 'it' : 'them') + ', unproven, to ' + job.repo + '’s follow-ups once you tick the box. Nothing is waived silently.'));
  }
  async function addJobItem(job) {
    const title = h('input', { class: 'input', type: 'text', maxlength: 200, placeholder: 'What has to happen, in a few words' });
    const detail = h('textarea', { class: 'textarea', rows: 3, maxlength: 2000, placeholder: 'Optional' });
    const must = h('input', { type: 'checkbox' });
    await SF.openModal({
      title: 'Add an item to ' + job.id,
      description: 'The builder owns it and the approver checks it, unless you change that later.',
      body: [SF.field('Title', title), SF.field('Detail', detail), h('label', { class: 'check' }, must, 'Must be done before merge')],
      dismissable: false,
      actions: [{ label: 'Cancel', value: 'cancel' }, { label: 'Add it', value: 'ok', primary: true, icon: 'plus' }],
      onSubmit: async () => {
        if (!title.value.trim()) { title.focus(); throw new Error('Give it a title.'); }
        const body = { title: title.value.trim() };
        if (detail.value.trim()) body.detail = detail.value.trim();
        if (must.checked) body.blocking = true;
        const r = await SF.api('/api/jobs/' + SF.enc(job.repo) + '/' + SF.enc(job.id) + '/items', { method: 'POST', body, retry409: true });
        SF.toast('Added ' + (r && r.item ? r.item.ref : 'it') + '.', { kind: 'ok' });
        changed();
        loadJobItems(job.repo, job.id);
      },
    });
  }
  function jobItemRow(row, st, job) {
    return inlineRow(row, st, () => rerenderJobTab(), () => loadJobItems(job.repo, job.id), 'j');
  }
  function renderJobTab(panel, job, ref) {
    JTAB.panel = panel;
    JTAB.ref = ref;
    if (!job) { SF.rebuild(panel, SF.skeleton(5)); return; }
    const key = job.repo + '/' + job.id;
    const st = jobState(key);
    if (!st.data && !st.loading && !st.error) loadJobItems(job.repo, job.id);
    else if (st.stale && !st.loading) loadJobItems(job.repo, job.id);
    loadAssumptions(job, false);
    if (!st.data) {
      SF.rebuild(panel, st.error ? SF.unavailable(st.error, 'This job’s items') : SF.skeleton(6));
      return;
    }
    const items = Array.isArray(st.data.items) ? st.data.items : [];
    const sum = SF.isObj(st.data.summary) ? st.data.summary : {};
    const ended = ENDED.has(job.stage);
    const head = h('div', { class: 'tr-tab-head' },
      h('div', null, h('h3', { class: 'tr-h3' }, 'Items'), h('p', { class: 'sf-muted' }, [SF.plural(sum.total || items.length, 'item'), (sum.open || 0) + ' open', (sum.blocking_open || 0) + ' must-do not proven',
        (sum.proposed || 0) + ' suggested', (sum.done || 0) + ' waiting for a check', (sum.verified || 0) + ' proven'].concat(sum.stale ? [sum.stale + ' stale'] : []).join(' · '))),
      h('span', { class: 'spacer' }));
    if (!ended) { const b = SF.btn('Item', { cls: 'btn-sm', icon: 'plus', dataset: { action: 'add-job-item', focus: 'add-job-item' } }); b.addEventListener('click', () => addJobItem(job)); head.appendChild(b); }
    else { const b = SF.btn('Follow-up', { cls: 'btn-sm', icon: 'plus', dataset: { action: 'add-followup', focus: 'add-followup' }, title: 'This job has ended: add a follow-up in ' + job.repo + ' instead' }); b.addEventListener('click', () => addItemModal('followups', job.repo)); head.appendChild(b); }
    const gate = items.filter(holdsGate);
    const sugg = items.filter((r) => r.status === 'proposed' && !holdsGate(r));
    const other = items.filter((r) => !holdsGate(r) && r.status !== 'proposed' && !CLOSED.has(r.status));
    const closed = items.filter((r) => CLOSED.has(r.status) && !holdsGate(r));
    const group = (id, title, rows, extra) => (rows.length ? h('section', { class: 'tr-group', 'aria-labelledby': 'tr-j-' + id }, h('h4', { class: 'tr-h4', id: 'tr-j-' + id }, title + ' (' + rows.length + ')', extra || null), h('ul', { class: 'tr-list' }, rows.map((r) => jobItemRow(r, st, job)))) : null);
    let dismissAll = null;
    if (sugg.length > 1) { dismissAll = SF.btn('Dismiss all…', { cls: 'btn-xs', icon: 'x', dataset: { action: 'dismiss-job-sugg', focus: 'dismiss-job-sugg' } }); dismissAll.addEventListener('click', () => bulkDismiss(sugg.map((r) => r.ref), SF.plural(sugg.length, 'suggestion'), () => loadJobItems(job.repo, job.id))); }
    const notices = SF.isObj(st.data.notices) ? Object.keys(st.data.notices).filter((a) => Array.isArray(st.data.notices[a]) && st.data.notices[a].length) : [];
    const unparsed = Array.isArray(st.data.unparsed) ? st.data.unparsed : [];
    const log = Array.isArray(st.data.log) ? st.data.log : [];
    const closedBox = closed.length ? h('details', { class: 'tr-closed', open: st.closedOpen }, h('summary', null, SF.icon('chevron', 'icon-xs chev'), 'Closed: proven, dropped or moved on (' + closed.length + ')'), h('ul', { class: 'tr-list' }, closed.map((r) => jobItemRow(r, st, job)))) : null;
    if (closedBox) closedBox.addEventListener('toggle', () => { st.closedOpen = closedBox.open; });
    const had = focusKey(panel);
    const nodes = [head,
      decisionsPanel(job, st, items),
      carryPanel(job, st, items),
      ended ? h('p', { class: 'tr-later' }, SF.icon('info', 'icon-sm'), 'This job has ended (' + job.stage + '): add a follow-up instead of an item, and reopen an item’s follow-up rather than the item.') : null,
      items.length ? null : SF.emptyState('checklist', 'No items yet', 'The spec’s acceptance criteria, review findings and the agents’ promises appear here as the job runs.', null, true),
      group('gate', 'Must-do, not proven', gate),
      group('sugg', 'Suggestions', sugg, dismissAll),
      group('open', 'Other open items', other),
      closedBox,
      notices.length ? h('details', { class: 'tr-closed tr-notices' }, h('summary', null, SF.icon('chevron', 'icon-xs chev'), 'What the agents will hear next (' + notices.length + ')'),
        notices.map((a) => h('div', { class: 'tr-notice' }, h('h5', null, a), h('ul', { class: 'tr-plain' }, st.data.notices[a].map((n) => h('li', { class: 'tr-text' }, String(n))))))) : null,
      unparsed.length ? h('details', { class: 'tr-closed tr-unparsed' }, h('summary', null, SF.icon('chevron', 'icon-xs chev'), 'Lines an agent wrote that were not understood (' + unparsed.length + ')'),
        h('ul', { class: 'tr-plain' }, unparsed.map((u) => h('li', null, h('span', { class: 'tr-mono' }, String(u.agent || '') + ' · ' + String(u.file || '') + (u.line ? ':' + u.line : '')), ' ', h('code', { class: 'tr-text' }, String(u.text || '')))))) : null,
      log.length ? h('details', { class: 'tr-closed tr-log' }, h('summary', null, SF.icon('chevron', 'icon-xs chev'), 'Every change, newest last (' + log.length + ')'), historyList(log.map((e) => Object.assign({}, e, { note: (e.ref ? String(e.ref).split('/').pop() + ': ' : '') + String(e.note || '') })))) : null,
    ];
    SF.rebuild(panel, nodes);
    if (had && (!document.activeElement || document.activeElement === document.body)) {   // the control went away
      const el = panel.querySelector('[data-focus="' + CSS.escape(had) + '"]') || rowFallback(panel, had, 'j');
      if (el) el.focus({ preventScroll: true });
    }
  }

  // ---------------------------------------------------------------- the job card, the drawer head, the run rows
  /** Product decisions still open, per job: one GET /api/items for every card (never a request per card), made
   *  while the board shows and again after items.updated. */
  const DEC = { byJob: new Map(), loading: null, stale: true, timer: 0, again: false, at: 0 };
  function loadDecisions() {
    if (DEC.loading) { DEC.again = true; return DEC.loading; }   // that answer may predate the change: read again after it
    DEC.stale = false;
    DEC.at = Date.now();
    DEC.loading = SF.api('/api/items?scope=job&status=open,doing&limit=1000').then((r) => {
      const next = new Map();
      for (const row of (r && Array.isArray(r.items) ? r.items : [])) {
        if (row.kind !== 'decision') continue;
        const sc = scopeOf(row);
        const k = sc.repo + '/' + sc.id;
        next.set(k, (next.get(k) || 0) + 1);
      }
      const sig = (m) => Array.from(m.entries()).sort().join(';');
      const moved = sig(next) !== sig(DEC.byJob);
      DEC.byJob = next;
      if (moved) SF.refreshJobs();
    }).catch(() => { DEC.stale = true; }).finally(() => { DEC.loading = null; if (DEC.again) { DEC.again = false; loadDecisions(); } });
    return DEC.loading;
  }
  /** After a change: one read 0.6 s later, and at most one every 2 s however many changes come (a busy factory). */
  function scheduleDecisions() {
    DEC.stale = true;
    if (DEC.timer) return;
    DEC.timer = setTimeout(() => { DEC.timer = 0; if (SF.route().view === 'factory') loadDecisions(); }, Math.max(600, 2000 - (Date.now() - DEC.at)));
  }
  function decisionsOpen(job) {
    if (DEC.stale && !DEC.loading && !DEC.timer && SF.route().view === 'factory') { if (!DEC.at) loadDecisions(); else scheduleDecisions(); }
    return DEC.byJob.get(job.repo + '/' + job.id) || 0;
  }
  function jobCardChips(job) {
    const s = SF.isObj(job.items) ? job.items : null;
    const out = [];
    if ((ACTIVE.has(job.stage) || job.stage === 'needs-human')) {
      const n = decisionsOpen(job);
      if (n) out.push(h('span', { class: 'tag tr-tag-human tr-card-confirm', title: SF.plural(n, 'product decision') + ' only you can answer' }, SF.icon('user', 'icon-xs'), n + ' to answer'));
    }
    if (!s || !s.total) return out;
    const stopped = job.stage === 'needs-human' || job.stage === 'failed';
    const tone = s.blocking_open && stopped ? 'tag-err' : s.stale ? 'tag-warn' : '';
    const text = (s.open || 0) + ' open' + (s.blocking_open ? ' · ' + s.blocking_open + ' must-do' : '');
    out.unshift(h('span', { class: 'tag tr-card-items ' + tone, title: 'Items: ' + [s.total + ' in all', (s.open || 0) + ' open', (s.blocking_open || 0) + ' must-do not proven', (s.proposed || 0) + ' suggested', (s.done || 0) + ' waiting for a check', (s.verified || 0) + ' proven'].join(', ') + (s.stale ? ', ' + s.stale + ' stale' : '') },
      SF.icon('checklist', 'icon-xs'), text));
    if (s.proposed) out.splice(1, 0, h('span', { class: 'tag tag-outline tr-card-sugg', title: SF.plural(s.proposed, 'suggestion') + ' to accept or dismiss' }, s.proposed + ' suggested'));
    return out;
  }
  function jobHeadBlock(job) {
    if (CONFIRM_CLOSED.has(job.stage)) return null;
    if (job.stage !== 'queued' && job.stage !== 'spec') loadAssumptions(job, false);   // once per stage, confirmation and items version
    const st = jobState(job.repo + '/' + job.id);
    // Its items too while a marked line is unconfirmed (and after items.updated): a line answered No is not waiting.
    const asking = !!(st.asm && Array.isArray(st.asm.unconfirmed) && st.asm.unconfirmed.length);
    if (asking && ((!st.data && !st.error) || st.stale) && !st.loading && SF.job(job.repo, job.id)) loadJobItems(job.repo, job.id);
    const lines = unansweredLines(st);
    const decisions = st.data && Array.isArray(st.data.items) ? st.data.items.filter((r) => r.kind === 'decision' && !CLOSED.has(r.status)).length : 0;
    const count = lines ? Math.max(lines.length, decisions) : decisionsOpen(job);
    if (!count || CONFIRM_CLOSED.has(job.stage)) return null;
    const human = job.stage === 'needs-human';
    const link = h('a', { class: 'btn btn-sm ' + (human ? 'btn-primary' : ''), href: '#/job/' + SF.enc(job.repo) + '/' + SF.enc(job.id) + '/items', dataset: { action: 'answer-decisions' } }, SF.icon('user', 'icon-sm'), 'Answer ' + (count === 1 ? 'it' : 'them'));
    return h('div', { class: 'tr-head-block' + (human ? ' is-human' : '') }, SF.callout(human ? 'human' : 'info', 'user', human ? 'A person must answer this:' : 'Product decisions:',
      SF.plural(count, 'product decision') + ' to confirm. No agent can answer them.'), link);
  }
  function jobMeta(job) {
    const s = SF.isObj(job.items) ? job.items : null;
    if (!s || !s.total) return null;
    return h('a', { class: 'meta-item meta-link', href: '#/job/' + SF.enc(job.repo) + '/' + SF.enc(job.id) + '/items', title: 'The job’s items', dataset: { action: 'show-items' } },
      SF.icon('checklist', 'icon-xs'), 'Items: ' + s.total + (s.blocking_open ? ' · ' + s.blocking_open + ' must-do open' : ''));
  }
  /** §26: the Grid Card receipt of a factory-line run (agent.start's grid {sha256, bytes}; chats, plans and the scout
   *  have none), labelled delivered, never read: the receipt says which card the prompt carried, not that the model
   *  read or followed it. SF.extend('runHead') gives {mode, start: agent.start's data, …}. */
  function gridChip(run) {
    const g = run && run.mode === 'job' && SF.isObj(run.start) && SF.isObj(run.start.grid) ? run.start.grid : null;
    if (!g || typeof g.sha256 !== 'string' || !/^[0-9a-f]{12,64}$/.test(g.sha256)) return null;
    const bytes = Number(g.bytes) || 0;
    const size = bytes.toLocaleString('en-US') + ' bytes';
    return h('span', { class: 'tag tag-outline tr-grid', dataset: { grid: g.sha256.slice(0, 12) },
      title: 'Grid Card delivered: the Factory Grid Card this run’s system prompt carried, sha256 ' + g.sha256 + ', ' + size + '. Delivered is not proof that the model read or followed it.' },
    SF.icon('grid', 'icon-xs'), h('span', { class: 'tr-grid-word' }, 'card delivered'), h('span', { class: 'tr-grid-short' }, 'delivered'), h('span', { class: 'sr-only' }, ', sha256 '),
    h('span', { class: 'tr-mono' }, g.sha256.slice(0, 7), h('span', { class: 'tr-grid-more' }, g.sha256.slice(7, 12))),
    h('span', { class: 'tr-grid-bytes' }, size));
  }

  // ---------------------------------------------------------------- the Approve dialog (SF.extend('form'), form 'approve')
  /** Before Approve: the job's must-do items that are not proven (items.holds_gate, the gate's own rule), with the
   *  required box "Move these N must-do items to follow-ups"; their ids go as carry_items (Factory._check_carry, 409
   *  {error, open_blocking} without them). Unanswered product decisions among them are said apart: approving goes on
   *  without the answer, and the copy is an ordinary follow-up (items.carry_copy). The list follows items.updated
   *  while the dialog is open; a read that fails is tried again, and nothing is sent until the list is known (the
   *  server would refuse it). A 409 naming must-do items (open_blocking) rebuilds the list, unticks the box and says
   *  so in plain words (f.onError). */
  function approveField(f) {
    if (!f || f.form !== 'approve' || !f.job || !f.job.repo || !f.job.id) return null;
    const job = f.job;
    const itemsPath = '/api/jobs/' + SF.enc(job.repo) + '/' + SF.enc(job.id) + '/items';
    const itemsHash = '#/job/' + SF.enc(job.repo) + '/' + SF.enc(job.id) + '/items';
    const st = { items: null, all: [], error: null, gen: 0, forced: [], tries: 0, timer: 0 };
    const note = h('div', { class: 'tr-carry-note sf-muted' });
    const decisions = h('div', { class: 'tr-carry-dec', hidden: true });
    const list = h('ul', { class: 'tr-plain tr-carry-list' });
    const box = h('input', { type: 'checkbox', dataset: { carryOk: '1', focus: 'carry-ok' } });
    const boxText = h('span');
    const label = h('label', { class: 'check tr-carry-check', hidden: true }, box, boxText);
    const el = h('section', { class: 'tr-carry-field', 'aria-label': 'Must-do items', 'aria-live': 'polite' }, note, decisions, list, label);
    /** What the approve moves: the must-do items not proven (the last read), and those a 409 named besides (unless
     *  that read shows them proven, dropped or moved since). */
    const moving = () => {
      const rows = (st.items || []).slice();
      for (const id of st.forced) {
        if (rows.some((r) => r.id === id)) continue;
        const known = st.all.find((r) => r.id === id);
        if (known && !holdsGate(known)) continue;
        rows.push(known || { id, title: null, status: null });
      }
      return rows;
    };
    const them = (n) => (n === 1 ? 'it' : 'them');
    const draw = () => {
      SF.clear(list);
      SF.clear(note);
      SF.clear(decisions);
      decisions.hidden = true;
      label.hidden = true;
      const rows = moving();
      if (!rows.length && st.error) {
        el.hidden = false;
        const again = SF.btn('Read them again', { cls: 'btn-xs', icon: 'refresh', dataset: { action: 'carry-reload', focus: 'carry-reload' } });
        again.addEventListener('click', () => load(true));
        SF.add(note, [SF.icon('alert', 'icon-sm'), h('span', null, 'This job’s must-do items could not be read (' + String(st.error.message || st.error) + '). Approve waits until they are: the server never approves while a must-do item is left unnamed. '), again]);
        return;
      }
      if (!rows.length && !st.items) { el.hidden = false; SF.add(note, [SF.icon('checklist', 'icon-sm'), 'Checking this job’s must-do items…']); return; }
      el.hidden = !rows.length;
      if (!rows.length) return;
      const n = rows.length;
      SF.add(note, [SF.icon('alert', 'icon-sm'), h('span', null, h('strong', null, SF.plural(n, 'must-do item') + (n === 1 ? ' is' : ' are') + ' not proven.'),
        ' Approving moves ' + them(n) + ', unproven, to ' + job.repo + '’s follow-ups, noted “merged (delivered) by human override while open”. Nothing is waived silently.')]);
      const dec = rows.filter((r) => r.kind === 'decision');
      if (dec.length) {
        const answer = SF.btn('Answer ' + them(dec.length) + ' first', { cls: 'btn-xs', icon: 'user', dataset: { action: 'answer-first', focus: 'answer-first' } });
        answer.addEventListener('click', () => { const dlg = el.closest('dialog'); if (dlg) dlg.close(); SF.go(itemsHash); });
        decisions.hidden = false;
        SF.add(decisions, [SF.callout('human', 'user', SF.plural(dec.length, 'product decision') + (dec.length === 1 ? ' has' : ' have') + ' no answer:',
          'approving goes on without ' + (dec.length === 1 ? 'an answer, and it becomes an ordinary follow-up' : 'answers, and they become ordinary follow-ups') + ', no longer a product decision. Only a person can answer ' + them(dec.length) + ': Yes or No in the Items tab.'), answer]);
      }
      SF.add(list, rows.map((r) => h('li', { class: 'tr-carry-row' + (r.kind === 'decision' ? ' is-decision' : ''), dataset: { id: String(r.id) } }, h('code', { class: 'tr-mono' }, String(r.id)),
        r.status ? pill(r) : null, r.kind === 'decision' ? tag('product decision', 'tr-tag-human') : null,
        h('span', { class: 'tr-text' + (r.title ? '' : ' sf-muted') }, r.title ? String(r.title) : 'named by the server; not read here yet'))));
      boxText.textContent = 'Move ' + (n === 1 ? 'this must-do item' : 'these ' + n + ' must-do items') + ' to follow-ups';
      label.hidden = false;
    };
    const load = (now) => {
      clearTimeout(st.timer);
      const gen = ++st.gen;
      if (now) { st.error = null; st.tries = 0; draw(); }
      SF.api(itemsPath).then((r) => {
        if (gen !== st.gen) return;
        const before = moving().map((x) => x.id).join();
        st.all = Array.isArray(r && r.items) ? r.items : [];
        st.items = st.all.filter(holdsGate);
        st.error = null;
        st.tries = 0;
        if (before && moving().map((x) => x.id).join() !== before) box.checked = false;   // a new list: ask again
      }).catch((e) => {
        if (gen !== st.gen) return;
        st.error = e;
        if (!SF.gone(e) && st.tries < 3) { st.tries++; st.timer = setTimeout(() => { if (el.isConnected) load(); }, 1500 * st.tries); }
      }).finally(() => { if (gen === st.gen) draw(); });
    };
    draw();
    load();
    const off = SF.on('items.updated', (ev) => {
      if (!el.isConnected) { off(); clearTimeout(st.timer); return; }
      if (ev.data && ev.data.ref === 'job:' + job.repo + '/' + job.id) load();
    });
    f.onError((e) => {
      const ids = e && e.status === 409 && SF.isObj(e.data) && Array.isArray(e.data.open_blocking) ? e.data.open_blocking.filter((x) => typeof x === 'string') : [];
      if (!ids.length) return null;
      st.forced = ids;
      box.checked = false;
      draw();
      load();
      const n = moving().length;
      return 'The server found ' + SF.plural(n, 'must-do item') + ' not proven (the list is read again): tick “Move ' + (n === 1 ? 'this must-do item' : 'these ' + n + ' must-do items')
        + ' to follow-ups” to move ' + them(n) + ' on unproven, or prove ' + them(n) + ' first in the Items tab.';
    });
    f.contribute((body) => {
      const rows = moving();
      if (!rows.length) {
        if (st.items) return;                       // nothing must-do is open
        if (st.error && SF.gone(st.error)) return;  // an older server without items: nothing is held
        if (st.error) { load(true); throw new Error('This job’s must-do items could not be read, so nothing was sent. They are being read again: approve once the list shows.'); }
        throw new Error('Still reading this job’s must-do items: try again in a moment.');
      }
      if (!box.checked) { box.focus(); throw new Error('Tick “' + boxText.textContent + '”, or prove them first in the Items tab.'); }
      body.carry_items = rows.map((r) => r.id);
    });
    return el;
  }
  // ---------------------------------------------------------------- Mermaid: Copy and Download .mmd (§26)
  /** GET /api/docs/maps (docs.mermaid_maps on the live config), read again when Procedures shows after a while. */
  function loadMaps(force) {
    if (T.mapsLoading) return T.mapsLoading;
    if (T.maps && !force) return Promise.resolve();
    T.mapsLoading = SF.api('/api/docs/maps').then((r) => { T.maps = Array.isArray(r) ? r : []; T.mapsAt = Date.now(); T.mapsError = null; })
      .catch((e) => { T.maps = T.maps || []; T.mapsError = e; })
      .finally(() => { T.mapsLoading = null; });
    return T.mapsLoading;
  }
  const mapOf = (id) => (T.maps || []).find((x) => x && x.id === id) || null;
  function mermaidButtons(spec) {
    const box = h('span', { class: 'tr-mermaid', dataset: { mermaid: spec.id } });
    const fill = () => {
      if (box.firstChild || !mapOf(spec.id)) return;   // a map the server does not serve as Mermaid gets no buttons
      const copy = SF.btn('Copy Mermaid', { cls: 'btn-xs', dataset: { action: 'copy-mermaid' }, title: 'Copy this map’s Mermaid source' });   // no icons: the figure's one svg is its map
      copy.addEventListener('click', () => {   // the source as last read (Download asks the server now)
        const m = mapOf(spec.id);
        if (m) SF.copyText(String(m.mermaid || ''), 'Mermaid source of “' + String(m.title || spec.id) + '” copied');
      });
      const dl = h('a', { class: 'btn btn-xs', href: '/api/docs/maps/' + SF.enc(spec.id) + '.mmd', download: spec.id + '.mmd', dataset: { action: 'download-mmd' }, title: 'Download ' + spec.id + '.mmd' },
        'Download .mmd');
      SF.add(box, [copy, dl]);
    };
    if (T.maps) fill();
    if (!T.maps || Date.now() - (T.mapsAt || 0) > 10000) loadMaps(true).then(fill);
    return box;
  }

  // ---------------------------------------------------------------- chats and plans (§15 "Chat turns", "The view")
  /** A chat's items (GET /api/chats/<id>/items {items, summary, log}) and a plan's roll-up (GET
   *  /api/plans/<repo>/<id>/items {items, summary, jobs: [{job_id, n, stage, summary}], log}), keyed by scope ref.
   *  The nodes the hooks give (a chip in the chat's or plan's meta row, the lines under each reply) are kept per
   *  scope and refilled in place when the data changes, so a re-render of the chat or plan never loses them. */
  const CS = new Map();
  const CSP = { sref: null };   // the scope panel that is open
  function csState(sref) {
    let st = CS.get(sref);
    if (!st) { st = { data: null, error: null, loading: null, again: false, stale: true, open: new Set(), hist: new Map(), chip: null, turns: new Map(), meta: null, closedOpen: false, focusRef: null }; CS.set(sref, st); }
    return st;
  }
  function csPath(sref) {
    const c = /^chat:(.+)$/.exec(sref);
    if (c) return '/api/chats/' + SF.enc(c[1]) + '/items';
    const p = /^plan:([^/]+)\/(.+)$/.exec(sref);
    return '/api/plans/' + SF.enc(p[1]) + '/' + SF.enc(p[2]) + '/items';
  }
  function csLoad(sref) {
    const st = csState(sref);
    if (st.loading) { st.again = true; return st.loading; }
    st.stale = false;
    st.loading = SF.api(csPath(sref)).then((r) => { st.data = r; st.error = null; st.hist.clear(); }).catch((e) => { st.error = e; })
      .finally(() => { st.loading = null; if (st.again) { st.again = false; csLoad(sref); } csPaint(sref); });
    return st.loading;
  }
  function csEnsure(sref) {
    const st = csState(sref);
    if ((st.stale || (!st.data && !st.error)) && !st.loading) csLoad(sref);
    return st;
  }
  const csShown = (st, sref) => !!((st.chip && st.chip.isConnected) || Array.from(st.turns.values()).some((n) => n.isConnected) || SF.drawer.isOpen('scope:' + sref));
  /** A change in that scope (items.updated, a person's action): read it again now if something shows it. */
  function csChanged(sref) {
    const st = CS.get(sref);
    if (!st) return;
    st.stale = true;
    if (csShown(st, sref)) csLoad(sref);
  }
  function csPaint(sref) {
    const st = CS.get(sref);
    if (!st) return;
    if (st.chip) fillChip(sref, st);
    for (const node of st.turns.values()) fillTurn(sref, st, node);
    if (SF.drawer.isOpen('scope:' + sref)) drawScopePanel(sref);
  }
  /** A plan's own items and its jobs' together. */
  function rollup(st) {
    const d = st.data;
    if (!d) return null;
    const out = { total: 0, open: 0, blocking_open: 0, proposed: 0, done: 0, verified: 0 };
    const addSum = (x) => { if (SF.isObj(x)) for (const k of Object.keys(out)) out[k] += Number(x[k]) || 0; };
    addSum(d.summary);
    for (const j of Array.isArray(d.jobs) ? d.jobs : []) addSum(j.summary);
    return out;
  }
  function fillChip(sref, st) {
    const chip = st.chip;
    const plan = /^plan:/.test(sref);
    const sum = plan ? rollup(st) : st.data && SF.isObj(st.data.summary) ? st.data.summary : null;
    const parts = [];
    if (sum) {
      if (sum.open) parts.push(sum.open + ' open');
      if (sum.blocking_open) parts.push(sum.blocking_open + ' must-do');
      if (sum.proposed) parts.push(sum.proposed + ' suggested');
      if (sum.done) parts.push(sum.done + ' to check');
    }
    SF.clear(chip);
    SF.add(chip, [SF.icon('checklist', 'icon-xs'), 'Items' + (parts.length ? ': ' + parts.join(' · ') : sum && sum.total ? ': all closed' : '')]);
    chip.title = plan ? 'The plan’s items and its jobs’ (the roll-up)' : 'This chat’s items: its promises, suggestions to track and your own';
    chip.classList.toggle('is-human', !!(sum && (sum.proposed || sum.done)));
    chip.hidden = plan && !(sum && sum.total);   // a plan with no item anywhere shows no chip
  }
  function scopeChip(sref, meta, focusKey) {
    const st = csEnsure(sref);
    st.meta = meta;
    if (CSP.sref && CSP.sref !== sref && CSP.sref.split(':')[0] === sref.split(':')[0] && SF.drawer.isOpen('scope:' + CSP.sref)) SF.drawer.close();   // another chat or plan shows now
    if (!st.chip) {
      st.chip = h('button', { type: 'button', class: 'meta-item meta-link tr-scope-chip', dataset: { action: /^chat:/.test(sref) ? 'chat-items' : 'plan-items', focus: focusKey } });
      st.chip.addEventListener('click', () => openScopePanel(sref));
    }
    fillChip(sref, st);
    keepFocusIn(st.chip);
    return st.chip;
  }
  function chatHeadChip(chat) {
    if (!SF.isObj(chat) || !chat.id) return null;
    return scopeChip('chat:' + chat.id, chat, 'chat-items:' + chat.id);
  }
  function planHeadChip(plan) {
    if (!SF.isObj(plan) || !plan.repo || !plan.id) return null;
    return scopeChip('plan:' + plan.repo + '/' + plan.id, plan, 'plan-items:' + plan.repo + '/' + plan.id);
  }
  /** Under a reply (SF.extend('chatReply')): the sweep suggestions of its turn (source.turn, or source.run for a turn
   *  shown live) with Track it / Not a promise, and what was tracked from it. A suggestion without a turn (no builtin
   *  transcript) is in the chat's Items panel. */
  function chatReplyItems(a) {
    if (!a || !SF.isObj(a.chat) || !a.chat.id) return null;
    const sref = 'chat:' + a.chat.id;
    const st = csEnsure(sref);
    st.meta = a.chat;
    const k = a.run && a.run.name ? 'run:' + a.run.name : Number.isInteger(a.turn) ? 'turn:' + a.turn : null;
    if (!k) return null;
    let node = st.turns.get(k);
    if (!node) {
      node = h('div', { class: 'tr-turn', dataset: { turnKey: k } });
      node._turn = Number.isInteger(a.turn) ? a.turn : null;
      node._run = a.run && a.run.name ? String(a.run.name) : null;
      st.turns.set(k, node);
      fillTurn(sref, st, node);
    }
    keepFocusIn(node);
    return node;
  }
  function fillTurn(sref, st, node) {
    const items = st.data && Array.isArray(st.data.items) ? st.data.items : [];
    const mine = items.filter((r) => {
      const s = r.source || {};
      if (s.type !== 'sweep') return false;
      if (node._run && s.run) return s.run === node._run;
      return node._turn !== null && s.turn === node._turn;
    });
    const sig = JSON.stringify(mine.map((r) => [r.ref, r.status, r.title, r.blocking]));
    if (node._sig === sig) return;
    node._sig = sig;
    const had = focusKey(node);
    SF.clear(node);
    if (!mine.length) return;
    const sugg = mine.filter((r) => r.status === 'proposed');
    const rest = mine.filter((r) => r.status !== 'proposed');
    const idBtn = (r, text) => {
      const b = h('button', { type: 'button', class: 'btn btn-ghost btn-xs tr-mono tr-turn-id', title: 'Show ' + r.ref + ' with this chat’s items', dataset: { action: 'show-chat-item', focus: 'showi:' + r.ref } }, text || r.id);
      b.addEventListener('click', () => openScopePanel(sref, r.ref));
      return b;
    };
    if (sugg.length) node.appendChild(h('p', { class: 'tr-turn-head' }, SF.icon('checklist', 'icon-xs'), sugg.length === 1 ? 'A promise in this reply? Track it, so it is not forgotten.' : sugg.length + ' promises in this reply? Track the ones that are.'));
    for (const r of sugg) {
      const track = SF.btn('Track it', { cls: 'btn-xs btn-primary', icon: 'check', dataset: { action: 'track-it', focus: 'track:' + r.ref } });
      const no = SF.btn('Not a promise', { cls: 'btn-xs', icon: 'x', dataset: { action: 'not-a-promise', focus: 'notp:' + r.ref } });
      track.addEventListener('click', () => quickAction(r, 'accept', track));
      no.addEventListener('click', () => quickAction(r, 'dismiss', no));
      node.appendChild(h('div', { class: 'tr-turn-sugg', dataset: { ref: r.ref } },
        h('span', { class: 'tr-turn-text' }, idBtn(r), ' ', h('span', { class: 'tr-text' }, String(r.title || r.ref)), r.blocking ? [' ', tag('must-do', 'tag-warn')] : null),
        h('span', { class: 'tr-turn-acts', role: 'group', 'aria-label': 'Is ' + r.id + ' a promise?' }, track, no)));
    }
    if (rest.length) node.appendChild(h('p', { class: 'tr-turn-done' }, 'From this reply: ', rest.map((r, i) => [i ? ', ' : null, idBtn(r, r.id + ' ' + (WORDS[r.status] || r.status))])));
    if (had) {
      const target = node.querySelector('[data-focus="' + CSS.escape(had) + '"]') || node.querySelector('[data-focus="' + CSS.escape('showi:' + had.slice(had.indexOf(':') + 1)) + '"]');
      if (target) target.focus({ preventScroll: true });
    }
  }
  /** Track it / Not a promise under a reply: one click (a dismissed sweep suggestion is "not a promise", the
   *  server's own reason, and is not suggested again in this repository). */
  async function quickAction(row, action, button) {
    SF.setBusy(button, true);
    try {
      await act(action, { ref: row.ref });
      SF.toast(action === 'accept' ? 'Tracked: ' + row.ref + ' is To do.' : row.ref + ' dismissed: not a promise.', { kind: 'ok' });
      changed();
    } catch (e) { SF.toastError(action === 'accept' ? 'Not tracked' : 'Not dismissed', e); SF.setBusy(button, false); }
  }
  function openScopePanel(sref, focusRef) {
    const st = csEnsure(sref);
    if (focusRef) { st.open.add(focusRef); st.focusRef = focusRef; }
    CSP.sref = sref;
    drawScopePanel(sref);
  }
  function drawScopePanel(sref) {
    const st = csState(sref);
    const chat = /^chat:/.test(sref);
    const meta = st.meta || {};
    const id = chat ? sref.slice(5) : sref.slice(sref.indexOf('/') + 1);
    let actions = null;
    if (chat && !meta.job) {
      const add = SF.btn('Item', { cls: 'btn-sm btn-primary', icon: 'plus', dataset: { action: 'add-chat-item', focus: 'add-chat-item' }, title: 'Add an item to this chat' });
      add.addEventListener('click', () => addChatItem(sref, meta));
      actions = [add];
    }
    openKept({ key: 'scope:' + sref, title: chat ? 'Items of this chat' : 'Items of plan ' + id,
      eyebrow: [h('span', { class: 'tag tag-outline' }, SF.icon(chat ? 'chat' : 'plan', 'icon-xs'), chat ? 'Chat ' + id : 'Plan ' + id)],
      sub: chat ? String(meta.title || '') : SF.firstLine(meta.request) || null, body: scopeBody(sref, st), actions,
      onClose: () => { if (CSP.sref === sref) CSP.sref = null; } }, (key) => rowFallback(document.getElementById('drawer'), key, 's') || document.querySelector('#drawer .tr-list .tr-row'));
    if (st.focusRef && st.data) {
      const row = document.querySelector('#drawer [data-focus="srow:' + CSS.escape(st.focusRef) + '"]');
      st.focusRef = null;
      if (row) { row.focus({ preventScroll: true }); row.scrollIntoView({ block: 'nearest' }); }
    }
  }
  /** A row that opens in place (the job's Items tab and the scope panel): the item's body, actions and history. */
  function inlineRow(row, st, redraw, after, prefix) {
    const open = st.open.has(row.ref);
    const btn = h('button', { type: 'button', class: 'tr-row tr-row-btn' + (row.waiting ? ' is-waiting' : ''), 'aria-expanded': open ? 'true' : 'false', dataset: { ref: row.ref, focus: prefix + 'row:' + row.ref, status: row.status } },
      h('span', { class: 'tr-row-top' }, SF.icon('chevron', 'icon-xs chev'), pill(row), h('span', { class: 'tr-mono tr-row-id' }, row.id), h('span', { class: 'tr-row-title' }, String(row.title || row.ref))),
      h('span', { class: 'tr-row-chips' }, chips(row)),
      h('span', { class: 'tr-row-meta' }, h('span', null, KINDS[row.kind] || row.kind), h('span', null, 'owner ' + who(row.owner)), h('span', null, 'checked by ' + who(row.verifier)), h('span', null, sourceLabel(row))));
    btn.addEventListener('click', () => { if (st.open.has(row.ref)) st.open.delete(row.ref); else st.open.add(row.ref); redraw(); });
    let hist = null;
    if (open) {
      const got = st.hist.get(row.ref);
      if (Array.isArray(got)) hist = historyList(got);
      else {
        hist = SF.btn(got === 'loading' ? 'Loading its history…' : 'Show its history', { cls: 'btn-xs', icon: 'clock', disabled: got === 'loading', dataset: { action: 'item-history', focus: prefix + 'hist:' + row.ref } });
        hist.addEventListener('click', () => {
          st.hist.set(row.ref, 'loading');
          redraw();
          SF.api('/api/items/history?ref=' + SF.enc(row.ref)).then((d) => st.hist.set(row.ref, Array.isArray(d && d.log) ? d.log : []))
            .catch((e) => { st.hist.delete(row.ref); SF.toastError('Could not load its history', e); })
            .finally(redraw);
        });
      }
    }
    return h('li', { class: 'tr-li' + (open ? ' is-open' : '') }, btn,
      open ? h('div', { class: 'tr-inline' }, itemBody(row, { after, prefix, history: hist, factsOpen: false })) : null);
  }
  function scopeBody(sref, st) {
    if (!st.data) return [st.error ? SF.unavailable(st.error, 'These items') : SF.skeleton(5)];
    const chat = /^chat:/.test(sref);
    const meta = st.meta || {};
    const items = Array.isArray(st.data.items) ? st.data.items : [];
    const redraw = () => drawScopePanel(sref);
    const after = () => csLoad(sref);
    const row = (r) => inlineRow(r, st, redraw, after, 's');
    const group = (key, title, rows, extra) => (rows.length ? h('section', { class: 'tr-group', 'aria-labelledby': 'tr-s-' + key }, h('h4', { class: 'tr-h4', id: 'tr-s-' + key }, title + ' (' + rows.length + ')', extra || null), h('ul', { class: 'tr-list' }, rows.map(row))) : null);
    const parts = [];
    if (chat && meta.job) {
      const j = typeof meta.job === 'string' ? { id: meta.job, repo: meta.repo } : meta.job;
      parts.push(SF.callout('info', 'handoff', 'Handed off to job ' + (j.id || '') + '.', 'Its open items went with it: work them in the job’s Items tab. A promised check-in stays here until its wake runs.'),
        j.id && (j.repo || meta.repo) ? h('p', null, plainHref('#/job/' + SF.enc(j.repo || meta.repo) + '/' + SF.enc(j.id) + '/items', 'Open the job’s items')) : null);
    } else if (chat && !meta.repo) {
      parts.push(h('p', { class: 'tr-later' }, SF.icon('info', 'icon-sm'), 'This chat has no repository, so there are no follow-ups: keep an item open here, prove it or drop it.'));
    } else if (chat) {
      parts.push(h('p', { class: 'sf-muted tr-scope-lead' }, 'What this chat promised or found, and what you added. Suggestions also show under the reply they came from. When you hand the chat off, its open items go to the job.'));
    }
    if (!chat) {
      const r = rollup(st);
      parts.push(h('p', { class: 'sf-muted tr-scope-lead' }, 'The plan’s own items (from the lead’s final review) and its jobs’ items: ' +
        [SF.plural(r.total, 'item'), r.open + ' open', r.blocking_open + ' must-do not proven', r.proposed + ' suggested', r.verified + ' proven'].join(' · ') + '.'));
    }
    const sugg = items.filter((r) => r.status === 'proposed');
    const live = items.filter((r) => r.status !== 'proposed' && !CLOSED.has(r.status));
    const closed = items.filter((r) => CLOSED.has(r.status));
    let dismissAll = null;
    if (sugg.length > 1) {
      dismissAll = SF.btn('Dismiss all…', { cls: 'btn-xs', icon: 'x', dataset: { action: 'dismiss-scope-sugg', focus: 'dismiss-scope-sugg' } });
      dismissAll.addEventListener('click', () => bulkDismiss(sugg.map((r) => r.ref), SF.plural(sugg.length, 'suggestion'), after));
    }
    if (!items.length) parts.push(SF.emptyState('checklist', 'No items yet', chat ? 'An agent’s promises in its replies, and what you add with + Item, show here.' : 'The lead’s final review adds items here; each job keeps its own.', null, true));
    parts.push(group('sugg', 'Suggestions', sugg, dismissAll), group('open', chat ? 'Open' : 'The plan’s open items', live));
    if (closed.length) {
      const box = h('details', { class: 'tr-closed', open: st.closedOpen }, h('summary', null, SF.icon('chevron', 'icon-xs chev'), 'Closed: proven, dropped or moved on (' + closed.length + ')'), h('ul', { class: 'tr-list' }, closed.map(row)));
      box.addEventListener('toggle', () => { st.closedOpen = box.open; });
      parts.push(box);
    }
    if (!chat) {
      const jobs = Array.isArray(st.data.jobs) ? st.data.jobs : [];
      const repo = sref.slice(5, sref.indexOf('/'));
      if (jobs.length) parts.push(h('section', { class: 'tr-group', 'aria-labelledby': 'tr-s-jobs' }, h('h4', { class: 'tr-h4', id: 'tr-s-jobs' }, 'Its jobs (' + jobs.length + ')'),
        h('ul', { class: 'tr-list' }, jobs.map((j) => {
          const s = SF.isObj(j.summary) ? j.summary : null;
          const text = s ? [SF.plural(s.total || 0, 'item'), (s.open || 0) + ' open', (s.blocking_open || 0) + ' must-do', (s.proposed || 0) + ' suggested'].join(' · ') : 'no items';
          return h('li', { class: 'tr-li' }, h('a', { class: 'tr-row', href: '#/job/' + SF.enc(repo) + '/' + SF.enc(j.job_id) + '/items', dataset: { focus: 'sjob:' + j.job_id } },
            h('span', { class: 'tr-row-top' }, h('span', { class: 'tr-mono tr-row-id' }, j.n ? 'job ' + j.n : 'job'), h('span', { class: 'tr-row-title' }, String(j.job_id))),
            h('span', { class: 'tr-row-meta' }, j.stage ? h('span', null, String(j.stage)) : null, h('span', { class: s && s.blocking_open ? 'tr-warn-text' : null }, text))));
        }))));
    }
    return parts;
  }
  async function addChatItem(sref, chat) {
    const title = h('input', { class: 'input', type: 'text', maxlength: 200, placeholder: 'What has to happen, in a few words' });
    const detail = h('textarea', { class: 'textarea', rows: 3, maxlength: 2000, placeholder: 'Optional' });
    const must = h('input', { type: 'checkbox' });
    await SF.openModal({
      title: 'Add an item to this chat',
      description: 'The chat’s agent owns it and you check it. When you hand the chat off, it goes to the job with the branch.',
      body: [SF.field('Title', title), SF.field('Detail', detail), chat && chat.repo ? h('label', { class: 'check' }, must, 'Must-do (it stays must-do in the job after a hand-off)') : null],
      dismissable: false,
      actions: [{ label: 'Cancel', value: 'cancel' }, { label: 'Add it', value: 'ok', primary: true, icon: 'plus' }],
      onSubmit: async () => {
        if (!title.value.trim()) { title.focus(); throw new Error('Give it a title.'); }
        const body = { title: title.value.trim() };
        if (detail.value.trim()) body.detail = detail.value.trim();
        if (must.checked) body.blocking = true;
        const r = await SF.api(csPath(sref), { method: 'POST', body, retry409: true });
        SF.toast('Added ' + (r && r.item ? r.item.ref : 'it') + '.', { kind: 'ok' });
        changed();
      },
    });
  }


  // ---------------------------------------------------------------- registering
  const handle = SF.registerView({
    id: 'tracker',
    title: 'Housekeeping Tracker',
    routes: ['housekeeping'],
    nav: { label: 'Tracker', icon: 'checklist', order: 25 },
    home: {
      title: 'Housekeeping Tracker', icon: 'checklist', order: 20, href: '#/housekeeping',
      text: 'Everything the agents promised, found or were asked to do, kept until it is proven done.',
      routes: [{ to: 'factory', label: 'follow-ups become jobs' }],
      events: ['items.updated', 'items.carried'],
      async load(hd) {
        const { items, counts } = await loadCounts();
        hd.badge(counts.waiting, SF.plural(counts.waiting || 0, 'item') + ' waiting for you');
        const decisions = items.filter((i) => i && i.kind === 'decision');
        const needs = decisions.map((i) => {
          const sc = scopeOf(i);
          return { key: 'item:' + i.ref, tone: 'human', icon: 'help', text: String(i.title || i.ref),
            sub: 'A product decision only you can answer' + (sc.kind === 'job' && sc.id ? ' (job ' + sc.id + ')' : ''),
            href: sc.kind === 'job' && sc.repo && sc.id ? '#/job/' + SF.enc(sc.repo) + '/' + SF.enc(sc.id) : itemHash(i.ref) };   // its head says "Answer them"
        });
        const rest = (counts.waiting || 0) - decisions.length;
        if (rest > 0) needs.push({ key: 'tracker:waiting', tone: counts.overdue ? 'warn' : 'human', icon: 'checklist', href: '#/housekeeping?status=waiting',
          text: SF.plural(rest, 'other item') + ' waiting for you', sub: counts.overdue ? SF.plural(counts.overdue, 'item') + ' overdue' : 'Suggestions to accept, checks to make, or items to move on' });
        const tone = counts.waiting ? 'human' : counts.stale ? 'warn' : 'ok';
        const text = counts.waiting ? SF.plural(counts.waiting, 'item') + ' waiting for you' : SF.plural(counts.open || 0, 'open item');
        const detail = [SF.plural(counts.blocking || 0, 'must-do item'), SF.plural(counts.stale || 0, 'stale item'), (counts.closed_7d || 0) + ' closed this week'].join(' · ');
        return { tone, text, detail, needs };
      },
    },
    start() {
      loadCounts().then((r) => badge(r.counts)).catch(() => {});
      SF.onReconnect(() => { T.drift = null; T.maps = null; T.personas = null; changed(); });
    },
    enter(route, prev) {
      if (route.parts.length) openItemPanel(route);
      else {
        T.panelRef = null;
        if (SF.drawer.isOpen() && prev && prev.parts && prev.parts.length) SF.drawer.close();
      }
    },
    leave() { T.panelRef = null; },
    events: {
      'items.updated': (ev, hd) => {
        const ref = String((ev.data && ev.data.ref) || '');
        staleHistory(ref);   // only that scope's items: an open panel of another scope is left as it is
        if (/^job:/.test(ref) || !DEC.byJob.size) scheduleDecisions();
        for (const [k, st] of JT) if (ref === 'job:' + k) { st.stale = true; if (JTAB.ref && JTAB.ref.repo + '/' + JTAB.ref.id === k) loadJobItems(JTAB.ref.repo, JTAB.ref.id); }
        const r = SF.route();
        if (r.job && ref === 'job:' + r.job.repo + '/' + r.job.id) SF.refreshJobs();   // the open drawer's head
        csChanged(ref);   // a chat's or a plan's own items
        const jm = /^job:([^/]+)\/(.+)$/.exec(ref);   // a plan's roll-up counts its jobs' items too
        if (jm) for (const [k, st] of CS) if (k.indexOf('plan:' + jm[1] + '/') === 0 && st.data && (st.data.jobs || []).some((j) => j && j.job_id === jm[2])) csChanged(k);
        scheduleList(hd);
        if (hd.isShown() && T.panelRef && scopeRefOf(T.panelRef) === ref) openItemPanel(SF.route());
        if (ref === 'system' && hd.isShown() && listQuery(SF.route()).scope === 'system') loadDrift();
      },
      'job.updated': (ev, hd) => stageMoved('job', ev.data && ev.data.job, hd),
      'plan.updated': (ev, hd) => stageMoved('plan', ev.data && ev.data.plan, hd),
      'items.carried': (ev) => {
        const d = ev.data || {};
        SF.toast(SF.plural(Number(d.count) || 0, 'open item') + ' moved to ' + (d.to === 'follow-ups' || !d.to ? 'follow-ups' : String(d.to)) + '.', { kind: 'info', action: { label: 'Show them', href: '#/housekeeping?scope=followups' } });
      },
      'items.unparsed': (ev) => {
        const ref = ev.data && ev.data.ref;
        for (const [k, st] of JT) if (ref === 'job:' + k) st.stale = true;
        rerenderJobTab();
      },
      'drift.checked': (ev, hd) => { if (hd.isShown()) loadDrift(); else T.drift = null; },
      'operator.receipts': (ev, hd) => scheduleList(hd),
    },
    render(root, route, hd) {
      renderList(root, route, hd);
    },
  });
  T.handle = handle;
  /** items.waiting_reason depends on a job's stage ("blocking" needs needs-human or failed, "suggested" a job that is
   *  not active, "ended" one that ended), and a bare stage change publishes no items.updated (Factory.retry,
   *  _needs_human): the list, the badge, Home and an open panel follow job.updated and plan.updated when the stage
   *  moved (the first one seen of each counts as moved: the list is read once more, debounced). */
  const STAGES = new Map();
  function stageMoved(kind, obj, hd) {
    if (!SF.isObj(obj) || !obj.repo || !obj.id) return;
    const sref = kind + ':' + obj.repo + '/' + obj.id;
    const prev = STAGES.get(sref);
    STAGES.set(sref, obj.stage);
    if (prev === obj.stage) return;
    staleHistory(sref);
    if (kind === 'job') { const st = JT.get(obj.repo + '/' + obj.id); if (st) st.stale = true; scheduleDecisions(); }
    else csChanged(sref);
    scheduleList(hd);
    if (hd.isShown() && T.panelRef && scopeRefOf(T.panelRef) === sref) openItemPanel(SF.route());
  }
  let listTimer = 0;
  /** After an event: the list is read again (now when it shows, else when it next opens), the badge and Home's card now. */
  function scheduleList(hd) {
    T.listKey = '';
    clearTimeout(listTimer);
    listTimer = setTimeout(() => {
      if (hd.isShown()) { T.listKey = ''; hd.refresh(); }
      hd.refreshHome();
      loadCounts().then((r) => badge(r.counts)).catch(() => {});
    }, 300);
  }
  // This module's styles (tr-* classes) are in style.css, with every other section's.

  SF.registerJobTab({ id: 'items', label: 'Items', icon: 'checklist', render: renderJobTab });
  SF.extend('jobCard', jobCardChips);
  SF.extend('jobMeta', jobMeta);
  SF.extend('jobHead', jobHeadBlock);
  SF.extend('runHead', gridChip);
  SF.extend('form', approveField);
  SF.extend('map', mermaidButtons);
  SF.extend('chatHead', chatHeadChip);
  SF.extend('chatReply', chatReplyItems);
  SF.extend('planHead', planHeadChip);
})(window.SF);
