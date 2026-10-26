/* Seraphina OS Harness: Messages & Wakes (DESIGN §34 "GUI interface notes (W1)": the inbox, a message, what came back,
 * phone setup with the ntfy and Telegram wizards, the Messages settings with the sensitive words and event sources;
 * §35 "GUI interface notes (W2, W2b, W3)": the Schedule view, reminders, model servers, the notice rulebook, a wake
 * persona's page (her instructions and their approval, memory with versions, rollback and the weekly review, the
 * journal, the thread, her inbox, re-imports, run logs) and chat check-ins).
 * Routes: #/messages (the inbox; ?unread=1&state=&channel=&persona=), #/messages/m/<M-id> (a message over the inbox: the
 * link a phone notice opens), #/messages/inbound, #/messages/setup, #/messages/setup/ntfy/<step>,
 * #/messages/setup/telegram/<step>, #/messages/settings, #/schedule, #/schedule/persona/<name>/<tab> (a wake persona's
 * page; the Personas view links its wake rows here), #/schedule/chats, #/schedule/setup.
 * Rail: "Messages" (also on the phone's bar) with the unread count. Home: its card, and in "Needs you" unread messages,
 * a settings change outside the cockpit, a stop, the self-stop breaker and instructions changed outside a persona's page.
 * A thin strip on every other page says when Seraphina is stopped or the settings changed outside the cockpit.
 * A view module: it reaches the page only through window.SF. Every text a person, a phone, a hook or a model wrote (a
 * message's title and body, what each phone got, inbound text, Telegram names, health details, hook names and subjects,
 * the sensitive words, copy blocks, a soul and its diff, memory, the journal, the thread, run logs, gate words) is
 * text only: SF.h() makes text nodes, never HTML, never SF.md, never a link, and none of it goes into the page title, a
 * toast or the URL (the URL carries ids only). The phone passphrase, hook tokens, a bot token and the sensitive words
 * live only in this file's memory while their step is open, never in SF.storage, the URL or an error report. */
