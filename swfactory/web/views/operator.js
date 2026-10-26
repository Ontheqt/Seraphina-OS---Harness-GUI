/* Seraphina OS Harness: the Operator (DESIGN §36 "GUI interface notes (operator persona)": who is marked, what the
 * harness guarantees and what it only records, her change receipts, the activity query; and her toolkit's status from
 * the GUI notes at the end of §39: GPU custody §37, drift sources §38, error capture §39).
 * Routes: #/operator (who, the guarantee note, her receipts), #/operator/receipt/<id> (one receipt in the panel),
 * #/operator/custody, #/operator/drift, #/operator/errors, #/activity?agent=&since=&until=&limit= ("what did I do
 * since X" for any agent or persona). Rail: "Operator", and Home: her card, whenever someone is marked, a persona that
 * can be the operator exists (the first mark is set on this page), or a custody register or drift source is set up:
 * then without an operator too, as a neutral "No operator yet" card. Home also lists what needs the owner (a rule
 * problem, a folder the harness can write, a set folder it cannot read, a mark that holds nothing). A view module:
 * it reaches the page only through window.SF.
 * Receipt text (intent, rollback, actor, every verified and not verified entry, warnings, problem, before and after,
 * refused why), activity summaries, register purposes, drift details and error bodies are outside text: SF.h() makes
 * text nodes, never HTML, Markdown or a link of their own, and none of it goes into a page title, a toast or the URL.
 * Every receipt says "Reported by the operator" and is never styled as verified: not verified comes first. */
