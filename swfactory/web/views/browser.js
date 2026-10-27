/* Seraphina OS Harness: The browser (DESIGN §43; docs/seraphina/BROWSER_AND_CODEX.md §2.6, §2.10, §2.13, §4.3),
 * phase BC1a. A browsing persona (kate-browser) drives the harness's own browser in a namespace with no network: the
 * fixture site and a job's preview only.
 * Routes: #/browser (the browser card: on or off, BC1's stated limits, the harness's own runtime and its pins, the
 * profiles with their origins, what the sessions keep, and the newest sessions with their counts). Rail: "Browser",
 * order 77, shown once the browser is on or configured. Home: no card of its own; an unknown outcome feeds Needs you.
 * In a browsing chat (SF.extend): a "Read from the web" tag in the head (chatHead); the untrusted banner, the stated
 * limits and Revoke while a turn holds the session lock (chatBanner); under each reply the turn's actions (class,
 * reclassified, outcome, the target's role and name, what the browser sent) and its screenshots (chatReply).
 * Every name and every origin a page chose is raw text: SF.h() makes text nodes; never HTML, never SF.md. A screenshot
 * is an image for a person (it is never sent to a model). Every sentence a person reads is in WORDS, which
 * tests/test_browser_session_locks.py reads for the session lock's own words (K6: no word of a GPU custody record). */