'use strict';
(function (SF) {
  const h = SF.h;

  // ---------------------------------------------------------------- words (notify.py, wakes.py, rules.py)
  const ACK_SENTENCE = 'I understand my AI agents can read my messages and send them anywhere';   // notify.ACK_SENTENCE
  const HOOK_KINDS = ['backup.failed', 'backup.ok', 'disk.low', 'service.down', 'service.up'];        // notify.HOOK_KINDS
  const RULE_WORDS = {   // notify.RULE_IDS in plain words
    'job.needs_human': 'A job stops for your decision', 'job.failed': 'A job fails', 'plan.attention': 'A plan waits for you',
    'items.overdue': 'Promises are overdue', 'items.waiting': 'Many items wait for you', 'backup.failed': 'A backup fails', 'backup.ok': 'A backup works again',
    'disk.low': 'Disk space runs low', 'service.down': 'A service stops', 'service.up': 'A service works again', 'wakes.breaker': 'Seraphina stops herself',
    'channel.down': 'A phone channel stops working', 'server.unclean': 'The server stopped without shutting down', 'wakes.tamper': 'Settings changed outside these pages',
    'inbound.forged': 'Something not from your phone arrives',
    'parity.held': 'Disk protection waits for you', 'parity.failed': 'A disk protection run fails', 'parity.ok': 'Disk protection is up to date',
    'disk.failing': 'A disk may be failing', 'parity.released_outside': 'A held run was released outside these pages',
    'parity.down': 'Disk protection stops answering', 'parity.lapsed': 'A skipped protection run is undone by a restart',
  };
  const KIND_WORDS = {   // the other notice kinds of notify.py and wakes.py, in plain words
    test: 'A test', reminder: 'A reminder you set', 'persona.integrity': 'A persona’s instructions changed', 'persona.flood': 'A persona sent too many messages',
    'persona.drift': 'A persona’s Open WebUI profile changed', 'inbound.alarm': 'Something odd came from a phone', 'inbound.hold': 'A phone message waits for you',
    reply: 'A reply', summary: 'A summary of notices that waited', notice: 'A notice', persona: 'A message from a persona', confirm: 'A confirmation',
    checkin: 'A chat check-in', 'wakes.watch': 'A watch on the schedule',
  };
  const CLASS_WORDS = { short: 'a fixed short notice', operational: 'about your server or your jobs', model: 'a persona’s own words', owner_text: 'your own words' };
  const kindWords = (kind) => RULE_WORDS[kind] || KIND_WORDS[kind] || String(kind || 'A notice');
  const STATE_WORDS = { queued: 'Waiting to send', sending: 'Sending', sent: 'Sent', failed: 'Failed', unknown: 'Outcome unknown', held: 'Held', off: 'Off',
    snoozed: 'Snoozed', inbox: 'Inbox only' };
  const STATE_TONE = { queued: 'accent', sending: 'accent', sent: 'ok', failed: 'err', unknown: 'warn', held: 'warn', off: 'muted', snoozed: 'muted', inbox: 'muted' };
  // A delivery that was sent as an outside call (DESIGN §40): its outcome in words. sent: the phone service confirmed
  // it; failed: refused, or it never left; unknown: it may or may not have arrived, and only Send again sends it again.
  const OUTCOME_OF = { sent: 'succeeded', failed: 'failed', unknown: 'unknown' };
  const VERB_WORDS = { ack: 'Got it', snooze_1h: 'Snooze 1 hour', snooze_3h: 'Snooze 3 hours', snooze_morning: 'Snooze until morning', quiet_3h: 'Quiet for 3 hours',
    quiet_morning: 'Quiet until morning', stop: 'Stop', mute: 'Mute', status: 'Status', help: 'Help' };
  const INBOUND_WORDS = { button: 'A button tap', text: 'You wrote', command: 'A command', stranger: 'A stranger wrote', pairing: 'Pairing',
    forged: 'Not from your phone: a forged tap', foreign: 'Not from your phone: a notice the harness did not send', ignored: 'Ignored (not text)',
    error: 'Could not be read', cockpit: 'Applied from these pages' };
  const INBOUND_KINDS = ['button', 'text', 'command', 'stranger', 'pairing', 'forged', 'foreign', 'ignored', 'error', 'cockpit'];
  const SOURCE_WORDS = { ambient: 'check-in', owner_message: 'your message', event: 'an event', self: 'she asked to be woken', poke: 'a poke',
    hook: 'an event source', run_now: 'you asked', catch_up: 'morning catch-up', consolidation: 'weekly memory review', chat_tool: 'a chat’s check-in',
    reminder: 'a reminder' };
  const ENDED_WORDS = { done: 'Done', declined: 'Declined', skipped: 'Skipped', failed: 'Failed', interrupted: 'Interrupted', cancelled: 'Cancelled' };
  const ENDED_TONE = { done: 'ok', declined: 'neutral', skipped: 'muted', failed: 'err', interrupted: 'warn', cancelled: 'muted' };
  const TAINT_WORDS = { private: 'private', untrusted: 'outside text' };
  const TAINT_TIPS = { private: 'It holds what you told her: kept off third-party phones', untrusted: 'It read text from outside (a forwarded note, a tool, a chat)' };
  const TAMPER_WORDS = { notify: 'phone message settings', wakes: 'persona and wake settings', pairing: 'paired phones', reminders: 'reminders',
    clean: 'privacy confirmations', terms: 'sensitive words', rules: 'the notice rulebook' };
  const PRIVACY_WORDS = { tailnet: 'Private: only over your tailnet', third_party: 'Third party: Telegram (or the server) can read it' };
  const SNOOZE_MINUTES = [[15, '15 minutes'], [30, '30 minutes'], [60, '1 hour'], [120, '2 hours'], [180, '3 hours'], [480, '8 hours'], [1440, '24 hours']];
  const RATE_FIELDS = [['per_persona_min_minutes', 'Minutes between two messages from one persona', 0, 1440], ['per_persona_max_per_day', 'Messages a day from one persona', 0, 48],
    ['personas_max_per_day', 'Messages a day from all personas', 0, 200], ['phone_max_per_day', 'Notices a day to your phones (server alerts are exempt)', 0, 200],
    ['dedupe_hours', 'Hours a repeated notice is merged into the first', 1, 168]];
  const BOUNDARY_FIELDS = [['max_title', 'Longest title a phone gets', 60], ['max_body', 'Longest text a phone gets', 300],
    ['token_run_chars', 'A run of token-like characters this long forces a heads-up', 20], ['digit_run', 'Digits in a row that force a heads-up', 8]];

  // ---------------------------------------------------------------- the data layer: one cache entry per server read
  const M = { handle: null, unread: null, panelSig: '', panelKey: null, busy: new Set(), timers: new Map(), filtersOpen: false, page: null,
    noCarry: new Set(), formErrors: new Map(), wizard: { ntfy: {}, telegram: {} }, secret: { passphrase: null, token: null, shownAt: 0 }, edits: new Map(), styled: false, strip: null, lastRoute: '',
    inboxShown: new Map(), live: null, beforeImport: new Map() };
  const D = Object.create(null);
  /** The cached answer of a read (undefined until it arrived). */
  const val = (key) => (D[key] ? D[key].v : undefined);
  const errOf = (key) => (D[key] ? D[key].err : null);
  const loaded = (key) => !!(D[key] && D[key].at);
  const INBOX_PAGE = 100;
  const INBOX_MAX = 500;   // server.py api_messages caps limit at 500
  const msgNum = (id) => parseInt(String(id || '').replace(/^M-/, ''), 10) || 0;
  /** What a key keeps of its answer: the inbox {rows, unread, more}, the inbound rows, else the answer itself. */
  function shapeOf(key, v, path, old) {
    if (key.indexOf('inbox:') === 0) {
      const limit = Number((/[?&]limit=(\d+)/.exec(path || '') || [])[1]) || INBOX_PAGE;
      const rows = arr(v && v.messages);
      const out = { rows, unread: Number(v && v.unread) || 0, more: rows.length >= limit };
      // Older pages the owner loaded beyond what one read returns stay below the fresh rows.
      if (old && old.older && rows.length && rows.length >= limit) {
        const last = msgNum(rows[rows.length - 1].id);
        const below = old.rows.filter((m) => msgNum(m.id) < last);
        if (below.length) { out.rows = rows.concat(below); out.more = old.more; }
        out.older = true;
      }
      return out;
    }
    if (key === 'inbound') return arr(v && v.inbound);
    if (key === 'channels') return arr(v && v.channels);
    return v;
  }
  /** Read path into D[key] (one request at a time per key); the view re-renders when it lands. */
  function fetchKey(key, path) {
    const d = D[key] || (D[key] = { v: undefined, err: null, at: 0, p: null });
    if (d.p) return d.p;
    let changed = false;
    d.p = SF.api(path).then((v) => {
      const next = shapeOf(key, v, path, d.v);
      changed = !d.at || !!d.err || JSON.stringify(next) !== JSON.stringify(d.v);
      d.v = next; d.err = null; d.at = Date.now();
    }, (e) => { if (e && e.status === 401) return; changed = !d.err || d.err.message !== (e && e.message); d.err = e; d.at = d.at || Date.now(); })
      // Only a changed answer redraws the page: a read that brings the same data keeps a selection, a long-press or
      // an open native picker as they are.
      .finally(() => { d.p = null; if (changed) { refresh(); if (key === 'notify' || key === 'wakes') paintStrip(); } });
    return d.p;
  }
  /** Read it unless a fresh copy is here (maxAge ms). */
  function ensure(key, path, maxAge) {
    const d = D[key];
    if (d && (d.p || (d.at && Date.now() - d.at < (maxAge === undefined ? 4000 : maxAge) && !d.err))) return d.p || Promise.resolve();
    return fetchKey(key, path);
  }
  function drop(prefix) { for (const k of Object.keys(D)) if (k === prefix || k.indexOf(prefix + ':') === 0) delete D[k]; }
  /** Read again after a change, keeping what is shown until the new answer lands (a read in flight may predate the change). */
  function reread(key, path) {
    const d = D[key];
    if (d && d.p) { d.p.then(() => fetchKey(key, path)); return; }
    fetchKey(key, path);
  }
  /** Read again soon (events come in bursts). */
  function soon(key, path, ms) {
    if (M.timers.has(key)) return;
    M.timers.set(key, setTimeout(() => { M.timers.delete(key); fetchKey(key, path); }, ms === undefined ? 250 : ms));
  }
  function refresh() { if (M.handle) M.handle.refresh(); }
  /** The next re-render shows the server's value in these fields, not what was typed (after a reload or a send). */
  function forget(...keys) { for (const k of keys) M.noCarry.add(k); }
  /** The element under root whose attribute is exactly value (no selector escaping needed). */
  const byAttr = (root, attr, value) => (root ? Array.from(root.querySelectorAll('[' + attr + ']')).find((e) => e.getAttribute(attr) === value) || null : null);
  /** A switch the server refused: back to what it was, here and in a copy a re-render made meanwhile. */
  function revertSwitch(el, value) {
    const key = el && el.dataset ? el.dataset.focus : null;
    if (el) { el.checked = value; delete el.dataset.dirty; }
    if (!key) return;
    forget(key);
    const cur = byAttr(M.handle && M.handle.root, 'data-focus', key);
    if (cur && cur !== el) { cur.checked = value; delete cur.dataset.dirty; }
  }
  /** A pairing's progress, said through the page's one live region (SF.announce): only a change is announced. */
  function announce(text) {
    const s = String(text || '');
    if (M.said === s) return;
    M.said = s;
    SF.announce(s);
  }

  /** A POST with the button busy, the server's words in a toast on failure; returns the answer or undefined. */
  async function act(button, path, body, okText, opts) {
    opts = opts || {};
    const key = opts.key || path;
    if (M.busy.has(key)) return undefined;
    M.busy.add(key);
    SF.setBusy(button, true);
    try {
      const r = await SF.api(path, { method: 'POST', body: body || {} });
      if (okText) SF.toast(okText, { kind: 'ok' });
      return r === null ? {} : r;
    } catch (e) {
      if (opts.onError) opts.onError(e); else SF.toastError(opts.what || 'The server refused', e);
      return undefined;
    } finally {
      M.busy.delete(key);
      if (button && button.isConnected) SF.setBusy(button, false);
    }
  }

  // ---------------------------------------------------------------- small helpers (all text, never HTML)
  const isObj = SF.isObj;
  const enc = SF.enc;
  const arr = (v) => (Array.isArray(v) ? v : []);
  const tag = (text, cls, title) => h('span', { class: 'tag ' + (cls || ''), title: title || null }, String(text));
  const pill = (text, tone, title) => h('span', { class: 'pill tone-' + (tone || 'neutral'), title: title || null }, String(text));
  const muted = (text) => h('span', { class: 'sf-muted' }, text);
  const textBlock = (text, cls) => h('div', { class: 'mw-text ' + (cls || '') }, String(text === null || text === undefined ? '' : text));
  const pre = (text, cls) => h('pre', { class: 'sf-pre ' + (cls || '') }, String(text === null || text === undefined ? '' : text));
  const shortSha = (s) => (s ? String(s).slice(0, 10) + '…' : '—');
  function section(title, sub, body, opts) {
    opts = opts || {};
    const id = opts.id || ('mw-s-' + String(title).toLowerCase().replace(/[^a-z0-9]+/g, '-'));
    return h('section', { class: 'mw-sec sf-card' + (opts.cls ? ' ' + opts.cls : ''), 'aria-labelledby': id + '-t', id, dataset: opts.data || null },
      h('div', { class: 'mw-sec-head' }, h('h2', { class: 'mw-sec-title', id: id + '-t' }, title), h('span', { class: 'spacer' }), opts.actions || null),
      sub ? h('p', { class: 'mw-sec-sub' }, sub) : null, body);
  }
  /** "14:05" today, "Mon 14:05" this week, else the date, in the browser's time zone; the full time on hover. */
  function localTime(ts) {
    const t = Date.parse(ts || '');
    if (!isFinite(t)) return '';
    const d = new Date(t);
    const now = new Date();
    const hm = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    if (d.toDateString() === now.toDateString()) return hm;
    if (Math.abs(t - now.getTime()) < 6 * 86400000) return d.toLocaleDateString([], { weekday: 'short' }) + ' ' + hm;
    return d.toLocaleDateString([], { day: 'numeric', month: 'short' }) + ' ' + hm;
  }
  function when(ts, cls) {
    const t = Date.parse(ts || '');
    if (!isFinite(t)) return muted('—');
    if (t > Date.now() + 5000) {
      const m = Math.round((t - Date.now()) / 60000);
      const rel = m < 1 ? 'in under a minute' : m < 60 ? 'in ' + m + ' min' : m < 48 * 60 ? 'in ' + Math.round(m / 60) + ' h' : 'in ' + Math.round(m / 1440) + ' days';
      return h('time', { class: cls || 'rel', datetime: ts, title: new Date(t).toLocaleString() }, localTime(ts) + ' (' + rel + ')');
    }
    return SF.timeEl(ts, cls);
  }
  const fmtSecs = (s) => (Number(s) >= 0 ? SF.fmtDur(Number(s) * 1000) : '—');
  function taintChips(taint) {
    return arr(taint).filter((t) => TAINT_WORDS[t]).map((t) => tag(TAINT_WORDS[t], t === 'untrusted' ? 'tag-warn mw-taint' : 'tag-outline mw-taint', TAINT_TIPS[t]));
  }
  /** job:<repo>/<id> → the job's drawer; plan:<repo>/<id> → the plan. */
  function aboutLink(about) {
    const m = /^(job|plan):([^/]+)\/(.+)$/.exec(String(about || ''));
    if (!m) return null;
    const href = m[1] === 'job' ? '#/job/' + enc(m[2]) + '/' + enc(m[3]) : '#/plans/' + enc(m[2]) + '/' + enc(m[3]);
    return h('a', { href, class: 'mw-about', dataset: { focus: 'about:' + about } }, icon(m[1] === 'job' ? 'factory' : 'plan'), (m[1] === 'job' ? 'Job ' : 'Plan ') + m[3].split('-')[0] + ' (' + m[2] + ')');
  }
  const icon = (name, cls) => SF.icon(name, cls || 'icon-sm');
  function senderName(row) {
    if (!row || row.sender === 'server' || !row.sender) return 'Your server';
    const w = val('wakes');
    const p = w && arr(w.personas).find((x) => x.name === (row.persona || row.sender));
    return p && p.display ? String(p.display) : String(row.persona || row.sender);
  }
  function personaDisplay(name) {
    const w = val('wakes');
    const p = w && arr(w.personas).find((x) => x.name === name);
    return p && p.display ? String(p.display) : String(name || '');
  }
  function retentionDays() {
    const c = val('config');
    const r = c && isObj(c.wakes) && isObj(c.wakes.retention) ? c.wakes.retention.text_days : null;
    return Number(r) > 0 ? Number(r) : 90;
  }
  /** A labelled switch (a checkbox with role switch). */
  function toggle(label, checked, hint, attrs, onChange) {
    const input = h('input', Object.assign({ type: 'checkbox', role: 'switch', checked: !!checked, class: 'mw-switch-input' }, attrs || {}));
    if (onChange) input.addEventListener('change', () => onChange(input.checked, input));
    return h('label', { class: 'mw-switch' }, input, h('span', { class: 'mw-switch-text' }, h('span', { class: 'mw-switch-label' }, label), hint ? h('span', { class: 'field-hint' }, hint) : null));
  }
  function checkRow(label, checked, attrs) {
    return h('label', { class: 'mw-check' }, h('input', Object.assign({ type: 'checkbox', checked: !!checked }, attrs || {})), h('span', null, label));
  }
  /** A text field; one with a dataset.field gets the focus key fld:<field> (so a re-render keeps focus and what was typed). */
  function withFocus(attrs) {
    const a = Object.assign({}, attrs || {});
    if (a.dataset && a.dataset.field && !a.dataset.focus) a.dataset = Object.assign({}, a.dataset, { focus: 'fld:' + a.dataset.field });
    return a;
  }
  function input(attrs) { return h('input', Object.assign({ class: 'input', type: 'text', autocomplete: 'off', spellcheck: 'false' }, withFocus(attrs))); }
  function numberInput(value, min, max, attrs) { return h('input', Object.assign({ class: 'input mw-num', type: 'number', min: String(min), max: String(max), step: '1', value: value === null || value === undefined ? '' : String(value) }, withFocus(attrs))); }
  const linkBtn = (label, href, opts) => h('a', { class: 'btn ' + ((opts && opts.cls) || 'btn-sm'), href, dataset: Object.assign({ focus: 'link:' + href }, (opts && opts.dataset) || {}) }, opts && opts.icon ? icon(opts.icon) : null, label);
  function errorBox(e, what) {
    if (!e) return null;
    return SF.unavailable(e, what);
  }
  /** Show a refusal (the server's {error}) under a form, verbatim; it stays through re-renders until the route changes. */
  function formError(el, e) {
    if (!el) return;
    const msg = e ? (e.message ? e.message : String(e)) : null;
    if (el.dataset.err) { if (msg) M.formErrors.set(el.dataset.err, msg); else M.formErrors.delete(el.dataset.err); }
    paintError(el, msg);
  }
  function paintError(el, msg) {
    SF.clear(el);
    el.hidden = !msg;
    if (msg) { el.appendChild(icon('alert')); el.appendChild(h('span', null, msg)); }
  }
  /** The error line of one form (key: where it is, so a re-render finds its message again). */
  function errLine(key) {
    const el = h('div', { class: 'form-error mw-form-error', role: 'alert', hidden: true, dataset: { err: key } });
    if (M.formErrors.has(key)) paintError(el, M.formErrors.get(key));
    return el;
  }



  // ---------------------------------------------------------------- the page model: route → page
  /** {kind: inbox|inbound|setup|ntfy|telegram|settings|schedule|persona|chats|wakesetup, ...}. */
  function pageOf(route) {
    const p = route.parts || [];
    if (route.name === 'schedule') {
      if (p[0] === 'persona' && p[1]) return { kind: 'persona', name: p[1], tab: p[2] || 'overview' };
      if (p[0] === 'chats') return { kind: 'chats', chat: p[1] || null };
      if (p[0] === 'setup') return { kind: 'wakesetup' };
      return { kind: 'schedule' };
    }
    if (p[0] === 'm' && p[1]) return { kind: 'inbox', message: p[1] };
    if (p[0] === 'inbound') return { kind: 'inbound' };
    if (p[0] === 'setup' && (p[1] === 'ntfy' || p[1] === 'telegram')) return { kind: p[1], step: Math.max(1, Math.min(7, parseInt(p[2], 10) || 1)) };
    if (p[0] === 'setup') return { kind: 'setup' };
    if (p[0] === 'settings') return { kind: 'settings' };
    return { kind: 'inbox' };
  }
  const TABS = [['inbox', 'Inbox', '#/messages', 'inbox'], ['inbound', 'What came back', '#/messages/inbound', 'send'],
    ['setup', 'Phone setup', '#/messages/setup', 'bell'], ['settings', 'Settings', '#/messages/settings', 'settings'],
    ['schedule', 'Schedule', '#/schedule', 'clock'], ['chats', 'Chat check-ins', '#/schedule/chats', 'chat']];
  const TAB_OF = { inbox: 'inbox', inbound: 'inbound', setup: 'setup', ntfy: 'setup', telegram: 'setup', settings: 'settings', schedule: 'schedule',
    persona: 'schedule', chats: 'chats', wakesetup: 'schedule' };
  function tabsNav(page) {
    const cur = TAB_OF[page.kind];
    return h('nav', { class: 'mw-tabs', 'aria-label': 'Messages & Wakes' }, TABS.map(([id, label, href, ic]) => {
      const a = h('a', { class: 'mw-tab', href, dataset: { focus: 'tab:' + id, tab: id }, 'aria-current': id === cur ? 'page' : null }, icon(ic), h('span', null, label));
      if (id === 'inbox' && M.unread) a.appendChild(h('span', { class: 'count-pill has', 'aria-label': SF.plural(M.unread, 'unread message') }, String(M.unread)));
      return a;
    }));
  }

  // ---------------------------------------------------------------- banners (GET /api/notify, GET /api/wakes)
  function restartButton(label, ok) {
    return SF.btn(label || 'Restart', { cls: 'btn-sm btn-primary', icon: 'play', dataset: { action: 'restart', focus: 'banner:restart' }, onClick: async (e) => {
      const r = await act(e.currentTarget, '/api/wakes/control', { action: 'resume' }, ok || 'Seraphina is running again.', { what: 'Could not restart' });
      if (r) reloadState();
    } });
  }
  function banner(kind, iconName, title, text, actions, data) {
    return h('div', { class: 'mw-banner callout callout-' + kind, role: kind === 'err' || kind === 'warn' ? 'alert' : 'status', dataset: Object.assign({ banner: data || '' }, {}) },
      icon(iconName), h('div', { class: 'mw-banner-body' }, h('strong', null, title), text ? h('span', null, ' ', text) : null),
      actions && actions.length ? h('div', { class: 'mw-banner-actions' }, actions) : null);
  }
  function tamperWhat(t) {
    const what = arr(t && t.what).map((w) => TAMPER_WORDS[w] || String(w));
    return what.length ? what.join(', ') : 'some settings';
  }
  function banners(page) {
    const st = val('notify');
    const wk = val('wakes');
    const out = [];
    if (st && isObj(st.tamper)) {
      out.push(banner('warn', 'shield', 'Settings changed outside these pages:', tamperWhat(st.tamper) + '. Phones can only stop until you look.', [
        linkBtn('Show the settings', '#/messages/settings', { cls: 'btn-sm', icon: 'settings' }),
        SF.btn('Accept', { cls: 'btn-sm', icon: 'check', dataset: { action: 'review-accept', focus: 'banner:accept' }, onClick: (e) => review(e.currentTarget, 'accept') }),
        SF.btn('Accept and restart', { cls: 'btn-sm btn-primary', icon: 'play', dataset: { action: 'review-restart', focus: 'banner:accept-restart' }, onClick: (e) => review(e.currentTarget, 'restart') }),
      ], 'tamper'));
    }
    const paused = (st && st.paused) || (wk && wk.paused);
    if (isObj(paused) && paused.by !== 'tamper-check') {
      out.push(banner('warn', 'stop', 'Seraphina is stopped', '(from ' + (paused.via || paused.by || 'somewhere') + (paused.at ? ' at ' + localTime(paused.at) : '') +
        '). Alerts from your server still come; her check-ins and messages wait until you restart her.', [restartButton()], 'paused'));
    } else if (isObj(paused) && !(st && isObj(st.tamper))) {
      out.push(banner('warn', 'shield', 'Settings changed outside these pages, so Seraphina stopped.', 'Review the settings, then restart her.', [
        linkBtn('Show the settings', '#/messages/settings', { icon: 'settings' }), restartButton('Restart')], 'paused-tamper'));
    }
    if (wk && isObj(wk.breaker) && wk.breaker.open) {
      out.push(banner('warn', 'zap', 'Seraphina stopped herself:', String(wk.breaker.why || '') + '. Alerts from your server still work.', [restartButton('Restart', 'Seraphina will try again.')], 'breaker'));
    }
    if (st && isObj(st.precondition)) {
      const pre = st.precondition;
      if (!pre.ok) {
        out.push(banner('warn', 'shield', 'Phone messages are off:', 'your agents run without the sandbox (Settings › Harness) or through a command-line driver, so they could read your messages.', [
          linkBtn('Settings › Harness', '#/settings/harness', { icon: 'settings' }),
          SF.btn('I accept the risk', { cls: 'btn-sm', dataset: { action: 'acknowledge', focus: 'banner:ack' }, onClick: () => acknowledgeDialog() })], 'precondition'));
      } else if (pre.acknowledged_at) {
        const on = new Date(Date.parse(pre.acknowledged_at)).toLocaleDateString();
        out.push(banner(pre.sandboxed ? 'info' : 'err', pre.sandboxed ? 'shield' : 'alert', 'You accepted that your agents can read your messages',
          '(on ' + on + '). ' + (pre.sandboxed ? 'The sandbox is on now: you can withdraw the acceptance.' : 'Turn on the sandbox, then withdraw it.'), [
          SF.btn('Withdraw', { cls: 'btn-sm', dataset: { action: 'withdraw', focus: 'banner:withdraw' }, onClick: async (e) => {
            const r = await act(e.currentTarget, '/api/wakes/acknowledge', { withdraw: true }, 'Withdrawn: phone messages stay off until the sandbox is on.');
            if (r) reloadState();
          } })], 'acknowledged'));
      }
    }
    if (st && st.timezone_error) out.push(banner('warn', 'clock', 'Your time zone setting is wrong', '(' + String(st.timezone_error) + '); the machine’s zone is used.', null, 'timezone'));
    if ((st && isObj(st.time_sync) && st.time_sync.ready === false) || (wk && wk.time_synced === false)) {
      out.push(banner('info', 'hourglass', 'Waiting for the clock after boot:', 'nothing is sent yet.', null, 'time-sync'));
    }
    if (wk && wk.enabled === false && (page.kind === 'schedule' || page.kind === 'persona')) {
      out.push(banner('info', 'persona', 'Personas that wake themselves are off.', 'Turn them on in persona setup when you want check-ins.', [linkBtn('Persona setup', '#/schedule/setup', { icon: 'settings' })], 'wakes-off'));
    }
    const chips = [];
    if (st && isObj(st.quiet_hours) && st.quiet_hours.now) {
      chips.push(h('span', { class: 'mw-chip', title: st.quiet_hours.source ? 'Do-not-disturb time ' + st.quiet_hours.source : null, dataset: { chip: 'dnd' } }, icon('moon'),
        st.quiet_until ? 'Quiet (you asked) until ' + localTime(st.quiet_until) : 'Do-not-disturb until ' + (st.quiet_hours.until || '?')));
    } else if (st && st.quiet_until && Date.parse(st.quiet_until) > Date.now()) {
      chips.push(h('span', { class: 'mw-chip', dataset: { chip: 'quiet' } }, icon('moon'), 'Quiet (you asked) until ' + localTime(st.quiet_until)));
    }
    if (st && isObj(st.today)) chips.push(h('span', { class: 'mw-chip', dataset: { chip: 'today' }, title: 'Alerts from your server do not count' }, icon('bell'), (st.today.phone || 0) + ' of ' + (st.today.cap || '?') + ' phone notices today'));
    const box = h('div', { class: 'mw-banners' }, out, chips.length ? h('div', { class: 'mw-chips' }, chips) : null);
    return out.length || chips.length ? box : null;
  }
  async function review(button, action) {
    const r = await act(button, '/api/wakes/review/' + action, {}, action === 'restart' ? 'Accepted. Seraphina is running again.' : 'Accepted as the new settings.');
    if (r) reloadState();
  }
  function reloadState() { fetchKey('notify', '/api/notify'); fetchKey('wakes', '/api/wakes'); if (M.handle) M.handle.refreshHome(); paintStrip(); }

  /** The S46 acknowledgement: the exact sentence, typed. */
  function acknowledgeDialog() {
    const field = h('textarea', { class: 'textarea', rows: '3', 'aria-describedby': 'mw-ack-what', autocomplete: 'off', spellcheck: 'false', dataset: { field: 'ack' } });
    return SF.openModal({
      title: 'Accept the risk', description: 'Your agents run without the sandbox or through a command-line driver, so they could read what reaches your phone and send it anywhere. Prefer turning on the sandbox (Settings › Harness).',
      body: [h('p', { id: 'mw-ack-what' }, 'To turn phone messages on anyway, type this sentence exactly:'), h('blockquote', { class: 'mw-quote' }, ACK_SENTENCE), SF.field('The sentence', field)],
      actions: [{ label: 'Cancel', value: 'cancel' }, { label: 'I accept the risk', value: 'ok', cls: 'btn-danger' }],
      dismissable: false,
      onSubmit: async () => {
        const r = await SF.api('/api/wakes/acknowledge', { method: 'POST', body: { text: field.value } });
        SF.toast('Accepted. You can withdraw it from the banner.', { kind: 'ok' });
        reloadState();
        return r;
      },
    });
  }

  // ---------------------------------------------------------------- the inbox (GET /api/messages, late inbound)
  /** The inbox's filters as a query (its cache key); the read adds the limit. */
  function inboxQuery(route) {
    const q = route.query || {};
    const parts = [];
    for (const k of ['persona', 'state', 'channel', 'kind']) if (q[k] && /^[a-z0-9._-]{1,41}$/.test(q[k])) parts.push(k + '=' + enc(q[k]));
    if (q.unread === '1') parts.push('unread=1');
    return parts.join('&');
  }
  /** One read of the inbox: as many rows as are shown (older pages included), up to the server's 500. */
  function inboxPath(qs) {
    const limit = Math.min(INBOX_MAX, Math.max(INBOX_PAGE, M.inboxShown.get(qs) || 0));
    return '/api/messages?' + (qs ? qs + '&' : '') + 'limit=' + limit;
  }
  function loadInbox(route) {
    const qs = inboxQuery(route);
    M.inboxQs = qs;
    ensure('inbox:' + qs, inboxPath(qs), 3000);
    ensure('inbound', '/api/notify/inbound?limit=200', 5000);
    ensure('channels', '/api/notify/channels', 10000);
    ensure('wakes', '/api/wakes', 10000);
    ensure('config', '/api/config', 60000);
  }
  async function loadOlder(button) {
    const qs = M.inboxQs || '';
    const key = 'inbox:' + qs;
    const cur = val(key);
    if (!cur || !cur.rows.length) return;
    SF.setBusy(button, true);
    try {
      const r = await SF.api('/api/messages?' + (qs ? qs + '&' : '') + 'limit=' + INBOX_PAGE + '&before=' + enc(cur.rows[cur.rows.length - 1].id));
      const more = arr(r && r.messages);
      const now = val(key) || cur;   // a refresh may have landed meanwhile
      now.rows = now.rows.concat(more.filter((m) => !now.rows.some((x) => x.id === m.id)));
      now.more = more.length >= INBOX_PAGE;
      now.older = true;
      M.inboxShown.set(qs, now.rows.length);
    } catch (e) { SF.toastError('Could not load older messages', e); }
    finally { SF.setBusy(button, false); refresh(); }
  }
  function deliveryChips(deliveries) {
    const names = Object.keys(isObj(deliveries) ? deliveries : {});
    if (!names.length) return [tag('Inbox only', 'tag-outline', 'This notice stays in this inbox: no phone gets it')];
    return names.map((ch) => {
      const st = String(deliveries[ch] || '');
      return h('span', { class: 'pill tone-' + (STATE_TONE[st] || 'neutral') + ' mw-dchip', dataset: { channel: ch, state: st }, title: ch + ': ' + (STATE_WORDS[st] || st) }, ch + ': ' + (STATE_WORDS[st] || st));
    });
  }
  function lateApplied(id) { return arr(val('inbound')).some((r) => r && r.kind === 'cockpit' && r.applies === id); }
  function lateRow(r) {
    const done = lateApplied(r.id);
    const verb = VERB_WORDS[r.pending_verb] || String(r.pending_verb || 'a command');
    return h('li', { class: 'mw-row mw-late', dataset: { inbound: r.id } },
      h('div', { class: 'mw-row-main' },
        h('div', { class: 'mw-row-top' }, icon('hourglass'), h('strong', null, 'Arrived late'), h('span', { class: 'spacer' }), when(r.at)),
        h('p', { class: 'mw-row-text' }, 'You sent “' + verb + '” from ' + String(r.channel || 'your phone') + (r.sent_at ? ' at ' + localTime(r.sent_at) : '') + '; it arrived late and was not applied.'),
        done ? h('p', { class: 'mw-row-sub' }, icon('check'), 'Applied from these pages.') : null),
      done ? null : h('div', { class: 'mw-row-actions' }, SF.btn('Apply now', { cls: 'btn-sm', icon: 'play', dataset: { action: 'apply', focus: 'apply:' + r.id }, onClick: (e) => applyLate(e.currentTarget, r) })));
  }
  async function applyLate(button, r) {
    const res = await act(button, '/api/notify/inbound/' + enc(r.id) + '/apply', {}, null, { what: 'Could not apply it' });
    if (!res) { fetchKey('inbound', '/api/notify/inbound?limit=200'); return; }
    SF.toast('Applied: ' + (VERB_WORDS[res.applied] || res.applied) + '.', { kind: 'ok' });
    fetchKey('inbound', '/api/notify/inbound?limit=200');
    fetchKey('notify', '/api/notify');
  }
  function messageRow(m) {
    const urgent = m.priority === 'high' || m.priority === 'max';
    const answer = m.kind === 'answer';
    const href = '#/messages/m/' + enc(m.id);
    const title = m.text_removed ? 'Text removed after ' + retentionDays() + ' days' : String(m.title || '(no title)');
    const cls = 'mw-row mw-msg' + (m.unread ? ' is-unread' : '') + (urgent ? ' is-urgent' : '') + (answer ? ' is-answer' : '') + (m.text_removed ? ' is-removed' : '');
    return h('li', { class: cls, dataset: { message: m.id, kind: m.kind } },
      h('a', { class: 'mw-row-main mw-row-link', href, dataset: { focus: 'msg:' + m.id } },
        h('div', { class: 'mw-row-top' },
          m.unread ? [h('span', { class: 'mw-dot', title: 'Unread', 'aria-hidden': 'true' }), h('span', { class: 'sr-only' }, 'Unread.')] : null,
          h('span', { class: 'mw-sender' }, answer ? 'Answer to your phone' : senderName(m)),
          urgent ? tag(m.priority === 'max' ? 'Most urgent' : 'Urgent', 'tag-err') : null,
          taintChips(m.taint), h('span', { class: 'spacer' }), when(m.at)),
        h('div', { class: 'mw-row-title' }, title),
        !m.text_removed && m.body ? h('div', { class: 'mw-row-text mw-clamp' }, String(m.body)) : null,
        h('div', { class: 'mw-row-chips' }, deliveryChips(m.deliveries),
          m.snoozed_until ? tag('Snoozed until ' + localTime(m.snoozed_until), 'tag-outline') : null)),
      m.summary || m.about ? h('div', { class: 'mw-row-links' },
        m.summary ? h('a', { href: '#/messages/m/' + enc(m.summary), class: 'mw-in-summary', dataset: { focus: 'sum:' + m.id } }, icon('layers'), 'In ' + m.summary) : null,
        aboutLink(m.about)) : null,
      m.unread ? h('div', { class: 'mw-row-actions' }, SF.btn('Got it', { cls: 'btn-sm', icon: 'check', dataset: { action: 'ack', focus: 'ack:' + m.id }, onClick: (e) => ack(e.currentTarget, m.id) })) : null);
  }
  async function ack(button, id) {
    const r = await act(button, '/api/messages/' + enc(id) + '/ack', {}, null, { what: 'Could not mark it read' });
    if (r) { afterMessageChange(id); }
  }
  /** Read the inbox again, keeping what is shown until the new list arrives (other filters are simply forgotten). */
  function reloadInbox() {
    const cur = typeof M.inboxQs === 'string' ? 'inbox:' + M.inboxQs : null;
    for (const k of Object.keys(D)) if (k.indexOf('inbox:') === 0 && k !== cur) delete D[k];
    if (cur && D[cur] && M.handle && M.handle.isShown()) fetchKey(cur, inboxPath(M.inboxQs));
    else if (cur) delete D[cur];
  }
  function afterMessageChange(id) {
    reloadInbox();
    if (id && D['msg:' + id] && M.page && M.page.message === id) fetchKey('msg:' + id, '/api/messages/' + enc(id));
    else if (id) drop('msg:' + id);
    loadUnread();
    if (M.handle && M.handle.isShown()) refresh();
  }
  function inboxFilters(route) {
    const q = route.query || {};
    const set = (k, v) => {
      const next = Object.assign({}, q);
      if (v) next[k] = v; else delete next[k];
      const qs = Object.keys(next).filter((x) => next[x]).map((x) => enc(x) + '=' + enc(next[x])).join('&');
      SF.go('#/messages' + (qs ? '?' + qs : ''));
    };
    const chans = arr(val('channels')).map((c) => [c.name, c.name]);
    const personas = arr((val('wakes') || {}).personas).map((p) => [p.name, String(p.display || p.name)]);
    const unread = h('input', { type: 'checkbox', checked: q.unread === '1', dataset: { focus: 'f:unread', filter: 'unread' } });
    unread.addEventListener('change', () => set('unread', unread.checked ? '1' : ''));
    const sel = (k, label, options) => {
      const s = SF.selectEl([['', label]].concat(options), q[k] || '', { class: 'select select-sm', 'aria-label': label, dataset: { focus: 'f:' + k, filter: k } });
      s.addEventListener('change', () => set(k, s.value));
      return s;
    };
    return h('div', { class: 'mw-filters', role: 'group', 'aria-label': 'Filter the inbox' },
      h('label', { class: 'mw-check' }, unread, h('span', null, 'Unread only')),
      sel('state', 'Every state', Object.keys(STATE_WORDS).filter((s) => s !== 'inbox').map((s) => [s, STATE_WORDS[s]])),
      chans.length ? sel('channel', 'Every phone', chans) : null,
      personas.length ? sel('persona', 'Everyone', personas) : null);
  }
  function renderInbox(route, page) {
    loadInbox(route);
    const key = 'inbox:' + M.inboxQs;
    const data = val(key);
    const lateRows = arr(val('inbound')).filter((r) => r && r.late && r.pending_verb);
    const filtered = Object.keys(route.query || {}).some((k) => route.query[k]);
    let list;
    if (errOf(key) && !data) list = errorBox(errOf(key), 'the inbox');
    else if (!data) list = SF.skeleton(5);
    else {
      const items = data.rows.map((m) => ({ at: Date.parse(m.at) || 0, node: () => messageRow(m) }));
      if (!filtered) for (const r of lateRows) items.push({ at: Date.parse(r.at) || 0, node: () => lateRow(r) });
      items.sort((a, b) => b.at - a.at);
      list = items.length ? h('ul', { class: 'mw-list', 'aria-label': 'Messages, newest first' }, items.map((x) => x.node()))
        : SF.emptyState('inbox', filtered ? 'No messages match' : 'No messages yet', filtered ? 'Change the filters to see more.' : 'Notices from your server and your personas land here first; each phone gets what its privacy allows.', filtered ? [linkBtn('Show everything', '#/messages')] : [linkBtn('Set up your phone', '#/messages/setup', { cls: 'btn-sm btn-primary', icon: 'bell' })], true);
    }
    return [
      inboxFilters(route),
      list,
      data && data.more ? h('div', { class: 'mw-more' }, SF.btn('Show older messages', { cls: 'btn-sm', dataset: { action: 'older', focus: 'older' }, onClick: (e) => loadOlder(e.currentTarget) })) : null,
      howTo([
        ['Read the newest first.', 'Bold lines are unread; Urgent ones came from a rule you marked high. Open a line to see the whole notice and exactly what each phone got.'],
        ['Answer in one tap.', 'Got it marks a notice read on every phone; Snooze brings it back later, never inside do-not-disturb time.'],
        ['Late commands wait for you.', 'A command your phone sent that arrived too late is shown here and not applied; Apply now does it from here.'],
        ['Your phone calls these pages “the cockpit”.', 'When a phone answers “Approve in the cockpit”, it means: open these pages and do it here.'],
      ]),
    ];
  }
  function howTo(steps, title) {
    return h('section', { class: 'wz-proc-card mw-how', 'aria-label': title || 'How this page works' }, h('h2', { class: 'wz-proc-title' }, title || 'How this page works'), SF.procedureList(steps));
  }

  // ---------------------------------------------------------------- a message (GET /api/messages/<id>), in the panel
  function renderPanel(page, route) {
    const id = page.message;
    if (!id) {
      if (M.panelKey && SF.drawer.isOpen(M.panelKey)) SF.drawer.close();
      M.panelKey = null;
      M.panelSig = '';
      return;
    }
    const key = 'msg:' + id;
    if (!/^M-\d{6,9}$/.test(id)) { D[key] = { v: undefined, err: new SF.ApiError(400, 'That is not a message id.'), at: Date.now(), p: null }; }
    else ensure(key, '/api/messages/' + enc(id), 3000);
    const data = val(key);
    const e = errOf(key);
    const back = '#/messages' + (route.query && Object.keys(route.query).length ? '?' + Object.keys(route.query).map((k) => enc(k) + '=' + enc(route.query[k])).join('&') : '');
    const sig = JSON.stringify([id, data ? data : null, e ? e.message : null, Array.from(M.busy)]);
    if (M.panelKey === 'msg:' + id && SF.drawer.isOpen(M.panelKey) && sig === M.panelSig) return;
    M.panelSig = sig;
    M.panelKey = 'msg:' + id;
    const m = data && data.message;
    SF.drawer.open({
      key: M.panelKey, back, wide: true,
      eyebrow: [h('span', { class: 'drawer-id' }, id), m ? pill(STATE_WORDS[m.state] || m.state, STATE_TONE[m.state]) : null, m && m.unread ? tag('Unread', 'tag-accent') : null],
      title: m ? (m.text_removed ? 'Text removed after ' + retentionDays() + ' days' : String(m.title || '(no title)')) : (e ? 'Message ' + id : 'Loading…'),
      sub: m ? 'From ' + senderName(m) + ' · ' + localTime(m.at) : null,
      body: e && !data ? [errorBox(e, 'this message')] : data ? messageBody(data) : [SF.skeleton(6)],
      actions: m ? messageActions(data) : null,
    });
  }
  function messageActions(data) {
    const m = data.message;
    const ds = arr(data.deliveries);
    const out = [];
    if (m.unread) out.push(SF.btn('Got it', { cls: 'btn-sm btn-primary', icon: 'check', dataset: { action: 'ack' }, onClick: (e) => ack(e.currentTarget, m.id) }));
    const minutes = SF.selectEl(SNOOZE_MINUTES, M.snoozeMinutes || 60, { class: 'select select-sm mw-snooze-min', 'aria-label': 'Snooze for' });
    minutes.addEventListener('change', () => { M.snoozeMinutes = Number(minutes.value) || 60; });
    out.push(h('span', { class: 'mw-snooze' }, minutes, SF.btn('Snooze', { cls: 'btn-sm', icon: 'clock', dataset: { action: 'snooze' }, onClick: async (e) => {
      const r = await act(e.currentTarget, '/api/messages/' + enc(m.id) + '/snooze', { minutes: Number(minutes.value) }, null, { what: 'Could not snooze it' });
      if (r && r.message) SF.toast('Snoozed until ' + (r.message.snoozed_until ? localTime(r.message.snoozed_until) : 'later') + '.', { kind: 'ok' });
      if (r) afterMessageChange(m.id);
    } })));
    if (ds.some((d) => d.state === 'failed' || d.state === 'held')) {
      out.push(SF.btn('Send now', { cls: 'btn-sm', icon: 'send', title: 'Send the failed or held deliveries now, past do-not-disturb time and the daily cap', dataset: { action: 'retry' }, onClick: async (e) => {
        const r = await act(e.currentTarget, '/api/messages/' + enc(m.id) + '/retry', {}, 'Sending it now.', { what: 'Could not send it' });
        if (r) afterMessageChange(m.id);
      } }));
    }
    if (ds.some((d) => d.state === 'unknown')) {
      out.push(SF.btn('Send again', { cls: 'btn-sm', icon: 'refresh', dataset: { action: 'resend' }, onClick: async (e) => {
        const ok = await SF.confirmModal({ title: 'Send it again?', description: 'It may have arrived: the phone did not confirm it. Send it again only if it did not arrive.', confirm: 'Send again', icon: 'send' });
        if (!ok) return;
        const r = await act(e.currentTarget, '/api/messages/' + enc(m.id) + '/resend', {}, 'Sending it again.', { what: 'Could not send it' });
        if (r) afterMessageChange(m.id);
      } }));
    }
    return out;
  }
  function messageBody(data) {
    const m = data.message;
    const fields = isObj(m.fields) ? m.fields : {};
    const facts = h('dl', { class: 'sf-kv mw-facts' },
      h('dt', null, 'From'), h('dd', null, senderName(m)),
      h('dt', null, 'When'), h('dd', null, when(m.at)),
      h('dt', null, 'Urgency'), h('dd', null, m.priority === 'high' || m.priority === 'max' ? tag(m.priority === 'max' ? 'Most urgent' : 'Urgent', 'tag-err') : ({ default: 'Normal', low: 'Low', min: 'Lowest' })[m.priority || 'default'] || String(m.priority)),
      h('dt', null, 'Kind'), h('dd', { title: String(m.kind || '') + (m.class ? ' · ' + String(m.class) : '') }, m.kind === 'answer' ? 'An answer to your own phone command'
        : kindWords(m.kind) + (CLASS_WORDS[m.class] ? ' (' + CLASS_WORDS[m.class] + ')' : '')),
      m.taint && m.taint.length ? [h('dt', null, 'Privacy'), h('dd', null, taintChips(m.taint))] : null,
      m.about ? [h('dt', null, 'About'), h('dd', null, aboutLink(m.about) || String(m.about))] : null,
      m.summary ? [h('dt', null, 'Carried in'), h('dd', null, h('a', { href: '#/messages/m/' + enc(m.summary) }, 'the summary ' + m.summary))] : null,
      m.snoozed_until ? [h('dt', null, 'Snoozed until'), h('dd', null, when(m.snoozed_until))] : null,
      m.read_at ? [h('dt', null, 'Read'), h('dd', null, when(m.read_at))] : null);
    const text = m.text_removed
      ? callout('info', 'info', 'Text removed after ' + retentionDays() + ' days.', 'Only fingerprints of the title and the text are kept: ' + shortSha(String(m.title || '').replace(/^sha256:/, '')) + '.')
      : h('div', { class: 'mw-full' }, h('div', { class: 'mw-full-title' }, String(m.title || '')), textBlock(m.body, 'mw-full-body'));
    return [
      facts, text,
      fields.detail ? h('div', { class: 'mw-detail' }, h('h3', { class: 'mw-h3' }, 'Detail from the event source'), h('p', { class: 'field-hint' }, 'Shown here only; it never goes to a phone.'), textBlock(fields.detail, 'mw-quoted')) : null,
      h('h3', { class: 'mw-h3' }, 'What each phone got'),
      arr(data.deliveries).length ? h('ul', { class: 'mw-deliveries' }, arr(data.deliveries).map(deliveryCard)) : muted('No phone: this notice is for the inbox only.'),
      arr(data.inbound).length ? [h('h3', { class: 'mw-h3' }, 'What came back'), h('ul', { class: 'mw-list mw-list-tight' }, arr(data.inbound).map(inboundRow))] : null,
    ];
  }
  function callout(kind, ic, title, text) { return SF.callout(kind, ic, title, text); }
  function outcomeLine(d) {
    const o = OUTCOME_OF[d.state];
    if (!o) return null;
    // partial: a try reached some of the channel's accounts (notify skips them next time), so "nothing arrived" is
    // said only without it (DESIGN §40: never stronger than known)
    const why = o === 'succeeded' ? 'the phone service confirmed it.'
      : o === 'failed' && d.partial ? 'some of your accounts already have it, and it was refused for the others' + (d.next_try ? ': the next try sends it only to them.' : '.')
        : o === 'failed' ? 'refused, or it never left the harness' + (d.next_try ? ', so nothing arrived: it is tried again by itself.' : '.')
          : (d.partial ? 'some of your accounts already have it; for the others, ' : '') + 'the phone service did not confirm it, so it is never sent again by itself. Look at your phone first: Send again is the only way to send it again.';
    return h('p', { class: 'mw-outcome', dataset: { outcome: o } }, pill(SF.words.outcome[o], SF.words.outcomeTone[o]), h('span', null, ' ', o === 'unknown' ? 'It may or may not have arrived: ' : '', why));
  }
  function deliveryCard(d) {
    const r = isObj(d.rendered) ? d.rendered : null;
    return h('li', { class: 'mw-delivery sf-card', dataset: { channel: d.channel, state: d.state } },
      h('div', { class: 'mw-row-top' }, h('strong', null, String(d.channel)), pill(STATE_WORDS[d.state] || d.state, STATE_TONE[d.state]), h('span', { class: 'spacer' }), d.at ? when(d.at) : null),
      outcomeLine(d),
      d.why ? h('p', { class: 'mw-why' }, icon('info'), h('span', null, String(d.why))) : null,
      r ? h('div', { class: 'mw-rendered', 'aria-label': 'Exactly what was sent' },
        h('div', { class: 'mw-rendered-title' }, String(r.title || '')), textBlock(r.text, 'mw-rendered-text'),
        r.why ? h('div', { class: 'mw-rendered-why' }, String(r.why)) : null,
        arr(r.buttons).length ? h('div', { class: 'mw-rendered-buttons', 'aria-label': 'Its buttons' }, arr(r.buttons).map((b) => tag(VERB_WORDS[b] || String(b), 'tag-outline'))) : null)
        : muted('Nothing was sent to this phone yet.'),
      h('dl', { class: 'sf-kv mw-facts-sm' },
        d.attempts ? [h('dt', null, 'Tries'), h('dd', null, String(d.attempts))] : null,
        d.next_try ? [h('dt', null, 'Next try'), h('dd', null, when(d.next_try))] : null,
        d.error ? [h('dt', null, 'Error'), h('dd', { class: 'mw-error-text' }, String(d.error))] : null));
  }

  // ---------------------------------------------------------------- what came back (GET /api/notify/inbound)
  function inboundRow(r) {
    const kind = String(r.kind || '');
    const security = kind === 'forged' || kind === 'foreign';
    const head = kind === 'text' ? 'You wrote (from ' + String(r.channel || 'your phone') + ')' : INBOUND_WORDS[kind] || kind;
    return h('li', { class: 'mw-row mw-inbound' + (security ? ' is-security' : ''), dataset: { inbound: r.id, kind } },
      h('div', { class: 'mw-row-main' },
        h('div', { class: 'mw-row-top' }, icon(security ? 'shield' : kind === 'stranger' ? 'ban' : kind === 'cockpit' ? 'check' : kind === 'button' ? 'check-circle' : 'send'),
          h('strong', null, head), kind !== 'text' && r.channel ? muted(String(r.channel)) : null,
          r.verb ? tag(VERB_WORDS[r.verb] || String(r.verb), 'tag-outline') : null,
          r.forwarded || r.untrusted ? tag('Has a link or was forwarded', 'tag-warn', 'Treated as outside text') : null,
          h('span', { class: 'spacer' }), when(r.at)),
        r.text ? textBlock(r.text, 'mw-quoted' + (security ? ' is-security' : '')) : null,
        h('p', { class: 'mw-row-sub' }, 'Outcome: ' + String(r.outcome || '—'),
          kind === 'stranger' && r.count_today ? ' · ' + SF.plural(r.count_today, 'stranger message') + ' today' : '',
          r.sent_at && r.late ? ' · sent ' + localTime(r.sent_at) : '',
          r.persona ? ' · to ' + personaDisplay(r.persona) : ''),
        r.message ? h('p', { class: 'mw-row-sub' }, 'About ', h('a', { href: '#/messages/m/' + enc(r.message), dataset: { focus: 'inb-msg:' + r.id } }, r.message)) : null,
        security ? h('p', { class: 'mw-row-sub' }, 'Dropped: the harness did not act on it. Repeated ones alert your other phones.') : null),
      r.late && r.pending_verb && !lateApplied(r.id) ? h('div', { class: 'mw-row-actions' }, SF.btn('Apply now', { cls: 'btn-sm', icon: 'play', dataset: { action: 'apply', focus: 'apply:' + r.id }, onClick: (e) => applyLate(e.currentTarget, r) })) : null);
  }
  function renderInbound(route) {
    const kind = route.query && INBOUND_KINDS.indexOf(route.query.kind) >= 0 ? route.query.kind : '';
    const key = 'inbound';
    ensure(key, '/api/notify/inbound?limit=200', 3000);
    ensure('wakes', '/api/wakes', 10000);
    const rows = arr(val(key)).filter((r) => !kind || r.kind === kind);
    const sel = SF.selectEl([['', 'Every kind']].concat(INBOUND_KINDS.map((k) => [k, INBOUND_WORDS[k]])), kind, { class: 'select select-sm', 'aria-label': 'Kind', dataset: { focus: 'f:inbound-kind', filter: 'kind' } });
    sel.addEventListener('change', () => SF.go('#/messages/inbound' + (sel.value ? '?kind=' + enc(sel.value) : '')));
    return [
      h('div', { class: 'mw-filters' }, sel),
      errOf(key) && !val(key) ? errorBox(errOf(key), 'what came back') : !loaded(key) ? SF.skeleton(4)
        : rows.length ? h('ul', { class: 'mw-list', 'aria-label': 'What came back from your phones, newest first' }, rows.map(inboundRow))
          : SF.emptyState('send', 'Nothing came back yet', 'Taps on your phone’s buttons, commands and what you write to Seraphina appear here.', null, true),
      howTo([
        ['Your own words.', 'What you wrote from a phone is kept here for you, with every secret the harness knows replaced by ***.'],
        ['Not from your phone.', 'A forged tap or a notice the harness did not send is dropped and shown as a security line; nothing acted on it.'],
        ['Commands are code, not a model.', 'Stop, Snooze, Quiet, Mute, Status and Help work from a phone; nothing from a phone approves, pairs or changes a setting.'],
      ], 'What comes back from your phones'),
    ];
  }

  // ---------------------------------------------------------------- copy blocks (prepare, channel actions, lost phone, hooks)
  /** {id, title, commands, expect, if_not, warning?, note?, files?, dropin?}: shown, never run. */
  function copyBlock(b, i) {
    if (!isObj(b)) return null;
    const cmds = arr(b.commands).map(String).join('\n');
    const copyBtn = (text, what, focus) => SF.btn('Copy', { cls: 'btn-xs', icon: 'copy', aria: 'Copy ' + what, dataset: { action: 'copy', focus }, onClick: () => SF.copyText(text, 'Copied: paste it into a terminal on the server.') });
    return h('div', { class: 'mw-block', dataset: { block: String(b.id || i) } },
      h('h3', { class: 'mw-h3' }, b.id ? h('span', { class: 'tag tag-mono' }, String(b.id)) : null, ' ', String(b.title || '')),
      b.warning ? callout('warn', 'alert', 'Before you run it:', String(b.warning)) : null,
      cmds ? h('div', { class: 'mw-cmds' }, pre(cmds, 'mw-cmd'), copyBtn(cmds, 'the commands of ' + (b.id || 'this block'), 'copy:' + (b.id || i))) : null,
      b.note ? h('p', { class: 'mw-note' }, String(b.note)) : null,
      isObj(b.files) ? Object.keys(b.files).map((path) => h('div', { class: 'mw-file' }, h('p', null, 'Create ', h('code', null, path), ' with:'), h('div', { class: 'mw-cmds' }, pre(b.files[path]), copyBtn(String(b.files[path]), path, 'copy-file:' + path)))) : null,
      b.dropin ? h('div', { class: 'mw-file' }, h('p', null, 'Paste into ', h('code', null, 'sudo systemctl edit <your unit>'), ':'), h('div', { class: 'mw-cmds' }, pre(b.dropin), copyBtn(String(b.dropin), 'the drop-in', 'copy-dropin:' + (b.id || i)))) : null,
      b.expect ? h('p', { class: 'mw-expect' }, h('strong', null, 'You should see: '), String(b.expect)) : null,
      b.if_not ? h('p', { class: 'mw-ifnot' }, h('strong', null, 'If not: '), String(b.if_not)) : null);
  }
  /** A secret shown once (passphrase, token): hidden until Show, with Copy; nothing keeps it after its step. */
  function secretOnce(value, label, onWrote, opts) {
    opts = opts || {};
    let shown = !!opts.shown;
    const out = h('code', { class: 'mw-secret-value', dataset: { secret: '1' } }, shown ? String(value) : '•••• •••• ••••');
    const show = SF.btn(shown ? 'Hide' : 'Show', { cls: 'btn-sm', icon: 'eye', aria: shown ? 'Hide ' + label : 'Show ' + label, dataset: { action: 'reveal', focus: 'secret:reveal' }, onClick: () => {
      shown = !shown;
      if (opts.onShown) opts.onShown(shown);
      out.textContent = shown ? String(value) : '•••• •••• ••••';
      show.querySelector('span').textContent = shown ? 'Hide' : 'Show';
      show.setAttribute('aria-label', (shown ? 'Hide ' : 'Show ') + label);
    } });
    const copy = SF.btn('Copy', { cls: 'btn-sm', icon: 'copy', aria: 'Copy ' + label, dataset: { action: 'copy-secret', focus: 'secret:copy' }, onClick: () => SF.copyText(String(value), label + ' copied. Paste it where it goes, then clear your clipboard.') });
    const wrote = h('input', { type: 'checkbox', checked: !!opts.wrote, dataset: { field: 'wrote', focus: 'secret:wrote' } });
    if (onWrote) wrote.addEventListener('change', () => onWrote(wrote.checked));
    return h('div', { class: 'mw-secret', role: 'group', 'aria-label': label },
      h('p', { class: 'mw-secret-head' }, icon('shield'), h('strong', null, label + ' (shown once)'), ' The harness keeps only its fingerprint; this page forgets it when you leave this step.'),
      h('div', { class: 'mw-secret-row' }, out, show, copy),
      onWrote ? h('label', { class: 'mw-check' }, wrote, h('span', null, 'I wrote it down (or saved it in my password manager)')) : null);
  }

  // ---------------------------------------------------------------- a QR code (byte mode, error correction M, versions 1-10)
  // Only for an address a phone opens (the Telegram link, the ntfy server address): never a passphrase or a token.
  const QR = (function () {
    const ECC = [10, 16, 26, 18, 24, 16, 18, 22, 22, 26];       // EC codewords per block, level M, versions 1-10
    const BLOCKS = [1, 1, 1, 2, 2, 4, 4, 4, 5, 5];
    const rawModules = (v) => { let r = (16 * v + 128) * v + 64; if (v >= 2) { const n = Math.floor(v / 7) + 2; r -= (25 * n - 10) * n - 55; if (v >= 7) r -= 36; } return r; };
    const dataCodewords = (v) => Math.floor(rawModules(v) / 8) - ECC[v - 1] * BLOCKS[v - 1];
    const mul = (x, y) => { let z = 0; for (let i = 7; i >= 0; i--) { z = (z << 1) ^ ((z >>> 7) * 0x11d); z ^= ((y >>> i) & 1) * x; } return z & 0xff; };
    function divisor(deg) {
      const r = new Array(deg).fill(0); r[deg - 1] = 1; let root = 1;
      for (let i = 0; i < deg; i++) { for (let j = 0; j < r.length; j++) { r[j] = mul(r[j], root); if (j + 1 < r.length) r[j] ^= r[j + 1]; } root = mul(root, 2); }
      return r;
    }
    function remainder(data, div) {
      const r = div.map(() => 0);
      for (const b of data) { const f = b ^ r.shift(); r.push(0); div.forEach((c, i) => { r[i] ^= mul(c, f); }); }
      return r;
    }
    function alignPositions(v, size) {
      if (v === 1) return [];
      const n = Math.floor(v / 7) + 2;
      const step = Math.ceil((v * 4 + 4) / (n * 2 - 2)) * 2;
      const out = [6];
      for (let pos = size - 7; out.length < n; pos -= step) out.splice(1, 0, pos);
      return out;
    }
    function encode(text) {
      const bytes = Array.from(new TextEncoder().encode(String(text)));
      let v = 1;
      while (v <= 10 && bytes.length + (v >= 10 ? 3 : 2) > dataCodewords(v)) v++;
      if (v > 10) return null;
      const size = v * 4 + 17;
      const cap = dataCodewords(v);
      const bits = [];
      const put = (val, len) => { for (let i = len - 1; i >= 0; i--) bits.push((val >>> i) & 1); };
      put(4, 4); put(bytes.length, v >= 10 ? 16 : 8); bytes.forEach((b) => put(b, 8));
      put(0, Math.min(4, cap * 8 - bits.length));
      while (bits.length % 8) bits.push(0);
      const data = [];
      for (let i = 0; i < bits.length; i += 8) data.push(bits.slice(i, i + 8).reduce((a, b) => (a << 1) | b, 0));
      for (let pad = 0xec; data.length < cap; pad ^= 0xec ^ 0x11) data.push(pad);
      // blocks, error correction, interleaving
      const nb = BLOCKS[v - 1], eccLen = ECC[v - 1], raw = Math.floor(rawModules(v) / 8);
      const short = nb - raw % nb, shortLen = Math.floor(raw / nb), div = divisor(eccLen);
      const blocks = [];
      for (let i = 0, k = 0; i < nb; i++) {
        const dat = data.slice(k, k + shortLen - eccLen + (i < short ? 0 : 1));
        k += dat.length;
        const ec = remainder(dat, div);
        if (i < short) dat.push(0);
        blocks.push(dat.concat(ec));
      }
      const words = [];
      for (let i = 0; i < blocks[0].length; i++) blocks.forEach((b, j) => { if (i !== shortLen - eccLen || j >= short) words.push(b[i]); });
      // the grid
      const mod = Array.from({ length: size }, () => new Array(size).fill(false));
      const fn = Array.from({ length: size }, () => new Array(size).fill(false));
      const set = (x, y, d) => { mod[y][x] = d; fn[y][x] = true; };
      for (let i = 0; i < size; i++) { set(6, i, i % 2 === 0); set(i, 6, i % 2 === 0); }
      for (const [cx, cy] of [[3, 3], [size - 4, 3], [3, size - 4]]) {
        for (let dy = -4; dy <= 4; dy++) for (let dx = -4; dx <= 4; dx++) {
          const d = Math.max(Math.abs(dx), Math.abs(dy)), x = cx + dx, y = cy + dy;
          if (x >= 0 && x < size && y >= 0 && y < size) set(x, y, d !== 2 && d !== 4);
        }
      }
      const al = alignPositions(v, size);
      for (let i = 0; i < al.length; i++) for (let j = 0; j < al.length; j++) {
        if ((i === 0 && j === 0) || (i === 0 && j === al.length - 1) || (i === al.length - 1 && j === 0)) continue;
        for (let dy = -2; dy <= 2; dy++) for (let dx = -2; dx <= 2; dx++) set(al[i] + dx, al[j] + dy, Math.max(Math.abs(dx), Math.abs(dy)) !== 1);
      }
      const format = (mask) => {
        const dat = (0 << 3) | mask;   // level M: format bits 00
        let rem = dat;
        for (let i = 0; i < 10; i++) rem = (rem << 1) ^ ((rem >>> 9) * 0x537);
        const b = ((dat << 10) | rem) ^ 0x5412;
        const bit = (i) => ((b >>> i) & 1) !== 0;
        for (let i = 0; i <= 5; i++) set(8, i, bit(i));
        set(8, 7, bit(6)); set(8, 8, bit(7)); set(7, 8, bit(8));
        for (let i = 9; i < 15; i++) set(14 - i, 8, bit(i));
        for (let i = 0; i < 8; i++) set(size - 1 - i, 8, bit(i));
        for (let i = 8; i < 15; i++) set(8, size - 15 + i, bit(i));
        set(8, size - 8, true);
      };
      format(0);
      if (v >= 7) {
        let rem = v;
        for (let i = 0; i < 12; i++) rem = (rem << 1) ^ ((rem >>> 11) * 0x1f25);
        const b = (v << 12) | rem;
        for (let i = 0; i < 18; i++) { const d = ((b >>> i) & 1) !== 0, a = size - 11 + (i % 3), c = Math.floor(i / 3); set(a, c, d); set(c, a, d); }
      }
      let i = 0;
      for (let right = size - 1; right >= 1; right -= 2) {
        if (right === 6) right = 5;
        for (let vert = 0; vert < size; vert++) for (let j = 0; j < 2; j++) {
          const x = right - j, up = ((right + 1) & 2) === 0, y = up ? size - 1 - vert : vert;
          if (!fn[y][x] && i < words.length * 8) { mod[y][x] = ((words[i >>> 3] >>> (7 - (i & 7))) & 1) !== 0; i++; }
        }
      }
      const MASKS = [(x, y) => (x + y) % 2 === 0, (x, y) => y % 2 === 0, (x) => x % 3 === 0, (x, y) => (x + y) % 3 === 0,
        (x, y) => (Math.floor(x / 3) + Math.floor(y / 2)) % 2 === 0, (x, y) => ((x * y) % 2) + ((x * y) % 3) === 0,
        (x, y) => (((x * y) % 2) + ((x * y) % 3)) % 2 === 0, (x, y) => (((x + y) % 2) + ((x * y) % 3)) % 2 === 0];
      const apply = (m) => { for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) if (!fn[y][x] && MASKS[m](x, y)) mod[y][x] = !mod[y][x]; };
      const penalty = () => {
        let p = 0, dark = 0;
        const line = (get) => {
          for (let a = 0; a < size; a++) {
            let run = 1;
            for (let b = 1; b <= size; b++) {
              if (b < size && get(a, b) === get(a, b - 1)) run++;
              else { if (run >= 5) p += run - 2; run = 1; }
            }
            for (let b = 0; b + 10 < size; b++) {
              const s = []; for (let k = 0; k < 11; k++) s.push(get(a, b + k) ? 1 : 0);
              const t = s.join('');
              if (t === '10111010000' || t === '00001011101') p += 40;
            }
          }
        };
        line((a, b) => mod[a][b]); line((a, b) => mod[b][a]);
        for (let y = 0; y + 1 < size; y++) for (let x = 0; x + 1 < size; x++) { const c = mod[y][x]; if (c === mod[y][x + 1] && c === mod[y + 1][x] && c === mod[y + 1][x + 1]) p += 3; }
        for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) if (mod[y][x]) dark++;
        return p + 10 * Math.floor(Math.abs(dark * 20 - size * size * 10) / (size * size));
      };
      let best = 0, bestP = Infinity;
      for (let m = 0; m < 8; m++) { apply(m); format(m); const pp = penalty(); if (pp < bestP) { bestP = pp; best = m; } apply(m); }
      apply(best); format(best);
      return mod;
    }
    /** A canvas with the code (black on white, a 4-module quiet zone), labelled for screen readers. */
    function canvas(text, label) {
      const grid = encode(text);
      if (!grid) return null;
      const n = grid.length, scale = Math.max(3, Math.floor(200 / (n + 8))), px = (n + 8) * scale;
      const c = h('canvas', { class: 'mw-qr', width: String(px), height: String(px), role: 'img', 'aria-label': label });
      const g = c.getContext && c.getContext('2d');
      if (!g) return null;
      g.fillStyle = '#fff'; g.fillRect(0, 0, px, px); g.fillStyle = '#000';
      for (let y = 0; y < n; y++) for (let x = 0; x < n; x++) if (grid[y][x]) g.fillRect((x + 4) * scale, (y + 4) * scale, scale, scale);
      return c;
    }
    return { encode, canvas };
  })();

  // ---------------------------------------------------------------- phone setup (GET /api/notify/channels)
  function privacyPill(p) { return pill(PRIVACY_WORDS[p] || String(p || 'unknown'), p === 'tailnet' ? 'ok' : 'warn', p === 'tailnet' ? 'Computed by the harness from the channel’s addresses' : 'Telegram, or an ntfy that is not on your tailnet'); }
  function channelCard(c) {
    const health = isObj(c.health) ? c.health : {};
    const isTg = c.type === 'telegram';
    const actions = [
      SF.btn('Send a test', { cls: 'btn-sm', icon: 'send', dataset: { action: 'test', focus: 'test:' + c.name }, onClick: (e) => sendTest(e.currentTarget, c.name) }),
      SF.btn('New tokens', { cls: 'btn-sm', icon: 'refresh', title: isTg ? 'Use a new bot token from @BotFather' : 'A new reply token and phone passphrase', dataset: { action: 'rotate', focus: 'rotate:' + c.name }, onClick: (e) => channelAction(e.currentTarget, c, 'rotate') }),
      SF.btn('It leaked', { cls: 'btn-sm', icon: 'alert', title: isTg ? 'Revoke the bot token and unpair' : 'New harness and reply tokens and a new passphrase', dataset: { action: 'leaked', focus: 'leaked:' + c.name }, onClick: (e) => channelAction(e.currentTarget, c, 'leaked') }),
      c.enabled ? SF.btn('Turn off', { cls: 'btn-sm btn-ghost', icon: 'ban', dataset: { action: 'disable', focus: 'disable:' + c.name }, onClick: (e) => channelAction(e.currentTarget, c, 'disable') })
        : SF.btn('Turn on', { cls: 'btn-sm btn-primary', icon: 'play', title: 'Send to this phone and listen to it again', dataset: { action: 'enable', focus: 'enable:' + c.name }, onClick: (e) => turnOn(e.currentTarget, c) }),
    ];
    const err = errLine('chan:' + c.name);
    return h('article', { class: 'sf-card mw-channel', dataset: { channel: c.name, type: c.type }, 'aria-labelledby': 'mw-ch-' + c.name },
      h('div', { class: 'mw-row-top' }, icon(isTg ? 'send' : 'bell'), h('h3', { class: 'mw-h3', id: 'mw-ch-' + c.name }, String(c.name)), tag(isTg ? 'Telegram' : 'ntfy', 'tag-outline'), h('span', { class: 'spacer' }), privacyPill(c.privacy)),
      c.active ? h('p', { class: 'mw-ok-line' }, icon('check-circle'), 'Can send now' + (c.receiving ? ' and listens for your answers.' : '.'))
        : h('p', { class: 'mw-off-line' }, icon('alert'), h('span', null, String(c.inactive_why || 'Cannot send now'))),
      h('dl', { class: 'sf-kv mw-facts-sm' },
        h('dt', null, 'Health'), h('dd', null, health.ok === true ? tag('Working', 'tag-ok') : health.ok === false ? tag('Not working', 'tag-err') : tag('Not checked yet', 'tag-outline'), ' ', h('span', { class: 'mw-health-detail' }, String(health.detail || ''))),
        health.last_ok ? [h('dt', null, 'Last worked'), h('dd', null, when(health.last_ok))] : null,
        health.last_sent ? [h('dt', null, 'Last sent'), h('dd', null, when(health.last_sent))] : null,
        health.last_received ? [h('dt', null, 'Last heard from'), h('dd', null, when(health.last_received))] : null,
        c.last_answer || health.last_answer ? [h('dt', null, 'Last answer'), h('dd', null, when(c.last_answer || health.last_answer))] : null,
        isTg && c.strangers_today ? [h('dt', null, 'Strangers today'), h('dd', null, SF.plural(c.strangers_today, 'message') + ' from people who are not paired (no answer was sent)')] : null),
      isTg ? h('div', { class: 'mw-paired' }, h('p', { class: 'mw-row-sub' }, arr(c.paired).length ? 'Paired accounts:' : 'No account is paired yet.'),
        arr(c.paired).length ? h('ul', { class: 'mw-list mw-list-tight' }, arr(c.paired).map((a) => h('li', { class: 'mw-row', dataset: { account: String(a.id) } },
          h('div', { class: 'mw-row-main' }, h('div', { class: 'mw-row-top' }, icon('user'), h('strong', null, String(a.first_name || 'Telegram account')), a.username ? muted('@' + String(a.username)) : null),
            a.paired_at ? h('p', { class: 'mw-row-sub' }, 'Paired ', when(a.paired_at)) : null),
          h('div', { class: 'mw-row-actions' }, SF.btn('Unpair', { cls: 'btn-sm btn-ghost', icon: 'x', dataset: { action: 'unpair', focus: 'unpair:' + a.id }, onClick: (e) => unpair(e.currentTarget, c.name, a) }))))) : null) : null,
      err,
      h('div', { class: 'actions mw-actions' }, actions,
        linkBtn(isTg ? 'Pair a phone' : 'Setup steps', '#/messages/setup/' + (isTg ? 'telegram/3' : 'ntfy/1') + '?channel=' + enc(c.name), { icon: 'arrow-right' })));
  }
  /** A channel that was turned off (Turn off, Lost my phone) goes back on; the S46 refusal shows under its card. */
  async function turnOn(button, c) {
    const err = byAttr(button.closest('.mw-channel'), 'data-err', 'chan:' + c.name);
    formError(err, null);
    const r = await act(button, '/api/config', { notify: { channels: { [c.name]: { enabled: true } } } }, c.name + ' is on again.', { onError: (x) => { formError(err, x); if (!err || !err.isConnected) refresh(); } });
    if (r) { reread('config', '/api/config'); fetchKey('channels', '/api/notify/channels'); fetchKey('notify', '/api/notify'); }
  }
  /** New tokens, It leaked and Lost my phone end every pairing: the wizards start their pairing steps again. */
  function resetPairing(kind, channel) {
    for (const k of kind ? [kind] : ['ntfy', 'telegram']) {
      const w = M.wizard[k];
      if (channel && w.channel && w.channel !== channel) continue;
      for (const f of ['code', 'pairState', 'pair', 'pairErr', 'pairing', 'pollErr', 'wrong', 'since', 'seen']) delete w[f];
    }
    stopPoll();
  }
  async function sendTest(button, name) {
    const r = await act(button, '/api/notify/test', { channel: name }, null, { what: 'Could not send a test' });
    if (r && r.message) SF.toast('Test sent to ' + name + '.', { kind: 'ok', action: { label: 'Open it', href: '#/messages/m/' + enc(r.message) } });
    if (r) { reloadInbox(); loadUnread(); }
    return r;
  }
  async function unpair(button, channel, a) {
    const ok = await SF.confirmModal({ title: 'Unpair this account?', description: 'It stops getting notices and its commands are ignored. You can pair it again later.', confirm: 'Unpair', cls: 'btn-danger', icon: 'x' });
    if (!ok) return;
    const r = await act(button, '/api/notify/unpair', { channel, chat_id: a.id }, 'Unpaired.', { what: 'Could not unpair' });
    if (r) fetchKey('channels', '/api/notify/channels');
  }
  async function channelAction(button, c, action) {
    const isTg = c.type === 'telegram';
    if (action === 'disable') {
      const ok = await SF.confirmModal({ title: 'Turn ' + c.name + ' off?', description: 'Nothing is sent to it and nothing it sends is read until you turn it on again here (Turn on).', confirm: 'Turn off', cls: 'btn-danger', icon: 'ban' });
      if (!ok) return;
      const r = await act(button, '/api/notify/channels/' + enc(c.name) + '/disable', {}, c.name + ' is off.', { what: 'Could not turn it off' });
      if (r) fetchKey('channels', '/api/notify/channels');
      return;
    }
    if (isTg) {
      const field = h('input', { class: 'input', type: 'password', autocomplete: 'off', spellcheck: 'false', dataset: { field: 'bot-token' } });
      const r = await SF.openModal({
        title: action === 'leaked' ? 'Telegram: the token leaked' : 'Telegram: a new bot token',
        description: 'In Telegram, send /revoke to @BotFather and pick your bot; it answers with a new token, and the old one stops working. Every paired account is unpaired, so pair your phone again afterwards.',
        body: SF.field('The new bot token', field, 'Pasted here only; it goes to the server’s env file and is never shown again.'),
        actions: [{ label: 'Cancel', value: 'cancel' }, { label: 'Use the new token', value: 'ok', primary: true }],
        dismissable: false,
        onSubmit: async () => { try { return await SF.api('/api/notify/channels/' + enc(c.name) + '/' + action, { method: 'POST', body: { token: field.value } }); } finally { field.value = ''; } },
      });
      if (r && isObj(r)) {
        resetPairing('telegram', c.name);
        SF.toast(String(r.next || 'Pair your phone again.'), { kind: 'ok', action: { label: 'Pair now', href: '#/messages/setup/telegram/3?channel=' + enc(c.name) } });
        fetchKey('channels', '/api/notify/channels');
      }
      return;
    }
    const ok = await SF.confirmModal({ title: action === 'leaked' ? 'ntfy: the tokens leaked' : 'ntfy: new tokens', confirm: 'Make new ones', icon: 'refresh',
      description: action === 'leaked' ? 'The harness makes new harness and reply tokens and a new phone passphrase. Run the copy blocks, then sign in on your phone again.'
        : 'The harness makes a new reply token and a new phone passphrase. Run the copy blocks, then sign in on your phone again.' });
    if (!ok) return;
    const r = await act(button, '/api/notify/channels/' + enc(c.name) + '/' + action, {}, null, { what: 'Could not make new tokens' });
    if (!r) return;
    resetPairing('ntfy', c.name);
    fetchKey('channels', '/api/notify/channels');
    await secretsDialog('New phone credentials for ' + c.name, r.passphrase, arr(r.copy_blocks), 'Run 2b and 2c again, then in the ntfy app sign in as owner-phone with the new passphrase, and send a test from step 7 of the setup.');
  }
  /** Copy blocks and a passphrase shown once in a dialog; closing it forgets them. */
  function secretsDialog(title, passphrase, blocks, next, done) {
    return SF.openModal({ title, wide: true, description: next || null,
      body: [arr(done).length ? h('ul', { class: 'mw-done' }, arr(done).map((d) => h('li', null, icon('check'), h('span', null, String(d))))) : null,
        passphrase ? secretOnce(passphrase, 'The phone’s passphrase') : null, arr(blocks).map(copyBlock)],
      actions: [{ label: 'Done', value: 'ok', primary: true }] });
  }
  async function lostPhone(button) {
    const ok = await SF.confirmModal({ title: 'Lost your phone?', confirm: 'Lock it out now', cls: 'btn-danger', icon: 'shield',
      description: 'This unpairs every Telegram account, stops publishing to ntfy, makes new phone credentials and stops Seraphina. Alerts wait in the inbox until you set up the new phone.' });
    if (!ok) return;
    const r = await act(button, '/api/notify/lost-phone', {}, null, { what: 'Could not lock the phone out' });
    if (!r) return;
    resetPairing(null, null);
    reloadState();
    fetchKey('channels', '/api/notify/channels');
    await secretsDialog('Your lost phone is locked out', r.passphrase, arr(r.copy_blocks), String(r.next || ''), arr(r.done));
  }
  function renderSetup() {
    ensure('channels', '/api/notify/channels', 4000);
    ensure('notify', '/api/notify', 4000);
    const chans = val('channels');
    const have = (type) => arr(chans).find((c) => c.type === type);
    const option = (type, title, text, needs, rec) => h('article', { class: 'sf-card mw-option' + (rec ? ' is-rec' : ''), dataset: { option: type } },
      h('div', { class: 'mw-row-top' }, icon(type === 'ntfy' ? 'bell' : 'send'), h('h3', { class: 'mw-h3' }, title), rec ? tag('Recommended', 'tag-ok') : tag('Third party', 'tag-warn')),
      h('p', null, text), h('p', { class: 'field-hint' }, needs),
      h('div', { class: 'actions' }, linkBtn(have(type) ? 'Set it up again' : 'Set up ' + (type === 'ntfy' ? 'ntfy' : 'Telegram'), '#/messages/setup/' + type + '/1', { cls: 'btn-sm' + (rec ? ' btn-primary' : ''), icon: 'arrow-right' })));
    const pre = (val('notify') || {}).precondition;
    return [
      pre && !pre.ok ? callout('warn', 'shield', 'Phone messages stay off for now.', 'Your agents run without the sandbox or through a command-line driver. Turn on the sandbox in Settings › Harness, or accept the risk from the banner above.') : null,
      h('div', { class: 'sf-cards mw-options' },
        option('ntfy', 'ntfy on your tailnet', 'Your own small message server on this machine, reached only over your tailnet: nobody else can read the notices, and your phone can answer with buttons.',
          'Needs: Tailscale on this machine and your phone, the ntfy app, and a few commands you copy into a terminal.', true),
        option('telegram', 'Telegram', 'A Telegram bot sends you notices. It is quick to set up, but Telegram can read what it carries, so it only ever gets short heads-ups without details.',
          'Needs: the Telegram app and a bot you make with @BotFather (two minutes).', false)),
      h('h2', { class: 'home-group-title' }, 'Your phone channels'),
      errOf('channels') && !chans ? errorBox(errOf('channels'), 'the phone channels') : !chans ? SF.skeleton(3)
        : chans.length ? h('div', { class: 'mw-channels' }, chans.map(channelCard)) : SF.emptyState('bell', 'No phone yet', 'Pick ntfy (private) or Telegram above; the inbox works without either.', null, true),
      section('Lost your phone?', 'Locks the lost phone out at once: unpairs Telegram, stops ntfy, makes new credentials and stops Seraphina.',
        h('div', { class: 'actions' }, SF.btn('Lost my phone', { cls: 'btn-sm btn-danger', icon: 'shield', dataset: { action: 'lost-phone', focus: 'lost-phone' }, onClick: (e) => lostPhone(e.currentTarget) })), { id: 'mw-lost' }),
      howTo([
        ['Private first.', 'Notices that mention a persona or what you told her go to the private channel when one works; Telegram gets only fixed heads-ups such as “Something needs you”.'],
        ['Test each phone.', 'Send a test and tap Got it on the phone: the channel then shows when it last heard from you.'],
        ['Tokens are never shown back.', 'New tokens and It leaked make new ones; a passphrase is shown once, when it is made.'],
      ], 'How phone setup works'),
    ];
  }

  // ---------------------------------------------------------------- the wizards (ntfy: 7 steps, Telegram: 5)
  const NTFY_STEPS = ['Name and address', 'Install ntfy', 'Prepare the settings', 'Install and serve', 'Check', 'The phone', 'Test'];
  const TG_STEPS = ['The bot', 'Turn it on', 'Pair', 'The code', 'Test'];
  function browserZone() { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ''; } catch (e) { return ''; } }
  function wizardShell(kind, step, steps, title, lead, body, nav) {
    const base = '#/messages/setup/' + kind + '/';
    return [
      h('div', { class: 'mw-wizard', dataset: { wizard: kind, step: String(step) } },
        h('ol', { class: 'mw-steps', 'aria-label': 'Steps' }, steps.map((name, i) => h('li', { class: i + 1 === step ? 'is-current' : i + 1 < step ? 'is-done' : null },
          h('a', { href: base + (i + 1) + wizardQs(kind), 'aria-current': i + 1 === step ? 'step' : null, dataset: { focus: 'step:' + (i + 1) } }, h('span', { class: 'mw-step-n' }, String(i + 1)), h('span', null, name))))),
        h('section', { class: 'sf-card mw-step', 'aria-labelledby': 'mw-step-t' },
          h('div', { class: 'wz-eyebrow' }, (kind === 'ntfy' ? 'ntfy on your tailnet' : 'Telegram') + ' · step ' + step + ' of ' + steps.length),
          h('h2', { class: 'mw-step-title', id: 'mw-step-t', tabindex: '-1' }, title),
          lead ? SF.procedureList(lead) : null, body,
          h('div', { class: 'mw-step-nav' }, step > 1 ? linkBtn('Back', base + (step - 1) + wizardQs(kind), { icon: 'chevron-left' }) : linkBtn('Phone setup', '#/messages/setup', { icon: 'chevron-left' }), h('span', { class: 'spacer' }), nav))),
    ];
  }
  function wizardQs(kind) { const w = M.wizard[kind]; return w && w.channel ? '?channel=' + enc(w.channel) : ''; }
  const goStep = (kind, n) => SF.go('#/messages/setup/' + kind + '/' + n + wizardQs(kind));
  function nextBtn(label, onClick, opts) {
    return SF.btn(label || 'Next', Object.assign({ cls: 'btn-sm btn-primary', icon: 'arrow-right', dataset: { action: 'next', focus: 'wz-next' }, onClick }, opts || {}));
  }
  function stopPoll() { if (M.poll) { clearTimeout(M.poll.t); M.poll = null; } }
  /** Ask GET /api/notify/pair every 2 s; onState(r) (may be async) stores the answer in the wizard and returns false to stop.
   * The page re-renders only when the pairing's state changed (a selection or a long-press on the code survives the
   * ticks), and the change is announced in the one live region outside the page. */
  function poll(kind, channel, onState) {
    if (M.poll && M.poll.kind === kind && M.poll.channel === channel) return;
    stopPoll();
    const me = { kind, channel, t: 0 };
    M.poll = me;
    const w = M.wizard[kind];
    // "waiting" draws the same as no answer yet: only another state (or an error) is a change worth a re-render
    const sig = () => JSON.stringify([w.pairState && w.pairState.state !== 'waiting' ? w.pairState : null, w.pollErr ? w.pollErr.message : null]);
    const tick = async () => {
      if (M.poll !== me) return;
      const before = sig();
      let go = true;
      try {
        const r = await SF.api('/api/notify/pair?channel=' + enc(channel));
        if (M.poll !== me) return;
        w.pollErr = null;
        go = (await onState(r)) !== false;
      } catch (e) { if (M.poll !== me) return; w.pollErr = e; }
      if (M.poll !== me) return;
      if (!go) M.poll = null;
      if (sig() !== before) { announce(pairWords(kind)); refresh(); }
      if (go) me.t = setTimeout(tick, 2000);
    };
    me.t = setTimeout(tick, 50);
  }
  /** What the live region says about a pairing (nothing while it only waits). */
  function pairWords(kind) {
    const w = M.wizard[kind];
    if (w.pollErr) return 'Could not ask the server: ' + w.pollErr.message;
    const st = w.pairState;
    if (!st || st.state === 'waiting') return '';
    if (st.state === 'paired') return kind === 'ntfy' ? 'Paired: your phone answered.' : 'Paired.';
    if (st.state === 'expired') return 'The code expired: start again.';
    if (st.state === 'confirm') {
      const a = st.account || {};
      return 'The bot heard from ' + String(a.first_name || 'someone') + (a.username ? ' (@' + String(a.username) + ')' : '') + '. Is this you? Type the 6-digit code it sent you.';
    }
    return '';
  }
  /** The channel the URL names wins over the one this page remembered (a new channel starts its wizard afresh). */
  function wizardFor(kind, route, fallback) {
    const q = route.query && /^[a-z][a-z0-9-]{0,31}$/.test(route.query.channel || '') ? route.query.channel : null;
    if (q && M.wizard[kind].channel && M.wizard[kind].channel !== q) { stopPoll(); M.wizard[kind] = {}; }
    const w = M.wizard[kind];
    if (!w.channel) w.channel = q || fallback();
    return w;
  }
  const lastAnswerOf = (rows, name) => { const c = arr(rows).find((x) => x.name === name) || {}; return c.last_answer || (isObj(c.health) ? c.health.last_answer : null) || null; };

  function renderNtfy(page, route) {
    const cfg = val('config');
    ensure('config', '/api/config', 15000);
    ensure('channels', '/api/notify/channels', 5000);
    const w = wizardFor('ntfy', route, () => (arr(val('channels')).find((c) => c.type === 'ntfy') || {}).name || 'phone');
    const step = page.step;
    if (step !== 3) M.secret.passphrase = null;
    if (step !== 7) stopPoll();
    const err = errLine('031');
    const entry = cfg && isObj(cfg.notify) && isObj(cfg.notify.channels) ? cfg.notify.channels[w.channel] : null;
    if (step === 1 && !cfg) {   // the fields start from the channel as saved: no form to type into before it is read
      return wizardShell('ntfy', 1, NTFY_STEPS, 'Name the channel and give its address', null,
        errOf('config') ? errorBox(errOf('config'), 'your settings') : SF.skeleton(4), null);
    }
    if (step === 1) {
      const pub = entry && entry.public_url ? /^https:\/\/([^/:]+)(?::(\d+))?/.exec(String(entry.public_url)) : null;
      const name = input({ value: w.channel, 'aria-describedby': 'mw-n-name-h', dataset: { field: 'channel' }, pattern: '[a-z][a-z0-9-]{0,31}' });
      const host = input({ value: w.host || (pub ? pub[1] : ''), placeholder: 'your-server.your-tailnet.ts.net', dataset: { field: 'host' } });
      const port = numberInput(w.port || (pub && pub[2]) || 8445, 1, 65535, { dataset: { field: 'port' } });
      const zone = input({ value: w.zone || (cfg && isObj(cfg.wakes) && cfg.wakes.timezone) || browserZone(), placeholder: 'Europe/London', dataset: { field: 'timezone' } });
      const save = async (e) => {
        formError(err, null);
        const chan = name.value.trim();
        w.host = host.value.trim().toLowerCase(); w.port = String(port.value || '8445'); w.zone = zone.value.trim();
        if (!/^[a-z][a-z0-9-]{0,31}$/.test(chan)) return formError(err, 'The name is one word of a-z, 0-9 and -, starting with a letter (for example phone).');
        if (!/^[a-z0-9-]+(\.[a-z0-9-]+)+$/.test(w.host)) return formError(err, 'Type the machine’s name on your tailnet, like your-server.your-tailnet.ts.net (no https, no slash).');
        const publicUrl = ['https:', '', w.host + ':' + w.port].join('/');   // built from what the owner typed
        // enabled: true, so setting a channel up again also turns it back on after Turn off or Lost my phone
        const body = { wakes: { timezone: w.zone || null }, notify: { channels: { [chan]: { type: 'ntfy', enabled: true, publish_url: 'unix:/run/ntfy/ntfy.sock', public_url: publicUrl,
          token_env: 'SERAPHINA_NTFY_TOKEN', reply_token_env: 'SERAPHINA_NTFY_REPLY_TOKEN' } } } };
        const r = await act(e.currentTarget, '/api/config', body, 'Saved the channel ' + chan + '.', { onError: (x) => formError(err, x) });
        if (r) {
          if (chan !== w.channel) { resetPairing('ntfy', null); w.channel = chan; }
          reread('config', '/api/config'); fetchKey('channels', '/api/notify/channels'); goStep('ntfy', 2);
        }
      };
      return wizardShell('ntfy', 1, NTFY_STEPS, 'Name the channel and give its address', [
        ['Pick a name.', 'One word for this phone channel; phone is fine.'],
        ['Give the machine’s tailnet name.', 'It is the name Tailscale shows for this server, ending in .ts.net. Your phone reaches ntfy there, on port 8445, and only over your tailnet.'],
        ['Check the time zone.', 'Do-not-disturb time and the morning summary follow it.'],
      ], h('div', { class: 'mw-form' },
        SF.field('Channel name', name, h('span', { id: 'mw-n-name-h' }, 'a-z, 0-9 and -')),
        h('div', { class: 'mw-form-row' }, SF.field('Machine name on your tailnet', host, 'Find it with: tailscale status'), SF.field('Port', port, 'The default is 8445')),
        w.host && !/\.ts\.net$/.test(w.host) ? callout('warn', 'alert', 'Not a tailnet name?', 'Names that do not end in .ts.net may not be private: the harness computes the privacy and Telegram-like rules then apply.') : null,
        SF.field('Time zone', zone, 'An IANA name such as Europe/London; your browser says ' + (browserZone() || 'nothing')), err),
      nextBtn('Save and continue', save));
    }
    if (step === 2) {
      const key = 'ntfycheck:' + w.channel;
      ensure(key, '/api/notify/ntfy/check?channel=' + enc(w.channel), 60000);
      const c = val(key);
      return wizardShell('ntfy', 2, NTFY_STEPS, 'Install ntfy on this machine', [
        ['Open a terminal on the server.', 'You need an account that may use sudo.'],
        ['Copy block 2a and run it.', 'It adds the ntfy package repository and installs ntfy; nothing is started yet.'],
      ], [errOf(key) && !c ? errorBox(errOf(key), 'the install commands') : !c ? SF.skeleton(3) : copyBlock(c.install_block, 0)],
      nextBtn('It is installed', () => goStep('ntfy', 3)));
    }
    if (step === 3) {
      const ready = !!w.prepared;
      const prepare = async (e) => {
        formError(err, null);
        const r = await act(e.currentTarget, '/api/notify/ntfy/prepare', { channel: w.channel }, null, { onError: (x) => { formError(err, x); w.notInstalled = /not installed/.test(x && x.message || ''); refresh(); } });
        if (r) { M.secret.passphrase = r.passphrase || null; M.secret.shown = false; w.blocks = arr(r.copy_blocks); w.setupDir = r.setup_dir; w.prepared = true; w.wrote = false; w.notInstalled = false; resetPairing('ntfy', w.channel); refresh(); }
      };
      return wizardShell('ntfy', 3, NTFY_STEPS, 'Prepare the settings', [
        ['Press Prepare the settings.', 'The harness makes the tokens (into its env file, never shown) and a passphrase for your phone, and writes ntfy’s settings into a folder for you to install.'],
        ['Write the passphrase down.', 'It is shown once. Your phone signs in with it as owner-phone; the harness keeps only its hash.'],
      ], [
        ready && M.secret.passphrase ? secretOnce(M.secret.passphrase, 'The phone’s passphrase', (v) => { w.wrote = v; refresh(); }, { wrote: w.wrote, shown: M.secret.shown, onShown: (v) => { M.secret.shown = v; } })
          : ready ? callout('info', 'info', 'Prepared.', 'The passphrase was shown when you prepared; prepare again for a new one if you did not write it down.') : null,
        w.setupDir ? h('p', { class: 'field-hint' }, 'The files wait in ', h('code', null, String(w.setupDir)), ' until the check sees ntfy working; then they are deleted.') : null,
        err, w.notInstalled ? h('p', null, linkBtn('Back to step 2: install ntfy', '#/messages/setup/ntfy/2' + wizardQs('ntfy'), { icon: 'chevron-left' })) : null,
        h('div', { class: 'actions' }, SF.btn(ready ? 'Prepare again' : 'Prepare the settings', { cls: 'btn-sm' + (ready ? '' : ' btn-primary'), icon: 'zap', dataset: { action: 'prepare', focus: 'prepare' }, onClick: prepare })),
      ], nextBtn('Next', () => goStep('ntfy', 4), { disabled: !ready || (!!M.secret.passphrase && !w.wrote), title: ready && !w.wrote ? 'Tick “I wrote it down” first' : null }));
    }
    if (step === 4) {
      const blocks = arr(w.blocks).filter((b) => b.id === '2b' || b.id === '2c');
      return wizardShell('ntfy', 4, NTFY_STEPS, 'Install the settings and serve ntfy on your tailnet', [
        ['Run block 2b.', 'It installs the prepared files, adds a daily limit and starts ntfy.'],
        ['Read the warning, then run block 2c.', 'It makes ntfy reachable from your tailnet only (never the internet).'],
      ], blocks.length ? blocks.map(copyBlock) : callout('info', 'info', 'Prepare the settings first.', 'The commands of this step come from step 3.'),
      nextBtn('Both ran', () => goStep('ntfy', 5)));
    }
    if (step === 5) {
      const key = 'ntfycheck:' + w.channel;
      const c = val(key);
      const again = async (e) => { SF.setBusy(e.currentTarget, true); drop(key); await fetchKey(key, '/api/notify/ntfy/check?channel=' + enc(w.channel)); };
      if (!c && !D[key]) fetchKey(key, '/api/notify/ntfy/check?channel=' + enc(w.channel));
      const line = (ok, text, bad, warnOnly) => h('li', { class: 'mw-check-line ' + (ok ? 'is-ok' : warnOnly ? 'is-warn' : 'is-bad') }, icon(ok ? 'check-circle' : 'alert'), h('span', null, ok ? text : bad));
      const lines = c ? [
        line(isObj(c.health) && c.health.ok, 'ntfy answers.', 'ntfy does not answer: ' + String((c.health || {}).detail || '')),
        line(c.socket !== false, 'Its socket is there.', 'Its socket is missing: is ntfy running? (block 2b)'),
        line(!!c.acl_ok, 'The access rules work: the harness may post, the reply token may not read.', 'The access rules do not work yet (block 2b).'),
        h('li', { class: 'mw-check-line is-info' }, icon('info'), h('span', null, 'Topics: ' + Object.keys(isObj(c.reads) ? c.reads : {}).map((t) => t + ' ' + (c.reads[t] === null ? 'no answer' : c.reads[t])).join(', '))),
        line(!!c.serve, 'It is served on your tailnet.', 'It is not served on your tailnet yet (block 2c).'),
        line(c.funnel_off !== false, 'Funnel is off: nothing is on the public internet.', 'Funnel is ON: ntfy is on the public internet. Turn it off: sudo tailscale funnel reset'),
        line(!c.tcp_listener, c.port80_other ? 'ntfy does not answer on port 80 (another program, such as your web server, listens there: that is fine).' : 'ntfy does not answer on port 80.', 'ntfy also answers on port 80.', true),
        line(c.privacy === 'tailnet', 'The channel is private (tailnet).', 'The channel is not private: the harness treats it like a third party.'),
        h('li', { class: 'mw-check-line is-info' }, icon('info'), h('span', null, 'Daily limits: ' + (c.tiers_ok === null || c.tiers_ok === undefined ? 'not read back yet (block 2b set them)' : c.tiers_ok ? 'set' : 'missing'))),
      ] : [];
      const good = c && c.acl_ok && c.funnel_off !== false && c.privacy === 'tailnet';
      return wizardShell('ntfy', 5, NTFY_STEPS, 'Check that it works', [
        ['Press Check again.', 'The harness asks ntfy for its health, tries each topic with its own token and makes sure the reply token is refused, and looks at Tailscale’s serve and Funnel settings.'],
        ['Fix a red line, then check again.', 'Each line names the block that fixes it. Once the access rules work, the staged folder is deleted.'],
      ], [errOf(key) && !c ? errorBox(errOf(key), 'the check') : !c ? SF.skeleton(4) : h('ul', { class: 'mw-checks', 'aria-live': 'polite' }, lines),
        h('div', { class: 'actions' }, SF.btn('Check again', { cls: 'btn-sm', icon: 'refresh', dataset: { action: 'check', focus: 'check' }, onClick: again }))],
      nextBtn(good ? 'Next' : 'Continue anyway', () => goStep('ntfy', 6), { cls: 'btn-sm' + (good ? ' btn-primary' : '') }));
    }
    if (step === 6 || step === 7) {
      if (!w.pair && !w.pairErr && !w.pairing) {
        w.pairing = true;
        SF.api('/api/notify/pair', { method: 'POST', body: { channel: w.channel } }).then((r) => { w.pair = r; }, (e) => { w.pairErr = e; }).finally(() => { w.pairing = false; refresh(); });
      }
      const p = w.pair;
      if (step === 6) {
        const qr = p && p.server_url ? QR.canvas(String(p.server_url), 'QR code with the ntfy server address') : null;
        return wizardShell('ntfy', 6, NTFY_STEPS, 'Add your phone', [
          ['Install the ntfy app.', 'From the App Store or Google Play.'],
          ['Add the server and sign in.', p ? 'In the app’s settings, add the server ' + p.server_url + ' and the user ' + (p.username || 'owner-phone') + ' with the passphrase you wrote down.' : 'The address appears here.'],
          ['Subscribe.', p && isObj(p.topics) ? 'Subscribe to ' + p.topics.notices + '. On Android also to ' + p.topics.chat + ' (name it “Write to Seraphina” and mute it): what you write there comes back here, under What came back.' : 'The topics appear here.'],
        ], [w.pairErr ? h('div', { class: 'form-error', role: 'alert' }, icon('alert'), h('span', null, w.pairErr.message)) : !p ? SF.skeleton(3) :
          h('div', { class: 'mw-pairbox' }, qr ? h('figure', { class: 'mw-qr-fig' }, qr, h('figcaption', null, 'Scan to copy the server address (it carries no passphrase).')) : null,
            h('dl', { class: 'sf-kv' }, h('dt', null, 'Server'), h('dd', null, h('code', null, String(p.server_url || ''))), h('dt', null, 'User'), h('dd', null, h('code', null, String(p.username || 'owner-phone'))),
              isObj(p.topics) ? [h('dt', null, 'Notices'), h('dd', null, h('code', null, String(p.topics.notices))), h('dt', null, 'Write to Seraphina'), h('dd', null, h('code', null, String(p.topics.chat)))] : null))],
          nextBtn('The phone is signed in', () => goStep('ntfy', 7)));
      }
      // GET /api/notify/pair says paired for ntfy once the phone ever answered (NtfyChannel.pair_status): only an answer
      // newer than the last one seen when this step opened (or when a test was sent) counts, so New tokens or a new
      // phone waits for the new phone's answer.
      if (w.since === undefined && loaded('channels')) w.since = lastAnswerOf(val('channels'), w.channel);
      const st = w.pairState;
      if (w.since !== undefined && (!st || st.state !== 'paired')) {
        poll('ntfy', w.channel, async (r) => {
          if (r && r.state === 'paired') {
            const last = lastAnswerOf(arr((await SF.api('/api/notify/channels') || {}).channels), w.channel);
            w.seen = last;
            if (last && (!w.since || Date.parse(last) > Date.parse(w.since))) { w.pairState = r; return false; }
            w.pairState = { state: 'waiting', account: null };
            return true;
          }
          w.pairState = r;
          return true;
        });
      }
      const test = (e) => {
        if (w.seen !== undefined) w.since = w.seen;
        if (w.pairState && w.pairState.state === 'paired') w.pairState = null;
        return sendTest(e.currentTarget, w.channel).then(() => refresh());
      };
      const paired = st && st.state === 'paired';
      return wizardShell('ntfy', 7, NTFY_STEPS, 'Send a test', [
        ['Press Send a test.', 'Your phone gets a test notice with a Got it button.'],
        ['Tap Got it on the phone.', 'This page waits for your answer and says when the phone is paired.'],
      ], [h('div', { class: 'actions' }, SF.btn(paired ? 'Send another test' : 'Send a test', { cls: 'btn-sm' + (paired ? '' : ' btn-primary'), icon: 'send', dataset: { action: 'test', focus: 'wz-test' }, onClick: test })),
        h('p', { class: 'mw-pair-state', dataset: { state: paired ? 'paired' : 'waiting' } },
          paired ? [icon('check-circle'), ' Paired: your phone answered.'] : [icon('hourglass'), ' Waiting for your phone to answer…']),
        !paired && w.since ? h('p', { class: 'field-hint' }, 'It last answered ', when(w.since), ': this step waits for a newer answer, from the phone signed in with the current passphrase.') : null,
        w.pollErr ? h('p', { class: 'mw-error-text' }, 'Could not ask the server: ' + String(w.pollErr.message)) : null],
      paired ? linkBtn('Done', '#/messages/setup', { cls: 'btn-sm btn-primary', icon: 'check' }) : null);
    }
    return [];
  }

  function renderTelegram(page, route) {
    const cfg = val('config');
    ensure('config', '/api/config', 15000);
    const w = wizardFor('telegram', route, () => 'telegram');
    const step = Math.min(page.step, 5);
    if (step !== 3 && step !== 4) stopPoll();
    const err = errLine('032');
    if (step === 1) {
      const field = h('input', { class: 'input', type: 'password', autocomplete: 'off', spellcheck: 'false', dataset: { field: 'bot-token', focus: 'fld:bot-token' } });
      const save = async (e) => {
        formError(err, null);
        const value = field.value.trim();
        if (!value) { goStep('telegram', 2); return; }
        const r = await act(e.currentTarget, '/api/secrets', { name: 'SERAPHINA_TELEGRAM_BOT_TOKEN', value }, 'Saved the bot token.', { onError: (x) => formError(err, x) });
        field.value = '';
        if (r) { w.tokenSaved = true; goStep('telegram', 2); }
      };
      return wizardShell('telegram', 1, TG_STEPS, 'Make a bot', [
        ['Open @BotFather in Telegram.', 'Send /newbot and give the bot a name that says nothing about you (say, “Box notices”).'],
        ['Copy the token it gives you.', 'Paste it below; it goes to the server’s env file and is never shown again.'],
      ], h('div', { class: 'mw-form' }, SF.field('The bot token', field, w.tokenSaved ? 'Saved. Paste a new one only to replace it.' : 'Leave it empty if you saved it before.'), err),
      nextBtn('Save and continue', save));
    }
    if (step === 2) {
      const entry = cfg && isObj(cfg.notify) && isObj(cfg.notify.channels) && isObj(cfg.notify.channels[w.channel]) ? cfg.notify.channels[w.channel] : {};
      const opsWas = entry.operational === true;   // Telegram's default is off (settings, notify.channels.<n>.operational)
      const zone = input({ value: w.zone || (cfg && isObj(cfg.wakes) && cfg.wakes.timezone) || browserZone(), placeholder: 'Europe/London', dataset: { field: 'timezone' } });
      const ops = h('input', { type: 'checkbox', checked: opsWas, dataset: { field: 'operational', focus: 'fld:operational' } });
      const on = async (e) => {
        formError(err, null);
        w.zone = zone.value.trim();
        // enabled: true turns a channel that was turned off back on; operational only when the box changed, so the
        // choice made in Message settings stays as it is
        const change = { type: 'telegram', enabled: true, token_env: 'SERAPHINA_TELEGRAM_BOT_TOKEN' };
        if (ops.checked !== opsWas) change.operational = ops.checked;
        const body = { wakes: { timezone: w.zone || null }, notify: { channels: { [w.channel]: change } } };
        const r = await act(e.currentTarget, '/api/config', body, 'Telegram is on.', { onError: (x) => formError(err, x) });
        if (r) { reread('config', '/api/config'); fetchKey('channels', '/api/notify/channels'); goStep('telegram', 3); }
      };
      return wizardShell('telegram', 2, TG_STEPS, 'Turn the channel on', [
        ['Know what Telegram sees.', 'Telegram can read every message its bots carry, so this channel only gets fixed heads-ups (“Something needs you”), never a persona’s words or your reminders’ text.'],
        ['Check the time zone.', 'Do-not-disturb time follows it.'],
      ], h('div', { class: 'mw-form' }, callout('warn', 'alert', 'Third party:', 'Telegram (the company) can read what this channel carries.'),
        SF.field('Time zone', zone, 'An IANA name such as Europe/London'),
        h('label', { class: 'mw-check' }, ops, h('span', null, 'Also send job numbers and steps (Telegram can then read them)')), err),
      nextBtn('Turn it on', on, { disabled: !cfg, title: cfg ? null : 'Reading the settings…' }));
    }
    if (step === 3 || step === 4) {
      const start = async (e) => {
        stopPoll();
        w.code = null; w.pairState = null; w.pairErr = null; w.wrong = false;
        refresh();
        const r = await act(e && e.currentTarget, '/api/notify/pair', { channel: w.channel }, null, { onError: (x) => { w.pairErr = x; refresh(); } });
        if (r) { w.code = r; w.pairErr = null; stopPoll(); if (page.step !== 3) goStep('telegram', 3); else refresh(); }
      };
      const c = w.code;
      const st = w.pairState;
      if (c && (!st || (st.state !== 'paired' && st.state !== 'expired'))) poll('telegram', w.channel, (r) => { w.pairState = r; if (r && r.state === 'confirm' && M.page && M.page.kind === 'telegram' && M.page.step === 3) goStep('telegram', 4); return !(r && (r.state === 'paired' || r.state === 'expired')); });
      const link = c ? SF.safeHref(c.link) : null;
      const codeField = input({ inputmode: 'numeric', pattern: '[0-9]{6}', maxlength: '6', placeholder: '123456', autocomplete: 'one-time-code', 'aria-describedby': 'mw-tg-code-h', dataset: { field: 'code' } });
      const confirm = async (button) => {
        formError(err, null);
        const r = await act(button, '/api/notify/pair/confirm', { channel: w.channel, code: codeField.value.trim() }, null, { onError: (x) => formError(err, x) });
        if (!r) return;
        if (r.state === 'paired') { w.pairState = r; stopPoll(); SF.toast('Paired.', { kind: 'ok' }); goStep('telegram', 5); }
        else if (r.state === 'expired') { w.pairState = r; stopPoll(); announce(pairWords('telegram')); refresh(); }
        else { w.wrong = true; codeField.value = ''; announce('That code was not right: try again. Three wrong codes cancel.'); refresh(); }
      };
      const confirmBtn = SF.btn('Pair this account', { cls: 'btn-sm btn-primary', icon: 'check', dataset: { action: 'confirm', focus: 'confirm' }, onClick: (e) => confirm(e.currentTarget) });
      codeField.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.isComposing) { e.preventDefault(); confirm(confirmBtn); } });
      const stateLine = !c ? null : h('p', { class: 'mw-pair-state', dataset: { state: st ? st.state : 'waiting' } },
        !st || st.state === 'waiting' ? [icon('hourglass'), ' Waiting for you to send the code to the bot…']
          : st.state === 'confirm' ? [icon('user'), ' The bot heard from ', h('strong', null, String((st.account || {}).first_name || 'someone')), (st.account || {}).username ? ' (@' + String(st.account.username) + ')' : '', '. Is this you?']
            : st.state === 'paired' ? [icon('check-circle'), ' Paired.'] : [icon('alert'), ' The code expired: start again.']);
      const again = (st && (st.state === 'paired' || st.state === 'expired')) || w.pairErr;
      const body = [
        !c && !w.pairErr ? h('div', { class: 'actions' }, SF.btn('Pair', { cls: 'btn-sm btn-primary', icon: 'zap', title: 'Get a one-time code for your bot (it works for 10 minutes)', dataset: { action: 'pair', focus: 'pair' }, onClick: start })) : null,
        w.pairErr ? h('div', { class: 'form-error', role: 'alert' }, icon('alert'), h('span', null, w.pairErr.message)) : null,
        c && step === 3 && !(st && st.state === 'paired') ? h('div', { class: 'mw-pairbox' },
          link ? h('figure', { class: 'mw-qr-fig' }, QR.canvas(link, 'QR code that opens your bot in Telegram'), h('figcaption', null, 'Scan with your phone’s camera.')) : null,
          h('div', { class: 'mw-stack' },
            link ? h('a', { class: 'btn btn-sm btn-primary', href: link, target: '_blank', rel: 'noopener noreferrer', dataset: { action: 'open-telegram', focus: 'open-telegram' } }, icon('external'), 'Open the bot in Telegram') : null,
            h('p', null, link ? 'Or send this to the bot yourself:' : 'Send this to your bot:'),
            h('div', { class: 'mw-code-row' }, h('code', { class: 'mw-code' }, String(c.text || '')),
              SF.btn('Copy', { cls: 'btn-xs', icon: 'copy', aria: 'Copy the text to send to your bot', dataset: { action: 'copy-code', focus: 'copy-code' }, onClick: () => SF.copyText(String(c.text || ''), 'Copied: paste it into your bot’s chat in Telegram.') })),
            c.expires_at ? h('p', { class: 'field-hint' }, 'Works once, until ', localTime(c.expires_at), '.') : null)) : null,
        stateLine,
        step === 4 && st && st.state === 'confirm' ? h('div', { class: 'mw-form' }, SF.field('The 6-digit code the bot sent you', codeField, h('span', { id: 'mw-tg-code-h' }, w.wrong ? 'That code was not right: try again (three wrong codes cancel).' : 'Type it here, on this page, not in Telegram; Enter pairs.')), err,
          h('div', { class: 'actions' }, confirmBtn)) : null,
        step === 4 && !c ? h('p', { class: 'field-hint' }, 'Press Pair first: the bot answers with the code once you send it the one-time text.') : null,
        again ? h('div', { class: 'actions' }, SF.btn(st && st.state === 'paired' ? 'Pair another account' : 'Start again', { cls: 'btn-sm', icon: 'refresh', dataset: { action: 'pair-again', focus: 'pair-again' }, onClick: start })) : null,
      ];
      return wizardShell('telegram', step, TG_STEPS, step === 3 ? 'Pair your phone' : 'Type the code', step === 3 ? [
        ['Press Pair.', 'The harness makes a one-time code for your bot; it works once, for ten minutes.'],
        ['Open your bot.', 'Tap the button or scan the code: Telegram opens your bot with the code already filled in. Press Start.'],
        ['Come back here.', 'This page notices when the bot hears from you.'],
      ] : [
        ['Check that it is you.', 'This page shows who wrote to the bot.'],
        ['Type the 6-digit code.', 'The bot answered with it; typing it here proves the account is yours.'],
      ], body, st && st.state === 'paired' ? nextBtn('Next', () => goStep('telegram', 5)) : null);
    }
    return wizardShell('telegram', 5, TG_STEPS, 'Send a test', [
      ['Press Send a test.', 'Your phone gets a short test notice with a Got it button.'],
      ['Turn on Two-Step Verification in Telegram.', 'In Telegram open Settings › Privacy and Security › Two-Step Verification: then nobody can take over your Telegram account with a text message.'],
    ], h('div', { class: 'actions' }, SF.btn('Send a test', { cls: 'btn-sm btn-primary', icon: 'send', dataset: { action: 'test', focus: 'wz-test' }, onClick: (e) => sendTest(e.currentTarget, w.channel) })),
    linkBtn('Done', '#/messages/setup', { cls: 'btn-sm', icon: 'check' }));
  }

  // ---------------------------------------------------------------- Messages settings (GET /api/notify, /api/config, /api/notify/rules)
  function statusSection() {
    const st = val('notify');
    if (!st) return errOf('notify') ? errorBox(errOf('notify'), 'the message settings') : SF.skeleton(4);
    const terms = isObj(st.sensitive_terms) ? st.sensitive_terms : {};
    const qh = isObj(st.quiet_hours) ? st.quiet_hours : {};
    const rules = isObj(st.rules) ? st.rules : {};
    return section('How messages run now', null, h('dl', { class: 'sf-kv' },
      h('dt', null, 'Power schedule'), h('dd', null, st.power ? powerWords(st.power) : 'No power-off set'),
      h('dt', null, 'Time zone'), h('dd', null, String(st.timezone || '—'), st.timezone_error ? h('span', { class: 'mw-error-text' }, ' (your setting is wrong: ' + String(st.timezone_error) + ')') : null),
      h('dt', null, 'Do-not-disturb time'), h('dd', null, qh.window ? String(qh.window) + (qh.source ? ' (' + String(qh.source) + ')' : '') : 'Off', qh.now ? tag('now', 'tag-accent') : null),
      h('dt', null, 'Today'), h('dd', null, (isObj(st.today) ? (st.today.phone || 0) + ' of ' + (st.today.cap || '?') : '—') + ' phone notices (alerts from your server do not count)'),
      h('dt', null, 'Notice sender'), h('dd', null, st.liveness ? ['last ran ', when(st.liveness)] : 'not running'),
      h('dt', null, 'Clock'), h('dd', null, isObj(st.time_sync) && st.time_sync.ready === false ? 'waiting for the clock after boot' : 'the time is right' + (isObj(st.time_sync) && st.time_sync.fallback_used ? ' (set without network time: the doctor warns)' : '')),
      h('dt', null, 'Sensitive words'), h('dd', null, terms.none_declared ? 'You said you have none' : terms.exists ? SF.plural(terms.count || 0, 'word') + ' kept' : 'No list yet',
        terms.mode_ok === false ? h('span', { class: 'mw-error-text' }, ' · others on this machine can read the file') : null),
      h('dt', null, 'Rulebook'), h('dd', null, rules.source === 'file' ? 'notice-rules.json' : 'from config.json until your first change below', rules.error ? h('span', { class: 'mw-error-text' }, ' · ' + String(rules.error)) : null)),
      { id: 'mw-status' });
  }
  async function saveConfig(button, patch, okText, err) {
    formError(err, null);
    const r = await act(button, '/api/config', patch, okText, { onError: (x) => (err ? formError(err, x) : SF.toastError('Not saved', x)) });
    if (r) { reread('config', '/api/config'); reread('notify', '/api/notify'); reread('channels', '/api/notify/channels'); }
    return r;
  }
  function enabledSection() {
    const cfg = val('config');
    if (!cfg) return null;
    const on = !(isObj(cfg.notify) && cfg.notify.enabled === false);
    const err = errLine('041');
    return section('Phone messages', 'Off: nothing is sent to a phone, no phone is listened to, pairing is refused and nothing answers. The inbox keeps working.',
      [toggle('Send notices to my phones', on, null, { dataset: { focus: 'notify-enabled', field: 'notify-enabled' } }, async (v, el) => {
        const r = await saveConfig(el, { notify: { enabled: v } }, v ? 'Phone messages are on.' : 'Phone messages are off.', err);
        if (!r) revertSwitch(el, !v);   // refused: the switch shows what the server still has
      }), err],
      { id: 'mw-enabled' });
  }
  function channelOptions() {
    const cfg = val('config');
    const chans = arr(val('channels'));
    const conf = cfg && isObj(cfg.notify) && isObj(cfg.notify.channels) ? cfg.notify.channels : {};
    const names = Object.keys(conf).filter((n) => isObj(conf[n]));
    if (!cfg || !names.length) return null;
    const safe = arr(cfg.notify.safe_repos).map(String);
    const repos = arr(cfg.repos).map((r) => (isObj(r) ? String(r.name || '') : '')).filter(Boolean);
    const safeHint = safe.length ? 'Only these projects: ' + safe.join(', ') + ' (chosen below).' : 'No project is marked safe yet: choose them below.';
    return section('What each phone may carry', 'A notice is always cut to what a channel may carry: anything else becomes a fixed heads-up.', h('div', { class: 'mw-stack' }, names.map((name) => {
      const e = conf[name];
      const row = chans.find((c) => c.name === name) || {};
      const isTg = e.type === 'telegram';
      const err = errLine('chopt:' + name);
      const enabled = e.enabled !== false;
      const onSw = toggle('Send to this phone', enabled, enabled ? 'Off: nothing is sent to it and nothing it sends is read.' : 'It is turned off (Turn off or Lost my phone): turn it on to use it again.',
        { dataset: { focus: 'chon:' + name } }, async (v, el) => {
          const r = await saveConfig(el, { notify: { channels: { [name]: { enabled: v } } } }, v ? name + ' is on again.' : name + ' is off.', err);
          if (!r) revertSwitch(el, !v);
        });
      const ops = h('input', { type: 'checkbox', role: 'switch', checked: e.operational === undefined ? !isTg : !!e.operational, dataset: { focus: 'ops:' + name } });
      const names2 = h('input', { type: 'checkbox', role: 'switch', checked: !!e.operational_names, dataset: { focus: 'opsn:' + name } });
      const text = SF.selectEl([['never', 'Never: only fixed notices'], ['clean_only', 'A persona’s own words when her turn was clean']], e.model_text || 'never', { class: 'select select-sm', dataset: { focus: 'mt:' + name }, disabled: row.privacy !== 'tailnet' });
      const save = SF.btn('Save', { cls: 'btn-sm', icon: 'check', dataset: { action: 'save-channel', focus: 'save-ch:' + name }, onClick: (ev) => {
        const change = { operational: ops.checked, operational_names: names2.checked };
        if (!isTg) change.model_text = text.value;
        saveConfig(ev.currentTarget, { notify: { channels: { [name]: change } } }, 'Saved ' + name + '.', err);
      } });
      return h('div', { class: 'mw-chopt', dataset: { channel: name } },
        h('div', { class: 'mw-row-top' }, h('strong', null, name), privacyPill(row.privacy), enabled ? null : tag('Turned off', 'tag-warn')),
        onSw,
        h('label', { class: 'mw-switch' }, ops, h('span', { class: 'mw-switch-text' }, h('span', { class: 'mw-switch-label' }, 'Job numbers and steps'), h('span', { class: 'field-hint' }, 'Like “Job 003 has waited since 11:10”, never a job’s name or a reason.'))),
        h('label', { class: 'mw-switch' }, names2, h('span', { class: 'mw-switch-text' }, h('span', { class: 'mw-switch-label' }, 'Project names you marked safe'), h('span', { class: 'field-hint' }, safeHint))),
        isTg ? tgWarning(ops, names2) : null,
        isTg ? null : SF.field('A persona’s own words', text, row.privacy === 'tailnet' ? 'Only on a private channel, and only from a turn that read nothing private or outside.' : 'Only a private (tailnet) channel can carry them.'),
        err, h('div', { class: 'actions' }, save));
    }), safeProjects(repos, safe)), { id: 'mw-chopts' });
  }
  /** notify.safe_repos: the projects whose names may show on a phone that allows project names. */
  function safeProjects(repos, safe) {
    const err = errLine('safe-repos');
    const boxes = repos.map((r) => checkRow(r, safe.indexOf(r) >= 0, { dataset: { repo: r, focus: 'safe:' + r } }));
    const save = SF.btn('Save the safe projects', { cls: 'btn-sm', icon: 'check', dataset: { action: 'save-safe', focus: 'save-safe' }, onClick: (ev) => {
      const chosen = boxes.map((l) => l.querySelector('input')).filter((x) => x.checked).map((x) => x.dataset.repo);
      saveConfig(ev.currentTarget, { notify: { safe_repos: chosen } }, chosen.length ? 'Saved: ' + SF.plural(chosen.length, 'project') + ' marked safe.' : 'Saved: no project is marked safe.', err);
    } });
    return h('fieldset', { class: 'mw-fieldset', dataset: { safe: 'repos' } }, h('legend', null, 'Projects whose names may show on a phone'),
      h('p', { class: 'field-hint' }, 'Only on a channel with “Project names you marked safe” on. Pick names that say nothing about you; every other project stays “a project”.'),
      repos.length ? h('div', { class: 'mw-checks-grid' }, boxes) : h('p', { class: 'field-hint' }, 'No project yet: ', h('a', { href: '#/settings/repos' }, 'add one in Settings › Repositories'), '.'),
      err, repos.length ? h('div', { class: 'actions' }, save) : null);
  }

  /** On Telegram, a warning that follows the two switches as they change. */
  function tgWarning(ops, names) {
    const box = callout('warn', 'alert', 'Telegram can read these.', '');
    const text = box.querySelector('div');
    const paint = () => {
      box.hidden = !(ops.checked || names.checked);
      if (text) { SF.clear(text); SF.add(text, [h('strong', null, 'Telegram can read these.'), ' With this on, Telegram sees job numbers and steps' + (names.checked ? ' and the safe project names' : '') + '.']); }
    };
    ops.addEventListener('change', paint);
    names.addEventListener('change', paint);
    paint();
    box.dataset.warn = 'telegram';
    return box;
  }
  function rulebookSection() {
    ensure('rules', '/api/notify/rules', 10000);
    const r = val('rules');
    if (!r) return section('The notice rulebook', null, errOf('rules') ? errorBox(errOf('rules'), 'the rulebook') : SF.skeleton(4), { id: 'mw-rules' });
    const err = errLine('043');
    const quiet = r.quiet_hours;
    const mode = SF.selectEl([['auto', 'Follow my power schedule'], ['window', 'These hours'], ['off', 'No do-not-disturb time']], quiet === null ? 'off' : quiet === 'auto' ? 'auto' : 'window',
      { class: 'select select-sm', 'aria-label': 'Do-not-disturb time', dataset: { focus: 'r:quiet-mode' } });
    const windowIn = input({ value: quiet && quiet !== 'auto' ? quiet : '21:30-08:00', placeholder: '21:30-08:00', 'aria-label': 'Do-not-disturb hours', dataset: { focus: 'r:quiet-window' } });
    const through = Object.keys(RULE_WORDS).map((id) => checkRow(RULE_WORDS[id], arr(r.quiet_breakthrough).indexOf(id) >= 0, { dataset: { rule: id, focus: 'r:bt:' + id } }));
    const limits = isObj(r.rate_limits) ? r.rate_limits : {};
    const rate = RATE_FIELDS.map(([k, label, lo, hi]) => SF.field(label, numberInput(limits[k], lo, hi, { dataset: { rate: k, focus: 'r:rate:' + k } }), 'From ' + lo + ' to ' + hi));
    const bound = isObj(r.boundary) ? r.boundary : {};
    const bnum = BOUNDARY_FIELDS.map(([k, label, floor]) => SF.field(label, numberInput(bound[k], 1, floor, { dataset: { bound: k, focus: 'r:b:' + k } }), 'At most ' + floor + ': stricter only'));
    const mtext = SF.selectEl([['clean_only', 'From a clean turn only'], ['never', 'Never']], bound.tailnet_model_text || 'clean_only', { class: 'select select-sm', dataset: { focus: 'r:mtext' } });
    const confirmed = isObj(r.confirmed_profiles) ? r.confirmed_profiles : {};
    const save = async (e) => {
      const change = {};
      const q = mode.value === 'auto' ? 'auto' : mode.value === 'off' ? null : windowIn.value.trim();
      if (q !== r.quiet_hours) change.quiet_hours = q;
      const bt = through.map((l) => l.querySelector('input')).filter((x) => x.checked).map((x) => x.dataset.rule);
      if (SF.isObj(r) && JSON.stringify(bt) !== JSON.stringify(arr(r.quiet_breakthrough).filter((x) => RULE_WORDS[x]))) change.quiet_breakthrough = bt;
      const rl = {};
      for (const el of e.currentTarget.closest('section').querySelectorAll('input[data-rate]')) { const v = parseInt(el.value, 10); if (String(v) !== String(limits[el.dataset.rate])) rl[el.dataset.rate] = isFinite(v) ? v : el.value; }
      if (Object.keys(rl).length) change.rate_limits = rl;
      const bd = {};
      for (const el of e.currentTarget.closest('section').querySelectorAll('input[data-bound]')) { const v = parseInt(el.value, 10); if (String(v) !== String(bound[el.dataset.bound])) bd[el.dataset.bound] = isFinite(v) ? v : el.value; }
      if (mtext.value !== (bound.tailnet_model_text || 'clean_only')) bd.tailnet_model_text = mtext.value;
      if (Object.keys(bd).length) change.boundary = bd;
      if (!Object.keys(change).length) { SF.toast('Nothing changed.', { kind: 'info' }); return; }
      formError(err, null);
      const res = await act(e.currentTarget, '/api/notify/rules', change, 'Saved the rulebook.', { onError: (x) => formError(err, x) });
      if (res) { D.rules = { v: res, err: null, at: Date.now(), p: null }; fetchKey('notify', '/api/notify'); refresh(); }
    };
    return section('The notice rulebook', r.source === 'file' ? 'notice-rules.json: one rulebook for every path to your phone, the harness’s and any notifier of your own. Saving here writes it.'
      : 'These come from config.json until your first change here; saving writes notice-rules.json, and from then on they are changed here only.', [
      r.error ? callout('err', 'alert', 'The rulebook does not validate:', String(r.error) + ' Until it does, a persona’s words stay off every phone.') : null,
      h('div', { class: 'mw-form' },
        h('div', { class: 'mw-form-row' }, SF.field('Do-not-disturb time', mode), SF.field('Hours (HH:MM-HH:MM)', windowIn, 'Used when you pick “These hours”')),
        h('fieldset', { class: 'mw-fieldset' }, h('legend', null, 'Urgent alerts that may still buzz in do-not-disturb time'), h('div', { class: 'mw-checks-grid' }, through)),
        h('fieldset', { class: 'mw-fieldset' }, h('legend', null, 'How many'), h('div', { class: 'mw-form-grid' }, rate)),
        h('fieldset', { class: 'mw-fieldset' }, h('legend', null, 'The boundary (stricter only)'), h('div', { class: 'mw-form-grid' }, bnum, SF.field('A persona’s own words on a private channel', mtext)),
          h('p', { class: 'field-hint' }, 'A third-party channel always gets fixed heads-ups, and a persona’s words stay a heads-up until you confirm her instructions hold nothing personal.')),
        h('fieldset', { class: 'mw-fieldset' }, h('legend', null, 'Personas confirmed clean'), Object.keys(confirmed).length
          ? h('ul', { class: 'mw-plain' }, Object.keys(confirmed).map((n) => h('li', null, h('a', { href: '#/schedule/persona/' + enc(n) }, personaDisplay(n)), ' · ', String(confirmed[n].what || 'soul'), ' ', h('code', null, shortSha(confirmed[n].sha256)), confirmed[n].at ? [' · ', when(confirmed[n].at)] : null)))
          : muted('None yet. You confirm a persona on her page.'), h('p', { class: 'field-hint' }, 'Read only here: they come from each persona’s page.')),
        err, h('div', { class: 'actions' }, SF.btn('Save the rulebook', { cls: 'btn-sm btn-primary', icon: 'check', dataset: { action: 'save-rules', focus: 'save-rules' }, onClick: save }))),
    ], { id: 'mw-rules' });
  }

  function termsSection() {
    const st = val('notify');
    const terms = st && isObj(st.sensitive_terms) ? st.sensitive_terms : {};
    const ed = M.edits.get('terms');
    const err = errLine('044');
    if (ed) {
      const ta = h('textarea', { class: 'textarea mono-input', rows: '8', 'aria-label': 'Sensitive words, one per line', spellcheck: 'false', autocomplete: 'off', dataset: { focus: 'terms-text', field: 'terms' } });
      ta.value = ed.loading ? '' : ed.text;
      const close = () => { M.edits.delete('terms'); refresh(); };
      const save = async (e) => {
        const r = await act(e.currentTarget, '/api/notify/terms', { text: ta.value }, null, { onError: (x) => formError(err, x) });
        if (r) { SF.toast('Saved ' + SF.plural(r.count || 0, 'word') + '.', { kind: 'ok' }); close(); fetchKey('notify', '/api/notify'); }
      };
      return section('Sensitive words', 'Words that must never reach a phone (a name, an address, a condition): a notice holding one becomes a heads-up. They appear only here.', [
        ed.loading ? SF.skeleton(3) : SF.field('One word or phrase per line (# starts a comment)', ta), err,
        h('div', { class: 'actions' }, SF.btn('Save', { cls: 'btn-sm btn-primary', icon: 'check', dataset: { action: 'save-terms', focus: 'save-terms' }, onClick: save }),
          SF.btn('Cancel', { cls: 'btn-sm btn-ghost', dataset: { action: 'cancel-terms', focus: 'cancel-terms' }, onClick: close }))], { id: 'mw-terms' });
    }
    const open = async (e) => {
      M.edits.set('terms', { loading: true, text: '' });
      refresh();
      try { const r = await SF.api('/api/notify/terms'); M.edits.set('terms', { loading: false, text: arr(r && r.terms).join('\n') }); }
      catch (x) { M.edits.delete('terms'); SF.toastError('Could not read the words', x); }
      refresh();
    };
    const none = async (e) => {
      const r = await act(e.currentTarget, '/api/notify/terms', { none: true }, 'Noted: you have no sensitive words.', { onError: (x) => formError(err, x) });
      if (r) fetchKey('notify', '/api/notify');
    };
    return section('Sensitive words', 'Words that must never reach a phone: a notice holding one becomes a heads-up.', [
      h('p', null, terms.none_declared ? 'You said you have none.' : terms.exists ? SF.plural(terms.count || 0, 'word') + ' kept in ' : 'No list yet. It will be kept in ', h('code', null, String(terms.path || 'sensitive-terms.txt')), '.'),
      terms.mode_ok === false ? callout('err', 'alert', 'Others can read the file.', 'Run: chmod 600 ' + String(terms.path || '')) : null,
      err, h('div', { class: 'actions' }, SF.btn(terms.exists ? 'Edit the words' : 'Add words', { cls: 'btn-sm', icon: 'edit', dataset: { action: 'edit-terms', focus: 'edit-terms' }, onClick: open }),
        terms.none_declared ? null : SF.btn('I have none', { cls: 'btn-sm btn-ghost', dataset: { action: 'terms-none', focus: 'terms-none' }, onClick: none }))], { id: 'mw-terms' });
  }

  function hooksSection() {
    const st = val('notify');
    const hooks = arr(st && st.hooks);
    const hookRow = (k) => h('li', { class: 'mw-row', dataset: { hook: k.name } },
      h('div', { class: 'mw-row-main' },
        h('div', { class: 'mw-row-top' }, icon('zap'), h('strong', null, String(k.name)), k.token_set ? tag('Token set', 'tag-ok') : tag('No token', 'tag-warn'), h('span', { class: 'spacer' }), k.last_used ? ['last reported ', when(k.last_used)] : muted('never reported')),
        h('p', { class: 'mw-row-sub' }, (arr(k.may).indexOf('event') >= 0 ? 'Reports: ' + (arr(k.events).map(kindWords).join(', ') || 'nothing') : 'Reports no events') +
          (arr(k.subjects).length ? ' · subjects: ' + arr(k.subjects).join(', ') : '') +
          (arr(k.may).indexOf('wake') >= 0 ? ' · may wake ' + (arr(k.personas).map(personaDisplay).join(', ') || 'nobody yet') : '') + ' · at most ' + k.per_minute + ' a minute, ' + k.per_day + ' a day'),
        k.wrong_tokens_today ? h('p', { class: 'mw-row-sub mw-error-text' }, SF.plural(k.wrong_tokens_today, 'wrong token') + ' today (all event sources)') : null,
        h('p', { class: 'mw-row-sub' }, k.created ? ['Added ', when(k.created)] : null, k.rotated ? [' · new token ', when(k.rotated)] : null)),
      h('div', { class: 'mw-row-actions' },
        SF.btn('New token', { cls: 'btn-sm', icon: 'refresh', dataset: { action: 'hook-rotate', focus: 'hook-rotate:' + k.name }, onClick: (e) => hookAction(e.currentTarget, k.name, 'rotate') }),
        SF.btn('Delete', { cls: 'btn-sm btn-ghost', icon: 'trash', dataset: { action: 'hook-delete', focus: 'hook-delete:' + k.name }, onClick: (e) => hookAction(e.currentTarget, k.name, 'delete') })));
    return section('Event sources', 'Your backup script or a service watcher tells Seraphina when something fails or works again; each source has its own token and may report only the events you allow.', [
      hooks.length ? h('ul', { class: 'mw-list mw-list-tight' }, hooks.map(hookRow)) : muted('None yet.'),
      h('div', { class: 'actions' }, SF.btn('Add an event source', { cls: 'btn-sm', icon: 'plus', dataset: { action: 'hook-add', focus: 'hook-add' }, onClick: addHook }),
        SF.btn('Report an event yourself', { cls: 'btn-sm btn-ghost', icon: 'send', dataset: { action: 'report-event', focus: 'report-event' }, onClick: reportEvent }))], { id: 'mw-hooks' });
  }
  function hookResult(title, r) {
    return SF.openModal({ title, wide: true, description: 'Copy the token into the header file now (block “header”): it is shown once, and the harness keeps only its fingerprint.',
      body: [r.token ? secretOnce(r.token, 'The event source’s token') : null, arr(r.snippets).map(copyBlock)], actions: [{ label: 'Done', value: 'ok', primary: true }] });
  }
  async function addHook() {
    const name = input({ placeholder: 'backup-script', dataset: { field: 'hook-name' } });
    const events = HOOK_KINDS.map((k) => checkRow(RULE_WORDS[k] + ' (' + k + ')', k === 'backup.failed' || k === 'backup.ok', { dataset: { kind: k } }));
    const subjects = input({ placeholder: 'nightly, weekly', dataset: { field: 'hook-subjects' } });
    const perMin = numberInput(10, 1, 60);
    const perDay = numberInput(200, 1, 10000);
    // wakes.hooks.<n>.may ["event", "wake"] and personas: a source may also wake a persona (POST /api/hooks/wake)
    const personas = arr((val('wakes') || {}).personas).map((p) => checkRow(String(p.display || p.name), false, { dataset: { persona: p.name } }));
    const r = await SF.openModal({ title: 'Add an event source', description: 'A name for the script or service that reports, and what it may report.',
      body: [SF.field('Name', name, 'a-z, 0-9 and -, starting with a letter'), h('fieldset', { class: 'mw-fieldset' }, h('legend', null, 'It may report'), h('div', { class: 'mw-checks-grid' }, events)),
        SF.field('Subjects it may name (optional)', subjects, 'Comma separated, like nightly. Other subjects are kept off the phone.'),
        personas.length ? h('fieldset', { class: 'mw-fieldset' }, h('legend', null, 'It may also wake (optional)'), h('p', { class: 'field-hint' }, 'It can then put a check-in in her queue; what it says counts as outside text, and the gate still decides when she runs.'),
          h('div', { class: 'mw-checks-grid' }, personas)) : null,
        h('div', { class: 'mw-form-row' }, SF.field('At most a minute', perMin), SF.field('At most a day', perDay))],
      actions: [{ label: 'Cancel', value: 'cancel' }, { label: 'Add', value: 'ok', primary: true }],
      onSubmit: async () => {
        const body = { name: name.value.trim(), events: events.map((l) => l.querySelector('input')).filter((x) => x.checked).map((x) => x.dataset.kind),
          per_minute: parseInt(perMin.value, 10) || undefined, per_day: parseInt(perDay.value, 10) || undefined };
        const subs = subjects.value.split(',').map((s) => s.trim()).filter(Boolean);
        if (subs.length) body.subjects = subs;
        const wake = personas.map((l) => l.querySelector('input')).filter((x) => x.checked).map((x) => x.dataset.persona);
        if (wake.length) { body.may = ['event', 'wake']; body.personas = wake; }
        return SF.api('/api/notify/hooks', { method: 'POST', body });
      } });
    if (r && isObj(r)) { fetchKey('notify', '/api/notify'); await hookResult('Event source added', r); }
  }
  async function hookAction(button, name, action) {
    const ok = await SF.confirmModal(action === 'delete'
      ? { title: 'Delete the event source ' + name + '?', description: 'Its token stops working at once; what it reported stays in the inbox.', confirm: 'Delete', cls: 'btn-danger', icon: 'trash' }
      : { title: 'A new token for ' + name + '?', description: 'The old token stops working at once: put the new one into the header file.', confirm: 'Make a new token', icon: 'refresh' });
    if (!ok) return;
    const r = await act(button, '/api/notify/hooks/' + enc(name) + '/' + action, {}, action === 'delete' ? 'Deleted ' + name + '.' : null, { what: 'Not done' });
    if (!r) return;
    fetchKey('notify', '/api/notify');
    if (action === 'rotate') await hookResult('A new token for ' + name, r);
  }
  async function reportEvent() {
    const kind = SF.selectEl(HOOK_KINDS.map((k) => [k, RULE_WORDS[k]]), 'backup.failed', { class: 'select', dataset: { field: 'event-kind' } });
    const subject = input({ placeholder: 'nightly', dataset: { field: 'event-subject' } });
    const detail = h('textarea', { class: 'textarea', rows: '3', maxlength: '200', dataset: { field: 'event-detail' } });
    const r = await SF.openModal({ title: 'Report an event', description: 'As your backup script would: a failure opens an incident (one alert, a few reminders), a recovery closes it.',
      body: [SF.field('What happened', kind), SF.field('Subject (optional)', subject, 'Like nightly or nextcloud: a-z, 0-9, ., _ and -'), SF.field('Detail (optional, shown on these pages only, never on a phone)', detail, 'At most 200 characters')],
      actions: [{ label: 'Cancel', value: 'cancel' }, { label: 'Report', value: 'ok', primary: true }],
      onSubmit: async () => {
        const body = { kind: kind.value };
        if (subject.value.trim()) body.subject = subject.value.trim();
        if (detail.value.trim()) body.detail = detail.value.trim();
        return SF.api('/api/notify/events', { method: 'POST', body });
      } });
    if (!r || !isObj(r)) return;
    if (r.message) SF.toast('Reported.', { kind: 'ok', action: { label: 'Open it', href: '#/messages/m/' + enc(r.message) } });
    else SF.toast(r.deduped ? 'Already open: it counts as a repeat of the same incident.' : 'Recorded: nothing was open to recover.', { kind: 'info' });
    reloadInbox();
    loadUnread();
  }
  function renderSettings() {
    ensure('notify', '/api/notify', 4000);
    ensure('config', '/api/config', 15000);
    ensure('channels', '/api/notify/channels', 10000);
    ensure('wakes', '/api/wakes', 15000);
    return [statusSection(), enabledSection(), channelOptions(), rulebookSection(), termsSection(), hooksSection()];
  }

  // ---------------------------------------------------------------- the Schedule (GET /api/wakes)
  /** The nightly power-off ("23:00-07:00", clock.PowerSchedule.text) in words. */
  function powerWords(text, short) {
    const m = /^(\d\d:\d\d)-(\d\d:\d\d)$/.exec(String(text || ''));
    if (!m) return (short ? 'power-off ' : 'Off ') + String(text);
    return short ? 'the server is off ' + m[1] + '–' + m[2] : 'The server powers off at ' + m[1] + ' and on again at ' + m[2];
  }
  /** Where an action came from, in words: the server records "cockpit" for these pages (server.py wake_control). */
  function viaWords(via, fallback) {
    const v = String(via || '');
    if (!v) return fallback ? 'from ' + fallback : '';
    return v === 'cockpit' ? 'from these pages' : v === 'cli' ? 'from the factory command' : 'from ' + v;
  }
  function stopLine(stop) {
    if (!isObj(stop)) return null;
    if (stop.by === 'integrity') return h('p', { class: 'mw-off-line', dataset: { stop: 'integrity' } }, icon('shield'), h('span', null, 'Stopped: her instructions changed outside her page. Review the change on her page; she restarts once you approve or put the old text back.'));
    const what = stop.kind === 'mute' ? 'Muted' : 'Stopped';
    const until = stop.until_unread !== undefined ? 'until you restart her (the end time could not be read)' : stop.until ? 'until ' + localTime(stop.until) : 'until you restart her';
    return h('p', { class: 'mw-off-line', dataset: { stop: stop.kind || 'stop' } }, icon(stop.kind === 'mute' ? 'bell' : 'stop'),
      h('span', null, what + ' ' + until + (stop.via ? ' (' + viaWords(stop.via) + ')' : '') + (stop.at ? ', since ' + localTime(stop.at) : '') + '.'));
  }
  function budgetLine(b) {
    if (!isObj(b)) return null;
    return h('p', { class: 'mw-row-sub' }, icon('gauge'), ' ', (b.model_wakes || 0) + ' of ' + (b.max_model_wakes || '?') + ' model wakes today · ' +
      fmtSecs(b.slot_seconds) + ' of ' + fmtSecs(b.max_slot_seconds) + ' model time · ' + SF.plural(b.notices || 0, 'notice'));
  }
  function nextLine(n) {
    return h('li', { class: 'mw-next', dataset: { wake: n.wake } }, h('span', { class: 'mw-src' }, SOURCE_WORDS[n.source] || String(n.source)), ' ', n.at ? when(n.at) : null,
      n.moved ? h('span', { class: 'mw-moved' }, ' · ' + String(n.moved)) : null, taintChips(n.taint),
      n.gate ? h('div', { class: 'mw-gate' }, icon('hourglass', 'icon-xs'), h('span', null, String(n.gate))) : null);
  }
  function lastLine(l) {
    return h('li', { class: 'mw-last', dataset: { wake: l.wake, state: l.state } }, pill(ENDED_WORDS[l.state] || l.state, ENDED_TONE[l.state]), ' ',
      h('span', { class: 'mw-src' }, SOURCE_WORDS[l.source] || String(l.source || '')), ' ', when(l.at),
      l.detail ? h('div', { class: 'mw-gate' }, String(l.detail)) : null);
  }
  async function control(button, body, okText) {
    const r = await act(button, '/api/wakes/control', body, okText, { what: 'Not done' });
    if (r) { reloadState(); if (body.persona) fetchKey('page:' + body.persona, '/api/personas/' + enc(body.persona)); }
    return r;
  }
  async function runNow(button, name) {
    const r = await act(button, '/api/wakes', { persona: name }, null, { what: 'Could not wake her' });
    if (r && r.wake) SF.toast(personaDisplay(name) + ' will wake as soon as the gate lets her (' + r.wake.id + ').', { kind: 'ok' });
    if (r) fetchKey('wakes', '/api/wakes');
  }
  /** Run now is offered while she is on, not stopped and her instructions need no review (the gate would stop her at
   * check 4 the moment the wake reached it: WakeManager._integrity_stop). */
  const mayRun = (p) => !!p.enabled && !isObj(p.stopped) && p.integrity !== 'changed';
  const runWhy = (p) => (!p.enabled ? 'She is off in persona setup' : isObj(p.stopped) ? (p.stopped.by === 'integrity' ? 'Review the change to her instructions first' : 'Restart her first')
    : p.integrity === 'changed' ? 'Review the change to her instructions first: a wake would stop her' : null);
  /** Her stop for a change outside her page: set at her next wake (gate check 4), so it may not be set yet. */
  const integrityWords = (p) => (isObj(p.stopped) && p.stopped.by === 'integrity' ? 'She is stopped until you review the change' : 'Her next wake stops her until you review the change');
  function personaCard(p) {
    return h('article', { class: 'sf-card mw-persona' + (p.integrity === 'changed' ? ' is-review' : ''), dataset: { persona: p.name }, 'aria-labelledby': 'mw-p-' + p.name },
      h('div', { class: 'mw-row-top' }, icon('persona'), h('h3', { class: 'mw-h3', id: 'mw-p-' + p.name }, h('a', { href: '#/schedule/persona/' + enc(p.name), dataset: { focus: 'persona:' + p.name } }, String(p.display || p.name))),
        p.enabled ? tag('On', 'tag-ok') : tag('Off', 'tag-outline'), p.role === 'operator' ? tag('Operator', 'tag-accent') : null, h('span', { class: 'spacer' }),
        pill(p.privacy === 'clean' ? 'Clean' : 'Private', p.privacy === 'clean' ? 'ok' : 'neutral', p.privacy === 'clean' ? 'You confirmed her instructions hold nothing personal' : 'Her words stay off phones until you confirm her instructions')),
      p.description ? h('p', { class: 'mw-row-text' }, String(p.description)) : null,
      p.copy_label ? h('p', { class: 'mw-row-sub' }, icon('layers'), ' ', String(p.copy_label)) : null,
      p.integrity === 'changed' ? h('p', { class: 'mw-review-line' }, icon('alert'), h('span', null, 'Her instructions changed outside her page. '), h('a', { href: '#/schedule/persona/' + enc(p.name) + '/instructions' }, 'Review the change')) : null,
      p.error ? h('p', { class: 'mw-off-line' }, icon('alert'), h('span', null, String(p.error))) : null,
      stopLine(p.stopped),
      h('p', { class: 'mw-row-sub' }, 'Model: ', p.model ? h('code', null, String(p.model)) : 'none yet (persona setup)'),
      budgetLine(p.budget),
      arr(p.next).length ? [h('h4', { class: 'mw-h4' }, 'Next'), h('ul', { class: 'mw-plain' }, arr(p.next).map(nextLine))] : h('p', { class: 'mw-row-sub' }, 'Nothing is waiting.'),
      arr(p.last).length ? [h('h4', { class: 'mw-h4' }, 'Recent'), h('ul', { class: 'mw-plain' }, arr(p.last).slice().reverse().map(lastLine))] : null,
      h('div', { class: 'actions mw-actions' },
        SF.btn('Run now', { cls: 'btn-sm', icon: 'play', disabled: !mayRun(p), title: runWhy(p), dataset: { action: 'run-now', focus: 'run:' + p.name }, onClick: (e) => runNow(e.currentTarget, p.name) }),
        linkBtn('Open her page', '#/schedule/persona/' + enc(p.name), { icon: 'arrow-right' })));
  }
  function queueRow(e) {
    const who = e.chat ? 'Chat ' + e.chat : personaDisplay(e.persona);
    return h('li', { class: 'mw-row', dataset: { wake: e.id, state: e.state } },
      h('div', { class: 'mw-row-main' },
        h('div', { class: 'mw-row-top' }, h('strong', null, who), h('span', { class: 'mw-src' }, SOURCE_WORDS[e.source] || String(e.source)),
          e.state === 'running' ? pill('Running', 'accent') : pill('Waiting', 'neutral'), taintChips(e.taint), h('span', { class: 'spacer' }), muted(e.id)),
        h('p', { class: 'mw-row-sub' }, Date.parse(e.not_before || '') > Date.now() ? 'Not before ' : 'Due since ', when(e.not_before), e.moved ? ' · ' + String(e.moved) : '',
          e.yields ? ' · stepped aside ' + SF.plural(e.yields, 'time') + ' for someone using the model' : ''),
        e.gate_words ? h('div', { class: 'mw-gate' }, icon('hourglass', 'icon-xs'), h('span', null, String(e.gate_words))) : null,
        e.reason_len ? h('p', { class: 'mw-row-sub' }, 'A reason was given (' + e.reason_len + ' characters, details in the run log).') : null,
        e.chat ? h('p', { class: 'mw-row-sub' }, h('a', { href: '#/chat/' + enc(e.chat) }, 'Open the chat'), e.item ? [' · ', h('a', { href: '#/housekeeping/' + enc(e.item) }, 'its promise ' + e.item)] : null)
          : e.item ? h('p', { class: 'mw-row-sub' }, h('a', { href: '#/housekeeping/' + enc(e.item) }, 'Its tracker item ' + e.item)) : null),
      h('div', { class: 'mw-row-actions' }, SF.btn('Cancel', { cls: 'btn-sm btn-ghost', icon: 'x', dataset: { action: 'cancel-wake', focus: 'cancel:' + e.id }, onClick: async (ev) => {
        const ok = await SF.confirmModal({ title: 'Cancel this wake?', description: e.state === 'running' ? 'The turn is cut short now.' : 'It leaves the queue; its tracker item, if any, is dropped.', confirm: 'Cancel the wake', cls: 'btn-danger', icon: 'x' });
        if (!ok) return;
        const r = await act(ev.currentTarget, '/api/wakes/' + enc(e.id) + '/cancel', {}, 'Cancelled.', { what: 'Could not cancel' });
        if (r) fetchKey('wakes', '/api/wakes');
      } })));
  }
  function remindersSection(wk) {
    const chans = arr(val('channels'));
    const privateChans = chans.filter((c) => c.privacy === 'tailnet');
    const rows = arr(wk.reminders).slice().sort((a, b) => String(b.at).localeCompare(String(a.at)));
    const err = errLine('051');
    const at = h('input', { class: 'input', type: 'datetime-local', 'aria-label': 'When', dataset: { focus: 'rem:at', field: 'rem-at' } });
    const text = h('textarea', { class: 'textarea', rows: '2', maxlength: '300', 'aria-label': 'What to remind you of', dataset: { focus: 'rem:text', field: 'rem-text' } });
    const show = privateChans.map((c) => checkRow('Show the text on ' + c.name + ' (private)', false, { dataset: { channel: c.name, focus: 'rem:show:' + c.name } }));
    const add = async (e) => {
      formError(err, null);
      const t = Date.parse(at.value);
      if (!isFinite(t)) return formError(err, 'Pick a day and a time.');
      const body = { at: new Date(t).toISOString().replace(/\.\d{3}Z$/, 'Z'), text: text.value.trim(), show_on: show.map((l) => l.querySelector('input')).filter((x) => x.checked).map((x) => x.dataset.channel) };
      const r = await act(e.currentTarget, '/api/wakes/reminders', body, null, { onError: (x) => formError(err, x) });
      if (!r) return;
      M.reminderMoved = r.reminder && r.reminder.moved ? String(r.reminder.moved) : null;
      SF.toast('Reminder added for ' + localTime(r.reminder && r.reminder.at) + '.', { kind: 'ok' });
      at.value = ''; text.value = ''; delete at.dataset.dirty; delete text.dataset.dirty;
      fetchKey('wakes', '/api/wakes');
    };
    return section('Reminders', 'The harness delivers them itself, at their time, without asking an AI model. A third-party phone only ever gets “A reminder you set is due.”', [
      M.reminderMoved ? callout('info', 'clock', 'It will arrive late:', M.reminderMoved) : null,
      rows.length ? h('ul', { class: 'mw-list mw-list-tight' }, rows.map((r) => h('li', { class: 'mw-row', dataset: { reminder: r.id, state: r.state } },
        h('div', { class: 'mw-row-main' }, h('div', { class: 'mw-row-top' }, icon('clock'), when(r.at), pill(r.state === 'pending' ? 'Waiting' : r.state === 'delivered' ? 'Delivered' : 'Cancelled', r.state === 'pending' ? 'accent' : r.state === 'delivered' ? 'ok' : 'muted')),
          textBlock(r.text, 'mw-row-text'),
          h('p', { class: 'mw-row-sub' }, arr(r.show_on).length ? 'Text shown on ' + arr(r.show_on).join(', ') : 'Text shown on these pages only', r.moved ? ' · ' + String(r.moved) : '')),
        r.state === 'pending' ? h('div', { class: 'mw-row-actions' }, SF.btn('Cancel', { cls: 'btn-sm btn-ghost', icon: 'x', dataset: { action: 'cancel-reminder', focus: 'rcancel:' + r.id }, onClick: async (e) => {
          const res = await act(e.currentTarget, '/api/wakes/reminders/' + enc(r.id) + '/cancel', {}, 'Reminder cancelled.', { what: 'Could not cancel it' });
          if (res) fetchKey('wakes', '/api/wakes');
        } })) : null))) : muted('No reminders.'),
      h('details', { class: 'mw-add', open: M.addReminderOpen ? true : null, dataset: { focus: 'rem:add' }, ontoggle: (e) => { M.addReminderOpen = e.currentTarget.open; } },
        h('summary', null, icon('plus'), 'Add a reminder'),
        h('div', { class: 'mw-form' }, SF.field('When', at, 'In your browser’s time zone'), SF.field('What (at most 300 characters)', text),
          show.length ? h('fieldset', { class: 'mw-fieldset' }, h('legend', null, 'Show the text on a phone'), show) : h('p', { class: 'field-hint' }, 'No private phone: the text stays on these pages and phones get “A reminder you set is due.”'),
          err, h('div', { class: 'actions' }, SF.btn('Add the reminder', { cls: 'btn-sm btn-primary', icon: 'plus', dataset: { action: 'add-reminder', focus: 'rem:submit' }, onClick: add })))),
    ], { id: 'mw-reminders' });
  }
  function serversSection(wk) {
    const servers = isObj(wk.servers) ? wk.servers : {};
    const names = Object.keys(servers);
    return section('Model servers', 'Check-ins run only on a model server that runs on this machine: tick it for each one you trust.', names.length ? h('ul', { class: 'mw-list mw-list-tight' }, names.map((name) => {
      const s = servers[name];
      const box = h('input', { type: 'checkbox', checked: !!s.local_attested, dataset: { focus: 'attest:' + name } });
      box.addEventListener('change', async () => {
        const r = await act(box, '/api/wakes/servers/' + enc(name) + '/attest', { local: box.checked }, box.checked ? 'Noted: ' + name + ' runs on this machine.' : 'Noted: ' + name + ' is not trusted for check-ins.', { what: 'Not saved' });
        if (r) fetchKey('wakes', '/api/wakes'); else revertSwitch(box, !box.checked);
      });
      return h('li', { class: 'mw-row', dataset: { server: name } }, h('div', { class: 'mw-row-main' },
        h('div', { class: 'mw-row-top' }, icon('server'), h('strong', null, name), tag(String(s.kind || 'other'), 'tag-outline'), h('code', { class: 'mw-url' }, String(s.root_url || ''))),
        h('label', { class: 'mw-check' }, box, h('span', null, 'This model server runs on this machine')),
        s.detected === 'openwebui' || s.detected === 'proxy' ? callout('warn', 'alert', 'This address is Open WebUI (or a proxy), not a model server:', 'check-ins refuse it. Point the persona at the model server itself.') : null));
    })) : muted('No model server is set up for check-ins yet.'), { id: 'mw-servers' });
  }
  function renderSchedule() {
    ensure('wakes', '/api/wakes', 3000);
    ensure('channels', '/api/notify/channels', 15000);
    const wk = val('wakes');
    if (!wk) return [errOf('wakes') ? errorBox(errOf('wakes'), 'the schedule') : SF.skeleton(6)];
    const paused = isObj(wk.paused);
    const queue = arr(wk.queue);
    return [
      h('div', { class: 'mw-summary' }, h('p', null, String(wk.instance_name || 'Seraphina OS Harness'), ' · scheduler ', wk.tick_at ? ['last ran ', when(wk.tick_at)] : 'not running',
        wk.power ? ' · ' + powerWords(wk.power, true) : '', isObj(wk.quiet_hours) && wk.quiet_hours.window ? ' · do-not-disturb ' + wk.quiet_hours.window : ''),
        h('span', { class: 'spacer' }), paused ? restartButton('Restart Seraphina') : SF.btn('Stop Seraphina', { cls: 'btn-sm btn-danger', icon: 'stop', dataset: { action: 'stop-all', focus: 'stop-all' }, onClick: async (e) => {
          const ok = await SF.confirmModal({ title: 'Stop Seraphina?', description: 'Every persona stops at once, a turn in flight is cut, and chat check-ins stop. Alerts from your server keep coming. Only these pages or the factory command on the server can restart her.', confirm: 'Stop her', cls: 'btn-danger', icon: 'stop' });
          if (ok) control(e.currentTarget, { action: 'pause' }, 'Seraphina is stopped.');
        } })),
      h('h2', { class: 'home-group-title' }, 'Personas that wake themselves'),
      arr(wk.personas).length ? h('div', { class: 'sf-cards mw-personas' }, arr(wk.personas).map(personaCard)) : SF.emptyState('persona', 'No persona yet', 'Seraphina ships with the harness; turn her on in persona setup.', [linkBtn('Persona setup', '#/schedule/setup', { cls: 'btn-sm btn-primary' })], true),
      section('The queue', 'Every wake waits here until it may run: the daily budget, the server’s power-off time and a free local model all have to allow it. Each line says what it waits for.', queue.length ? h('ul', { class: 'mw-list mw-list-tight' }, queue.map(queueRow)) : muted('The queue is empty.'), { id: 'mw-queue' }),
      remindersSection(wk),
      serversSection(wk),
      section('Chat check-ins', 'A chat you allow may schedule one check-in per reply.', h('p', null, SF.plural(arr(wk.chat_wakes).length, 'chat check-in') + ' waiting or running. ', h('a', { href: '#/schedule/chats', dataset: { focus: 'to-chats' } }, 'Choose which chats may schedule them')), { id: 'mw-chatwakes' }),
      howTo([
        ['Stop works from anywhere.', 'Stop here, with the factory command on the server or from a phone: every persona stops at once. Only these pages or the factory command restart her.'],
        ['The gate decides, in plain words.', 'A wake waits while the model is busy, the budget is used or it is do-not-disturb time; each line says which.'],
        ['Run now still goes through the gate.', 'It asks for a turn now; the same checks apply.'],
        ['Wake her from a script.', '`factory wake poke seraphina` (or an event source you allowed to wake her, in Message settings) puts a poke in her queue; what it says counts as outside text.'],
      ], 'How the schedule works'),
    ];
  }

  // ---------------------------------------------------------------- chat check-ins (GET/POST /api/chats/<chat>/wakes)
  function renderChats() {
    ensure('chats', '/api/chats', 8000);
    ensure('wakes', '/api/wakes', 10000);
    ensure('config', '/api/config', 30000);
    const chats = arr((val('chats') || {}).chats);
    for (const c of chats) ensure('chatwakes:' + c.id, '/api/chats/' + enc(c.id) + '/wakes', 8000);
    const cfg = val('config');
    const wcfg = cfg && isObj(cfg.wakes) ? cfg.wakes : {};
    const off = cfg && (wcfg.enabled !== true || !(isObj(wcfg.chat_wakes) && wcfg.chat_wakes.enabled === true));
    const row = (c) => {
      const cw = val('chatwakes:' + c.id);
      const allow = cw ? !!cw.allow : false;
      const err = errLine('chat:' + c.id);
      const sw = toggle('Let this chat schedule check-ins', allow, null,
        { disabled: !cw, dataset: { focus: 'allow:' + c.id, chat: c.id } }, async (v, el) => {
          formError(err, null);
          const r = await act(el, '/api/chats/' + enc(c.id) + '/wakes', { allow: v }, v ? 'This chat may schedule check-ins.' : 'Check-ins are off for this chat.', { onError: (x) => { formError(err, x); revertSwitch(el, !v); } });
          if (r) { D['chatwakes:' + c.id] = { v: r, err: null, at: Date.now(), p: null }; fetchKey('wakes', '/api/wakes'); refresh(); }
        });
      return h('li', { class: 'mw-row mw-chatrow', dataset: { chat: c.id } }, h('div', { class: 'mw-row-main' },
        h('div', { class: 'mw-row-top' }, icon('chat'), h('a', { href: '#/chat/' + enc(c.id), class: 'mw-chat-title', dataset: { focus: 'chat:' + c.id } }, String(c.title || c.id)),
          c.repo ? tag(String(c.repo), 'tag-outline') : tag('no repository', 'tag-outline'), c.mode ? muted(String(c.mode)) : null),
        errOf('chatwakes:' + c.id) && !cw ? errorBox(errOf('chatwakes:' + c.id), 'this chat’s check-ins') : sw, err,
        cw && arr(cw.wakes).length ? h('ul', { class: 'mw-plain' }, arr(cw.wakes).map((w) => h('li', { class: 'mw-next', dataset: { wake: w.wake } },
          pill(w.state === 'running' ? 'Running' : 'Waiting', w.state === 'running' ? 'accent' : 'neutral'), ' ', when(w.at), w.moved ? ' · ' + String(w.moved) : '',
          w.gate ? h('div', { class: 'mw-gate' }, String(w.gate)) : null, w.item ? h('div', null, h('a', { href: '#/housekeeping/' + enc(w.item) }, 'Its promise ' + w.item)) : null))) : null));
    };
    return [
      off ? callout('info', 'info', 'Chat check-ins are off.', ['Turn on personas and chat check-ins in ', h('a', { href: '#/schedule/setup' }, 'Persona setup'), '; until then each switch below only records your choice.']) : null,
      h('p', { class: 'field-hint' }, 'One check-in per reply. A chat needs the sandbox, its own branch (worktree) and the built-in driver; a check-in is skipped if any of them is gone when it is due.'),
      errOf('chats') && !val('chats') ? errorBox(errOf('chats'), 'the chats') : !val('chats') ? SF.skeleton(3)
        : chats.length ? h('ul', { class: 'mw-list' }, chats.map(row)) : SF.emptyState('chat', 'No chats yet', 'Start a chat; then you can let it schedule check-ins here.', [linkBtn('Open Chat', '#/chat', { cls: 'btn-sm' })], true),
      howTo([
        ['Allow a chat.', 'Its replies get a Wake tool: the assistant may ask to look again later (“check the CI in an hour”). Each check-in also opens a promise in the tracker.'],
        ['A check-in is a short, read-only turn.', 'It reads files and runs the status commands you allowed; it cannot write.'],
        ['Stopping Seraphina also stops a running check-in.', 'Turning a chat’s switch off cancels the ones it scheduled.'],
      ], 'How chat check-ins work'),
    ];
  }

  // ---------------------------------------------------------------- persona setup (POST /api/config {wakes})
  /** wakes.chat_wakes.commands' default (wakes.CHAT_WAKE_DEFAULTS), in words for the setup's Default choice. */
  const CHAT_DEFAULT_COMMANDS = 'gh run view, gh run list, gh pr checks, git status and git log';
  /** wake.commands (and wakes.chat_wakes.commands) as the setup's choice: missing or null → default, [] → none, a list → list. */
  const cmdModeOf = (wake) => (!isObj(wake) || wake.commands === null || wake.commands === undefined ? 'default' : Array.isArray(wake.commands) && !wake.commands.length ? 'none' : 'list');
  function renderWakeSetup() {
    ensure('config', '/api/config', 10000);
    ensure('wakes', '/api/wakes', 10000);
    const cfg = val('config');
    const wk = val('wakes');
    if (!cfg || !wk) return [errOf('config') ? errorBox(errOf('config'), 'the settings') : SF.skeleton(5)];
    const w = isObj(cfg.wakes) ? cfg.wakes : {};
    const models = Object.keys(isObj(cfg.models) ? cfg.models : {});
    const err = errLine('053');
    const on = h('input', { type: 'checkbox', role: 'switch', checked: w.enabled === true, dataset: { focus: 'ws:enabled' } });
    const chat = h('input', { type: 'checkbox', role: 'switch', checked: isObj(w.chat_wakes) && w.chat_wakes.enabled === true, dataset: { focus: 'ws:chat' } });
    const zone = input({ value: w.timezone || '', placeholder: browserZone() || 'Europe/London', dataset: { focus: 'ws:tz' } });
    // DESIGN §35: wakes.chat_wakes.commands by the same rule as a persona's wake.commands: Default leaves the key out
    // (the five gh and git commands), No commands saves [], These commands the list; an emptied list is never [].
    const chatCfg = isObj(w.chat_wakes) ? w.chat_wakes : {};
    const chatCmd = {
      mode: SF.selectEl([['default', 'Default: ' + CHAT_DEFAULT_COMMANDS], ['none', 'No commands: a check-in only reads files'], ['list', 'These commands']], cmdModeOf(chatCfg),
        { class: 'select select-sm', 'aria-label': 'Status commands during a chat check-in', dataset: { focus: 'ws:chat:cmdmode' } }),
      list: h('textarea', { class: 'textarea mono-input', rows: '3', spellcheck: 'false', 'aria-label': 'Chat check-in commands', dataset: { focus: 'ws:chat:commands' } }),
    };
    chatCmd.list.value = cmdModeOf(chatCfg) === 'list' ? arr(chatCfg.commands).map((c) => arr(c).join(' ')).join('\n') : '';
    chatCmd.box = SF.field('Commands a check-in may run (one per line)', chatCmd.list, 'The start of a command, like gh run view: what follows it is the check-in’s choice. No shell syntax.');
    const showChatList = () => { chatCmd.box.hidden = chatCmd.mode.value !== 'list'; };
    chatCmd.mode.addEventListener('change', showChatList);
    showChatList();
    queueMicrotask(showChatList);
    const names = Array.from(new Set(arr(wk.personas).map((p) => p.name).concat(Object.keys(isObj(w.personas) ? w.personas : {}))));
    const forms = names.map((name) => {
      const pc = isObj(w.personas) && isObj(w.personas[name]) ? w.personas[name] : {};
      const mem = isObj(pc.memory) ? pc.memory : {};
      const wake = isObj(pc.wake) ? pc.wake : {};
      const f = {
        enabled: h('input', { type: 'checkbox', role: 'switch', checked: pc.enabled === true, dataset: { focus: 'wp:' + name + ':enabled' } }),
        model: SF.selectEl([['', 'No model yet']].concat(models.map((m) => [m, m])), pc.model || '', { class: 'select select-sm', dataset: { focus: 'wp:' + name + ':model' } }),
        every: numberInput(isObj(pc.ambient) && pc.ambient.every_minutes ? pc.ambient.every_minutes : 120, 15, 1440, { dataset: { focus: 'wp:' + name + ':every' } }),
        curation: SF.selectEl([['owner', 'You keep her memory'], ['self', 'She keeps her own (changes listed)']], mem.curation || 'owner', { class: 'select select-sm', dataset: { focus: 'wp:' + name + ':curation' } }),
        tools: SF.selectEl([['none', 'No tools'], ['read_only', 'Read-only tools (needs the sandbox)']], wake.tools || 'none', { class: 'select select-sm', dataset: { focus: 'wp:' + name + ':tools' } }),
        // wake.commands: a missing or null key (Default: git status and git log) and an explicit [] (No commands) are
        // different settings, so the choice is made here, never read from an emptied field (DESIGN §35)
        cmdMode: SF.selectEl([['default', 'Default: git status and git log'], ['none', 'No commands: a wake reads; it doesn’t poke'], ['list', 'These commands']], cmdModeOf(wake),
          { class: 'select select-sm', 'aria-label': 'Status commands during a wake', dataset: { focus: 'wp:' + name + ':cmdmode' } }),
        commands: h('textarea', { class: 'textarea mono-input', rows: '3', spellcheck: 'false', 'aria-label': 'Status commands', dataset: { focus: 'wp:' + name + ':commands' } }),
        decider: SF.selectEl([['owner', 'You decide on re-imports'], ['persona', 'She decides (you may still reject)']], pc.reimport_decider || 'owner', { class: 'select select-sm', dataset: { focus: 'wp:' + name + ':decider' } }),
        // memory.external.ledger: offered only where her memory is external (a folder or a database is set)
        ledger: isObj(mem.external) && (mem.external.folder || mem.external.sqlite) ? h('input', { type: 'checkbox', role: 'switch', checked: mem.external.ledger === true, dataset: { focus: 'wp:' + name + ':ledger' } }) : null,
      };
      const row = arr(wk.personas).find((p) => p && p.name === name);
      // the server's word (commands_off) that the ledger without bwrap turns her set commands off
      f.off = row && isObj(row.settings) && row.settings.commands_off === 'ledger_without_sandbox'
        ? callout('warn', 'shield', 'Her status commands are off now.', 'The ' + LEDGER_NO_SANDBOX.slice(4)) : null;
      if (f.off) f.off.dataset.part = 'commands-off';
      f.commands.value = cmdModeOf(wake) === 'list' ? arr(wake.commands).map((c) => arr(c).join(' ')).join('\n') : '';
      const showList = () => { f.commandsBox.hidden = f.cmdMode.value !== 'list'; };
      f.commandsBox = SF.field('Commands she may run (one per line)', f.commands, 'Local commands only, like git status: her wake runs with no network.');
      f.cmdMode.addEventListener('change', showList);
      showList();
      queueMicrotask(showList);   // after a re-render puts back a choice typed before it (restore runs after this)
      return { name, f, pc };
    });
    const save = async (e) => {
      const patch = { wakes: {} };
      if (on.checked !== (w.enabled === true)) patch.wakes.enabled = on.checked;
      const chatCh = {};
      if (chat.checked !== (chatCfg.enabled === true)) chatCh.enabled = chat.checked;
      const chatWas = cmdModeOf(chatCfg);
      const chatMode = chatCmd.mode.value;
      if (chatMode === 'default' && chatWas !== 'default') chatCh.commands = null;   // back to the default: the key goes
      else if (chatMode === 'none' && chatWas !== 'none') chatCh.commands = [];
      else if (chatMode === 'list') {
        const cmds = chatCmd.list.value.split('\n').map((l) => l.trim().split(/\s+/).filter(Boolean)).filter((c) => c.length);
        if (!cmds.length) { formError(err, 'Chat check-ins: type at least one command, or choose “No commands”.'); return; }
        if (chatWas !== 'list' || JSON.stringify(cmds) !== JSON.stringify(chatCfg.commands)) chatCh.commands = cmds;
      }
      if (Object.keys(chatCh).length) patch.wakes.chat_wakes = chatCh;
      if (zone.value.trim() !== (w.timezone || '')) patch.wakes.timezone = zone.value.trim() || null;
      const personas = {};
      for (const { name, f, pc } of forms) {
        const ch = {};
        if (f.enabled.checked !== (pc.enabled === true)) ch.enabled = f.enabled.checked;
        if ((f.model.value || null) !== (pc.model || null)) ch.model = f.model.value || null;
        const every = parseInt(f.every.value, 10);
        if (every !== ((isObj(pc.ambient) && pc.ambient.every_minutes) || 120)) ch.ambient = { every_minutes: isFinite(every) ? every : f.every.value };
        if (f.curation.value !== ((isObj(pc.memory) && pc.memory.curation) || 'owner')) ch.memory = { curation: f.curation.value };
        const wakeCh = {};
        if (f.tools.value !== ((isObj(pc.wake) && pc.wake.tools) || 'none')) wakeCh.tools = f.tools.value;
        const was = cmdModeOf(isObj(pc.wake) ? pc.wake : {});
        const mode = f.cmdMode.value;
        if (mode === 'default' && was !== 'default') wakeCh.commands = null;   // back to the default: the key goes
        else if (mode === 'none' && was !== 'none') wakeCh.commands = [];
        else if (mode === 'list') {
          const cmds = f.commands.value.split('\n').map((l) => l.trim().split(/\s+/).filter(Boolean)).filter((c) => c.length);
          if (!cmds.length) { formError(err, personaDisplay(name) + ': type at least one command, or choose “No commands”.'); return; }
          if (was !== 'list' || JSON.stringify(cmds) !== JSON.stringify(pc.wake.commands)) wakeCh.commands = cmds;
        }
        if (Object.keys(wakeCh).length) ch.wake = wakeCh;
        if (f.ledger && f.ledger.checked !== (isObj(pc.memory) && isObj(pc.memory.external) && pc.memory.external.ledger === true)) {
          ch.memory = Object.assign({}, ch.memory || {}, { external: { ledger: f.ledger.checked } });
        }
        if (f.decider.value !== (pc.reimport_decider || 'owner')) ch.reimport_decider = f.decider.value;
        if (Object.keys(ch).length) personas[name] = ch;
      }
      if (Object.keys(personas).length) patch.wakes.personas = personas;
      if (!Object.keys(patch.wakes).length) { SF.toast('Nothing changed.', { kind: 'info' }); return; }
      const r = await saveConfig(e.currentTarget, patch, 'Saved persona setup.', err);
      if (r) fetchKey('wakes', '/api/wakes');
    };
    return [
      section('Personas and check-ins', 'Off by default. Personas and chat check-ins stay off while your agents run without the sandbox, unless you accepted the risk in Phone setup.', h('div', { class: 'mw-form' },
        h('label', { class: 'mw-switch' }, on, h('span', { class: 'mw-switch-text' }, h('span', { class: 'mw-switch-label' }, 'Personas wake themselves'), h('span', { class: 'field-hint' }, 'Check-ins, your messages to them and reminders.'))),
        h('label', { class: 'mw-switch' }, chat, h('span', { class: 'mw-switch-text' }, h('span', { class: 'mw-switch-label' }, 'Chats may schedule check-ins'), h('span', { class: 'field-hint' }, 'Only the chats you allow, in Chat check-ins.'))),
        SF.field('Status commands during a chat check-in', chatCmd.mode, 'A check-in reads files and may run these to see how CI or a branch is doing. It cannot write.'),
        chatCmd.box,
        SF.field('Time zone', zone, 'Needed once a phone channel is on')), { id: 'mw-wsetup' }),
      forms.map(({ name, f }) => section(personaDisplay(name), null, h('div', { class: 'mw-form', dataset: { persona: name } },
        h('label', { class: 'mw-switch' }, f.enabled, h('span', { class: 'mw-switch-text' }, h('span', { class: 'mw-switch-label' }, 'She wakes herself'))),
        h('div', { class: 'mw-form-grid' }, SF.field('Model', f.model, 'A model on this machine'), SF.field('Check in every (minutes)', f.every, '15 to 1440'),
          SF.field('Memory', f.curation), SF.field('Tools during a wake', f.tools), SF.field('Re-imports from Open WebUI', f.decider)),
        SF.field('Status commands during a wake', f.cmdMode, 'With read-only tools only. Default runs git status and git log; No commands means she reads files and harness status and runs nothing.'),
        f.commandsBox, f.off,
        f.ledger ? h('label', { class: 'mw-switch' }, f.ledger, h('span', { class: 'mw-switch-text' }, h('span', { class: 'mw-switch-label' }, 'Keep a read ledger of her memory'),
          h('span', { class: 'field-hint' }, 'Off by default. Each read of her memory keeps a private copy of what was read, so you can look back, compare and prepare a restore on her Memory tab. The harness never writes her memory. With the ledger on, her status commands run only inside the sandbox (bwrap); without it she runs none.'))) : null), { id: 'mw-wp-' + name })),
      err, h('div', { class: 'actions mw-savebar' }, SF.btn('Save persona setup', { cls: 'btn-sm btn-primary', icon: 'check', dataset: { action: 'save-wakes', focus: 'save-wakes' }, onClick: save })),
    ];
  }

  // ---------------------------------------------------------------- a wake persona's page (GET /api/personas/<name>)
  const P_TABS = [['overview', 'Overview'], ['instructions', 'Instructions'], ['memory', 'Memory'], ['journal', 'Journal'], ['thread', 'Thread'], ['runs', 'Runs']];
  function diffBlock(text, label) {
    const lines = String(text || '').split('\n');
    return h('pre', { class: 'sf-pre mw-diff', 'aria-label': label || 'Differences' }, lines.map((l) => h('span', { class: 'mw-dl' + (/^\+(?!\+\+)/.test(l) ? ' is-add' : /^-(?!--)/.test(l) ? ' is-del' : /^@@/.test(l) ? ' is-hunk' : '') }, l + '\n')));
  }
  function pageOf2(name) { return val('page:' + name); }
  function loadPage(name, force) {
    const key = 'page:' + name;
    if (force) return fetchKey(key, '/api/personas/' + enc(name));
    return ensure(key, '/api/personas/' + enc(name), 4000);
  }
  async function personaPost(button, name, path, body, okText, opts) {
    const r = await act(button, '/api/personas/' + enc(name) + path, body, okText, opts);
    if (r) { loadPage(name, true); fetchKey('wakes', '/api/wakes'); }
    return r;
  }
  /** An edit keeps the base it started from ({text, base}): the version or fingerprint shown when the owner began to
   * type. A refresh (her own write, an outside edit, an event) never moves that base under the typed text, so a save
   * after a newer write is refused (409) instead of overwriting it. */
  function startEdit(key, text, base) {
    const cur = M.edits.get(key) || {};
    M.edits.set(key, Object.assign({}, cur, { text, base: cur.base !== undefined ? cur.base : base }));
  }
  /** A 409 (a stale base), or a newer version that arrived while editing: say so, keep what was typed, offer a reload. */
  function staleNote(key, now, what) {
    const s = M.edits.get(key);
    if (!s) return null;
    if (s.conflict) return callout('warn', 'refresh', 'It changed since you opened it.', String(s.conflict));
    if (s.base !== undefined && now !== undefined && String(s.base) !== String(now)) {
      return callout('warn', 'refresh', 'It changed since you opened it.', what + ' Saving would be refused: reload to see the new text (copy yours first to keep it).');
    }
    return null;
  }
  const isStale = (key, now) => { const s = M.edits.get(key); return !!(s && (s.conflict || (s.base !== undefined && now !== undefined && String(s.base) !== String(now)))); };
  function personaHead(name, data) {
    const p = data.persona;
    const stopped = isObj(p.stopped);
    const until = input({ placeholder: 'HH:MM', 'aria-label': 'Stop her until (optional, HH:MM)', class: 'input input-sm mw-until', dataset: { focus: 'p:until' } });
    return h('div', { class: 'mw-phead' },
      h('div', { class: 'mw-row-top' }, h('span', { class: 'mw-avatar', 'aria-hidden': 'true' }, String(p.display || name).slice(0, 1).toUpperCase()),
        h('div', null, h('div', { class: 'wz-eyebrow' }, 'Persona that wakes herself'), h('h1', { class: 'view-title', tabindex: '-1', id: 'mw-ptitle' }, String(p.display || name)),
          h('p', { class: 'view-sub' }, h('code', null, name), p.description ? ' · ' + String(p.description) : '')),
        h('span', { class: 'spacer' }),
        h('div', { class: 'mw-ptags' }, p.enabled ? tag('On', 'tag-ok') : tag('Off', 'tag-outline'), pill(p.privacy === 'clean' ? 'Clean' : 'Private', p.privacy === 'clean' ? 'ok' : 'neutral'),
          p.integrity === 'changed' ? pill('Review needed', 'human', 'Her instructions changed outside this page') : null, p.role === 'operator' ? tag('Operator', 'tag-accent') : null)),
      h('div', { class: 'actions mw-pactions' },
        SF.btn('Run now', { cls: 'btn-sm', icon: 'play', disabled: !mayRun(p), title: runWhy(p), dataset: { action: 'run-now', focus: 'p:run' }, onClick: (e) => runNow(e.currentTarget, name) }),
        // "Stop her [until HH:MM]": the time is optional, and says so where it is typed.
        !stopped ? h('span', { class: 'mw-stopbox' }, SF.btn('Stop her', { cls: 'btn-sm btn-danger', icon: 'stop', dataset: { action: 'stop', focus: 'p:stop' }, onClick: (e) => {
          const body = { action: 'pause', persona: name };
          if (until.value.trim()) body.until = until.value.trim();
          control(e.currentTarget, body, personaDisplay(name) + ' is stopped' + (body.until ? ' until ' + body.until : '') + '.');
        } }), h('span', { class: 'mw-until-word', 'aria-hidden': 'true' }, 'until'), until, h('span', { class: 'field-hint', 'aria-hidden': 'true' }, '(optional)'))
          : p.stopped.by === 'integrity' ? null
            : SF.btn('Restart her', { cls: 'btn-sm btn-primary', icon: 'play', dataset: { action: 'restart-persona', focus: 'p:restart' }, onClick: (e) => control(e.currentTarget, { action: p.stopped.kind === 'mute' ? 'unmute' : 'resume', persona: name }, personaDisplay(name) + ' is running again.') }),
        SF.btn('Allow sound', { cls: 'btn-sm btn-ghost', icon: 'bell', title: 'Her first days’ messages are silent; let them make a sound now', dataset: { action: 'sound', focus: 'p:sound' }, onClick: (e) => personaPost(e.currentTarget, name, '/sound', {}, 'Her messages may make a sound now.') })),
      stopLine(p.stopped));
  }
  function overviewTab(name, data) {
    const p = data.persona;
    const s = isObj(p.settings) ? p.settings : {};
    const ri = data.reimport;
    return [
      p.integrity === 'changed' ? callout('human', 'alert', 'Her instructions changed outside this page.', integrityWords(p) + ' on the Instructions tab.') : null,
      p.copy_label ? callout('info', 'layers', String(p.copy_label), 'Every wake block says so, and her replies carry the label.') : null,
      section('Now', null, [budgetLine(p.budget),
        arr(p.next).length ? h('ul', { class: 'mw-plain' }, arr(p.next).map(nextLine)) : h('p', { class: 'mw-row-sub' }, 'Nothing is waiting.'),
        arr(p.last).length ? [h('h4', { class: 'mw-h4' }, 'Recent decisions'), h('ul', { class: 'mw-plain' }, arr(p.last).slice().reverse().map(lastLine))] : null], { id: 'mw-p-now' }),
      section('Her settings', 'Changed in persona setup.', h('dl', { class: 'sf-kv' },
        h('dt', null, 'Model'), h('dd', null, p.model ? h('code', null, String(p.model)) : 'none yet'),
        h('dt', null, 'Memory'), h('dd', null, s.external_memory ? 'Kept by her own tools (the harness only reads it)' : s.curation === 'self' ? 'She keeps her own; every change is listed' : 'You keep it; she proposes'),
        h('dt', null, 'Tools during a wake'), h('dd', null, s.tools === 'read_only' ? 'Read-only tools' : 'None'),
        h('dt', null, 'Commands'), h('dd', { dataset: { part: 'commands' } }, commandScope(s)),
        h('dt', null, 'Re-imports'), h('dd', null, s.reimport_decider === 'persona' ? 'She decides (you may reject)' : 'You decide')),
        { id: 'mw-p-settings', actions: linkBtn('Persona setup', '#/schedule/setup', { icon: 'settings' }) }),
      data.import ? section('Changes before import', 'What changed in Open WebUI before her soul was imported here, as the operator reported it.', SF.beforeImport(data.import, { persona: name, state: biState(name), bare: true,
        onChange: (imp) => { const pg = pageOf2(name); if (pg) pg.import = imp; refresh(); loadPage(name, true); } }), { id: 'mw-p-import' }) : null,
      ri ? section('A new version from Open WebUI', 'Staged, never applied on its own: ' + (ri.decider === 'persona' ? 'she decides on her next wake; you may still reject it.' : 'you decide.'), [
        h('p', { class: 'mw-row-sub' }, String(ri.model || ''), ': ', String(ri.from || '?'), ' → ', String(ri.to || '?'), ri.at ? [' · ', when(ri.at)] : null),
        diffBlock(ri.diff, 'What would change'),
        arr(ri.flagged).length ? h('p', null, 'Placeholders the harness does not know: ', arr(ri.flagged).map((f) => h('code', { class: 'mw-flag' }, String(f)))) : null,
        h('div', { class: 'actions' },
          SF.btn('Accept', { cls: 'btn-sm btn-primary', icon: 'check', disabled: ri.decider === 'persona', title: ri.decider === 'persona' ? 'She decides on her own re-import' : null, dataset: { action: 'reimport-accept', focus: 'ri:accept' }, onClick: (e) => personaPost(e.currentTarget, name, '/reimport/accept', {}, 'Accepted: her instructions are the new version.') }),
          SF.btn('Reject', { cls: 'btn-sm', icon: 'x', dataset: { action: 'reimport-reject', focus: 'ri:reject' }, onClick: (e) => personaPost(e.currentTarget, name, '/reimport/reject', {}, 'Rejected.') }))], { id: 'mw-p-reimport' }) : null,
      section('Her inbox', 'Notes for her, such as changes to her Open WebUI profile; she reads them on her next wake.', arr(data.inbox).length ? h('ul', { class: 'mw-list mw-list-tight' }, arr(data.inbox).slice().reverse().map((n) => h('li', { class: 'mw-row', dataset: { note: n.id } },
        h('div', { class: 'mw-row-main' }, h('div', { class: 'mw-row-top' }, icon('inbox'), h('strong', null, n.kind === 'drift' ? 'Her profile changed' : String(n.kind || 'note')), n.from || n.to ? muted(String(n.from || '?') + ' → ' + String(n.to || '?')) : null, h('span', { class: 'spacer' }), when(n.at)),
          n.summary ? textBlock(n.summary, 'mw-row-text') : null,
          h('p', { class: 'mw-row-sub' }, n.delivered ? ['Read on her wake ', String((n.delivered || {}).wake || ''), ' ', when((n.delivered || {}).at)] : 'Not read yet')))))
        : muted('Empty.'), { id: 'mw-p-inbox' }),
    ];
  }
  /** What her wake may run (the persona row's settings.commands: what RunCheck may run now, DESIGN §35): [] reads "runs
   *  no commands", a wake reads; it doesn't poke; commands_off says when her set commands are off for now. */
  const LEDGER_NO_SANDBOX = 'the read ledger is on and the sandbox (bwrap) cannot be used here, so no command may run beside her memory. Turn on the sandbox (Settings › Harness), or turn the ledger off.';
  function commandScope(s) {
    const cmds = arr(s.commands).filter((c) => arr(c).length);
    if (s.tools !== 'read_only') return 'None: she has no tools during a wake, so she runs no commands';
    if (s.commands_off === 'ledger_without_sandbox') return ['She reads files and harness status; runs no commands now. ', h('span', { class: 'mw-cmd-off', dataset: { part: 'commands-off' } }, 'Her commands are off: ' + LEDGER_NO_SANDBOX)];
    if (!cmds.length) return 'She reads files and harness status; runs no commands';
    return ['She reads files and harness status, and may run (with no network): ', cmds.map((c, i) => [i ? ', ' : '', h('code', null, arr(c).join(' '))])];
  }
  /** The attach form's state of one persona's "Changes before import" (what was typed, who attaches, its error). */
  function biState(name) {
    if (!M.beforeImport.has(name)) M.beforeImport.set(name, { ids: '', by: 'owner', error: null, open: false });
    return M.beforeImport.get(name);
  }
  function instructionsTab(name, data) {
    const p = data.persona;
    const soul = data.soul || {};
    const key = 'soul:' + name;
    const ed = M.edits.get(key) || {};
    const err = errLine('061');
    const ta = h('textarea', { class: 'textarea mw-soul', rows: '14', spellcheck: 'false', 'aria-label': 'Her instructions', dataset: { focus: 'soul:text', field: 'soul' } });
    ta.value = ed.text !== undefined ? ed.text : String(soul.text || '');
    const shownSha = soul.file_sha256;   // the file this textarea was drawn from
    ta.addEventListener('input', () => startEdit(key, ta.value, shownSha));
    const baseOf = () => { const cur = M.edits.get(key); return cur && cur.base !== undefined ? cur.base : soul.file_sha256; };
    const clean = h('input', { type: 'checkbox', checked: p.privacy === 'clean', dataset: { focus: 'soul:clean' } });
    clean.addEventListener('change', async () => {
      const r = await personaPost(clean, name, '/privacy', { what: 'soul', ref: baseOf(), clean: clean.checked }, clean.checked ? 'Confirmed: her instructions are clean.' : 'She is private again.', { onError: (x) => { revertSwitch(clean, !clean.checked); SF.toastError('Not saved', x); } });
      if (!r) refresh();
    });
    const save = async (e) => {
      formError(err, null);
      const r = await act(e.currentTarget, '/api/personas/' + enc(name) + '/soul', { text: ta.value, base_sha256: baseOf() }, 'Saved her instructions.', { onError: (x) => {
        if (x && x.status === 409) { M.edits.set(key, Object.assign({}, M.edits.get(key), { text: ta.value, base: baseOf(), conflict: x.message })); refresh(); } else formError(err, x);
      } });
      if (r) { M.edits.delete(key); forget('soul:text', 'soul:clean'); refresh(); loadPage(name, true); fetchKey('wakes', '/api/wakes'); }
    };
    return [
      p.integrity === 'changed' ? section('Review the change', 'Her file is not the one you last saved or approved here. ' + integrityWords(p) + '. Was it you?', [
        soul.diff ? diffBlock(soul.diff, 'The last approved text against the file now') : muted('No approved text to compare with.'),
        h('div', { class: 'actions' },
          SF.btn('It was me, approve', { cls: 'btn-sm btn-primary', icon: 'check', dataset: { action: 'approve', focus: 'int:approve' }, onClick: (e) => { forget('soul:text', 'soul:clean'); M.edits.delete(key); return personaPost(e.currentTarget, name, '/integrity/approve', {}, 'Approved. She is still private until you confirm her instructions are clean.'); } }),
          SF.btn('Put the approved text back', { cls: 'btn-sm', icon: 'refresh', dataset: { action: 'restore', focus: 'int:restore' }, onClick: (e) => { forget('soul:text', 'soul:clean'); M.edits.delete(key); return personaPost(e.currentTarget, name, '/integrity/restore', {}, 'The approved text is back.'); } }))],
      { id: 'mw-p-review', cls: 'is-review' }) : null,
      section('Her instructions', 'What she is told on every wake. Saving makes her private again until you confirm the new text.', [
        staleNote(key, soul.file_sha256, 'Her file changed while you were editing (outside this page, or saved elsewhere).'),
        isStale(key, soul.file_sha256) ? h('div', { class: 'actions' }, SF.btn('Reload her instructions', { cls: 'btn-sm', icon: 'refresh', dataset: { action: 'reload', focus: 'soul:reload' }, onClick: () => { M.edits.delete(key); forget('soul:text'); refresh(); loadPage(name, true); } })) : null,
        SF.field('Instructions', ta), err,
        h('label', { class: 'mw-check' }, clean, h('span', null, 'These instructions hold no personal or health details')),
        h('p', { class: 'field-hint' }, 'Until you tick it, a phone gets only fixed heads-ups from her. It covers exactly this text: any later edit makes her private again.'),
        h('div', { class: 'actions' }, SF.btn('Save', { cls: 'btn-sm btn-primary', icon: 'check', dataset: { action: 'save-soul', focus: 'soul:save' }, onClick: save }))], { id: 'mw-p-soul' }),
      h('details', { class: 'mw-file-view' }, h('summary', null, 'The whole file (', h('code', null, String(soul.path || '')), ')'), pre(soul.file),
        h('p', { class: 'field-hint' }, 'Fingerprint ', h('code', null, shortSha(soul.file_sha256)))),
    ];
  }
  function memoryTab(name, data) {
    const p = data.persona;
    const s = isObj(p.settings) ? p.settings : {};
    if (s.external_memory) return [callout('info', 'info', 'Her memory is kept by her own tools;', 'the harness only reads it and leaves what she wants to remember in her outbox.'), ledgerSection(name)];
    const mem = data.memory || {};
    const key = 'mem:' + name;
    const ed = M.edits.get(key) || {};
    const err = errLine('062');
    const ta = h('textarea', { class: 'textarea', rows: '8', spellcheck: 'false', 'aria-label': 'Her memory summary', dataset: { focus: 'mem:text', field: 'memory' } });
    ta.value = ed.text !== undefined ? ed.text : String(mem.text || '');
    const shownVersion = Number(mem.version) || 0;   // the version this textarea was drawn from
    ta.addEventListener('input', () => startEdit(key, ta.value, shownVersion));
    const baseOf = () => { const cur = M.edits.get(key); return cur && cur.base !== undefined ? cur.base : Number(mem.version) || 0; };
    const clean = h('input', { type: 'checkbox', checked: !!mem.clean, dataset: { focus: 'mem:clean' } });
    const save = async (e) => {
      formError(err, null);
      const r = await act(e.currentTarget, '/api/personas/' + enc(name) + '/memory', { text: ta.value, base_version: baseOf(), clean: clean.checked }, 'Saved her memory (a new version).', { onError: (x) => {
        if (x && x.status === 409) { M.edits.set(key, Object.assign({}, M.edits.get(key), { text: ta.value, base: baseOf(), conflict: x.message })); refresh(); } else formError(err, x);
      } });
      if (r) { M.edits.delete(key); forget('mem:text', 'mem:clean'); refresh(); loadPage(name, true); }
    };
    const c = data.consolidation;
    const versions = arr(data.memory_versions);
    return [
      s.curation === 'self' ? callout('info', 'info', 'She keeps her own memory;', 'every change is listed here and you can roll any back.') : null,
      c ? section('Her weekly memory review', 'A proposed new summary, line by line, with the journal entries it comes from.', [
        c.stale ? callout('warn', 'alert', 'Your summary changed since this was proposed (now version ' + (c.stale.version !== undefined ? c.stale.version : '?') + '),', 'so it can only be rejected; the next weekly review starts from your text.') : null,
        h('ol', { class: 'mw-proposal' }, arr(c.lines).map((l) => h('li', null, textBlock(l.text), h('div', { class: 'mw-row-sub' }, 'From ', arr(l.sources).map((src, i) => [i ? ', ' : null, h('code', null, String(src.id || '')), taintChips(src.taint)]))))),
        c.diff ? diffBlock(c.diff, 'Against her summary now') : null,
        h('div', { class: 'actions' },
          SF.btn('Accept', { cls: 'btn-sm btn-primary', icon: 'check', disabled: !!c.stale, title: c.stale ? 'Only Reject: your summary changed since' : null, dataset: { action: 'consolidation-accept', focus: 'con:accept' }, onClick: (e) => personaPost(e.currentTarget, name, '/consolidation/accept', {}, 'Accepted: her summary is the reviewed one.', {
            onError: (x) => { SF.toastError('Not accepted', x); loadPage(name, true); } }) }),
          SF.btn('Reject', { cls: 'btn-sm', icon: 'x', dataset: { action: 'consolidation-reject', focus: 'con:reject' }, onClick: (e) => personaPost(e.currentTarget, name, '/consolidation/reject', {}, 'Rejected: her summary stays as it is.') }))],
      { id: 'mw-p-consolidation' }) : null,
      section('Her memory summary', 'Version ' + (mem.version || 0) + (mem.updated_at ? ', ' + (mem.by ? 'written by ' + (mem.by === 'owner' ? 'you' : mem.by === 'persona' ? 'her' : mem.by) + ' ' : '') + SF.relTime(mem.updated_at) : '') + '. Every save is a new version you can roll back.', [
        staleNote(key, Number(mem.version) || 0, 'Version ' + (mem.version || 0) + ' was written ' + (mem.by === 'persona' ? 'by her' : mem.by === 'owner' ? 'by you, elsewhere' : mem.by === 'consolidation' ? 'by the weekly review' : mem.by ? 'by ' + mem.by : '') + ' while you were editing version ' + ((M.edits.get(key) || {}).base) + '.'),
        isStale(key, Number(mem.version) || 0) ? h('div', { class: 'actions' }, SF.btn('Reload her memory', { cls: 'btn-sm', icon: 'refresh', dataset: { action: 'reload', focus: 'mem:reload' }, onClick: () => { M.edits.delete(key); forget('mem:text', 'mem:clean'); refresh(); loadPage(name, true); } })) : null,
        SF.field('Summary', ta), h('label', { class: 'mw-check' }, clean, h('span', null, 'This summary holds no personal or health details')), err,
        h('div', { class: 'actions' }, SF.btn('Save', { cls: 'btn-sm btn-primary', icon: 'check', dataset: { action: 'save-memory', focus: 'mem:save' }, onClick: save }))], { id: 'mw-p-memory' }),
      section('Versions', 'Newest first.', versions.length ? h('ul', { class: 'mw-list mw-list-tight' }, versions.map((v) => h('li', { class: 'mw-row', dataset: { version: String(v.version) } },
        h('div', { class: 'mw-row-main' }, h('div', { class: 'mw-row-top' }, h('strong', null, 'Version ' + v.version), v.version === mem.version ? tag('Now', 'tag-accent') : null, v.clean ? tag('Clean', 'tag-ok') : tag('Private', 'tag-outline'), muted(SF.plural(v.chars || 0, 'character')))),
        h('div', { class: 'mw-row-actions' },
          SF.btn('Read', { cls: 'btn-sm', icon: 'eye', dataset: { action: 'read-version', focus: 'ver:read:' + v.version }, onClick: (e) => readVersion(e.currentTarget, name, v.version) }),
          v.version !== mem.version ? SF.btn('Roll back to this', { cls: 'btn-sm btn-ghost', icon: 'refresh', dataset: { action: 'rollback', focus: 'ver:rb:' + v.version }, onClick: async (e) => {
            const ok = await SF.confirmModal({ title: 'Roll back to version ' + v.version + '?', description: 'Its text becomes a new version on top; nothing is deleted. It is private until you tick it clean.', confirm: 'Roll back', icon: 'refresh' });
            if (ok) personaPost(e.currentTarget, name, '/memory/rollback', { version: v.version }, 'Rolled back: a new version with the text of version ' + v.version + '.');
          } }) : null))))
        : muted('No version yet.'), { id: 'mw-p-versions' }),
      section('Who wrote it', 'Every write, newest first: when, who, why, and the fingerprints before and after.', arr(data.memory_writes).length ? h('ul', { class: 'mw-list mw-list-tight mw-writes' }, arr(data.memory_writes).map((w) => h('li', { class: 'mw-row', dataset: { by: w.by } },
        h('div', { class: 'mw-row-main' }, h('div', { class: 'mw-row-top' }, when(w.at), tag(w.by === 'owner' ? 'You' : w.by === 'persona' ? personaDisplay(name) : w.by === 'consolidation' ? 'Weekly review' : String(w.by || '?'), w.by === 'persona' ? 'tag-accent' : 'tag-outline'),
          muted('version ' + (w.version || '?')), w.clean ? tag('Clean', 'tag-ok') : null),
          w.reason ? textBlock(w.reason, 'mw-row-text') : null,
          h('p', { class: 'mw-row-sub mono' }, shortSha(w.before_sha256), ' → ', shortSha(w.after_sha256), w.wake ? ' · ' + w.wake : '')))))
        : muted('Nothing written yet.'), { id: 'mw-p-writes' }),
    ];
  }
  // ---------------------------------------------------------------- the read ledger of her own memory (GET …/external)
  // DESIGN §35 "The read ledger of an external memory": what the harness read of her memory, kept as private copies so
  // the owner can look back, compare and prepare a restore. Everything here is private and her text: text nodes only,
  // never in a page title, a toast or the URL. The harness never writes her memory: a restore is text to apply.
  const kb = (n) => (Number(n) >= 1024 * 1024 ? (Number(n) / 1048576).toFixed(1) + ' MB' : Number(n) >= 1024 ? Math.round(Number(n) / 1024) + ' KB' : SF.plural(Number(n) || 0, 'byte'));
  function ledgerSection(name) {
    const key = 'ledger:' + name;
    ensure(key, '/api/personas/' + enc(name) + '/external', 15000);
    const L = val(key);
    const head = { id: 'mw-p-ledger', actions: tag('Private', 'tag-outline mw-private', 'Her memory, as the harness read it: shown on these pages only') };
    if (!L) return section('What the harness read of her memory', null, errOf(key) ? errorBox(errOf(key), 'the read ledger') : SF.skeleton(3), head);
    const files = arr(L.files);
    const caps = isObj(L.caps) ? L.caps : {};
    const capNotes = [
      caps.evicted ? 'The ledger is at its cap: ' + SF.plural(Number(caps.evicted), 'older copy', 'older copies') + ' were removed (the newest of each file is kept).' : null,
      caps.trimmed ? SF.plural(Number(caps.trimmed), 'older read', 'older reads') + ' left the record to keep it within its size.' : null,
      caps.not_kept ? SF.plural(Number(caps.not_kept), 'version') + ' did not fit and were not kept.' : null].filter(Boolean);
    return section('What the harness read of her memory', L.ledger
      ? 'The read ledger is on: each time the harness reads her memory, it records what it read and keeps a private copy of each version, so you can look back, compare and prepare a restore. It never writes her memory.'
      : 'The read ledger is off: the harness keeps no copy of what it reads. Turn it on in Persona setup (Keep a read ledger)' + (files.length ? '; what it kept before is still listed.' : '.'), [
      capNotes.length ? callout('warn', 'alert', null, capNotes.join(' ')) : null,
      h('p', { class: 'mw-row-sub', dataset: { part: 'ledger-size' } }, SF.plural(Number(L.reads) || 0, 'read') + ' recorded · copies ' + kb(L.kept_bytes) + ' of ' + kb(L.cap_bytes)),
      files.length ? files.map((f) => ledgerFile(name, f)) : muted('Nothing read yet.'),
      arr(L.sqlite).length ? [h('h4', { class: 'mw-h4' }, 'Her database, as read'), h('ul', { class: 'mw-plain mw-ledger-db' }, arr(L.sqlite).map((r) => h('li', { dataset: { db: r.error ? 'error' : 'ok' } }, when(r.at), ' ',
        r.error ? h('span', { class: 'mw-error-text' }, 'could not be read (' + String(r.error) + ')') : SF.plural(Number(r.rows) || 0, 'row') + ' ', r.sha256 ? muted('fingerprint ' + shortSha(r.sha256)) : null)))] : null,
      arr(L.erased).length ? [h('h4', { class: 'mw-h4' }, 'Erased by you'), h('p', { class: 'field-hint' }, 'Only their fingerprints are kept; her own notes were not touched.'),
        h('ul', { class: 'mw-plain mw-ledger-erased' }, arr(L.erased).map((x) => h('li', { dataset: { erased: String(x.sha256 || '') } }, h('code', null, shortSha(x.sha256)), ' ', muted('erased '), when(x.erased_at))))] : null,
    ], head);
  }
  function ledgerFile(name, f) {
    const versions = arr(f.versions);
    return h('div', { class: 'mw-ledger-file', dataset: { file: String(f.name || '') } }, h('h4', { class: 'mw-h4' }, icon('file'), ' ', h('code', null, String(f.name || '')), ' ', muted(SF.plural(versions.length, 'version'))),
      h('ul', { class: 'mw-list mw-list-tight' }, versions.map((v, i) => {
        const newer = versions.slice(0, i).reverse().find((x) => x.kept);   // the next newer kept version, to compare with
        return h('li', { class: 'mw-row', dataset: { version: String(v.sha256 || '') } },
          h('div', { class: 'mw-row-main' }, h('div', { class: 'mw-row-top' }, h('code', { title: String(v.sha256 || '') }, shortSha(v.sha256)), i === 0 ? tag('Newest', 'tag-accent') : null,
            v.kept ? tag('Copy kept', 'tag-outline') : tag('No copy', 'tag-outline', 'The ledger kept no copy of this version (not text, empty, or past its cap)'),
            v.redacted ? tag('A secret taken out', 'tag-warn', 'A secret the harness knows reads *** in this copy') : null,
            v.truncated ? tag('Read in part', 'tag-warn', 'The harness read only the first part of it: no whole copy to restore') : null),
            h('p', { class: 'mw-row-sub' }, kb(v.bytes), v.truncated ? ' (read ' + kb(v.read_bytes) + ')' : '', ' · read ' + SF.plural(Number(v.reads) || 0, 'time'), ' · first ', when(v.first_read_at), ' · last ', when(v.last_read_at))),
          h('div', { class: 'mw-row-actions' },
            v.kept ? SF.btn('Show', { cls: 'btn-sm', icon: 'eye', dataset: { action: 'ledger-show', focus: 'led:show:' + v.sha256 }, onClick: (e) => ledgerShow(e.currentTarget, name, v.sha256) }) : null,
            v.kept && newer ? SF.btn('Compare with newer', { cls: 'btn-sm btn-ghost', icon: 'search', dataset: { action: 'ledger-diff', focus: 'led:diff:' + v.sha256 }, onClick: (e) => ledgerDiff(e.currentTarget, name, v.sha256, newer.sha256) }) : null,
            v.kept && !v.truncated ? SF.btn('Prepare a restore', { cls: 'btn-sm btn-ghost', icon: 'refresh', dataset: { action: 'ledger-restore', focus: 'led:restore:' + v.sha256 }, onClick: (e) => ledgerRestore(e.currentTarget, name, v.sha256, f.name) }) : null,
            SF.btn('Erase this version', { cls: 'btn-sm btn-ghost', icon: 'trash', dataset: { action: 'ledger-erase', focus: 'led:erase:' + v.sha256 }, onClick: (e) => ledgerErase(e.currentTarget, name, v.sha256) })));
      })));
  }
  async function ledgerGet(button, path) {
    SF.setBusy(button, true);
    try { return await SF.api(path); } catch (e) { SF.toastError('Could not read it', e); return null; } finally { if (button.isConnected) SF.setBusy(button, false); }
  }
  const privateNote = () => h('p', { class: 'field-hint mw-private-note' }, icon('shield', 'icon-xs'), ' Private: her memory as the harness read it. It is shown here only.');
  async function ledgerShow(button, name, sha) {
    const v = await ledgerGet(button, '/api/personas/' + enc(name) + '/external/versions/' + enc(sha));
    if (!v) return;
    await SF.openModal({ title: 'A version of her memory', wide: true, body: [privateNote(),
      h('p', { class: 'mw-row-sub' }, 'Read as ', arr(v.names).map((n, i) => [i ? ', ' : '', h('code', null, String(n))]), ' · ', kb(v.bytes), ' · fingerprint ', h('code', null, shortSha(v.sha256))),
      v.redacted ? callout('warn', 'alert', 'A secret was taken out of this copy:', 'it reads *** here.') : null,
      pre(v.text, 'mw-ledger-text')], actions: [{ label: 'Close', value: 'ok', primary: true }] });
  }
  async function ledgerDiff(button, name, older, newer) {
    const d = await ledgerGet(button, '/api/personas/' + enc(name) + '/external/diff?from=' + enc(older) + '&to=' + enc(newer));
    if (!d) return;
    await SF.openModal({ title: 'What changed between two versions', wide: true, body: [privateNote(),
      h('p', { class: 'mw-row-sub' }, h('code', null, shortSha(d.from)), ' → ', h('code', null, shortSha(d.to))),
      d.diff ? diffBlock(d.diff, 'Differences between the two versions') : muted('No difference.')], actions: [{ label: 'Close', value: 'ok', primary: true }] });
  }
  async function ledgerRestore(button, name, sha, file) {
    const r = await act(button, '/api/personas/' + enc(name) + '/external/restore', { sha256: sha, file: file ? String(file) : undefined }, null, { what: 'Could not prepare it' });
    const x = r && isObj(r.restore) ? r.restore : null;
    if (!x) return;
    const cur = isObj(x.current) ? x.current : null;
    await SF.openModal({ title: 'A restore, prepared', wide: true, body: [privateNote(),
      callout('info', 'info', 'The harness never writes her memory:', 'her own tools or you apply this.'),
      h('p', { class: 'mw-row-sub' }, String(x.apply || '')),
      h('p', { class: 'mw-row-sub' }, 'For ', h('code', null, String(x.name || '')), ': version ', h('code', null, shortSha(x.sha256)),
        cur ? [' · her newest read held ', h('code', null, shortSha(cur.sha256)), ' ', when(cur.read_at)] : ' · no newer read of this file'),
      arr(x.warnings).length ? h('ul', { class: 'mw-plain mw-ledger-warn' }, arr(x.warnings).map((w) => h('li', null, icon('alert', 'icon-xs'), ' ', String(w)))) : null,
      x.diff ? [h('h4', { class: 'mw-h4' }, 'From her newest read to this version'), diffBlock(x.diff, 'From her newest read to this version')] : null,
      h('h4', { class: 'mw-h4' }, 'The text to apply'), pre(x.text, 'mw-ledger-text'),
      h('div', { class: 'actions' }, SF.btn('Copy the text', { cls: 'btn-sm', icon: 'copy', dataset: { action: 'ledger-copy' }, onClick: () => SF.copyText(String(x.text || ''), 'Copied') }))],
      actions: [{ label: 'Close', value: 'ok', primary: true }] });
  }
  async function ledgerErase(button, name, sha) {
    const ok = await SF.confirmModal({ title: 'Erase this version?', description: 'Its copy is deleted and the ledger keeps only its fingerprint; her own note is not touched.', confirm: 'Erase', cls: 'btn-danger', icon: 'trash' });
    if (!ok) return;
    const r = await act(button, '/api/personas/' + enc(name) + '/external/erase', { sha256: sha }, 'Erased: only its fingerprint is kept.', { what: 'Could not erase it' });
    if (r) fetchKey('ledger:' + name, '/api/personas/' + enc(name) + '/external');
  }
  async function readVersion(button, name, version) {
    SF.setBusy(button, true);
    try {
      const v = await SF.api('/api/personas/' + enc(name) + '/memory/versions/' + enc(version));
      await SF.openModal({ title: 'Version ' + version + ' of her memory', wide: true, body: [pre(v && v.text), h('p', { class: 'field-hint' }, (v && v.clean ? 'Clean' : 'Private') + ' · fingerprint ', h('code', null, shortSha(v && v.sha256)))],
        actions: [{ label: 'Close', value: 'ok', primary: true }] });
    } catch (e) { SF.toastError('Could not read it', e); }
    finally { if (button.isConnected) SF.setBusy(button, false); }
  }
  function journalTab(name, data) {
    const p = data.persona;
    if (isObj(p.settings) && p.settings.external_memory) return [callout('info', 'info', 'Her memory is kept by her own tools;', 'the harness only reads it and leaves what she wants to remember in her outbox.')];
    const before = h('input', { class: 'input input-sm', type: 'date', 'aria-label': 'Forget everything before', dataset: { focus: 'j:before' } });
    const entries = arr(data.journal);
    return [
      section('Her journal', 'What she noted on her wakes; the weekly review builds her summary from it (never from entries marked outside text).', entries.length ? h('ul', { class: 'mw-list mw-list-tight' }, entries.map((j) => h('li', { class: 'mw-row', dataset: { entry: j.id } },
        h('div', { class: 'mw-row-main' }, h('div', { class: 'mw-row-top' }, when(j.at), tag(j.by === 'owner' ? 'You' : personaDisplay(name), 'tag-outline'), taintChips(j.taint), h('span', { class: 'spacer' }), muted(j.id)),
          j.text ? textBlock(j.text, 'mw-row-text') : muted('Text removed.')),
        h('div', { class: 'mw-row-actions' }, SF.btn('Forget this', { cls: 'btn-sm btn-ghost', icon: 'trash', dataset: { action: 'retract', focus: 'j:retract:' + j.id }, onClick: (e) => personaPost(e.currentTarget, name, '/journal/' + enc(j.id) + '/retract', {}, 'Forgotten: it leaves every later prompt.') })))))
        : muted('Nothing noted yet.'), { id: 'mw-p-journal' }),
      section('Forget everything before a day', 'Journal entries and thread messages before it lose their text, and older memory versions are deleted.', h('div', { class: 'sf-row' }, before,
        SF.btn('Forget everything before…', { cls: 'btn-sm btn-danger', icon: 'trash', dataset: { action: 'forget', focus: 'j:forget' }, onClick: async (e) => {
          if (!/^\d{4}-\d{2}-\d{2}$/.test(before.value)) { SF.toast('Pick a day first.', { kind: 'info' }); return; }
          const ok = await SF.confirmModal({ title: 'Forget everything before ' + before.value + '?', description: 'This cannot be undone.', confirm: 'Forget', cls: 'btn-danger', icon: 'trash' });
          if (!ok) return;
          const r = await personaPost(e.currentTarget, name, '/forget', { before: before.value });
          if (r && isObj(r.removed)) SF.toast('Forgotten: ' + SF.plural(r.removed.journal || 0, 'journal entry', 'journal entries') + ', ' + SF.plural(r.removed.thread || 0, 'message') + ', ' + SF.plural(r.removed.versions || 0, 'old version') + '.', { kind: 'ok' });
        } })), { id: 'mw-p-forget' }),
    ];
  }
  function threadTab(name, data) {
    const key = 'thread:' + name;
    const err = errLine('063');
    const ta = h('textarea', { class: 'textarea', rows: '3', 'aria-label': 'Write to ' + personaDisplay(name), dataset: { focus: 'thread:text', field: 'thread' } });
    ta.value = M.edits.get(key) || '';
    ta.addEventListener('input', () => M.edits.set(key, ta.value));
    const send = async (e) => {
      if (!ta.value.trim()) return;
      formError(err, null);
      const r = await act(e.currentTarget, '/api/personas/' + enc(name) + '/messages', { text: ta.value }, null, { onError: (x) => formError(err, x) });
      if (r) { M.edits.delete(key); forget('thread:text'); SF.toast('Sent. She answers on her next turn.', { kind: 'ok' }); loadPage(name, true); fetchKey('wakes', '/api/wakes'); }
    };
    ta.addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); send({ currentTarget: e.currentTarget.closest('section').querySelector('[data-action="send-thread"]') }); } });
    const msgs = arr(data.thread);
    return [section('Your thread with ' + personaDisplay(name), 'What you write here (or from a phone) wakes her; it is your intent, never an approval.', [
      msgs.length ? h('ol', { class: 'mw-thread', 'aria-label': 'Messages, oldest first' }, msgs.map((m) => {
        const mine = m.role === 'owner';
        const untrusted = arr(m.taint).indexOf('untrusted') >= 0;
        return h('li', { class: 'mw-tmsg' + (mine ? ' is-mine' : m.role === 'harness' ? ' is-harness' : '') + (m.kind === 'decline' ? ' is-decline' : ''), dataset: { message: m.id, kind: m.kind || '' } },
          h('div', { class: 'mw-row-top' }, h('strong', null, mine ? 'You' : m.role === 'harness' ? 'The harness' : personaDisplay(name)), m.kind === 'decline' ? tag('Declined', 'tag-outline') : null,
            m.via && m.via !== 'harness' ? muted(m.via === 'cockpit' ? 'on these pages' : 'via ' + String(m.via)) : null, m.forwarded ? tag('Forwarded', 'tag-warn') : null, h('span', { class: 'spacer' }), when(m.at)),
          m.text ? textBlock(m.text, 'mw-tmsg-text') : muted('Text removed.'),
          m.label ? h('p', { class: 'mw-row-sub mw-label' }, icon('layers', 'icon-xs'), ' ', String(m.label)) : null,
          !mine && untrusted ? h('p', { class: 'mw-row-sub mw-untrusted' }, icon('alert', 'icon-xs'), ' Written after reading outside text') : null);
      })) : muted('No messages yet.'),
      h('div', { class: 'mw-composer' }, SF.field('Write to ' + personaDisplay(name), ta, 'Ctrl+Enter sends'), err,
        h('div', { class: 'actions' }, SF.btn('Send', { cls: 'btn-sm btn-primary', icon: 'send', dataset: { action: 'send-thread', focus: 'thread:send' }, onClick: send })))], { id: 'mw-p-thread' })];
  }
  function runsTab(name, data) {
    const open = M.edits.get('run:' + name);
    const runs = arr(data.runs);
    const view = open ? val('run:' + name + ':' + open) : null;
    if (open) ensure('run:' + name + ':' + open, '/api/personas/' + enc(name) + '/runs/' + enc(open), 60000);
    return [section('Her runs', 'One log per wake: what she was given, what came back, how it was checked and what was done.', runs.length ? h('ul', { class: 'mw-list mw-list-tight' }, runs.map((r) => h('li', { class: 'mw-row' + (open === r.log ? ' is-open' : ''), dataset: { run: r.log } },
      h('div', { class: 'mw-row-main' }, h('div', { class: 'mw-row-top' }, h('strong', null, String(r.log)), r.outcome ? pill(ENDED_WORDS[r.outcome] || r.outcome, ENDED_TONE[r.outcome]) : null,
        h('span', { class: 'mw-src' }, SOURCE_WORDS[r.trigger] || String(r.trigger || '')), taintChips(r.taint), h('span', { class: 'spacer' }), when(r.at)),
        h('p', { class: 'mw-row-sub' }, (arr(r.actions).length ? 'Did: ' + arr(r.actions).join(', ') : 'Did nothing') + (r.seconds ? ' · ' + fmtSecs(r.seconds) : '') + (r.wake ? ' · ' + r.wake : '')),
        r.error ? h('p', { class: 'mw-row-sub mw-error-text' }, String(r.error)) : null,
        open === r.log ? (errOf('run:' + name + ':' + open) ? errorBox(errOf('run:' + name + ':' + open), 'the run log') : !view ? SF.skeleton(3)
          : h('ol', { class: 'mw-runlog', 'aria-label': 'The run log' }, arr(view.lines).map((l) => h('li', { class: 'mw-runline', dataset: { type: String((l && l.type) || '') } },
            h('span', { class: 'tag tag-mono' }, String((l && l.type) || 'line')), l && l.tokenizer ? tag('tokenizer: ' + l.tokenizer, l.tokenizer === 'same' ? 'tag-ok' : 'tag-warn') : null, pre(JSON.stringify(l, null, 2)))))) : null),
      h('div', { class: 'mw-row-actions' }, SF.btn(open === r.log ? 'Close' : 'Open', { cls: 'btn-sm', icon: open === r.log ? 'x' : 'eye', dataset: { action: 'open-run', focus: 'run:' + r.log }, onClick: () => { if (open === r.log) M.edits.delete('run:' + name); else M.edits.set('run:' + name, r.log); refresh(); } })))))
      : muted('No runs yet.'), { id: 'mw-p-runs' })];
  }
  function renderPersona(page) {
    const name = page.name;
    loadPage(name);
    ensure('wakes', '/api/wakes', 10000);
    const data = pageOf2(name);
    const e = errOf('page:' + name);
    if (!data) return [e ? (e.status === 404 ? SF.emptyState('persona', 'No persona called ' + name, 'She may have been removed, or she is not a persona that wakes herself.', [linkBtn('Back to the schedule', '#/schedule')], true) : errorBox(e, 'her page')) : SF.skeleton(8)];
    const tab = P_TABS.some((t) => t[0] === page.tab) ? page.tab : 'overview';
    const nav = h('nav', { class: 'mw-tabs mw-ptabs', 'aria-label': personaDisplay(name) }, P_TABS.map(([id, label]) => h('a', { class: 'mw-tab', href: '#/schedule/persona/' + enc(name) + '/' + id,
      'aria-current': id === tab ? 'page' : null, dataset: { focus: 'ptab:' + id, tab: id } }, label, id === 'instructions' && data.persona.integrity === 'changed' ? h('span', { class: 'count-pill has', 'aria-label': 'review needed', title: 'Review needed' }, '!') : null,
      id === 'memory' && data.consolidation && !data.consolidation.stale ? h('span', { class: 'count-pill has', 'aria-label': 'a review waits' }, '1') : null)));
    const body = tab === 'instructions' ? instructionsTab(name, data) : tab === 'memory' ? memoryTab(name, data) : tab === 'journal' ? journalTab(name, data)
      : tab === 'thread' ? threadTab(name, data) : tab === 'runs' ? runsTab(name, data) : overviewTab(name, data);
    return [h('p', { class: 'mw-crumb' }, h('a', { href: '#/schedule', dataset: { focus: 'crumb' } }, icon('chevron-left'), 'Schedule'),
      h('span', { class: 'sf-muted', 'aria-hidden': 'true' }, ' · '), h('a', { href: '#/personas', dataset: { focus: 'crumb-personas' } }, 'All personas')), personaHead(name, data), nav, body];
  }

  // ---------------------------------------------------------------- render
  const TITLES = {
    inbox: ['Messages', 'Notices from your server and your personas, newest first, and exactly what each phone got.'],
    inbound: ['What came back', 'Taps, commands and what you wrote from your phones, and what the harness did with each.'],
    setup: ['Phone setup', 'Connect your phone: ntfy on your tailnet keeps notices private; Telegram is quicker but a third party can read it.'],
    ntfy: ['Set up ntfy', 'A private phone channel on your tailnet, in seven short steps.'],
    telegram: ['Set up Telegram', 'A Telegram bot for short heads-ups, paired to your own account.'],
    settings: ['Message settings', 'What reaches your phone, when, and how much: the rulebook, sensitive words and event sources.'],
    schedule: ['Schedule', 'When each persona wakes next, why a wake waits, your reminders and the model servers check-ins may use.'],
    chats: ['Chat check-ins', 'Which chats may schedule a check-in, and the ones waiting.'],
    wakesetup: ['Persona setup', 'Turn personas and chat check-ins on, and choose each persona’s model, memory and tools.'],
  };
  /** Values typed into this page survive a re-render (an event, a reload) until the route changes. */
  function carry(root) {
    const out = new Map();
    const skip = M.noCarry;
    M.noCarry = new Set();
    for (const el of root.querySelectorAll('input[data-focus], textarea[data-focus], select[data-focus]')) {
      if (el.dataset.dirty !== '1' || el.type === 'file' || skip.has(el.dataset.focus)) continue;
      const focused = el === el.ownerDocument.activeElement;
      out.set(el.dataset.focus, { value: el.value, checked: el.checked, start: focused && typeof el.selectionStart === 'number' ? el.selectionStart : null, end: focused ? el.selectionEnd : null });
    }
    return out;
  }
  function restore(root, carried) {
    if (!carried || !carried.size) return;
    for (const el of root.querySelectorAll('input[data-focus], textarea[data-focus], select[data-focus]')) {
      const v = carried.get(el.dataset.focus);
      if (!v) continue;
      if (el.type === 'checkbox' || el.type === 'radio') el.checked = v.checked; else el.value = v.value;
      el.dataset.dirty = '1';
      if (v.start !== null && el === el.ownerDocument.activeElement && el.setSelectionRange) { try { el.setSelectionRange(v.start, v.end); } catch (e) { /* not a text field */ } }
    }
  }
  function markDirty(e) { const t = e.target; if (t && t.dataset && t.dataset.focus) t.dataset.dirty = '1'; }
  /** Once, from start(): fields typed into are marked, so a re-render keeps what was typed. This area's styles
   *  (mw-* classes) are in style.css, loaded with the page, so the strip on another page has them too. */
  function ensureStyle() {
    const root = M.handle && M.handle.root;
    if (M.styled || !root) return;
    M.styled = true;
    root.addEventListener('input', markDirty);
    root.addEventListener('change', markDirty);
  }
  function render(root, route, handle) {
    M.handle = handle;
    ensureStyle();
    const page = pageOf(route);
    M.page = page;
    const path = String(route.name) + '/' + (route.parts || []).join('/');
    const prev = M.lastRoute;
    const carried = prev === path ? carry(root) : null;
    if (prev !== path) { M.reminderMoved = null; M.formErrors.clear(); if (page.kind !== 'ntfy' || page.step !== 3) M.secret.passphrase = null; }
    const wizard = page.kind === 'ntfy' || page.kind === 'telegram';
    // A wizard entered from outside starts its pairing again: a code, a pairing state or a phone's answer from an
    // earlier visit (before New tokens, It leaked or Lost my phone) is not this pairing.
    if (wizard && prev.indexOf('messages/setup/' + page.kind + '/') !== 0) resetPairing(page.kind, null);
    if (!wizard) stopPoll();
    if (page.kind !== 'settings') M.edits.delete('terms');
    ensure('notify', '/api/notify', 8000);
    if (page.kind === 'schedule' || page.kind === 'persona' || page.kind === 'wakesetup' || page.kind === 'chats') ensure('wakes', '/api/wakes', 5000);
    const body = page.kind === 'inbound' ? renderInbound(route) : page.kind === 'setup' ? renderSetup() : page.kind === 'ntfy' ? renderNtfy(page, route)
      : page.kind === 'telegram' ? renderTelegram(page, route) : page.kind === 'settings' ? renderSettings() : page.kind === 'schedule' ? renderSchedule()
        : page.kind === 'persona' ? renderPersona(page) : page.kind === 'chats' ? renderChats() : page.kind === 'wakesetup' ? renderWakeSetup() : renderInbox(route, page);
    const t = TITLES[page.kind] || TITLES.inbox;
    const head = page.kind === 'persona' ? null : SF.viewHead(t[0], t[1], page.kind === 'schedule' ? linkBtn('Persona setup', '#/schedule/setup', { icon: 'settings' }) : null);
    SF.rebuild(root, [h('div', { class: 'mw-page', dataset: { page: page.kind } }, head, tabsNav(page), banners(page), h('div', { class: 'mw-body sf-stack' }, body))]);
    restore(root, carried);
    M.lastRoute = path;
    // A new wizard step: the keyboard and a screen reader land on its title (as the panel does with its own).
    if (wizard && prev && prev !== path) { const t = root.querySelector('#mw-step-t'); if (t) t.focus({ preventScroll: false }); }
    renderPanel(page, route);
    paintStrip();
  }

  // ---------------------------------------------------------------- the strip on every other page
  function paintStrip() {
    if (!M.handle) return;
    if (!M.strip) {
      const main = M.handle.root.closest('main') || M.handle.root.parentNode && M.handle.root.parentNode.parentNode;
      if (!main) return;
      M.strip = h('div', { class: 'mw-strip', role: 'status', hidden: true, dataset: { module: 'messages' } });
      main.insertBefore(M.strip, main.firstChild);
    }
    const st = val('notify');
    const wk = val('wakes');
    let text = null;
    let href = '#/messages';
    if (st && isObj(st.tamper)) text = 'Settings changed outside these pages: phones can only stop until you look.';
    else if ((st && isObj(st.paused)) || (wk && isObj(wk.paused))) text = 'Seraphina is stopped. Alerts from your server still come.';
    else if (wk && isObj(wk.breaker) && wk.breaker.open) { text = 'Seraphina stopped herself.'; href = '#/schedule'; }
    const show = !!text && !M.handle.isShown();
    M.strip.hidden = !show;
    const sig = show ? text + href : '';
    if (M.strip.dataset.sig === sig) return;
    M.strip.dataset.sig = sig;
    SF.clear(M.strip);
    if (show) SF.add(M.strip, [icon('shield'), h('span', null, text), ' ', h('a', { href, class: 'mw-strip-link' }, 'Open Messages & Wakes')]);
  }

  // ---------------------------------------------------------------- the unread count, Home and the registration
  function loadUnread() {
    return SF.api('/api/messages?limit=1').then((r) => {
      M.unread = r && Number.isFinite(Number(r.unread)) ? Number(r.unread) : 0;
      if (M.handle) { M.handle.badge(M.unread, SF.plural(M.unread, 'unread message')); M.handle.refreshHome(); }
      return M.unread;
    }, () => M.unread);
  }
  async function homeLoad(handle) {
    M.handle = handle;
    const r = await SF.api('/api/messages?limit=1');
    M.unread = r && Number.isFinite(Number(r.unread)) ? Number(r.unread) : 0;
    handle.badge(M.unread, SF.plural(M.unread, 'unread message'));
    let st = null;
    let wk = null;
    try { st = await SF.api('/api/notify'); D.notify = { v: st, err: null, at: Date.now(), p: null }; } catch (e) { if (!SF.gone(e)) throw e; }
    try { wk = await SF.api('/api/wakes'); D.wakes = { v: wk, err: null, at: Date.now(), p: null }; } catch (e) { if (!SF.gone(e)) throw e; }
    paintStrip();
    const n = M.unread;
    const needs = [];
    if (n) needs.push({ key: 'inbox', tone: 'human', icon: 'bell', text: SF.plural(n, 'unread message'), sub: 'Your inbox: notices from your server and your personas', href: '#/messages' });
    if (st && isObj(st.tamper)) needs.push({ key: 'tamper', tone: 'warn', icon: 'shield', href: '#/messages/settings', text: 'Settings changed outside these pages', sub: 'Phones can only stop until you look: ' + tamperWhat(st.tamper) });
    const paused = (st && st.paused) || (wk && wk.paused);
    if (isObj(paused) && !(st && isObj(st.tamper))) needs.push({ key: 'paused', tone: 'warn', icon: 'stop', href: '#/schedule', text: 'Seraphina is stopped', sub: 'Stopped ' + viaWords(paused.via || paused.by, 'somewhere') + '; restart her on the Schedule page' });
    if (wk && isObj(wk.breaker) && wk.breaker.open) needs.push({ key: 'breaker', tone: 'warn', icon: 'zap', href: '#/schedule', text: 'Seraphina stopped herself', sub: String(wk.breaker.why || '') });
    for (const p of arr(wk && wk.personas)) {
      if (p.integrity === 'changed') needs.push({ key: 'integrity:' + p.name, tone: 'human', icon: 'persona', href: '#/schedule/persona/' + enc(p.name) + '/instructions', text: String(p.display || p.name) + '’s instructions changed outside her page', sub: (isObj(p.stopped) && p.stopped.by === 'integrity' ? 'She is stopped' : 'Her next wake stops her') + ' until you approve the change or put the old text back' });
    }
    if (st && isObj(st.precondition) && !st.precondition.ok && arr(st.channels).length) needs.push({ key: 'precondition', tone: 'warn', icon: 'shield', href: '#/messages/setup', text: 'Phone messages are off', sub: 'Your agents run without the sandbox or through a CLI driver' });
    if (isObj(paused)) return { tone: 'warn', text: 'Seraphina is stopped', detail: 'Stopped from ' + String(paused.via || paused.by || 'somewhere') + (paused.at ? ' ' + SF.relTime(paused.at) : '') + '. Alerts from your server still come.', needs };
    const chans = arr(st && st.channels);
    const detail = (st && st.today ? (st.today.phone || 0) + ' of ' + (st.today.cap || '?') + ' phone notices today' : '') + (chans.length ? ' · ' + SF.plural(chans.filter((c) => c.active).length, 'phone') + ' can send' : ' · no phone yet');
    return { tone: n ? 'human' : chans.length ? 'ok' : 'todo', text: n ? SF.plural(n, 'unread message') : 'no unread messages', detail, needs };
  }

  /** The chat's meta row (a kind other builds may not have: then nothing is added). */
  function chatChip(chat) {
    if (!chat || !chat.id) return null;
    const cw = val('chatwakes:' + chat.id);
    if (!cw) { ensure('chatwakes:' + chat.id, '/api/chats/' + enc(chat.id) + '/wakes', 30000); return null; }
    return h('a', { class: 'tag tag-outline mw-chat-chip', href: '#/schedule/chats', title: 'Whether this chat may schedule check-ins' }, icon('clock', 'icon-xs'), cw.allow ? 'Check-ins on' + (arr(cw.wakes).length ? ' (' + arr(cw.wakes).length + ')' : '') : 'Check-ins off');
  }

  const handle = SF.registerView({
    id: 'messages',
    title: 'Messages & Wakes',
    routes: ['messages', 'schedule'],
    nav: { label: 'Messages', icon: 'bell', order: 40, phone: true },
    home: {
      title: 'Messages & Wakes', icon: 'bell', order: 40, href: '#/messages',
      text: 'Notices from your server and your personas reach your phone; you can answer, snooze or stop them here.',
      routes: [{ to: 'tracker', label: 'promised check-ins' }],
      events: ['notify.queued', 'notify.acked', 'wake.paused', 'wake.resumed', 'wake.breaker', 'persona.updated'],
      load: homeLoad,
    },
    start(hd) { M.handle = hd; ensureStyle(); loadUnread(); fetchKey('notify', '/api/notify'); fetchKey('wakes', '/api/wakes'); },
    // Coming from another page (Settings, Harness, say): the banners read the state again, not what was cached.
    enter(route, prev, hd) { M.handle = hd; if (!prev && M.ready) { fetchKey('notify', '/api/notify'); fetchKey('wakes', '/api/wakes'); } paintStrip(); },
    leave() { stopPoll(); M.secret.passphrase = null; M.edits.delete('terms'); M.lastRoute = ''; paintStrip(); },
    render,
    events: {
      'notify.queued': (ev, hd) => {
        loadUnread();
        const kind = ev.data && ev.data.kind;
        if (kind === 'wakes.tamper') fetchKey('notify', '/api/notify');
        // The gate's integrity stop and a delivered reminder have no event of their own: their notice is the signal.
        if (kind === 'persona.integrity' || kind === 'reminder') { if (hd.isShown()) soon('wakes', '/api/wakes', 400); else drop('wakes'); if (kind === 'persona.integrity') pageEvent(ev, true); }
        if (hd.isShown()) { reloadInbox(); refreshDetail(ev); refresh(); }
      },
      'notify.acked': (ev, hd) => { loadUnread(); if (hd.isShown()) { reloadInbox(); refreshDetail(ev); refresh(); } },
      'notify.sent': (ev, hd) => deliveryEvent(ev, hd),
      'notify.failed': (ev, hd) => deliveryEvent(ev, hd),
      'notify.unknown': (ev, hd) => deliveryEvent(ev, hd),
      'notify.held': (ev, hd) => deliveryEvent(ev, hd),
      'inbound.received': (ev, hd) => { if (hd.isShown()) soon('inbound', '/api/notify/inbound?limit=200'); else drop('inbound'); },
      'channel.health': (ev, hd) => { if (hd.isShown()) soon('channels', '/api/notify/channels'); else drop('channels'); },
      'wake.paused': (ev) => { reloadState(); pageEvent(ev, true); },
      'wake.resumed': (ev) => { reloadState(); pageEvent(ev, true); },
      'wake.breaker': () => reloadState(),
    },
  });
  M.handle = handle;
  function refreshDetail(ev) {
    const id = ev && ev.data && ev.data.message;
    if (id && D['msg:' + id]) soon('msg:' + id, '/api/messages/' + enc(id));
  }
  function deliveryEvent(ev, hd) {
    if (!hd.isShown()) { reloadInbox(); return; }
    if (typeof M.inboxQs === 'string') soon('inbox:' + M.inboxQs, inboxPath(M.inboxQs), 300);
    refreshDetail(ev);
  }
  /** The persona an event is about: data.persona, a stop's scope, else the display name the server puts in agent
   * (WakeManager._publish sets agent to her display name, "Seraphina", and wake.decided, wake.skipped, wake.deferred,
   * wake.yielded and wake.failed carry no persona), mapped back to her name through the schedule or a loaded page. */
  function personaOf(ev) {
    const d = ev && isObj(ev.data) ? ev.data : {};
    if (d.persona) return String(d.persona);
    if (d.scope && d.scope !== 'all') return String(d.scope);
    const a = String((ev && ev.agent) || d.agent || '');
    if (!a) return null;
    const p = arr((val('wakes') || {}).personas).find((x) => x.name === a || String(x.display || '') === a);
    if (p) return p.name;
    for (const k of Object.keys(D)) {
      const v = k.indexOf('page:') === 0 ? D[k].v : null;
      if (v && isObj(v.persona) && (v.persona.name === a || String(v.persona.display || '') === a)) return k.slice(5);
    }
    return null;
  }
  /** Read her page again, or every loaded page when the event names nobody and all is set (a stop of everyone). */
  function pageEvent(ev, all) {
    const name = personaOf(ev);
    const keys = name ? ['page:' + name] : all ? Object.keys(D).filter((k) => k.indexOf('page:') === 0) : [];
    for (const k of keys) {
      if (!D[k]) continue;
      if (handle.isShown()) soon(k, '/api/personas/' + enc(k.slice(5)), 300); else drop(k);
    }
  }
  /** Wake and persona events carry ids, never text: read the schedule (and her page) again. These are the types the
   * server publishes (wakes.py WakeManager._publish); reminders and the integrity stop come as notify.queued. */
  SF.on(['wake.requested', 'wake.coalesced', 'wake.deferred', 'wake.skipped', 'wake.started', 'wake.yielded', 'wake.decided', 'wake.failed'], (ev) => {
    if (handle.isShown()) soon('wakes', '/api/wakes', 400); else drop('wakes');
    pageEvent(ev, false);
  });
  SF.on(['persona.updated', 'persona.drift'], (ev) => {
    pageEvent(ev, false);
    if (handle.isShown()) soon('wakes', '/api/wakes', 400); else drop('wakes');
  });
  SF.onReconnect(() => { for (const k of Object.keys(D)) if (!D[k].p) D[k].at = 0; loadUnread(); if (handle.isShown()) refresh(); });
  try { SF.extend('chatHead', chatChip); } catch (e) { /* this build has no chat meta row extension: the switch is on #/schedule/chats */ }

  M.ready = true;
})(window.SF);