'use strict';
(function (SF) {
  const h = SF.h;

  // ---------------------------------------------------------------- words
  const LABEL = 'Reported by the operator';
  const OPERATOR_HELP = SF.words.operatorHelp;
  // receipts.OUTCOMES' tones (SF.words.receiptTone): never green; outcome_unknown amber, reported_failed red
  const OUTCOME_TONE = SF.words.receiptTone;
  /** The outcome her receipt itself gives (operator-receipt/1, DESIGN §40): unknown amber, failed red, succeeded
   *  never green (she reports it; the harness does not check it). */
  const REPORTED_TONE = { unknown: 'warn', failed: 'err', succeeded: 'neutral' };
  // Receipts.scan's problem when no folder is set: nothing to read, which is not a folder the harness cannot read.
  const NOT_SET = 'operator.receipts_dir is not set';
  const ITEM_WORDS = SF.words.items;   // the tracker's words (items.WORDS), shared by every section
  const SOURCES = {
    receipt: { label: 'Receipt', icon: 'file', text: 'Her change receipts, as the harness read them' },
    tracker: { label: 'Tracker', icon: 'checklist', text: 'Changes to tracker items it made' },
    audit: { label: 'Audit', icon: 'list', text: 'Messages and wakes' },
    run: { label: 'Run', icon: 'play', text: 'Runs started and ended' },
    custody: { label: 'Custody', icon: 'gauge', text: 'What the harness asked the GPU custody register' },
    drift: { label: 'Drift', icon: 'eye', text: 'Drift checks worth a record' },
    snapraid: { label: 'Disk protection', icon: 'shield', text: 'The disk protection panel’s record: guard reports, health findings, runs outside Seraphina' },
  };
  // Custody answers that are not a plain grant or release: amber, never red or green (the register is hers; the harness only asked).
  const CUSTODY_PLAIN = new Set(['lease granted', 'lease released', 'lease wait ended']);
  const DRIFT_WORDS = SF.words.drift;
  const SNAPRAID_KINDS = { guard: 'Guard report', health: 'Health check', outside: 'Ran outside Seraphina' };
  const LEASE_WORDS = SF.words.lease;
  const LEASE_TEXT = {
    held: 'This harness holds it; it is renewed while its runs go on.',
    released: 'Given back at the register. That alone never means the memory is free: residency is read again.',
    lost: 'The register no longer honours it: its calls are refused.',
    unknown: 'A connection was lost during a call about it, so nobody knows the answer; nothing was sent again.',
    unreleased: 'The register could not be reached to give it back; it expires there by itself.',
  };
  const SINCE = [['60m', 'Last hour'], ['24h', 'Last 24 hours'], ['7d', 'Last 7 days'], ['30d', 'Last 30 days'], ['custom', 'From a date and time…']];
  const TABS = [['receipts', 'Receipts', 'file'], ['activity', 'Activity', 'clock'], ['custody', 'GPU custody', 'gauge'], ['drift', 'Drift checks', 'eye'], ['errors', 'Errors & capture', 'alert']];
  const LIMIT = 200;
  const RECEIPTS_MAX = 1000;   // GET /api/operator/receipts' largest limit (server.py)
  const ERRORS_KEPT = 200;
  // activity.RELATIVE_RE: a span back from now the server accepts for since and until
  const SPAN_RE = /^(\d{1,5})\s*(m|min|h|d|w)$/i;
  const SPAN_UNIT = { m: ['minute', 'minutes'], min: ['minute', 'minutes'], h: ['hour', 'hours'], d: ['day', 'days'], w: ['week', 'weeks'] };

  // ---------------------------------------------------------------- state
  const O = {
    handle: null, state: null, stateErr: null, stateLoading: null,
    receipts: null, recErr: null, recLoading: null, recAt: 0, recLimit: LIMIT, filter: 'all', scanning: false,
    detail: new Map(), personas: null, personasErr: null, personasLoading: null,
    custody: null, custodyCodes: null, custodyErr: null, custodyLoading: null, custodyAt: null, reading: false,
    drift: null, driftErr: null, driftLoading: null,
    errors: [], errorKeys: new Set(),
    behind: null, lastActivity: '', route: null, open: new Set(),
    live: null, seen: {}, focusAfter: null,
  };
  let ACT = { key: null, q: null, pages: [], first: null, loading: false, error: null, moreError: null, next: null, stale: false };

  // ---------------------------------------------------------------- small helpers
  const str = (v) => (v === null || v === undefined ? '' : String(v));
  const arr = (v) => (Array.isArray(v) ? v : []);
  /** "kate", "kate and seraphina", "a, b and c". */
  function listWords(names) {
    const n = arr(names).map(str);
    return n.length < 2 ? n.join('') : n.slice(0, -1).join(', ') + ' and ' + n[n.length - 1];
  }
  /** The server's words as a sentence of their own, so the GUI's next sentence does not run on from them. */
  const sentence = SF.sentence;
  /** Busy without disabled: a disabled button drops keyboard focus to <body>; the caller ignores clicks meanwhile. */
  function busy(b, on) {
    b.classList.toggle('is-busy', !!on);
    b.setAttribute('aria-busy', on ? 'true' : 'false');
    if (on) b.setAttribute('aria-disabled', 'true'); else b.removeAttribute('aria-disabled');
    return b;
  }
  /** Say something once to a screen reader (a polite live region outside the page, which every refresh rebuilds). */
  const announce = (text) => SF.announce(str(text));
  /** Banners are plain callouts (a refresh re-inserts them, and a role=alert would be read out each time): each is
   *  announced once, when it first appears in its tab after the tab was first drawn. */
  function noteBanners(tab, list) {
    const keys = new Set(list.map((b) => b.key));
    const seen = O.seen[tab];
    if (seen) for (const b of list) if (!seen.has(b.key) && b.say) announce(b.say);
    O.seen[tab] = keys;
  }
  const tsOf = (iso) => { const t = Date.parse(iso || ''); return isFinite(t) ? t : NaN; };
  function localWhen(iso, opts) {
    const t = tsOf(iso);
    if (!isFinite(t)) return '';
    return new Date(t).toLocaleString(undefined, opts || { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
  }
  /** A time in the viewer's own zone, the UTC text as its tooltip. */
  function whenEl(iso, cls, opts) {
    if (!iso) return h('span', { class: 'sf-muted' }, '—');
    return h('time', { class: cls || 'op-when', datetime: str(iso), title: str(iso) + ' (UTC)' }, localWhen(iso, opts) || str(iso));
  }
  const clockOf = (iso) => localWhen(iso, { hour: '2-digit', minute: '2-digit' });
  const dayOf = (iso) => localWhen(iso, { weekday: 'long', day: 'numeric', month: 'long', year: 'numeric' });
  function tag(text, cls, title) { return h('span', { class: 'tag ' + (cls || ''), title: title || null }, text); }
  function pill(text, tone, title) { return h('span', { class: 'pill no-dot tone-' + (tone || 'neutral'), title: title || null }, text); }
  const labelTag = () => tag([SF.icon('shield', 'icon-xs'), LABEL], 'tag-outline op-label', 'What she wrote, as she wrote it: the harness never re-checks her checks or tests her rollback');
  function code(text, label, focus) {
    return h('span', { class: 'op-code' }, h('code', { class: 'sf-wrap' }, str(text)),
      SF.btn('', { cls: 'btn-ghost btn-icon btn-xs', icon: 'copy', aria: 'Copy ' + label, title: 'Copy ' + label, dataset: focus ? { focus } : null, onClick: () => SF.copyText(str(text), 'Copied') }));
  }
  const itemLink = (ref) => SF.itemLink(str(ref), 'op-item');
  function itemLinks(refs) { return arr(refs).length ? h('span', { class: 'op-items' }, arr(refs).map(itemLink)) : null; }
  const receiptHref = (id) => '#/operator/receipt/' + SF.enc(id);
  function jobLink(repo, id) {
    if (!repo || !id) return null;
    return SF.job(repo, id) ? h('a', { href: '#/job/' + SF.enc(repo) + '/' + SF.enc(id), class: 'op-joblink' }, repo + '/' + id) : h('code', null, repo + '/' + id);
  }
  function section(title, id, sub, actions, kids) {
    return h('section', { class: 'op-sec', 'aria-labelledby': id },
      h('div', { class: 'op-sec-head' }, h('h2', { class: 'op-h2', id }, title), h('span', { class: 'spacer' }), actions || null),
      sub ? h('p', { class: 'op-sec-sub' }, sub) : null, kids);
  }
  function kv(pairs) {
    const out = h('dl', { class: 'sf-kv op-kv' });
    for (const [k, v] of pairs) if (v !== null && v !== undefined && v !== false) SF.add(out, [h('dt', null, k), h('dd', null, v)]);
    return out;
  }
  /** A <details> that stays open across re-renders (the page is rebuilt on every refresh). */
  function disclosure(key, cls, summary, body, data) {
    const d = h('details', { class: cls, open: O.open.has(key), dataset: Object.assign({ disclosure: key }, data || {}) },
      h('summary', { dataset: { focus: 'd-' + key } }, summary), body);
    d.addEventListener('toggle', () => { if (d.open) O.open.add(key); else O.open.delete(key); });
    return d;
  }

  // ---------------------------------------------------------------- data
  function configured() {
    const st = O.state;
    return !!st && (arr(st.operators).length > 0 || arr(st.marked).length > 0);
  }
  /** Whether the page is worth a rail entry and a Home card: someone is marked; or, with nobody marked, a persona can
   *  be (the first mark is set on this page), or it has custody registers or drift sources to show. A fresh install
   *  with none of these leaves it out. */
  function reachable() {
    if (configured()) return true;
    if (arr(O.personas).some((p) => p && (p.kind === 'openwebui' || p.kind === 'wake') && !p.error)) return true;
    return arr(O.custody).length > 0 || arr(O.drift).length > 0;
  }
  /** With nobody marked, read what reachable() needs (with someone marked, nothing more is read for it). */
  function loadReach(force) {
    if (configured()) return Promise.resolve();
    return Promise.all([loadPersonas(force), O.custody ? null : loadCustody(false), O.drift ? null : loadDrift()]);
  }
  const paintNav = () => { if (O.handle) O.handle.setNav(reachable()); };
  /** The problem of a folder that is set and cannot be read; null when it reads, or when no folder is set (then the
   *  index says NOT_SET, which is nothing to read, not an alarm). */
  function folderProblem(st) {
    const p = st && SF.isObj(st.scan) ? st.scan.problem : null;
    return p && st.receipts_dir && p !== NOT_SET ? p : null;
  }
  /** GET /api/operator → {operators, marked, several, problem, receipts_dir, label, scan} (Factory.operator_state). */
  function loadState() {
    if (O.stateLoading) return O.stateLoading;
    O.stateLoading = SF.api('/api/operator').then((st) => { O.state = SF.isObj(st) ? st : null; O.stateErr = null; })
      .catch((e) => { O.stateErr = e; })
      .finally(() => { O.stateLoading = null; paintNav(); if (O.handle) O.handle.refresh(); });
    return O.stateLoading;
  }
  /** GET /api/operator/receipts?limit=&rescan= → {receipts (newest first), refused, scan} (Factory.operator_receipts).
   *  force: ask again although a read is on its way (a new limit, a rescan). */
  function loadReceipts(rescan, force) {
    if (O.recLoading && !rescan && !force) return O.recLoading;
    const p = SF.api('/api/operator/receipts?limit=' + O.recLimit + (rescan ? '&rescan=1' : ''))
      .then((r) => { if (O.recLoading === p) { O.receipts = SF.isObj(r) ? r : { receipts: [], refused: [], scan: {} }; O.recErr = null; O.recAt = Date.now(); } })
      .catch((e) => { if (O.recLoading === p) O.recErr = e; })
      .finally(() => { if (O.recLoading === p) O.recLoading = null; if (O.handle) O.handle.refresh(); });
    O.recLoading = p;
    return p;
  }
  /** GET /api/personas. force: read again (a persona imported meanwhile). A failed read is kept as the error, never
   *  as "no personas". */
  function loadPersonas(force) {
    if (O.personasLoading) return O.personasLoading;
    if (O.personas && !force) return Promise.resolve();
    const p = SF.api('/api/personas').then((r) => { O.personas = arr(r && r.personas); O.personasErr = null; })
      .catch((e) => { O.personasErr = e; })
      .finally(() => { O.personasLoading = null; paintNav(); if (O.handle) O.handle.refresh(); });
    O.personasLoading = p;
    return p;
  }
  function loadDetail(id, force) {
    const have = O.detail.get(id);
    if (have && (have.loading || (have.data && !force))) return;
    O.detail.set(id, { loading: true, data: have ? have.data : null, error: null });
    SF.api('/api/operator/receipts/' + SF.enc(id))
      .then((r) => { O.detail.set(id, { loading: false, data: r, error: null }); })
      .catch((e) => { O.detail.set(id, { loading: false, data: null, error: e }); })
      .finally(() => { if (O.handle) { O.handle.refresh(); paintPanel(); } });
  }
  /** GET /api/custody[?refresh=1] → {providers: [...]} (Factory.custody): read only, the register is hers. */
  function loadCustody(refresh) {
    if (refresh && O.reading) return O.custodyLoading;   // one "Read now" at a time: each asks her register
    if (O.custodyLoading && !refresh) return O.custodyLoading;
    if (refresh) { O.reading = true; if (O.handle) O.handle.refresh(); }
    const p = SF.api('/api/custody' + (refresh ? '?refresh=1' : '')).then((r) => { O.custody = arr(r && r.providers); O.custodyCodes = SF.isObj(r && r.reason_codes) ? r.reason_codes : null; O.custodyErr = null; O.custodyAt = new Date().toISOString(); })
      .catch((e) => { O.custodyErr = e; })
      .finally(() => { if (O.custodyLoading === p) O.custodyLoading = null; if (refresh) O.reading = false; paintNav(); if (O.handle) O.handle.refresh(); });
    O.custodyLoading = p;
    return p;
  }
  /** GET /api/drift-sources → {sources: [...]} (DriftSources.views; never a token). */
  function loadDrift() {
    if (O.driftLoading) return O.driftLoading;
    O.driftLoading = SF.api('/api/drift-sources').then((r) => { O.drift = arr(r && r.sources); O.driftErr = null; })
      .catch((e) => { O.driftErr = e; })
      .finally(() => { O.driftLoading = null; paintNav(); if (O.handle) O.handle.refresh(); });
    return O.driftLoading;
  }
  /** agent.error and an unfinished persona.window, as the event stream brings them (its buffer at connect, then live). */
  function addError(ev) {
    const d = ev && SF.isObj(ev.data) ? ev.data : null;
    if (!d) return false;
    if (ev.type === 'persona.window' && d.ok !== false) return false;
    const key = [ev.type, ev.agent, d.failed_at || d.ended_at, d.kind, d.attempt, d.id, d.chat].join('|');
    if (O.errorKeys.has(key)) return false;
    O.errorKeys.add(key);
    O.errors.push(ev);
    if (O.errors.length > ERRORS_KEPT) { const gone = O.errors.shift(); O.errorKeys.delete([gone.type, gone.agent, (gone.data || {}).failed_at || (gone.data || {}).ended_at, (gone.data || {}).kind, (gone.data || {}).attempt, (gone.data || {}).id, (gone.data || {}).chat].join('|')); }
    return true;
  }

  // ---------------------------------------------------------------- the page frame
  function tabOf(route) {
    if (!route) return 'receipts';
    if (route.name === 'activity') return 'activity';
    const p = route.parts && route.parts[0];
    if (p === 'custody' || p === 'drift' || p === 'errors') return p;
    if (p === 'receipt' && O.behind) return tabOf(O.behind);
    return 'receipts';
  }
  function tabHref(id) {
    if (id === 'receipts') return '#/operator';
    if (id === 'activity') return O.lastActivity || '#/activity';
    return '#/operator/' + id;
  }
  function errorsBadge(n) {
    return n ? [h('span', { class: 'op-tab-n', 'aria-hidden': 'true' }, String(n)), h('span', { class: 'sr-only' }, ', ' + SF.plural(n, 'failed call'))] : null;
  }
  function tabsNav(current) {
    return h('nav', { class: 'op-tabs', 'aria-label': 'Operator sections' }, TABS.map(([id, label, ic]) =>
      h('a', { class: 'op-tab' + (id === current ? ' is-on' : ''), href: tabHref(id), 'aria-current': id === current ? 'page' : null, dataset: { tab: id, focus: 'tab-' + id } },
        SF.icon(ic, 'icon-sm'), label, id === 'errors' ? errorsBadge(O.errors.length) : null)));
  }
  /** A new failed call while another tab shows: only the Errors tab's count changes (no rebuild of the page). */
  function paintErrorsBadge() {
    const a = O.handle && O.handle.root.querySelector('.op-tab[data-tab="errors"]');
    if (!a) return;
    for (const el of Array.from(a.querySelectorAll('.op-tab-n, .sr-only'))) el.remove();
    SF.add(a, errorsBadge(O.errors.length));
  }
  function whoWords() {
    const st = O.state;
    if (!st) return null;
    const ops = arr(st.operators);
    if (ops.length) return listWords(ops) + (ops.length === 1 ? ' is the operator' : ' are the operators');
    if (arr(st.marked).length) return 'Marked, but nobody is the operator yet';
    return 'No persona is the operator';
  }
  function head(tab, route) {
    const st = O.state;
    const ops = st ? arr(st.operators) : [];
    const sub = tab === 'activity' ? 'What an agent or persona did, oldest first, from the harness’s own files: every entry says where it was read.'
      : ops.length === 1 ? 'What ' + ops[0] + ' reports she did on the server, beside the harness’s own records. The harness keeps her reports; it does not approve or block what she does.'
        : ops.length ? 'What ' + listWords(ops) + ' report they did on the server, beside the harness’s own records. The harness keeps their reports; it does not approve or block what they do.'
          : 'The operator persona’s toolkit: her change receipts, what she did, GPU custody, drift checks and error capture.';
    const title = tab === 'activity' ? 'Activity: ' + activityQuery(route).agent : 'Operator';
    const actions = tab === 'receipts' ? h('div', { class: 'op-head-actions' },
      ops.map((n, i) => h('a', { class: 'btn btn-sm', href: '#/activity?agent=' + SF.enc(n) + '&since=7d', dataset: { focus: 'head-activity-' + i, activity: n } },
        SF.icon('clock', 'icon-sm'), ops.length === 1 ? 'What she did this week' : 'What ' + n + ' did this week')),
      scanButton()) : null;
    return SF.viewHead(title, sub, actions);
  }
  function scanButton() {
    const b = SF.btn(O.scanning ? 'Scanning…' : 'Scan now', { cls: 'btn-sm btn-primary', icon: 'refresh', dataset: { action: 'scan', focus: 'scan' }, title: 'Read her receipts folder now (only what changed is read again)', onClick: scanNow });
    return busy(b, O.scanning);
  }
  async function scanNow() {
    if (O.scanning) return;
    O.scanning = true;
    O.handle.refresh();
    try {
      const r = await SF.api('/api/operator/receipts/scan', { method: 'POST', body: {} });
      const s = SF.isObj(r && r.scan) ? r.scan : {};
      const parts = [];
      if (s.new) parts.push(SF.plural(Number(s.new), 'new receipt'));
      if (s.changed) parts.push(SF.plural(Number(s.changed), 'changed receipt'));
      if (s.gone) parts.push(SF.plural(Number(s.gone), 'file') + ' gone');
      if (!s.receipts_dir || s.problem === NOT_SET) SF.toast('No receipts folder: nothing to read. Set one with “Receipts folder…”.', { kind: 'info' });
      else if (s.problem) SF.toast('The scan could not read her folder: see the page.', { kind: 'error' });
      else SF.toast(parts.length ? 'Scanned: ' + parts.join(', ') + '.' : 'Scanned: nothing new.', { kind: 'ok' });
    } catch (e) {
      SF.toastError('Scan failed', e);
    } finally {
      O.scanning = false;
      await Promise.all([loadState(), loadReceipts(false, true)]);
      O.handle.refreshHome();
    }
  }

  // ---------------------------------------------------------------- Receipts tab: banners, who, the guarantee note
  function banner(kind, key, say, kids) {
    return { key, say, el: h('div', { class: 'callout callout-' + kind + ' op-banner', dataset: { banner: key } }, SF.icon(kind === 'info' ? 'info' : 'alert', 'icon-sm'), h('div', null, kids)) };
  }
  function banners() {
    const st = O.state;
    if (!st) return null;
    const scan = SF.isObj(st.scan) ? st.scan : {};
    const out = [];
    const marked = arr(st.marked);
    const ops = arr(st.operators);
    if (!marked.length) {
      out.push(banner('info', 'none', null, [h('strong', null, 'No persona is the operator. '),
        'Mark one with “Choose the operator” below: only an Open WebUI persona or one that wakes herself can be. The tools on this page still work for every agent.']));
    }
    if (st.problem) {
      out.push(banner('warn', 'problem', 'The operator setting needs a look: until it is fixed, nobody is the operator.', [
        h('strong', null, 'The operator setting needs a look. '), h('span', { class: 'sf-wrap' }, sentence(st.problem)), ' Until it is fixed, nobody is the operator.',
        h('div', { class: 'op-banner-actions' }, SF.btn('Keep one or allow several…', { cls: 'btn-sm', dataset: { action: 'fix-rule', focus: 'fix-rule' }, onClick: fixRule }))]));
    }
    const notOps = marked.filter((n) => ops.indexOf(n) < 0);
    if (notOps.length && !st.problem) {
      out.push(banner('warn', 'not-operator', listWords(notOps) + ': marked, but not the operator.', [
        h('strong', null, listWords(notOps) + ': '), 'marked, but not the operator: ', h('a', { href: '#/doctor' }, 'see the doctor'), ' for why (only an Open WebUI persona or one that wakes herself, with a valid file, can be).']));
    }
    if (marked.length && !st.receipts_dir) {
      out.push(banner('info', 'no-folder', null, [h('strong', null, 'No receipts folder yet. '),
        'Set it with “Receipts folder…” below: until then the harness has nothing of hers to read.']));
    }
    if (scan.writable) {
      out.push(banner('err', 'writable', 'The harness’s own user can write her receipts folder: a file put there would read as hers.', [
        h('strong', null, 'The harness’s own user can write this directory, '), 'so a file put there would read as hers. Her items from it are marked untrusted. Give the directory to her user (see the ', h('a', { href: '#/doctor' }, 'doctor'), ').']));
    }
    const fp = folderProblem(st);
    if (fp) {
      out.push(banner('warn', 'scan-problem', 'The harness cannot read the receipts directory.', [
        h('strong', null, 'The harness cannot read the receipts directory: '), h('span', { class: 'sf-wrap' }, sentence(fp)), ' What was read before is still shown.']));
    }
    // A scan of GET /api/operator/receipts that failed (Factory.operator_receipts: "the scan failed (…): what was read
    // before is shown"); GET /api/operator's scan is the index's summary and never carries it.
    const listScan = O.receipts && SF.isObj(O.receipts.scan) ? O.receipts.scan : {};
    if (listScan.problem && listScan.problem !== NOT_SET && listScan.problem !== scan.problem) {
      out.push(banner('warn', 'scan-failed', 'The last scan of her folder did not finish.', [
        h('strong', null, 'The last scan did not finish: '), h('span', { class: 'sf-wrap' }, sentence(listScan.problem)), ' Scan now tries again.']));
    }
    if (Number(scan.past_limit) > 0) {
      out.push(banner('info', 'past-limit', null, [h('strong', null, SF.plural(Number(scan.past_limit), 'older receipt') + ' ' + (Number(scan.past_limit) === 1 ? 'was' : 'were') + ' not read '),
        '(operator.max_receipts). They are still in her folder; the newest are read first.']));
    }
    noteBanners('receipts', out);
    return out.length ? h('div', { class: 'op-banners' }, out.map((b) => b.el)) : null;
  }
  function whoCard() {
    const st = O.state || {};
    const scan = SF.isObj(st.scan) ? st.scan : {};
    const ops = arr(st.operators);
    const notOps = arr(st.marked).filter((n) => ops.indexOf(n) < 0);
    const opRows = ops.length ? h('ul', { class: 'op-who-list' }, ops.map((n) => h('li', { dataset: { operator: n } },
      tag([SF.icon('shield', 'icon-xs'), 'Operator'], 'tag-accent', OPERATOR_HELP), h('strong', null, n),
      h('span', { class: 'op-who-links' }, h('a', { href: '#/personas/' + SF.enc(n) }, 'her persona'), h('a', { href: '#/activity?agent=' + SF.enc(n) + '&since=7d' }, 'what she did'))))) : h('span', { class: 'sf-muted' }, 'Nobody');
    const counts = scan.scanned_at ? [SF.plural(Number(scan.read) || 0, 'receipt') + ' read', Number(scan.cannot_read) ? Number(scan.cannot_read) + ' cannot be read' : null,
      Number(scan.refused) ? Number(scan.refused) + ' not read' : null, Number(scan.gone) ? Number(scan.gone) + ' gone' : null].filter(Boolean).join(' · ') : null;
    return h('div', { class: 'sf-card op-who', dataset: { card: 'who' } },
      h('h2', { class: 'op-h2' }, 'Who and where'),
      kv([
        ['Operator', opRows],
        notOps.length ? ['Marked, not the operator', h('span', null, notOps.join(', '), ' — ', h('a', { href: '#/doctor' }, 'see the doctor'))] : null,
        ['Receipts folder', st.receipts_dir ? code(st.receipts_dir, 'the folder path', 'copy-dir') : h('span', { class: 'sf-muted' }, 'Not set')],
        ['Last scan', scan.scanned_at ? h('span', null, SF.timeEl(scan.scanned_at), counts ? h('span', { class: 'sf-muted' }, ' · ' + counts) : null) : h('span', { class: 'sf-muted' }, 'Never: the harness has not read her folder yet')],
      ].filter(Boolean)),
      h('div', { class: 'op-who-actions sf-row' },
        SF.btn('Choose the operator…', { cls: 'btn-sm', icon: 'persona', dataset: { action: 'mark', focus: 'mark' }, onClick: markDialog }),
        SF.btn('Receipts folder…', { cls: 'btn-sm', icon: 'folder', dataset: { action: 'folder', focus: 'folder' }, onClick: folderDialog })),
      h('p', { class: 'op-fine' }, OPERATOR_HELP));
  }
  const COMPARE = [
    ['Root', 'No agent of the harness holds root or a key that can change the server.', 'She holds root. The harness cannot see or limit what she runs.'],
    ['How a change happens', 'Only through the harness: its worktrees, its write rules and its sandbox.', 'Through her own tools, outside the harness.'],
    ['Approvals', 'The build steps, the merge check and must-do items stop for you.', 'None from the harness. Her own rule is to tell you before anything that cannot be undone.'],
    ['What the harness knows', 'What it ran: job files, run logs, events, the tracker.', 'What she reports: one change receipt per change, labelled “Reported by the operator”.'],
    ['Rollback', 'Checked by code before a change (the executor, in v2).', 'As she wrote it in the receipt. The harness never tests it.'],
    ['Her checks', 'Evidence checked by code, or by someone other than the owner.', 'Verified and not verified exactly as she reported them. Each check she did not do becomes a tracker item a person sees.'],
  ];
  function guaranteeCard() {
    return h('section', { class: 'sf-card op-guarantee', 'aria-labelledby': 'op-guarantee-t', dataset: { card: 'guarantee' } },
      h('h2', { class: 'op-h2', id: 'op-guarantee-t' }, 'What the harness guarantees, and what it only records'),
      SF.procedureList([
        ['She acts outside the harness.', 'The operator holds root and uses her own tools; the harness never approves or blocks her actions.'],
        ['The harness keeps what she reports.', 'One receipt per change, shown as she wrote it and labelled “Reported by the operator”. It never re-checks her checks or tests her rollback.'],
        ['What she did not check goes on the tracker.', 'Each check she reports as not verified becomes a suggestion that a person accepts or dismisses, and a person closes it.'],
        ['Its guarantees are for its own agents.', '“No agent holds root” and the approval steps are about the harness’s agents; her actions are outside them.'],
      ], 'proc-sm'),
      disclosure('compare', 'op-compare', 'Compare the two side by side',
        h('table', { class: 'op-cmp' },
          h('thead', null, h('tr', null, h('th', { scope: 'col' }, h('span', { class: 'sr-only' }, 'Topic')), h('th', { scope: 'col' }, 'The harness’s own agents'), h('th', { scope: 'col' }, 'The operator’s own actions'))),
          h('tbody', null, COMPARE.map(([topic, a, b]) => h('tr', null, h('th', { scope: 'row' }, topic),
            h('td', { dataset: { label: 'The harness’s own agents' } }, a), h('td', { dataset: { label: 'The operator’s own actions' } }, b)))))));
  }

  // ---------------------------------------------------------------- Receipts tab: the list
  const unverified = (r) => r.status === 'read' && (arr(r.not_verified).length > 0 || (r.outcome && r.outcome.state === 'nothing_verified'));
  const FILTERS = [['all', 'All', () => true], ['unverified', 'Something not verified', unverified], ['cannot', 'Cannot be read', (r) => r.status !== 'read']];
  function checksLine(r) {
    const nv = arr(r.not_verified).length;
    const v = arr(r.verified).length;
    return h('span', { class: 'op-checks' },
      h('span', { class: 'op-nv' + (nv ? ' is-some' : ''), title: 'Checks she reports as not verified' }, 'Not verified ' + nv),
      v ? h('span', { class: 'op-v', title: 'Checks she reports as verified (not re-checked by the harness)' }, 'Verified ' + v) : h('span', { class: 'op-v is-none' }, 'Nothing verified'));
  }
  function flagTags(r, dup) {
    const out = [];
    if (r.changed_at) out.push(tag('Changed after it was first read', 'tag-warn', 'Changed on ' + localWhen(r.changed_at) + ': what it said when first read stays the reference'));
    if (r.gone) out.push(tag('File gone', 'tag-outline', r.gone_at ? 'Went away ' + localWhen(r.gone_at) + '; what she reported stays reported' : null));
    if (r.duplicate_of || dup) out.push(tag('Same id as the receipt in ' + str(r.duplicate_of || dup), 'tag-outline', 'A second file with an id already read: counted once'));
    if (r.harness_writable) out.push(tag('May not be hers', 'tag-warn', 'It lies where the harness’s own user could have written it'));
    return out;
  }
  /** Her undo class as the server words it, verbatim (undo.words: "reported as …", never an executor's badge), and
   *  as she wrote it (undo.original). */
  function undoChip(undo) {
    return h('span', { class: 'op-undo-chip' },
      tag(str(undo.words || 'reversibility unknown'), undo.class ? 'tag-outline' : 'tag-warn'),
      undo.original ? h('span', { class: 'op-undo-orig' }, 'as written: ', h('code', null, str(undo.original))) : null);
  }
  /** reported_outcome.words when her receipt gives an outcome (or a response without one). */
  function reportedChip(rep) {
    if (!SF.isObj(rep)) return null;
    return pill('The change: ' + str(rep.words || 'no outcome reported'), REPORTED_TONE[rep.state] || 'warn', 'The outcome her receipt itself reports (DESIGN §40), not checked by the harness');
  }
  /** What the other side answered her, as her receipt records it (response: status, ids, a hash of its body). */
  function responseLine(rep) {
    const a = SF.isObj(rep) && SF.isObj(rep.response) ? rep.response : null;
    if (!a) return h('p', { class: 'sf-muted' }, SF.isObj(rep) ? 'Her receipt records no response from the other side.' : 'Her receipt gives no outcome of its own.');
    return kv([
      ['Status', a.status !== null && a.status !== undefined ? h('code', null, str(a.status)) : h('span', { class: 'sf-muted' }, 'not given')],
      ['Ids it returned', arr(a.ids).length ? h('span', null, arr(a.ids).map((x) => h('code', { class: 'op-dup sf-wrap' }, str(x)))) : h('span', { class: 'sf-muted' }, 'none')],
      ['Answer’s sha256', a.body_sha256 ? h('code', { class: 'sf-wrap op-sha' }, str(a.body_sha256)) : h('span', { class: 'sf-muted' }, 'no body')],
    ]);
  }
  function receiptRow(r, many) {
    if (r.status !== 'read') {
      return h('li', { class: 'op-rcpt is-unread', dataset: { file: str(r.file), status: 'cannot-read' } },
        h('div', { class: 'op-rcpt-head' }, h('code', { class: 'op-rcpt-file sf-wrap' }, str(r.file)), pill('Cannot read', 'warn'), h('span', { class: 'spacer' }), whenEl(r.mtime, 'op-when', null)),
        h('p', { class: 'op-rcpt-problem sf-wrap' }, str(r.problem)),
        h('div', { class: 'op-rcpt-chips' }, labelTag(), flagTags(r)));
    }
    const undo = SF.isObj(r.undo) ? r.undo : {};
    const outcome = SF.isObj(r.outcome) ? r.outcome : {};
    const titleId = 'op-r-' + str(r.file).replace(/[^A-Za-z0-9_-]/g, '_');
    return h('li', { class: 'op-rcpt', dataset: { receipt: str(r.id), file: str(r.file), outcome: str(outcome.state) } },
      h('div', { class: 'op-rcpt-head' },
        h('a', { class: 'op-rcpt-id', id: titleId, href: receiptHref(r.id), dataset: { focus: 'r-' + r.file } }, str(r.id)),
        h('span', { class: 'op-rcpt-actor sf-muted sf-wrap' }, 'by ', str(r.actor)),
        h('span', { class: 'spacer' }), whenEl(r.time)),
      many || r.gone || r.duplicate_of ? h('p', { class: 'op-rcpt-where' }, 'in ', h('code', { class: 'sf-wrap' }, str(r.file))) : null,
      h('p', { class: 'op-rcpt-intent sf-wrap' }, str(r.intent)),
      h('div', { class: 'op-rcpt-chips' },
        labelTag(),
        pill(str(outcome.words || 'reversibility unknown'), OUTCOME_TONE[outcome.state] || 'warn'),
        reportedChip(r.reported_outcome),
        checksLine(r),
        undoChip(undo),
        flagTags(r)),
      arr(r.items).length ? h('div', { class: 'op-rcpt-items' }, h('span', { class: 'sf-muted' }, 'On the tracker: '), itemLinks(r.items)) : null);
  }
  /** How many files the index holds (Receipts.rows lists the gone ones too): read, cannot read, gone; from the
   *  list's scan or GET /api/operator's, whichever was read later and counts more. */
  function receiptTotal(data) {
    const of = (sc) => (SF.isObj(sc) ? (Number(sc.read) || 0) + (Number(sc.cannot_read) || 0) + (Number(sc.gone) || 0) : 0);
    return Math.max(of(data && data.scan), of(O.state && O.state.scan));
  }
  /** The list asked for O.recLimit rows and the index holds more. */
  function isCut(rows, data) {
    const total = receiptTotal(data);
    return rows.length >= O.recLimit && (total > rows.length || !total);
  }
  function cutNote(rows, data) {
    if (!isCut(rows, data)) return null;
    const total = receiptTotal(data);
    return h('p', { class: 'callout callout-info op-cut', dataset: { cut: String(rows.length) } }, SF.icon('info', 'icon-sm'), h('span', null,
      total > rows.length ? 'The newest ' + rows.length + ' of ' + total + ' receipts are listed, and the counts below are of these.'
        : 'The newest ' + rows.length + ' receipts are listed; older ones are not, and the counts below are of these.'));
  }
  function cutMore(rows, data) {
    if (!isCut(rows, data)) return null;
    if (O.recLimit < RECEIPTS_MAX) {
      const n = Math.min(LIMIT, RECEIPTS_MAX - O.recLimit);
      return h('div', { class: 'op-more' }, busy(SF.btn(O.recLoading ? 'Loading…' : 'Show ' + n + ' older', { cls: 'btn-sm', icon: 'arrow-down', dataset: { action: 'receipts-more', focus: 'rcpt-more' }, onClick: showOlder }), !!O.recLoading));
    }
    const ops = O.state ? arr(O.state.operators) : [];
    const oldest = tsOf((rows[rows.length - 1] || {}).time || (rows[rows.length - 1] || {}).mtime);
    const iso = (t) => new Date(t).toISOString().replace(/\.\d{3}Z$/, 'Z');
    const href = ops.length && isFinite(oldest) ? '#/activity?agent=' + SF.enc(ops[0]) + '&since=' + SF.enc(iso(oldest - 30 * 86400000)) + '&until=' + SF.enc(iso(oldest)) : null;
    return h('p', { class: 'op-fine op-more', dataset: { focus: 'rcpt-end' }, tabindex: '-1' },'This page lists at most ' + RECEIPTS_MAX + ' receipts.',
      href ? [' Older ones are in ', h('a', { href }, 'her activity before the oldest listed'), '.'] : null);
  }
  function showOlder() {
    if (O.recLoading) return;
    const from = arr(O.receipts && O.receipts.receipts).length;
    const mine = document.activeElement && document.activeElement.dataset && document.activeElement.dataset.focus === 'rcpt-more';
    O.recLimit = Math.min(RECEIPTS_MAX, O.recLimit + LIMIT);
    loadReceipts(false, true).then(() => {
      const rows = arr(O.receipts && O.receipts.receipts);
      announce('Showing ' + rows.length + ' receipts.');
      if (mine && !(isCut(rows, O.receipts) && O.recLimit < RECEIPTS_MAX)) {   // the button is gone: go to the first new row
        O.focusAfter = [rows[from] ? 'r-' + rows[from].file : null, 'rcpt-end'];
        O.handle.refresh();
      }
    });
    O.handle.refresh();
  }
  function receiptsSection() {
    const data = O.receipts;
    if (O.recErr && !data) return section('Her receipts', 'op-rcpts-t', null, null, SF.unavailable(O.recErr, 'her receipts'));
    if (!data) return section('Her receipts', 'op-rcpts-t', null, null, SF.skeleton(4));
    const rows = arr(data.receipts);
    const counts = FILTERS.map(([id, label, fn]) => [id, label, rows.filter(fn).length]);
    const fn = (FILTERS.find((f) => f[0] === O.filter) || FILTERS[0])[2];
    const shown = rows.filter(fn);
    const filters = h('div', { class: 'op-filters', role: 'group', 'aria-label': 'Show' }, counts.map(([id, label, n]) =>
      h('button', { type: 'button', class: 'op-filter' + (O.filter === id ? ' is-on' : ''), 'aria-pressed': O.filter === id ? 'true' : 'false', dataset: { filter: id, focus: 'filter-' + id },
        onclick: () => { O.filter = id; SF.storage.set('sf.operator.filter', id); O.handle.refresh(); } }, label, h('span', { class: 'op-filter-n' }, String(n)))));
    const refused = arr(data.refused);
    const ids = new Map();
    for (const r of rows) if (r.id) ids.set(r.id, (ids.get(r.id) || 0) + 1);
    const list = shown.length ? h('ul', { class: 'op-rcpts', role: 'list' }, shown.map((r) => receiptRow(r, ids.get(r.id) > 1)))
      : SF.emptyState('file', rows.length ? 'Nothing here with this filter' : 'No receipts yet', rows.length ? 'Choose “All” to see every receipt.' : 'When she writes a change receipt into her folder, the next scan lists it here.', null, true);
    return section('Her receipts', 'op-rcpts-t', 'Newest first. Each is what she wrote about one change: what it was for, how it can be undone, and the checks she did and did not do.',
      O.recErr ? h('span', { class: 'tag tag-warn' }, 'Could not refresh: ' + O.recErr.message) : null,
      [cutNote(rows, data), filters, list, cutMore(rows, data),
        refused.length ? disclosure('refused', 'op-refused', 'Not read (' + refused.length + ')', [
          h('p', { class: 'sf-muted op-fine' }, 'Symbolic links are refused and never followed; files past the limit wait in her folder.'),
          h('ul', { class: 'op-refused-list' }, refused.map((x) => h('li', null, h('code', { class: 'sf-wrap' }, str(x.file)), h('span', { class: 'sf-wrap' }, str(x.why)))))], { refused: String(refused.length) }) : null]);
  }
  function receiptsTab() {
    return [banners(), h('div', { class: 'op-grid2' }, whoCard(), guaranteeCard()), receiptsSection()];
  }

  // ---------------------------------------------------------------- one receipt (the panel)
  function textBlock(text, empty) {
    return text ? h('p', { class: 'op-text sf-wrap' }, str(text)) : h('p', { class: 'sf-muted' }, empty);
  }
  function entryList(list, cls) { return h('ul', { class: 'op-list ' + (cls || '') }, arr(list).map((t) => h('li', { class: 'sf-wrap' }, str(t)))); }
  function checksBlock(r) {
    const nv = arr(r.not_verified);
    const v = arr(r.verified);
    return [
      h('h3', { class: 'op-h3' }, 'Not verified (' + nv.length + ')'),
      nv.length ? entryList(nv, 'op-list-nv') : h('p', { class: 'sf-muted' }, 'None: she lists no check she left undone.'),
      arr(r.items).length ? h('p', { class: 'op-fine' }, 'On the tracker, for a person to accept or dismiss: ', itemLinks(r.items)) : null,
      h('h3', { class: 'op-h3' }, v.length ? 'Verified, as she reports (' + v.length + ')' : 'Verified'),
      v.length ? entryList(v, 'op-list-v') : h('p', { class: 'op-nothing' }, 'Nothing verified'),
    ];
  }
  function jsonBox(label, value) {
    let text;
    try { text = JSON.stringify(value === undefined ? null : value, null, 2); } catch (e) { text = String(value); }
    const omitted = SF.isObj(value) && value._omitted;
    return h('div', { class: 'op-ba-col' }, h('h4', { class: 'op-h4' }, label),
      omitted ? h('p', { class: 'op-fine' }, 'Too large for the harness to keep: it kept the keys. The file has all of it.') : null,
      h('pre', { class: 'sf-pre op-json', tabindex: '0', 'aria-label': label + ', as data' }, text));
  }
  function panelBody(id) {
    const d = O.detail.get(id);
    if (!d || (d.loading && !d.data)) return SF.skeleton(6);
    if (d.error) return SF.gone(d.error) && d.error.status === 404 ? SF.emptyState('file', 'No receipt ' + id, 'The harness has not read a receipt with this id.', null, true) : SF.unavailable(d.error, 'this receipt');
    const r = SF.isObj(d.data.receipt) ? d.data.receipt : {};
    const dups = arr(d.data.duplicates);
    const undo = SF.isObj(r.undo) ? r.undo : {};
    const outcome = SF.isObj(r.outcome) ? r.outcome : {};
    const ops = O.state ? arr(O.state.operators) : [];
    const who = r.operator || (ops.length === 1 ? ops[0] : null);   // with several and none attributed: nobody to ask about
    const t = tsOf(r.time);
    const around = who && isFinite(t) ? '#/activity?agent=' + SF.enc(who) + '&since=' + SF.enc(new Date(t - 3600000).toISOString().replace(/\.\d{3}Z$/, 'Z')) + '&until=' + SF.enc(new Date(t + 3600000).toISOString().replace(/\.\d{3}Z$/, 'Z')) : null;
    return h('div', { class: 'op-panel', dataset: { receipt: str(r.id) } },
      h('div', { class: 'callout callout-info op-panel-label' }, SF.icon('shield', 'icon-sm'), h('div', null, h('strong', null, LABEL + '. '),
        'What she wrote, shown as she wrote it. The harness never re-checks her checks or tests her rollback.')),
      h('div', { class: 'op-rcpt-chips op-panel-chips' }, pill(str(outcome.words || ''), OUTCOME_TONE[outcome.state] || 'warn'), reportedChip(r.reported_outcome), flagTags(r, null)),
      outcome.state === 'outcome_unknown' ? SF.callout('warn', 'alert', 'Outcome unknown: check before retrying.', 'She reports the change may or may not have happened: look, read only, before she or anyone tries it again. Her checks reported verified do not settle it.') : null,
      h('h3', { class: 'op-h3' }, 'What it was for'),
      textBlock(r.intent, 'No intent written.'),
      checksBlock(r),
      SF.isObj(r.reported_outcome) ? [h('h3', { class: 'op-h3' }, 'How the change ended, as she reports'), h('p', { class: 'op-text sf-wrap' }, str(r.reported_outcome.words)), responseLine(r.reported_outcome)] : null,
      h('h3', { class: 'op-h3' }, 'Can it be undone?'),
      h('p', { class: 'op-undo' }, h('strong', null, str(undo.words || 'reversibility unknown')), ' ',
        h('span', { class: 'sf-muted' }, '(as written: ', h('code', null, str(undo.original)), undo.class ? [' → read as ', h('code', null, str(undo.class))] : ' → not a class this harness knows', ')')),
      h('h3', { class: 'op-h3' }, 'Rollback, as she wrote it'),
      textBlock(r.rollback, 'No rollback written.'),
      arr(r.warnings).length ? [h('h3', { class: 'op-h3' }, 'What the harness noticed (' + arr(r.warnings).length + ')'), entryList(r.warnings, 'op-list-warn')] : null,
      h('h3', { class: 'op-h3' }, 'Where it was read'),
      kv([
        ['When', h('span', null, whenEl(r.time), r.time_as_written && r.time_as_written !== r.time ? h('span', { class: 'sf-muted' }, ' (written as ', h('code', null, str(r.time_as_written)), ')') : null)],
        ['Actor', h('span', { class: 'sf-wrap' }, str(r.actor))],
        ['Attributed to', r.operator ? str(r.operator) : h('span', { class: 'sf-muted' }, 'nobody (its new items go to a person)')],
        ['Schema', h('code', null, str(r.schema))],
        ['File', code(r.file, 'the file path', 'p-file')],
        dups.length ? ['Also in', h('span', null, dups.map((f) => h('code', { class: 'op-dup sf-wrap' }, f)))] : null,
        ['sha256', h('code', { class: 'sf-wrap op-sha' }, str(r.sha256))],
        r.first_sha256 && r.first_sha256 !== r.sha256 ? ['First read as', h('code', { class: 'sf-wrap op-sha' }, str(r.first_sha256))] : null,
        ['First read', whenEl(r.first_read_at)],
        r.changed_at ? ['Changed', whenEl(r.changed_at)] : null,
        r.gone ? ['File gone', whenEl(r.gone_at)] : null,
        arr(r.jobs).length ? ['Jobs', h('span', null, arr(r.jobs).map((j) => h('code', { class: 'op-dup sf-wrap' }, str(j))))] : null,
      ].filter(Boolean)),
      h('h3', { class: 'op-h3' }, 'Before and after, as data'),
      h('div', { class: 'op-ba' }, jsonBox('Before', r.before), jsonBox('After', r.after)),
      around ? h('p', { class: 'op-fine' }, h('a', { href: around, dataset: { action: 'around' } }, SF.icon('clock', 'icon-xs'), 'What she did in the hour around it')) : null);
  }
  let panelId = null;
  function openPanel(id) {
    panelId = id;
    loadDetail(id, true);   // always read again: a scan may have changed it since; a cached copy is only the first paint
    paintPanel();
  }
  function paintPanel() {
    if (!panelId) return;
    const r = O.route && O.route.parts && O.route.parts[0] === 'receipt' ? O.route : null;
    if (!r || r.parts[1] !== panelId) return;
    const d = O.detail.get(panelId);
    const rec = d && d.data && SF.isObj(d.data.receipt) ? d.data.receipt : null;
    SF.drawer.open({ key: 'receipt:' + panelId, eyebrow: labelTag(), title: 'Receipt ' + panelId, sub: rec ? localWhen(rec.time) : null,
      body: panelBody(panelId), back: O.behind ? O.behind.hash || '#/operator' : '#/operator', wide: true,
      onClose: () => { panelId = null; } });
  }

  // ---------------------------------------------------------------- Activity tab
  function personaNames() { return arr(O.personas).map((p) => str(p && p.name)).filter(Boolean); }
  function defaultAgent() {
    const ops = O.state ? arr(O.state.operators) : [];
    return ops[0] || personaNames()[0] || 'builder';
  }
  function activityQuery(route) {
    const q = (route && route.query) || {};
    const lim = parseInt(q.limit, 10);
    return { agent: str(q.agent).trim() || defaultAgent(), since: str(q.since).trim() || '24h', until: str(q.until).trim(), limit: lim >= 1 && lim <= 5000 ? lim : LIMIT };
  }
  const actKey = (q) => [q.agent, q.since, q.until, q.limit].join('\n');
  function actHash(q) {
    return '#/activity?agent=' + SF.enc(q.agent) + (q.since ? '&since=' + SF.enc(q.since) : '') + (q.until ? '&until=' + SF.enc(q.until) : '') + (q.limit !== LIMIT ? '&limit=' + q.limit : '');
  }
  async function loadActivity(q, more) {
    if (more && ACT.loading) return;   // Show more stays focusable while it loads: ignore a second press
    const key = actKey(q);
    const before = more ? [].concat(...ACT.pages).length : 0;
    const act = document.activeElement;
    const fromMore = !!(more && act && act.dataset && act.dataset.focus === 'act-more');
    if (!more) ACT = { key, q, pages: [], first: null, loading: true, error: null, moreError: null, next: null, stale: false };
    else { ACT.loading = true; ACT.moreError = null; }
    O.handle.refresh();
    const params = 'agent=' + SF.enc(q.agent) + (q.since ? '&since=' + SF.enc(q.since) : '') + (q.until ? '&until=' + SF.enc(q.until) : '') + '&limit=' + q.limit + (more ? '&after=' + SF.enc(ACT.next) : '');
    try {
      const r = await SF.api('/api/activity?' + params);
      if (ACT.key !== key) return;
      if (!more) ACT.first = r;
      ACT.pages.push(arr(r && r.entries));
      ACT.next = r && r.truncated ? r.next_after || null : null;
      if (more) {
        const n = [].concat(...ACT.pages).length;
        const total = Number(ACT.first && ACT.first.count) || n;
        announce('Showing ' + n + ' of ' + total + (total === 1 ? ' entry.' : ' entries.'));
        if (fromMore && !ACT.next) O.focusAfter = ['ent-' + before, 'act-count'];   // the button is gone: the first new entry
      }
    } catch (e) {
      if (ACT.key !== key) return;
      if (more) { ACT.moreError = e; announce('Could not load more: ' + e.message); } else ACT.error = e;
    } finally {
      if (ACT.key === key) ACT.loading = false;
      O.handle.refresh();
    }
  }
  function toLocalInput(iso) {
    const t = tsOf(iso);
    if (!isFinite(t)) return '';
    const d = new Date(t);
    const p = (n) => String(n).padStart(2, '0');
    return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + 'T' + p(d.getHours()) + ':' + p(d.getMinutes());
  }
  function fromLocalInput(v) {
    const t = new Date(v).getTime();
    return isFinite(t) ? new Date(t).toISOString().replace(/\.\d{3}Z$/, 'Z') : '';
  }
  function spanWords(v) {
    const m = SPAN_RE.exec(str(v).trim());
    if (!m) return null;
    const n = Number(m[1]);
    const u = SPAN_UNIT[m[2].toLowerCase()];
    return n + ' ' + (n === 1 ? u[0] : u[1]);
  }
  /** A time from the URL for the date field: the server reads a time without a zone as UTC (activity.parse_when). */
  function urlTime(v) {
    const s = str(v).trim();
    return /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(s) ? s + 'Z' : s;
  }
  const UNTIL = [['', 'Now'], ['custom', 'Until a date and time…']];
  /** The Since or Until picker for the URL's value: a preset; a date for the date field; or else the value as an option
   *  of its own (a span the server takes, such as 2w or 12h, or anything else it is left to judge), so the picker never
   *  goes blank and a change of Who is never blocked by it. */
  function timeChoice(value, presets, ownLabel) {
    const v = str(value).trim();
    if (presets.some((p) => p[0] === v && p[0] !== 'custom')) return { options: presets, value: v, at: '' };
    const span = spanWords(v);
    const at = !span && /^\d{4}-\d{2}-\d{2}/.test(v) ? toLocalInput(urlTime(v)) : '';
    if (at) return { options: presets, value: 'custom', at };
    const options = presets.slice(0, -1).concat([[v, ownLabel(span, v)]], presets.slice(-1));
    return { options, value: v, at: '' };
  }
  /** The pickers. They apply on Show only (a closed <select> changes on an arrow key, and loading then would move the
   *  keyboard's place: WCAG 3.2.2). Kept as the same node across re-renders while the query and the names are the
   *  same, so a date being typed or a choice not shown yet survives an event that refreshes the page; when only the
   *  names change (the personas arrive), what is being chosen is carried into the new form. */
  let formCache = null;
  function activityForm(q) {
    const ops = O.state ? arr(O.state.operators) : [];
    const pers = personaNames().filter((n) => ops.indexOf(n) < 0);
    const agents = SF.agents().map((a) => str(a && a.name)).filter((n) => n && ops.indexOf(n) < 0 && pers.indexOf(n) < 0);
    const known = ops.concat(pers, agents);
    const qkey = actKey(q);
    const sig = qkey + '\n' + known.join(',');
    if (formCache && formCache.sig === sig) return formCache.form;
    const carry = formCache && formCache.qkey === qkey ? formCache.read() : null;
    const groups = [];
    if (ops.length) groups.push({ group: 'Operator', options: ops.map((n) => [n, n]) });
    if (pers.length) groups.push({ group: 'Personas', options: pers.map((n) => [n, n]) });
    if (agents.length) groups.push({ group: 'Factory agents', options: agents.map((n) => [n, n]) });
    if (known.indexOf(q.agent) < 0) groups.push({ group: 'Other', options: [[q.agent, q.agent]] });
    const agentSel = SF.selectEl(groups, q.agent, { id: 'op-act-agent', name: 'agent', dataset: { focus: 'act-agent' } });
    const sc = timeChoice(q.since, SINCE, (span, v) => (span ? 'Last ' + span : 'From ' + v));
    const sinceSel = SF.selectEl(sc.options, sc.value, { id: 'op-act-since', name: 'since', dataset: { focus: 'act-since' } });
    const sinceAt = h('input', { type: 'datetime-local', class: 'input', id: 'op-act-since-at', 'aria-label': 'From this date and time (your time zone)', value: sc.at, hidden: sc.value !== 'custom', dataset: { focus: 'act-since-at' } });
    const uc = timeChoice(q.until, UNTIL, (span, v) => (span ? 'Until ' + span + ' ago' : 'Until ' + v));
    const untilSel = SF.selectEl(uc.options, uc.value, { id: 'op-act-until', name: 'until', dataset: { focus: 'act-until' } });
    const untilAt = h('input', { type: 'datetime-local', class: 'input', id: 'op-act-until-at', 'aria-label': 'Until this date and time (your time zone)', value: uc.at, hidden: uc.value !== 'custom', dataset: { focus: 'act-until-at' } });
    const err = h('p', { class: 'field-error', role: 'alert', hidden: true });
    const pending = h('p', { class: 'op-act-pending', role: 'status' });
    const changed = () => agentSel.value !== q.agent || sinceSel.value !== sc.value || (sinceSel.value === 'custom' && sinceAt.value !== sc.at)
      || untilSel.value !== uc.value || (untilSel.value === 'custom' && untilAt.value !== uc.at);
    const update = () => {
      sinceAt.hidden = sinceSel.value !== 'custom';
      untilAt.hidden = untilSel.value !== 'custom';
      err.hidden = true;
      const say = changed() ? 'Not shown yet: choose Show to see it.' : '';
      if (pending.textContent !== say) pending.textContent = say;
    };
    const apply = () => {
      const next = { agent: agentSel.value, since: sinceSel.value, until: untilSel.value, limit: q.limit };
      if (next.since === 'custom') {
        next.since = sinceAt.value ? fromLocalInput(sinceAt.value) : '';
        if (!next.since) { sinceAt.hidden = false; err.textContent = 'Choose the date and time to start from.'; err.hidden = false; sinceAt.focus(); return; }
      }
      if (next.until === 'custom') {
        next.until = untilAt.value ? fromLocalInput(untilAt.value) : '';
        if (!next.until) { untilAt.hidden = false; err.textContent = 'Choose the date and time to end at, or “Now”.'; err.hidden = false; untilAt.focus(); return; }
      }
      SF.go(actHash(next));
    };
    for (const el of [agentSel, sinceSel, untilSel]) el.addEventListener('change', update);
    for (const el of [sinceAt, untilAt]) el.addEventListener('input', update);
    if (carry) {
      const put = (sel, v) => { const was = sel.value; sel.value = v; if (sel.value !== v) sel.value = was; };
      put(agentSel, carry.agent); put(sinceSel, carry.since); put(untilSel, carry.until);
      sinceAt.value = carry.sinceAt; untilAt.value = carry.untilAt;
      update();
    }
    const form = h('form', { class: 'op-act-form', 'aria-label': 'What to show', novalidate: true },
      SF.field('Who', agentSel, null, { cls: 'op-f-agent' }),
      h('div', { class: 'field op-f-since' }, h('label', { class: 'field-label', for: 'op-act-since' }, 'Since'), sinceSel, sinceAt),
      h('div', { class: 'field op-f-until' }, h('label', { class: 'field-label', for: 'op-act-until' }, 'Until'), untilSel, untilAt),
      h('div', { class: 'op-f-go' }, SF.btn('Show', { cls: 'btn-primary btn-sm', type: 'submit', icon: 'search', dataset: { action: 'show', focus: 'act-show' } })),
      pending, err);
    form.addEventListener('submit', (e) => { e.preventDefault(); apply(); });
    formCache = { sig, qkey, form, read: () => ({ agent: agentSel.value, since: sinceSel.value, sinceAt: sinceAt.value, until: untilSel.value, untilAt: untilAt.value }) };
    return form;
  }
  function kindChip(e) {
    if (e.source === 'receipt') return e.kind === 'change' ? null : pill('Cannot read', 'warn');
    if (e.source === 'custody') return pill(str(e.kind), CUSTODY_PLAIN.has(e.kind) ? 'neutral' : 'warn', CUSTODY_PLAIN.has(e.kind) ? null : 'The register is the operator’s: the harness only asked, and this is what it was told');
    if (e.source === 'drift') return pill(DRIFT_WORDS[e.status] || str(e.status), e.status === 'ok' ? 'neutral' : 'warn');
    if (e.source === 'snapraid') return pill(SNAPRAID_KINDS[e.kind] || str(e.kind), e.kind === 'outside' ? 'warn' : 'neutral');
    if (e.source === 'tracker') return pill((ITEM_WORDS[e.from] || (e.from ? str(e.from) : 'New')) + ' → ' + (ITEM_WORDS[e.to] || str(e.to)), 'neutral');
    return pill(str(e.kind), 'neutral');
  }
  function evidenceLine(e) {
    const ev = SF.isObj(e.evidence) ? e.evidence : {};
    const where = str(ev.file) + (ev.line ? ':' + ev.line : '');
    return h('div', { class: 'op-ev' }, h('span', { class: 'op-ev-k' }, 'Read from'), code(where, 'where this was read'),
      ev.sha256 ? h('span', { class: 'op-ev-sha', title: 'sha256 ' + str(ev.sha256) }, 'sha256 ' + str(ev.sha256).slice(0, 12) + '…') : null,
      ev.gone_at ? tag('File gone ' + localWhen(ev.gone_at), 'tag-outline') : null);
  }
  function receiptMore(e) {
    if (e.kind !== 'change') return null;
    const nv = arr(e.not_verified);
    const v = arr(e.verified);
    const also = arr((e.evidence || {}).also);
    const outcome = SF.isObj(e.outcome) ? e.outcome : {};
    return [
      h('div', { class: 'op-rcpt-chips' }, labelTag(), pill(str(outcome.words || ''), OUTCOME_TONE[outcome.state] || 'warn'), reportedChip(e.reported_outcome), itemLinks(e.items),
        e.receipt ? h('a', { class: 'op-open-r', href: receiptHref(e.receipt), dataset: { action: 'open-receipt', focus: 'open-' + e.receipt } }, SF.icon('file', 'icon-xs'), 'Open receipt') : null),
      disclosure('ent:' + str(e.receipt || (e.evidence || {}).file), 'op-ent-more', 'Not verified (' + nv.length + '), then verified (' + v.length + ')' + (arr(e.warnings).length ? ', notes (' + arr(e.warnings).length + ')' : '') + (also.length ? ', other files (' + also.length + ')' : ''), [
        h('h4', { class: 'op-h4' }, 'Not verified (' + nv.length + ')'), nv.length ? entryList(nv, 'op-list-nv') : h('p', { class: 'sf-muted' }, 'None reported.'),
        h('h4', { class: 'op-h4' }, 'Verified, as she reports (' + v.length + ')'), v.length ? entryList(v, 'op-list-v') : h('p', { class: 'op-nothing' }, 'Nothing verified'),
        arr(e.warnings).length ? [h('h4', { class: 'op-h4' }, 'What the harness noticed'), entryList(e.warnings, 'op-list-warn')] : null,
        also.length ? h('ul', { class: 'op-list op-also' }, also.map((a) => h('li', null, 'Also in: ', h('code', { class: 'sf-wrap' }, str(a.file)), ' (' + str(a.why) + ')'))) : null]),
    ];
  }
  function entryExtra(e) {
    if (e.source === 'receipt') return receiptMore(e);
    if (e.source === 'custody') {
      return (e.purpose || e.lease_id || e.generation !== undefined) ? h('p', { class: 'op-ent-meta' },
        e.purpose ? [h('span', { class: 'sf-muted' }, 'for '), h('span', { class: 'sf-wrap op-purpose' }, str(e.purpose))] : null,
        e.lease_id ? [h('span', { class: 'sf-muted' }, ' · lease '), h('code', null, str(e.lease_id))] : null,
        e.generation !== undefined && e.generation !== null ? h('span', { class: 'sf-muted' }, ' · generation ' + e.generation) : null) : null;
    }
    if (e.source === 'drift') {
      return h('p', { class: 'op-ent-meta' }, h('code', null, str(e.drift_source)),
        e.observed_at ? h('span', { class: 'sf-muted' }, ' · observed ', whenEl(e.observed_at)) : h('span', { class: 'sf-muted' }, ' · no observation read'),
        arr(e.items).length ? [h('span', { class: 'sf-muted' }, ' · items moved: '), itemLinks(e.items)] : null,
        e.status !== 'ok' ? h('span', { class: 'op-nodrift-note' }, ' This check says nothing about drift.') : null);
    }
    if (e.source === 'tracker') return e.ref ? h('p', { class: 'op-ent-meta' }, itemLink(e.ref)) : null;
    if (e.source === 'snapraid') return arr(e.items).length ? h('p', { class: 'op-ent-meta' }, h('span', { class: 'sf-muted' }, 'Items: '), itemLinks(e.items)) : null;
    if (e.source === 'run') {
      const where = e.repo && e.job ? jobLink(e.repo, e.job) : e.plan ? h('code', null, str(e.repo) + '/' + str(e.plan)) : e.chat ? h('code', null, 'chat ' + str(e.chat)) : e.wake ? h('code', null, 'wake ' + str(e.wake)) : null;
      return where ? h('p', { class: 'op-ent-meta' }, where) : null;
    }
    return null;
  }
  function entryRow(e, i) {
    const src = SOURCES[e.source] || { label: str(e.source), icon: 'list' };
    return h('li', { class: 'op-ent', tabindex: '-1', dataset: { source: str(e.source), kind: str(e.kind), focus: 'ent-' + i } },
      h('div', { class: 'op-ent-head' },
        h('time', { class: 'op-ent-time', datetime: str(e.at), title: str(e.at) + ' (UTC)' }, clockOf(e.at)),
        tag([SF.icon(src.icon, 'icon-xs'), src.label], 'tag-outline op-src', src.text),
        kindChip(e)),
      h('p', { class: 'op-ent-sum sf-wrap' }, str(e.summary)),
      entryExtra(e),
      evidenceLine(e));
  }
  function activityList() {
    if (ACT.error) return h('div', { class: 'op-act-err' }, SF.gone(ACT.error) ? SF.unavailable(ACT.error, 'The activity query') : SF.callout('err', 'alert', 'The activity could not be read:', ACT.error.message));
    if (!ACT.first) return SF.skeleton(6);
    const r = ACT.first;
    const entries = [].concat(...ACT.pages);
    const total = Number(r.count) || entries.length;
    const counts = SF.isObj(r.sources) ? r.sources : {};
    const errs = arr(r.errors);
    const scanErr = errs.filter((x) => /^receipt scan: /.test(str(x)));
    const otherErr = errs.filter((x) => !/^receipt scan: /.test(str(x)));
    const whoLine = h('p', { class: 'op-act-who' }, h('strong', null, str(r.agent)), r.operator ? ' is the operator: her receipts, custody and drift checks are included.' : ' is not the operator: the entries are its own.',
      ' From ', r.since ? whenEl(r.since) : 'the beginning', ' to ', r.until ? whenEl(r.until) : 'now', '.');
    const srcs = h('ul', { class: 'op-act-srcs', 'aria-label': 'Entries per source' }, Object.keys(SOURCES).map((k) => h('li', { class: Number(counts[k]) ? '' : 'is-zero', dataset: { src: k } }, SOURCES[k].label + ' ' + (Number(counts[k]) || 0))));
    const notes = [
      scanErr.length ? h('p', { class: 'op-act-note sf-muted', role: 'note' }, SF.icon('info', 'icon-xs'), h('span', { class: 'sf-wrap' }, 'The receipts are from the last good scan: ' + scanErr.map((x) => str(x).replace(/^receipt scan: /, '')).join('; '))) : null,
      otherErr.length ? h('p', { class: 'op-act-note sf-muted', role: 'note' }, SF.icon('info', 'icon-xs'), h('span', { class: 'sf-wrap' }, 'Not read: ' + otherErr.map(str).join('; ') + '. The other sources are shown.')) : null,
      ACT.stale ? h('p', { class: 'op-act-note' }, 'New receipts were read since this list was loaded. ', SF.btn('Refresh', { cls: 'btn-xs', icon: 'refresh', dataset: { action: 'act-refresh', focus: 'act-refresh' }, onClick: () => loadActivity(ACT.q, false) })) : null,
    ];
    if (!entries.length) return [whoLine, srcs, notes, SF.emptyState('clock', 'Nothing recorded in this window', 'Choose an earlier “Since”, or another agent or persona.', null, true)];
    const days = [];
    entries.forEach((e, i) => {
      const day = dayOf(e.at) || 'Unknown day';
      if (!days.length || days[days.length - 1].day !== day) days.push({ day, items: [] });
      days[days.length - 1].items.push([e, i]);
    });
    const more = ACT.next ? h('div', { class: 'op-more' },
      busy(SF.btn(ACT.loading ? 'Loading…' : 'Show more', { cls: 'btn-sm', icon: 'arrow-down', dataset: { action: 'more', focus: 'act-more' }, onClick: () => loadActivity(ACT.q, true) }), ACT.loading),
      ACT.moreError ? h('span', { class: 'field-error' }, 'Could not load more: ' + ACT.moreError.message) : null) : null;
    return [whoLine, srcs, notes,
      // Not a live region: it is rebuilt on every refresh; loadActivity announces the new count once.
      h('p', { class: 'op-act-count sf-muted', tabindex: '-1', dataset: { focus: 'act-count' } }, 'Showing ' + entries.length + ' of ' + total + (total === 1 ? ' entry' : ' entries') + ', oldest first.'),
      days.map((d, i) => h('section', { class: 'op-day', 'aria-labelledby': 'op-day-' + i }, h('h3', { class: 'op-day-t', id: 'op-day-' + i }, d.day), h('ol', { class: 'op-ents' }, d.items.map(([e, n]) => entryRow(e, n))))),
      more];
  }
  function activityTab(route) {
    const q = activityQuery(route);
    return [h('div', { class: 'sf-card op-act-card' }, activityForm(q)),
      h('div', { class: 'op-act', 'aria-busy': ACT.loading ? 'true' : 'false' }, activityList())];
  }

  // ---------------------------------------------------------------- GPU custody tab
  function residentList(res) {
    if (!res) return null;
    const list = arr(res.resident);
    return list.length ? h('div', { class: 'sf-scroll-x' }, h('table', { class: 'op-table' },
      h('thead', null, h('tr', null, h('th', { scope: 'col' }, 'Model'), h('th', { scope: 'col' }, 'Server'), h('th', { scope: 'col' }, 'Memory'), h('th', { scope: 'col' }, 'Since'))),
      h('tbody', null, list.map((x) => h('tr', null, h('td', null, h('code', { class: 'sf-wrap' }, str(x.model))), h('td', null, str(x.provider)),
        h('td', null, x.vram_mib ? (Number(x.vram_mib) / 1024).toFixed(1) + ' GB' : '—'), h('td', null, whenEl(x.since))))))) : h('p', { class: 'sf-muted' }, 'Nothing resident, as the register said.');
  }
  const pairs = (list) => (Array.isArray(list) ? (list.length ? list.map((p) => (Array.isArray(p) ? str(p[1]) : str(p))).join(', ') : 'nothing') : null);
  function leaseRow(l) {
    const lostRestart = l.state === 'lost' && /^the register restarted/.test(str(l.error));
    const word = lostRestart ? 'Lost: the register restarted' : LEASE_WORDS[l.state] || str(l.state);
    return h('li', { class: 'op-lease', dataset: { lease: str(l.lease_id), state: str(l.state) } },
      h('div', { class: 'op-lease-head' }, h('code', { class: 'sf-wrap' }, str(l.register_model || l.model)), pill(word, l.state === 'held' ? 'accent' : l.state === 'released' ? 'neutral' : 'warn', LEASE_TEXT[l.state] || null),
        h('span', { class: 'spacer' }), h('span', { class: 'sf-muted op-fine' }, 'until ', whenEl(l.until))),
      h('p', { class: 'op-ent-meta' }, h('span', { class: 'sf-muted' }, 'for '), h('span', { class: 'sf-wrap op-purpose' }, str(l.purpose)),
        h('span', { class: 'sf-muted' }, ' · lease '), h('code', null, str(l.lease_id)), h('span', { class: 'sf-muted' }, ' · generation ' + str(l.generation) + ' · ' + SF.plural(Number(l.users) || 0, 'run') + ' using it')),
      h('p', { class: 'op-fine' }, LEASE_TEXT[l.state] || ''),
      l.error ? h('p', { class: 'op-lease-err sf-wrap' }, str(l.error)) : null,
      h('p', { class: 'op-fine sf-muted' }, 'Through ', h('code', null, str(l.provider)), ' for ', h('code', null, str(l.model)),
        l.resident_before ? ['; on the GPUs when it was asked for: ', pairs(l.resident_before)] : null,
        l.resident_after ? ['; read again after it was given back: ', pairs(l.resident_after)] : null));
  }
  const historyWords = (x) => SF.custodyAnswer(x, O.custodyCodes);
  const historyPlain = SF.custodyPlain;
  function historyList(list, key) {
    const all = arr(list).slice().reverse();
    if (!all.length) return h('p', { class: 'sf-muted' }, 'No answers yet in this process.');
    const open = O['hist:' + key];
    const shown = open ? all : all.slice(0, 8);
    return [h('ol', { class: 'op-hist' }, shown.map((x) => h('li', { class: 'op-hist-row', dataset: { op: str(x.op), outcome: str(x.outcome) } },
      h('time', { class: 'op-ent-time', datetime: str(x.at), title: str(x.at) + ' (UTC)' }, localWhen(x.at, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit' })),
      pill(historyWords(x), historyPlain(x) ? 'neutral' : 'warn'),
      h('span', { class: 'op-hist-what sf-wrap' },
        x.model ? h('code', null, str(x.model)) : null, x.provider ? h('span', { class: 'sf-muted' }, ' on ' + str(x.provider)) : null,
        x.purpose ? [h('span', { class: 'sf-muted' }, ' for '), h('span', { class: 'op-purpose' }, str(x.purpose))] : null,
        x.lease_id ? [h('span', { class: 'sf-muted' }, ' · '), h('code', null, str(x.lease_id))] : null,
        x.generation !== undefined && x.generation !== null ? h('span', { class: 'sf-muted' }, ' · generation ' + x.generation) : null,
        x.reason && !(x.op === 'claim' && x.outcome === 'denied') && x.op !== 'wait' && !/^not sent: /.test(str(x.reason)) ? h('span', { class: 'op-hist-reason' }, ' — ' + str(x.reason)) : null,
        /^not sent: /.test(str(x.reason)) ? h('span', { class: 'op-hist-reason' }, ' — nothing was sent: ' + str(x.reason).replace(/^not sent: /, '')) : null,
        Array.isArray(x.resident_after) ? h('span', { class: 'sf-muted' }, ' · on the GPUs afterwards: ' + pairs(x.resident_after)) : null)))),
    all.length > 8 ? SF.btn(open ? 'Show fewer' : 'Show all ' + all.length, { cls: 'btn-xs btn-ghost', dataset: { focus: 'hist-' + key, action: 'hist-all' }, onClick: () => { O['hist:' + key] = !open; O.handle.refresh(); } }) : null];
  }
  function registerCard(group, i) {
    const reg = group.register || {};
    const res = reg.residency;
    const users = group.rows.map((row) => h('li', { dataset: { provider: str(row.provider) } }, h('code', null, str(row.provider)), ' ',
      row.custody_from ? h('span', { class: 'sf-muted' }, 'follows ' + row.custody_from + ' (the same server): its custody is edited on ' + row.custody_from)
        : row.needs_lease ? h('span', { class: 'sf-muted' }, 'needs a lease' + (row.lazy_load ? ' (loads its model on the first request)' : row.allow_model_swap ? ' (may swap models)' : ''))
          : h('span', { class: 'sf-muted' }, 'needs no lease: its requests load nothing'),
      row.needs_lease ? h('span', { class: 'op-held-wait', dataset: { part: 'held-wait' } }, ' · ', row.held_wait_seconds ? 'a HELD answer waits up to ' + Math.round(Number(row.held_wait_seconds)) + 's, once per run' : 'a HELD answer does not wait', '; FULL never waits') : null));
    const set = (group.rows.find((row) => !row.custody_from) || group.rows[0] || {}).settings || {};
    const token = set.token_credential ? ['the systemd credential ', h('code', null, str(set.token_credential)), ' (a LoadCredential= line in the harness’s unit)']
      : set.token_file ? ['the file ', h('code', { class: 'sf-wrap' }, str(set.token_file))] : set.token_env ? ['the name ', h('code', null, str(set.token_env)), ' in the env file'] : ['none'];
    const fenced = SF.isObj(reg.fenced) ? reg.fenced : null;
    const leases = arr(reg.leases);
    return h('article', { class: 'sf-card op-reg', 'aria-labelledby': 'op-reg-' + i, dataset: { register: str(reg.url) } },
      h('div', { class: 'op-reg-head' }, h('h3', { class: 'op-h3', id: 'op-reg-' + i }, 'Register ', h('code', { class: 'sf-wrap' }, str(reg.url))),
        h('span', { class: 'spacer' }), h('span', { class: 'op-gen', title: 'The highest generation this harness has seen; answers older than it are refused' },
          reg.generation === null || reg.generation === undefined ? 'no generation seen yet' : 'Generation ' + reg.generation, reg.epoch ? ' · epoch ' + str(reg.epoch) : '')),
      h('ul', { class: 'op-reg-users' }, users),
      h('p', { class: 'op-fine sf-muted', dataset: { part: 'token' } }, 'Its token: ', token, '. The token itself is never shown.'),
      fenced ? h('div', { class: 'callout callout-warn', dataset: { banner: 'fenced' } }, SF.icon('alert', 'icon-sm'), h('div', null,
        'The register’s generation went back from ' + fenced.seen + ' to ' + fenced.answered + ' (since ', whenEl(fenced.since), '): its answers are refused until it passes ' + fenced.seen + ' or the harness restarts. The register should keep its generation, or send an epoch.')) : null,
      h('div', { class: 'op-reg-cols' },
        h('div', { class: 'op-reg-col', dataset: { part: 'residency' } }, h('h4', { class: 'op-h4' }, 'On the GPUs (residency)'),
          res ? [h('p', { class: 'op-fine sf-muted' }, 'Read by the harness ', SF.timeEl(res.read_at), '; the register’s own time ', whenEl(res.observed_at), ' · generation ' + str(res.generation)), residentList(res)]
            : h('p', { class: 'op-fine' }, 'The register has not been read (or cannot be: the ', h('a', { href: '#/doctor' }, 'doctor'), '’s ', h('code', null, 'custody ' + str(group.rows[0] && group.rows[0].provider)), ' check says why).')),
        h('div', { class: 'op-reg-col', dataset: { part: 'leases' } }, h('h4', { class: 'op-h4' }, 'Leases this harness holds (' + leases.length + ')'),
          leases.length ? h('ul', { class: 'op-leases' }, leases.map(leaseRow)) : h('p', { class: 'sf-muted' }, 'None now.'))),
      disclosure('hist:' + str(reg.url), 'op-reg-hist', 'What the register answered (' + arr(reg.history).length + ')', historyList(reg.history, i)));
  }
  function custodyTab() {
    const read = busy(SF.btn(O.reading ? 'Reading…' : 'Read now', { cls: 'btn-sm', icon: 'refresh', dataset: { action: 'read-custody', focus: 'read-custody' }, title: 'Ask each register what is on the GPUs now', onClick: () => loadCustody(true) }), O.reading);
    const intro = [h('p', { class: 'op-sec-sub' }, 'The operator keeps a register of which models are on the GPUs and who may load one. The harness never writes it: it only asks, for providers whose requests may load a model, and records what it was told. A run that gets no lease goes to the next build or its overflow.'),
      SF.procedureList([
        ['Residency is what is on the GPUs.', 'It is read before every lease and again after every give-back, and kept apart from the leases: giving a lease back never proves the memory is free.'],
        ['A lease is permission for runs.', 'Runs that want the same model share one; it is renewed while they run and given back when they end.'],
        ['Answers that cannot be believed are refused.', 'An answer older than the newest generation seen is stale; a lost connection is an unknown outcome, and nothing is sent again.'],
        ['Held waits once; full moves on.', 'When someone else holds the card (HELD), a run waits once, up to its provider’s “Overflow after” time. When the card cannot fit the model now (FULL), it goes on at once to the next build or its overflow.'],
      ], 'proc-sm')];
    let body;
    if (O.custodyErr && !O.custody) body = SF.unavailable(O.custodyErr, 'GPU custody');
    else if (!O.custody) body = SF.skeleton(5);
    else if (!O.custody.length) body = SF.emptyState('gauge', 'No provider uses a custody register', 'Custody is off by default. A provider’s register is set in Settings → Models & providers.', [h('a', { class: 'btn btn-sm', href: '#/settings' }, 'Open Settings')], true);
    else {
      const groups = [];
      for (const row of O.custody) {
        const url = str(row.register && row.register.url);
        let g = groups.find((x) => x.url === url);
        if (!g) { g = { url, register: row.register, rows: [] }; groups.push(g); }
        g.rows.push(row);
        if (!row.custody_from) g.register = row.register;
      }
      const weight = (g) => (g.rows.some((r) => r.needs_lease) ? 1000 : 0) + arr(g.register && g.register.leases).length * 10 + (g.register && g.register.fenced ? 5 : 0);
      groups.sort((a, b) => weight(b) - weight(a));
      noteBanners('custody', groups.filter((g) => g.register && g.register.fenced).map((g) => ({ key: 'fenced:' + g.url, say: 'A custody register’s generation went back: its answers are refused.' })));
      body = h('div', { class: 'sf-stack' }, groups.map(registerCard));
    }
    // No event says a lease changed: the tab reads GET /api/custody each time it is shown (that never asks the
    // register), and says when.
    const asOf = O.custodyAt ? h('p', { class: 'op-fine op-asof', dataset: { part: 'as-of' } }, O.reading ? 'Asking the registers… ' : '', 'As this harness held it at ',
      whenEl(O.custodyAt, 'op-when', { hour: '2-digit', minute: '2-digit', second: '2-digit' }), '. Read now asks each register again.') : null;
    return section('GPU custody', 'op-custody-t', null, h('div', { class: 'sf-row' }, tag('Contract proposed', 'tag-warn', 'The register’s contract waits for the operator’s confirmation'), read), [intro,
      O.custodyErr && O.custody ? SF.callout('warn', 'alert', 'Could not refresh:', O.custodyErr.message) : null, asOf, body]);
  }

  // ---------------------------------------------------------------- Drift checks tab
  function driftStatement(s) {
    // GET /api/drift-sources carries no "findings" flag: last_result is set only once findings were acted on
    // (DriftSources._act), so a source that only ever sent an inventory reads ok, nothing present, last_result null.
    if (s.status === 'ok' && !arr(s.present).length && !SF.isObj(s.last_result)) {
      return h('p', { class: 'op-drift-say is-unknown', dataset: { say: 'inventory' } }, 'Inventory only: this source has sent no findings, so it says nothing about drift.');
    }
    if (s.status === 'ok') return arr(s.present).length ? h('p', { class: 'op-drift-say' }, SF.plural(arr(s.present).length, 'finding') + ' reported, from the last findings it sent.')
      : h('p', { class: 'op-drift-say is-clear' }, 'No drift reported in the last findings it sent.');
    const why = { never: 'Not checked yet: nothing is known about drift from it.', busy: 'A check is running: this says nothing about drift yet.' }[s.status];
    return h('p', { class: 'op-drift-say is-unknown' }, why || (DRIFT_WORDS[s.status] || str(s.status)) + ': this says nothing about drift. What was found before stays open.');
  }
  function driftCard(s) {
    const past = s.valid_until && tsOf(s.valid_until) < Date.now();
    const last = SF.isObj(s.last_result) ? s.last_result : null;
    return h('article', { class: 'sf-card op-drift', dataset: { source: str(s.name), status: str(s.status) }, 'aria-labelledby': 'op-ds-' + s.name },
      h('div', { class: 'op-drift-head' }, h('h3', { class: 'op-h3', id: 'op-ds-' + s.name }, str(s.name)), pill(DRIFT_WORDS[s.status] || str(s.status), s.status === 'ok' ? 'accent' : s.status === 'never' ? 'neutral' : 'warn')),
      driftStatement(s),
      s.detail ? h('p', { class: 'op-drift-detail sf-wrap' }, str(s.detail)) : null,
      kv([
        ['Last check', s.checked_at ? whenEl(s.checked_at) : h('span', { class: 'sf-muted' }, 'never')],
        ['Observed', s.observed_at ? whenEl(s.observed_at) : null],
        ['Valid until', s.valid_until ? h('span', null, whenEl(s.valid_until), past ? tag('expired', 'tag-warn') : null) : null],
        ['Next check', s.next_at ? whenEl(s.next_at) : null],
        Number(s.failures) ? ['Failures in a row', String(s.failures)] : null,
        s.http ? ['HTTP', String(s.http)] : null,
        s.schema_version ? ['Schema', h('code', null, str(s.schema_version))] : null,
        arr(s.present).length ? ['Findings', h('span', { class: 'op-keys' }, arr(s.present).map((k) => h('code', { class: 'sf-wrap' }, str(k))))] : null,
        arr(s.items).length ? ['Tracker items', itemLinks(s.items)] : null,
        last && (arr(last.opened).length || arr(last.reopened).length || arr(last.closed).length) ? ['Last change', h('span', null, ['opened', 'reopened', 'closed'].filter((k) => arr(last[k]).length).map((k) => h('span', { class: 'op-res' }, k + ' ' + arr(last[k]).length + ' ', itemLinks(last[k]))))] : null,
        s.manifest ? ['Role manifest', h('span', null, 'generated, never applied · ', h('a', { href: '#/housekeeping?scope=system' }, 'see it on the tracker'))] : null,
      ].filter(Boolean)),
      h('p', { class: 'op-fine sf-muted' }, h('code', { class: 'sf-wrap' }, str(s.url)), s.host_header ? ' · Host ' + s.host_header : '', ' · token from ' + (s.token_from === 'env' ? 'a credential name' : 'a file')));
  }
  function driftTab() {
    let body;
    if (O.driftErr && !O.drift) body = SF.unavailable(O.driftErr, 'Drift sources');
    else if (!O.drift) body = SF.skeleton(5);
    else if (!O.drift.length) body = SF.emptyState('eye', 'No drift sources are set up', 'A drift source is an outside checker the harness reads; it is added in the config (drift_sources).', null, true);
    else body = h('div', { class: 'sf-cards op-drifts' }, O.drift.map(driftCard));
    return section('Drift checks', 'op-drift-t', 'Outside, read-only checkers compare what is deployed with what should be. The harness only reads them: each finding becomes a tracker item, which closes only when a fresh, readable check no longer reports it. Only “Checked” says anything about drift.',
      h('a', { class: 'btn btn-sm', href: '#/housekeeping?scope=system', dataset: { focus: 'drift-tracker' } }, SF.icon('checklist', 'icon-sm'), 'Check now, on the tracker'), [O.driftErr && O.drift ? SF.callout('warn', 'alert', 'Could not refresh:', O.driftErr.message) : null, body]);
  }

  // ---------------------------------------------------------------- Errors & capture tab
  function minus5(iso) {
    const t = tsOf(iso);
    return isFinite(t) ? new Date(t - 300000).toISOString().replace(/\.\d{3}Z$/, 'Z') : '15m';
  }
  const slug = (s) => str(s).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 40) || 'failure';
  function captureCommand(d) {
    const at = d.failed_at || d.ended_at || '';
    return 'factory capture --since ' + minus5(at) + ' --until now --out ~/captures/' + (str(at).slice(0, 10) || 'today') + '-' + slug(d.kind === 'tool' ? d.name : d.provider || d.persona) ;
  }
  function serverLogText(d) {
    const lines = ['failed_at ' + str(d.failed_at || d.ended_at)];
    if (d.started_at) lines.push('started_at ' + str(d.started_at));
    for (const [k, v] of Object.entries(SF.isObj(d.request_ids) ? d.request_ids : {})) lines.push(k + ': ' + str(v));
    return lines.join('\n');
  }
  function statusWords(d) {
    if (d.kind === 'tool') return 'Tool call failed';
    if (d.http_status === null || d.http_status === undefined) return 'No answer';
    if (d.status !== null && d.status !== undefined && d.status !== d.http_status) return 'HTTP ' + d.http_status + ', error ' + d.status + ' in the answer';
    return 'HTTP ' + d.http_status;
  }
  function errorRow(ev, i) {
    const d = ev.data || {};
    const persona = ev.type === 'persona.window';
    const where = ev.repo && ev.job ? jobLink(ev.repo, ev.job) : d.chat ? h('code', null, 'chat ' + str(d.chat)) : null;
    const title = persona ? 'A persona turn did not finish' : d.kind === 'tool' ? 'Tool ' + str(d.name) + ' failed' : 'Model call to ' + str(d.provider) + ' failed';
    const ids = SF.isObj(d.request_ids) ? Object.entries(d.request_ids) : [];
    const key = [ev.type, ev.agent, d.failed_at || d.ended_at, d.attempt, d.id].join('|');
    const box = h('details', { class: 'op-err-box', open: O.open.has('err:' + key) },
        h('summary', { dataset: { focus: 'err-' + key } },
          h('span', { class: 'op-err-head' },
            SF.icon('chevron', 'icon-xs chev'),
            h('time', { class: 'op-ent-time', datetime: str(d.failed_at || d.ended_at), title: str(d.failed_at || d.ended_at) + ' (UTC)' }, localWhen(d.failed_at || d.ended_at, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit' })),
            h('strong', { class: 'op-err-title sf-wrap' }, title),
            persona ? null : pill(statusWords(d), 'warn'),
            persona || d.kind === 'tool' ? null : pill(d.will_retry ? 'retried' : 'gave up', 'neutral'),
            d.connection ? tag('connection lost or answer cut off', 'tag-warn') : null),
          h('span', { class: 'op-err-sub sf-muted sf-wrap' }, ev.agent ? str(ev.agent) : '', where ? [' · ', where] : null, d.model_id ? ' · ' + str(d.model_id) : '', d.attempt ? ' · attempt ' + d.attempt : '')),
        h('div', { class: 'op-err-body' },
          persona ? h('p', { class: 'op-fine' }, 'Asked ' + str(d.model) + ' ' + localWhen(d.started_at, { hour: '2-digit', minute: '2-digit', second: '2-digit' }) + ' to ' + localWhen(d.ended_at, { hour: '2-digit', minute: '2-digit', second: '2-digit' }) + ' (' + str(d.started_at) + ' to ' + str(d.ended_at) + ' UTC): the window to look for in Open WebUI’s log.') : null,
          d.error ? h('p', { class: 'op-err-msg sf-wrap' }, str(d.error)) : null,
          kv([
            ['Failed at', h('code', null, str(d.failed_at || d.ended_at))],
            d.started_at ? ['Started at', h('code', null, str(d.started_at))] : null,
            d.elapsed_ms !== undefined && d.elapsed_ms !== null ? ['Took', SF.fmtDur(Number(d.elapsed_ms)) + ' (' + d.elapsed_ms + ' ms)'] : null,
            d.url ? ['URL', h('code', { class: 'sf-wrap' }, str(d.url))] : null,
            ids.length ? ['Request ids', h('span', { class: 'op-keys' }, ids.map(([k, v]) => h('code', { class: 'sf-wrap' }, k + ': ' + str(v))))] : d.kind === 'provider' ? ['Request ids', h('span', { class: 'sf-muted' }, 'none in the answer')] : null,
          ].filter(Boolean)),
          d.kind === 'provider' && (d.body || d.body_truncated) ? disclosure('body:' + key, 'op-err-raw', 'Response body' + (d.body_truncated ? ' (first 16384 characters)' : ''), h('pre', { class: 'sf-pre op-json', tabindex: '0' }, str(d.body))) : null,
          d.kind === 'tool' ? disclosure('out:' + key, 'op-err-raw', 'Tool output' + (d.output_truncated ? ' (first 16384 characters)' : ''), [
            h('p', { class: 'op-fine sf-muted' }, 'The local run log has it; a capture bundle carries only its length and sha256.'), h('pre', { class: 'sf-pre op-json', tabindex: '0' }, str(d.output))]) : null,
          h('div', { class: 'sf-row op-err-actions' },
            persona ? null : SF.btn('Copy for the server’s log', { cls: 'btn-xs', icon: 'copy', dataset: { action: 'copy-log', focus: 'copy-log-' + i }, title: 'The request ids and the times, to find the same moment in the server’s own log', onClick: () => SF.copyText(serverLogText(d), 'Request ids and times copied') }),
            SF.btn('Copy the capture command', { cls: 'btn-xs', icon: 'terminal', dataset: { action: 'copy-capture', focus: 'copy-cap-' + i }, onClick: () => SF.copyText(captureCommand(d), 'Capture command copied') })),
          h('pre', { class: 'sf-pre op-cmd' }, captureCommand(d))));
    box.addEventListener('toggle', () => { if (box.open) O.open.add('err:' + key); else O.open.delete('err:' + key); });
    return h('li', { class: 'op-err', dataset: { kind: persona ? 'persona' : str(d.kind), at: str(d.failed_at || d.ended_at) } }, box);
  }
  function errorsTab() {
    const at = (ev) => tsOf((ev.data || {}).failed_at || (ev.data || {}).ended_at || ev.ts) || 0;
    const list = O.errors.slice().sort((a, b) => at(b) - at(a));
    return [section('Capture before you fix', 'op-capture-t', 'When a model call, a tool or a persona chat fails, take the evidence before anything is restarted, edited or retried: a restart or a retry overwrites it.', null, [
      SF.procedureList([
        ['Note the time of the failure.', 'Each failed call below has its exact time; `factory logs` has them too.'],
        ['Capture first.', 'On the server, run the capture command below (each failed call has its own, with its time filled in): change --since to a few minutes before the failure and --container to the model server’s or Open WebUI’s container. It only reads, and writes nothing outside its folder.'],
        ['Match the request ids.', 'Find each failure’s request ids and time in the container’s log in the bundle (and the server’s own logs by the same times); for a persona chat, its window bounds the turn.'],
        ['Read the bundle before sharing it.', 'Its redaction is by pattern, which is not proof. Then fix, and keep the bundle with the fix’s tracker item.'],
      ]),
      h('div', { class: 'op-cmd-row' }, h('pre', { class: 'sf-pre op-cmd' }, 'factory capture --since 15m --until now --container open-webui --out ~/captures/' + new Date().toISOString().slice(0, 10) + '-what-failed'),
        SF.btn('Copy', { cls: 'btn-xs', icon: 'copy', dataset: { action: 'copy-generic', focus: 'copy-generic' }, onClick: () => SF.copyText('factory capture --since 15m --until now --container open-webui --out ~/captures/' + new Date().toISOString().slice(0, 10) + '-what-failed', 'Capture command copied') })),
      h('p', { class: 'op-fine sf-muted' }, 'There is no capture button: the bundle is made on the server, by you, so it never leaves your hands unread.')]),
    section('Failed calls the server reported (' + list.length + ')', 'op-errors-t', 'Newest first: what the server’s event stream still held when this page connected, then each new one as it happens. Every run log keeps all of them.', null,
      list.length ? h('ol', { class: 'op-errs' }, list.map(errorRow)) : SF.emptyState('check-circle', 'No failed calls seen', 'Nothing failed since the server started, as far as its event stream still holds.', null, true))];
  }

  // ---------------------------------------------------------------- dialogs: the mark and the receipts folder
  const saveConfig = (body) => SF.api('/api/config', { body });
  const isSeveral400 = (e) => e instanceof SF.ApiError && e.status === 400 && str(e.message).indexOf('operator.several') >= 0;
  async function markDialog() {
    // Read the personas every time (one may have been imported since), and the mark as it is now.
    await Promise.all([loadPersonas(true), loadState()]);
    const st = O.state || {};
    const marked = new Set(arr(st.marked));
    const boxes = new Map();
    const KIND = { openwebui: 'Open WebUI persona', wake: 'wakes herself', snapshot: 'imported copy' };
    const wrap = h('div', { class: 'op-mark-wrap' });
    const paint = () => {
      SF.clear(wrap);
      boxes.clear();
      if (O.personasErr && !O.personas) {
        const retry = SF.btn('Try again', { cls: 'btn-sm', icon: 'refresh', dataset: { action: 'personas-retry' }, onClick: async () => {
          busy(retry, true);
          await loadPersonas(true);
          paint();
          const first = wrap.querySelector('input, button');
          if (first) first.focus();
        } });
        SF.add(wrap, [SF.callout('err', 'alert', 'The personas could not be read:', O.personasErr.message), h('div', { class: 'sf-row op-mark-retry' }, retry)]);
        return;
      }
      const rows = arr(O.personas);
      SF.add(wrap, rows.length ? h('ul', { class: 'op-mark-list' }, rows.map((p) => {
        const name = str(p.name);
        const can = (p.kind === 'openwebui' || p.kind === 'wake') && !p.error;
        const id = 'op-mark-' + name;
        const box = h('input', { type: 'checkbox', role: 'switch', id, checked: marked.has(name), disabled: !can && !marked.has(name), dataset: { persona: name } });
        boxes.set(name, box);
        return h('li', { class: 'op-mark-row' }, box, h('label', { for: id }, h('strong', null, name), h('span', { class: 'sf-muted' }, ' · ' + (KIND[p.kind] || str(p.kind)))),
          can ? null : h('span', { class: 'op-fine sf-muted' }, p.error ? 'Its file is invalid: it cannot be the operator.' : 'Only an Open WebUI persona or one that wakes herself can be the operator.'));
      })) : h('p', { class: 'sf-muted' }, 'No personas yet: import one on the Personas page first.'));
    };
    paint();
    const result = await SF.openModal({
      title: 'Choose the operator',
      description: OPERATOR_HELP + ' The mark gives her no power inside the harness.',
      body: [wrap, h('p', { class: 'op-fine' }, 'At most one persona is the operator, unless you allow several.')],
      actions: [{ label: 'Cancel', value: 'cancel' }, { label: 'Save', value: 'ok', primary: true }],
      onSubmit: async () => {
        const agents = {};
        for (const [name, box] of boxes) if (box.checked !== marked.has(name)) agents[name] = { role: box.checked ? 'operator' : null };
        const checked = Array.from(boxes).filter(([, b]) => b.checked).map(([n]) => n);
        // Several marked against the rule and nothing changed: the way out is to allow them, or to keep one.
        if (!Object.keys(agents).length) return st.problem && checked.length > 1 ? { several: checked, agents, fresh: [] } : 'same';
        try {
          await saveConfig({ agents });
          return 'saved';
        } catch (e) {
          // The one-operator rule (settings._check_operator): ask about operator.several once this dialog has closed.
          if (!isSeveral400(e)) throw e;
          return { several: checked, agents, fresh: Object.keys(agents).filter((n) => agents[n].role === 'operator') };
        }
      },
    });
    let saved = result === 'saved';
    if (result === 'same') SF.toast('Nothing changed: ' + whoWords() + '.', { kind: 'info' });
    if (result && result.several) saved = await resolveSeveral(result.several, result.agents, result.fresh);
    if (saved) await afterMark();
  }
  async function afterMark() {
    await loadState();
    O.handle.refreshHome();
    SF.toast(whoWords() + '.', { kind: 'ok' });
  }
  /** "Allow several operators?" and what the answer saves. wanted: every name that would be marked; agents: the
   *  dialog's other changes (unmarks); fresh: the names just marked (first among the keep buttons). */
  async function resolveSeveral(wanted, agents, fresh) {
    const choice = await severalDialog(wanted, fresh);
    const marked = new Set(arr(O.state && O.state.marked));
    try {
      if (choice === 'several') {
        await saveConfig(Object.assign(Object.keys(agents).length ? { agents } : {}, { operator: { several: wanted } }));
        return true;
      }
      if (typeof choice === 'string' && choice.indexOf('keep:') === 0) {
        const keep = choice.slice(5);
        const only = Object.assign({}, agents);
        for (const n of wanted) {
          const on = n === keep;
          if (on !== marked.has(n)) only[n] = { role: on ? 'operator' : null };
          else delete only[n];
        }
        if (Object.keys(only).length) { await saveConfig({ agents: only }); return true; }
      }
      SF.toast('Nothing changed: ' + whoWords() + '.', { kind: 'info' });
    } catch (e) {
      SF.toastError('Not saved', e);
    }
    return false;
  }
  function severalDialog(names, fresh) {
    const first = arr(fresh).filter((n) => names.indexOf(n) >= 0);
    const order = first.concat(names.filter((n) => first.indexOf(n) < 0));
    return SF.openModal({
      title: 'Allow several operators?',
      description: 'At most one persona is the operator unless the setting operator.several lists every one of them.',
      body: [h('p', { class: 'op-several-all' }, '“Allow several”: ' + listWords(names) + ' would all be operators, each with her own receipts and items.'),
        h('ul', { class: 'op-several-keep op-fine' }, order.map((n) => h('li', null, '“Keep only ' + n + '”: ' + n + ' is the only operator, and ' + listWords(names.filter((x) => x !== n)) + ' loses the mark.')))],
      actions: [{ label: 'Cancel', value: 'cancel' }].concat(order.map((n) => ({ label: 'Keep only ' + n, value: 'keep:' + n })), [{ label: 'Allow several', value: 'several', primary: true }]),
    });
  }
  /** The banner's way out of a broken one-operator rule (several marked, operator.several not listing them all). */
  async function fixRule() {
    await loadState();
    const st = O.state || {};
    const marked = arr(st.marked);
    if (!st.problem || marked.length < 2) { O.handle.refresh(); return; }
    if (await resolveSeveral(marked, {}, [])) await afterMark();
  }
  async function folderDialog() {
    const st = O.state || {};
    const input = h('input', { type: 'text', class: 'input', id: 'op-folder', value: str(st.receipts_dir), placeholder: '/home/<her user>/receipts', autocomplete: 'off', spellcheck: 'false' });
    const result = await SF.openModal({
      title: 'Her receipts folder',
      description: 'Where the operator writes one receipt per change. The harness’s user needs to read it and must not be able to write it: a file the harness put there would read as hers.',
      body: SF.field('Folder', input, 'A folder of her own, apart from the harness’s data, its config and your repositories. Leave it empty for none.'),
      actions: [{ label: 'Cancel', value: 'cancel' }, { label: 'Save', value: 'ok', primary: true }],
      onSubmit: async () => {
        const v = input.value.trim();
        if ((v || null) === (st.receipts_dir || null)) return 'same';
        await SF.api('/api/config', { body: { operator: { receipts_dir: v || null } } });
        return 'saved';
      },
    });
    if (result === 'saved') {
      await loadReceipts(true, true);   // the scan first, so GET /api/operator's summary is the new folder's
      await loadState();
      O.handle.refreshHome();
      const now = O.state || {};
      if (!now.receipts_dir) SF.toast('No receipts folder now: there is nothing of hers to read until one is set.', { kind: 'info' });
      else if (folderProblem(now)) SF.toast('Receipts folder saved, but the harness cannot read it: see the page.', { kind: 'error' });
      else SF.toast('Receipts folder saved; her folder was read again.', { kind: 'ok' });
    }
  }

  // ---------------------------------------------------------------- render, routes, events
  function onError(handle) {
    if (!handle.isShown()) return;
    if (tabOf(O.route) === 'errors') handle.refresh(); else paintErrorsBadge();
  }
  function render(root, route) {
    // Where the keyboard is, taken before anything is built: the pickers form is reused across renders, and putting
    // it into the new page moves it out of the document (blurring it) before SF.rebuild could see the focus.
    const was = document.activeElement;
    const keep = was && was !== root && root.contains(was) && was.dataset ? was.dataset.focus || null : null;
    const tab = tabOf(route);
    const eff = O.behind && route.parts && route.parts[0] === 'receipt' ? O.behind : route;
    let body;
    if (tab === 'activity') body = activityTab(eff);
    else if (tab === 'custody') body = custodyTab();
    else if (tab === 'drift') body = driftTab();
    else if (tab === 'errors') body = errorsTab();
    else body = O.stateErr && !O.state ? SF.unavailable(O.stateErr, 'the operator setting') : receiptsTab();
    SF.rebuild(root, h('div', { class: 'op-page', dataset: { tab } }, head(tab, eff), tabsNav(tab), body));
    if (keep && !root.contains(document.activeElement)) {
      const el = root.querySelector('[data-focus="' + CSS.escape(keep) + '"]');
      if (el) el.focus({ preventScroll: true });
    }
    // Focus a node that replaced the one the keyboard was on (the last "Show more", a panel closed by a tab change).
    if (O.focusAfter) {
      const want = [].concat(O.focusAfter).filter(Boolean);
      O.focusAfter = null;
      for (const key of want) {
        const el = root.querySelector('[data-focus="' + CSS.escape(key) + '"]');
        if (el) { el.focus({ preventScroll: false }); break; }
      }
    }
  }
  function enter(route, prev) {
    O.route = route;
    const receipt = route.parts && route.parts[0] === 'receipt' && route.parts[1] ? route.parts[1] : null;
    if (receipt) {
      if (prev && !(prev.parts && prev.parts[0] === 'receipt')) O.behind = prev;
      else if (!prev) O.behind = null;
    } else O.behind = null;
    const tab = tabOf(route);
    const arriving = !prev;   // from another view: what the page shows may have changed meanwhile
    // The Receipts tab shows the mark and the folder, which a config save changes without an event: read them again.
    if (arriving || !O.state || tab === 'receipts') loadState();
    if (tab === 'receipts') {
      if (arriving || !O.receipts || Date.now() - O.recAt > 15000) loadReceipts(false);
    } else if (tab === 'activity') {
      const src = O.behind && receipt ? O.behind : route;
      const go = () => {
        const q = activityQuery(src);
        O.lastActivity = actHash(q);
        if (ACT.key !== actKey(q)) loadActivity(q, false);
      };
      // The default agent is the operator: wait for her name (and the personas) when the URL names nobody.
      if (!(src.query && src.query.agent) && (!O.state || !O.personas)) Promise.all([O.state ? null : loadState(), loadPersonas(arriving)]).then(() => { if (O.route === route) go(); });
      else { go(); if (arriving || !O.personas) loadPersonas(arriving); }
    } else if (tab === 'custody') loadCustody(false);   // a plain GET never asks the register; no event says a lease changed
    else if (tab === 'drift') loadDrift();
    if (receipt) openPanel(receipt);
    else {
      // The route left the receipt for another tab of this view (the panel's "hour around it" link, a typed hash):
      // the app closes a module's panel only on a view change or at its back hash, so close it here.
      if (panelId && SF.drawer.isOpen('receipt:' + panelId)) {
        panelId = null;
        SF.drawer.close();
        O.focusAfter = 'tab-' + tab;
      }
      panelId = null;
    }
  }

  O.handle = SF.registerView({
    id: 'operator',
    title: 'Operator',
    routes: ['operator', 'activity'],
    // Shown once reachable() says so (from start: hidden until GET /api/operator and, with nobody marked, the rest).
    nav: { label: 'Operator', icon: 'shield', order: 70, hidden: true },
    home: {
      title: 'Operator', icon: 'shield', order: 50, href: '#/operator',
      text: 'Your operator persona acts on the server with her own tools; here is what she reports she did.',
      routes: [{ to: 'tracker', label: 'what she did not check' }],
      events: ['operator.receipts'],
      async load() {
        await loadState();
        if (O.stateErr) throw O.stateErr;
        const st = O.state;
        if (!configured()) {
          await loadReach(true);   // a persona may have been imported since
          paintNav();
          if (!reachable()) return { hidden: true };
          return { tone: null, text: 'No operator yet', detail: 'Choose one if a persona looks after the server. GPU custody, drift checks and error capture are on the same page.', needs: [] };
        }
        const ops = arr(st.operators);
        const scan = SF.isObj(st.scan) ? st.scan : {};
        const needs = [];
        if (st.problem) needs.push({ key: 'operator:problem', tone: 'warn', icon: 'alert', href: '#/operator', text: 'The operator setting needs a look', sub: sentence(st.problem) });
        const notOps = arr(st.marked).filter((n) => ops.indexOf(n) < 0);
        if (notOps.length && !st.problem) needs.push({ key: 'operator:not-operator', tone: 'warn', icon: 'alert', href: '#/operator', text: listWords(notOps) + (notOps.length === 1 ? ' is' : ' are') + ' marked, but not the operator', sub: 'See the doctor for why.' });
        if (scan.writable) needs.push({ key: 'operator:writable', tone: 'err', icon: 'alert', href: '#/operator', text: 'The harness can write her receipts folder', sub: 'A file put there would read as hers: give the folder to her user.' });
        const fp = folderProblem(st);
        if (fp) needs.push({ key: 'operator:unreadable', tone: 'warn', icon: 'alert', href: '#/operator', text: 'The harness cannot read her receipts folder', sub: sentence(fp) });
        const who = ops.length ? listWords(ops) + (ops.length === 1 ? ' is the operator' : ' are the operators') : 'Marked, but not the operator: see the doctor';
        const detail = !st.receipts_dir ? 'No receipts folder set yet.' : scan.scanned_at ? SF.plural(Number(scan.read) || 0, 'receipt') + ' read, last scan ' + SF.relTime(scan.scanned_at) : 'No scan of her receipts yet.';
        return { tone: st.problem || scan.writable || fp ? 'warn' : 'ok', text: who, detail, needs };
      },
    },
    start(handle) {
      // This module's styles (op-* classes) are in style.css, with every other section's.
      // Counts and new banners are said through SF.announce, the page's one live region outside the root.
      const f = SF.storage.get('sf.operator.filter');
      if (FILTERS.some((x) => x[0] === f)) O.filter = f;
      loadState().then(() => loadReach(false));
      SF.onReconnect(() => {
        loadState();
        if (O.receipts) loadReceipts(false);
        if (O.custody) loadCustody(false);
        if (O.drift) loadDrift();
        if (ACT.q && handle.isShown() && tabOf(O.route) === 'activity') loadActivity(ACT.q, false);
      });
    },
    enter,
    render,
    leave() { panelId = null; O.behind = null; },
    events: {
      'operator.receipts': (ev, handle) => {
        loadState();
        if (handle.isShown() && tabOf(O.route) === 'receipts') loadReceipts(false, true); else O.recAt = 0;
        if (ACT.first && !ACT.stale) {
          ACT.stale = true;
          if (handle.isShown() && tabOf(O.route) === 'activity') announce('New receipts were read since this list was loaded.');
        }
        // A scan may have changed any receipt: only the open one is worth keeping (and it is read again).
        for (const id of Array.from(O.detail.keys())) if (id !== panelId) O.detail.delete(id);
        if (panelId) loadDetail(panelId, true);
        handle.refresh();
      },
      'items.updated': (ev, handle) => {
        if (!ev.data || ev.data.ref !== 'system') return;
        if (handle.isShown() && tabOf(O.route) === 'receipts') loadReceipts(false); else O.recAt = 0;
      },
      'drift.checked': (ev, handle) => { if (handle.isShown() && tabOf(O.route) === 'drift') loadDrift(); },
      // A failed call anywhere: the Errors tab redraws; another tab only updates its count (no rebuild, which would
      // re-insert what the page shows for no change of its own).
      'agent.error': (ev, handle) => { if (addError(ev)) onError(handle); },
      'persona.window': (ev, handle) => { if (addError(ev)) onError(handle); },
    },
  });
})(window.SF);