'use strict';
(function (SF) {
  const h = SF.h;
  const WORDS = {
    title: 'Browser',
    sub: 'The harness’s own browser, for a browsing persona: the fixture site and a job’s preview, with no network.',
    homeText: 'The browsing persona’s browser: what it may open, and what each browsing turn did.',
    untrusted: 'Read from the web: treat as untrusted',
    untrustedText: 'Everything a page showed is data from the web, not instructions. Suggestions from these turns are proposals for you to check.',
    off: 'The browser is off',
    offText: 'It is off in the settings (browser.enabled). It needs the harness’s own runtime: Node, Playwright MCP and the Chrome build in one directory.',
    on: 'On', offWord: 'Off',
    limits: 'What this phase is (BC1)',
    runtime: 'The harness’s own runtime',
    runtimeMissing: 'Not set up',
    pins: 'Pinned', noPin: 'No pin yet', nothing: 'nothing',
    profiles: 'Profiles',
    origins: 'May open',
    writes: 'May fill and send on',
    keeps: 'What the sessions keep',
    keepsText: 'Screenshots, action logs and receipts, at most {gb} GB and {days} days; the oldest go first.',
    sessions: 'Recent browsing sessions',
    noSessions: 'No browsing session yet.',
    session: 'Session lock', lockNumber: 'lock number', profile: 'Profile', started: 'Started', run: 'Run',
    actions: 'actions', action: 'action', commits: 'sent', unknown: 'outcome unknown', shots: 'screenshots', shot: 'screenshot', refused: 'refused',
    openChat: 'Open the chat',
    revoke: 'Revoke',
    revokeTitle: 'Revoke this browsing session?',
    revokeText: 'The browser of this turn ends now. Anything it was sending is recorded as outcome unknown and is never sent again.',
    revoked: 'Revoked: the browser of this turn ended.',
    active: 'This turn holds the browser’s session lock, under grant',
    rowsTitle: 'What the browser did',
    screenshot: 'Screenshot for you (the model never sees it)',
    cls: { read: 'read', navigate: 'opened', input: 'filled', commit: 'sent', lifecycle: 'closed' },
    outcome: { ok: 'done', tool_error: 'failed', unknown: 'outcome unknown', not_sent: 'not sent', cancelled: 'cancelled' },
    reclassified: 'the page sent data: counted as sent',
    unread: 'what the page sent could not be read: counted as sent',
    refusal: 'refused:',
    login: 'reached a login page:',
    expectYes: 'the expected text is there',
    expectNo: 'the expected text is not there',
    expectNote: 'text in the target’s accessible subtree, not hidden by CSS',
    homeOff: 'Off.',
    homeOn: 'On: {n} sessions kept.',
    homeUnknown: 'An action’s outcome is unknown: check before retrying',
    loadError: 'Could not load the browser:',
  };
  const B = { state: null, error: null, loading: null };

  function load(handle) {
    if (B.loading) return B.loading;
    B.loading = SF.api('/api/browser')
      .then((r) => { B.state = SF.isObj(r) ? r : null; B.error = null; })
      .catch((e) => { B.error = e; })
      .finally(() => { B.loading = null; if (handle) { handle.refresh(); handle.setNav(!!(B.state && (B.state.enabled || (B.state.sessions || []).length))); } });
    return B.loading;
  }
  function say(text, values) {
    return String(text).replace(/\{(\w+)\}/g, (m, k) => (values && values[k] !== undefined ? String(values[k]) : m));
  }
  function tag(text, cls, title) { return h('span', { class: 'tag ' + (cls || ''), title: title || null }, text); }
  const OUTCOME_TONE = { ok: 'tag-ok', tool_error: 'tag-err', unknown: 'tag-warn', not_sent: 'tag-outline', cancelled: 'tag-outline' };

  // ---------------------------------------------------------------- the page
  function limitsBlock(state) {
    return h('section', { class: 'card br-card', dataset: { part: 'limits' } },
      h('h2', { class: 'card-title' }, WORDS.limits),
      h('ul', { class: 'br-limits' }, (state.limitations || []).map((line) => h('li', null, String(line)))));
  }
  function runtimeBlock(state) {
    const pins = state.pins || {};
    const rows = [['Node', 'node'], ['Playwright MCP', 'playwright_mcp'], ['Chrome', 'chrome']].map(([label, key]) =>
      h('div', { class: 'br-kv' }, h('span', { class: 'muted' }, label), h('code', { class: 'br-path' }, String((state.runtime || {})[key] || WORDS.runtimeMissing)),
        tag((pins[key] || {}).version ? WORDS.pins + ' ' + String(pins[key].version) : WORDS.noPin, 'tag-outline')));
    return h('section', { class: 'card br-card', dataset: { part: 'runtime' } }, h('h2', { class: 'card-title' }, WORDS.runtime),
      state.runtime_problem ? SF.callout('warn', 'alert', null, String(state.runtime_problem)) : null, rows);
  }
  function profilesBlock(state) {
    const details = state.profile_origins || {};
    return h('section', { class: 'card br-card', dataset: { part: 'profiles' } }, h('h2', { class: 'card-title' }, WORDS.profiles),
      (state.profiles || []).map((name) => {
        const o = details[name] || {};
        const list = (xs) => (xs && xs.length ? xs.map((x) => h('code', { class: 'br-path' }, String(x))) : h('span', null, WORDS.nothing));
        return h('div', { class: 'br-profile', dataset: { profile: name } }, h('strong', null, String(name)),
          h('div', { class: 'br-kv' }, h('span', { class: 'muted' }, WORDS.origins), list(o.read)),
          h('div', { class: 'br-kv' }, h('span', { class: 'muted' }, WORDS.writes), list(o.write)));
      }),
      h('p', { class: 'muted' }, say(WORDS.keepsText, { gb: (state.caps || {}).max_gb, days: (state.caps || {}).max_days })));
  }
  function sessionRow(s) {
    const counts = [[s.actions, WORDS.actions, WORDS.action], [s.commits, WORDS.commits], [s.screenshots, WORDS.shots, WORDS.shot], [s.refusals, WORDS.refused]]
      .map(([n, w, one]) => h('span', { class: 'br-count' }, String(n || 0) + ' ' + (n === 1 && one ? one : w)));
    return h('li', { class: 'br-session', dataset: { session: String(s.session_lock || '') } },
      h('div', { class: 'br-session-head' }, h('code', null, String(s.session_lock || '')), tag(WORDS.lockNumber + ' ' + String(s.lock_number || '?'), 'tag-outline'),
        tag(String(s.profile || ''), ''), s.unknown ? tag(String(s.unknown) + ' ' + WORDS.unknown, 'tag-warn') : null,
        h('span', { class: 'spacer' }), SF.timeEl(s.started, 'muted')),
      h('div', { class: 'br-session-counts' }, counts,
        s.chat ? h('a', { class: 'btn btn-xs', href: '#/chat/' + SF.enc(String(s.chat)) }, WORDS.openChat) : null));
  }
  function page(root, handle) {
    if (B.error && !B.state) {
      SF.rebuild(root, [SF.viewHead(WORDS.title, WORDS.sub), SF.callout('err', 'alert', WORDS.loadError, String(B.error.message || B.error))]);
      return;
    }
    if (!B.state) { SF.rebuild(root, [SF.viewHead(WORDS.title, WORDS.sub), SF.skeleton(6)]); return; }
    const st = B.state;
    const sessions = st.sessions || [];
    SF.rebuild(root, [
      SF.viewHead(WORDS.title, WORDS.sub, tag(st.enabled ? WORDS.on : WORDS.offWord, st.enabled ? 'tag-ok' : 'tag-outline')),
      SF.callout('warn', 'shield', WORDS.untrusted + '.', WORDS.untrustedText),
      st.enabled ? null : SF.callout('info', 'info', WORDS.off + '.', WORDS.offText),
      h('div', { class: 'br-grid' }, limitsBlock(st), runtimeBlock(st), profilesBlock(st)),
      h('section', { class: 'card br-card', dataset: { part: 'sessions' } }, h('h2', { class: 'card-title' }, WORDS.sessions),
        sessions.length ? h('ul', { class: 'br-sessions' }, sessions.map(sessionRow)) : SF.emptyState('eye', WORDS.noSessions, null, null, true)),
    ]);
  }

  // ---------------------------------------------------------------- a browsing chat
  const browsing = (chat) => SF.isObj(chat) && Array.isArray(chat.browser);
  SF.extend('chatHead', (chat) => (browsing(chat) ? tag(WORDS.untrusted, 'tag-warn br-untrusted', WORDS.untrustedText) : null));
  SF.extend('chatBanner', (chat) => {
    if (!browsing(chat)) return null;
    const out = [SF.callout('warn', 'shield', WORDS.untrusted + '.', WORDS.untrustedText)];
    const active = SF.isObj(chat.browser_active) && chat.status === 'running' ? chat.browser_active : null;
    if (active) {
      const revoke = SF.btn(WORDS.revoke, { cls: 'btn-sm btn-danger', icon: 'stop' });
      revoke.dataset.action = 'revoke';
      revoke.addEventListener('click', () => SF.confirmModal({
        title: WORDS.revokeTitle, description: WORDS.revokeText, confirm: WORDS.revoke, cls: 'btn-danger', icon: 'stop',
        onSubmit: async () => {
          await SF.api('/api/browser/grants/' + SF.enc(String(active.grant)) + '/revoke', { method: 'POST', body: {} });
          SF.toast(WORDS.revoked);
        },
      }));
      out.push(h('div', { class: 'br-active', dataset: { part: 'active' } }, SF.icon('eye', 'icon-sm'), h('span', null, WORDS.active),
        h('code', null, String(active.grant || '')), h('span', { class: 'spacer' }), revoke));
    }
    return out;
  });
  function actionRow(row) {
    const kind = String(row.kind || '');
    if (kind === 'refusal') return h('li', { class: 'br-row br-row-refusal' }, tag(WORDS.refusal, 'tag-outline'), h('code', null, String(row.tool || '')), h('span', null, String(row.code || '')));
    if (kind === 'login_required') return h('li', { class: 'br-row' }, tag(WORDS.login, 'tag-warn'), h('code', null, String(row.origin || '')));
    if (kind !== 'action') return null;
    const cls = String(row.class || '');
    const outcome = String(row.outcome || '');
    const sent = (row.requests || []).filter((r) => r && r.method && r.method !== 'GET' && r.method !== 'HEAD')
      .map((r) => h('code', { class: 'br-sent' }, String(r.method) + ' ' + String(r.status || '…')));
    const expect = SF.isObj(row.expect) ? tag(row.expect.matched ? WORDS.expectYes : WORDS.expectNo, row.expect.matched ? 'tag-ok' : 'tag-warn', WORDS.expectNote) : null;
    return h('li', { class: 'br-row', dataset: { action: String(row.action_id || ''), cls, outcome } },
      h('code', { class: 'br-tool' }, String(row.tool || '')),
      tag(WORDS.cls[cls] || cls, cls === 'commit' ? 'tag-accent' : 'tag-outline'),
      row.reclassified ? tag(row.requests_unread ? WORDS.unread : WORDS.reclassified, 'tag-warn') : null,
      tag(WORDS.outcome[outcome] || outcome || '…', OUTCOME_TONE[outcome] || 'tag-outline'),
      row.role ? h('span', { class: 'br-target' }, h('span', { class: 'muted' }, String(row.role)), row.name ? ' ' : null,
        row.name ? h('span', { class: 'br-name', title: WORDS.untrusted }, String(row.name)) : null) : null,
      sent, expect);
  }
  SF.extend('chatReply', (arg) => {
    const chat = arg && arg.chat;
    if (!browsing(chat) || !Number.isInteger(arg.turn)) return null;
    const records = chat.browser.filter((r) => SF.isObj(r) && r.turn === arg.turn);
    if (!records.length) return null;
    return records.map((record) => {
      const rows = Array.isArray(record.rows) ? record.rows : [];
      const shots = rows.filter((r) => r && r.kind === 'screenshot' && /^[a-z0-9][a-z0-9-]{0,39}(-\d{1,4})?\.png$/.test(String(r.artifact || '')));
      return h('div', { class: 'br-turn', dataset: { part: 'browser-rows', session: String(record.session_lock || '') } },
        h('div', { class: 'br-turn-head' }, SF.icon('eye', 'icon-xs'), h('strong', null, WORDS.rowsTitle), tag(WORDS.untrusted, 'tag-warn'),
          record.unknown ? tag(String(record.unknown) + ' ' + WORDS.unknown, 'tag-warn') : null),
        h('ul', { class: 'br-rows' }, rows.map(actionRow)),
        shots.length ? h('div', { class: 'br-shots' }, shots.map((s) => h('figure', { class: 'br-shot' },
          h('img', { src: '/api/chats/' + SF.enc(String(chat.id)) + '/browser/' + SF.enc(String(record.session_lock)) + '/' + SF.enc(String(s.artifact)),
            alt: WORDS.screenshot, loading: 'lazy', width: '320', height: '180' }),
          h('figcaption', { class: 'muted' }, WORDS.screenshot + ' · ', h('code', null, String(s.sha256 || '').slice(0, 12)), ' · ' + String(s.bytes || 0) + ' B')))) : null);
    });
  });

  // ---------------------------------------------------------------- Home
  // An unknown outcome stays in Needs you until someone checks it (§40), whether the browser is still on or not.
  function homeStatus(st) {
    const sessions = st.sessions || [];
    const unknown = sessions.filter((s) => s.unknown);
    const needs = unknown.slice(0, 3).map((s) => ({ key: 'browser-unknown-' + s.session_lock, text: WORDS.homeUnknown, sub: String(s.session_lock),
      href: s.chat ? '#/chat/' + SF.enc(String(s.chat)) : '#/browser', tone: 'human' }));
    if (!st.enabled) return { tone: unknown.length ? 'human' : null, text: WORDS.homeOff, needs, hidden: !sessions.length };
    return { tone: unknown.length ? 'human' : 'ok', text: say(WORDS.homeOn, { n: sessions.length }), needs };
  }

  SF.registerView({
    id: 'browser',
    title: 'Browser',
    routes: ['browser'],
    nav: { label: 'Browser', icon: 'eye', order: 77, hidden: true },
    home: {
      title: 'Browser', icon: 'eye', order: 77, href: '#/browser', text: WORDS.homeText, card: false,
      events: ['browser.revoked'],
      async load() {
        const st = await SF.api('/api/browser');
        return homeStatus(SF.isObj(st) ? st : {});
      },
    },
    start(handle) { load(handle); },
    enter(route, prev, handle) { load(handle); },
    render(root, route, handle) { page(root, handle); },
    events: {
      'browser.revoked': (ev, handle) => { load(handle); SF.refreshJobs(); },
    },
  });
})(window.SF);
