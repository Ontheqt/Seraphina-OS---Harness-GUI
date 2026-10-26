/* Seraphina OS Harness: Personas (DESIGN §33 "Open WebUI profiles (Kate) and personas" with its capability map; §15
 * "Chats without a repository, personas and Open WebUI profiles"; §35 "A new persona from an Open WebUI profile";
 * §36's operator mark, shown read-only here).
 * Routes: #/personas (the list: Open WebUI personas, offline copies, personas that wake themselves; Import; Check
 * now) and #/personas/<name> (an Open WebUI or offline persona's profile card page; #/personas/<name>/card is the same
 * page, and a workspace model's id finds the persona bound to it, so the tracker's drift items can link here). A wake
 * persona's own page is the Messages area's, #/schedule/persona/<name>. Rail: "Personas" beside Agents. Home: its card.
 * It adds to the Software Factory's chats through SF.extend: a persona start in New chat (chatStart); the persona's
 * label, version, base model and what does not come through the API in a chat's head (chatHead); her warnings, drift
 * and stale-copy banners and the "Start a chat with …" button of a refused turn (chatBanner); under each reply the
 * imported copy's label, a newer version's mark and the Open WebUI time window (chatReply).
 * A view module: it reaches the page only through window.SF. Everything from Open WebUI, a persona file, a reply or a
 * server error is raw text: SF.h() makes text nodes; never HTML, never SF.md, never in a page title or the URL. */
'use strict';
(function (SF) {
  const h = SF.h;
  const isObj = SF.isObj;
  const NAME_RE = /^[a-z0-9][a-z0-9-]{0,40}$/;
  const RESERVED = ['import', 'check'];
  const MAX_BODY = 1024 * 1024;   // the server takes at most 1 MB per request (its bytes: UTF-8)
  const TOO_BIG = 'The file is larger than 1 MB even without its pictures: export only this one model in Open WebUI and try again.';
  const bodyBytes = (b) => new Blob([JSON.stringify(b)]).size;
  const KIND = {
    openwebui: { word: 'Through Open WebUI', icon: 'cloud', cls: 'tag-accent', title: 'Her prompt, settings, skills and filters stay in Open WebUI and apply live on every call.' },
    snapshot: { word: 'Offline copy', icon: 'server', cls: 'tag-local', title: 'A copy that runs on this machine’s own model, not through Open WebUI. It does not follow later changes.' },
    wake: { word: 'Wakes herself', icon: 'bell', cls: 'tag-outline', title: 'She is never a chat: she checks in on her own and messages you (Messages & Wakes).' },
  };
  const GROUPS = [
    ['openwebui', 'Through Open WebUI', 'Chats here reach these personas through Open WebUI, which applies their prompt and settings live.'],
    ['snapshot', 'Offline copies', 'Imported copies that run on this machine’s own model. Each reply says it came from the copy.'],
    ['wake', 'Personas that wake themselves', 'They are never a chat: they check in on their own and message you. Their pages are in Messages & Wakes.'],
  ];
  const DRIFT = {
    new: ['first look', 'tag-outline', 'This was the first look at it: the baseline later checks compare with.'],
    none: ['unchanged', 'tag-ok', 'Open WebUI lists it as it was at the last look.'],
    cosmetic: ['cosmetic change', 'tag-outline', 'Only its name, description or pictures changed: it behaves the same.'],
    behaviour: ['changed in Open WebUI', 'tag-warn', 'What it does changed (its tools, filters, knowledge, base model or settings).'],
    unknown: ['may have changed', 'tag-warn', 'May be cosmetic: this key cannot read the prompt and settings to tell.'],
  };
  const OPERATOR_HELP = SF.words.operatorHelp;
  const SURFACE_ICON = { openwebui: 'cloud', 'harness-chat': 'chat', 'offline-copy': 'server', browsing: 'search', wake: 'bell' };
  const DELEGATION_WORDS = { act: 'Act on results', code: 'Code' };

  const P = {
    rows: null, error: null, loading: null, at: 0,
    pages: new Map(),          // name → {data, error, loading, at}: GET /api/personas/<name>/card
    check: null,               // the last Check now: {at, scope, rows} or {at, scope, error}
    checking: null,            // the scope a check is running for ('' for all)
    turns: new Map(),          // '<chat>:<run>' → persona.turn data (the version that answers a live turn)
    windows: new Map(),        // '<chat>:<run>' → persona.window data
    warnOpen: new Map(),       // '<chat>:<warning hash>' → whether its details are open (a phone starts them closed)
    handle: null, reloadTimer: 0,
    beforeImport: new Map(),   // name → the Changes before import form's state (what was typed, who attaches, its error)
  };

  // ================================================================ data
  /** GET /api/personas → {personas: [persona_view rows]} (server.api_personas, personas.persona_view). */
  function loadRows(force) {
    if (P.loading) return P.loading;
    if (!force && P.rows && Date.now() - P.at < 2000) return Promise.resolve(P.rows);
    P.loading = SF.api('/api/personas').then((r) => {
      P.rows = r && Array.isArray(r.personas) ? r.personas.filter((x) => isObj(x) && typeof x.name === 'string') : [];
      P.error = null;
      P.at = Date.now();
      return P.rows;
    }).catch((e) => { P.error = e; return P.rows; })
      .finally(() => { P.loading = null; if (P.handle) P.handle.refresh(); SF.refreshJobs(); });
    return P.loading;
  }
  function reloadSoon() {
    clearTimeout(P.reloadTimer);
    P.reloadTimer = setTimeout(() => {
      loadRows(true);
      const r = P.handle && P.handle.route();
      const t = r && r.parts[0] ? target(r.parts[0]) : null;
      if (t && t.row && t.row.kind !== 'wake') loadPage(t.row.name, true);
    }, 250);
  }
  const rowOf = (name) => (P.rows || []).find((r) => r.name === name) || null;
  /** A route's name: a persona, else the persona bound to that workspace model (an Open WebUI one first). */
  function target(name) {
    const row = rowOf(name);
    if (row) return { row, name: row.name, via: null };
    const bound = (P.rows || []).filter((r) => isObj(r.binding) && r.binding.model === name && r.kind !== 'wake')
      .sort((a, b) => (a.kind === 'openwebui' ? 0 : 1) - (b.kind === 'openwebui' ? 0 : 1));
    return bound.length ? { row: bound[0], name: bound[0].name, via: name, others: bound.slice(1) } : { row: null, name: null, via: null };
  }
  /** GET /api/personas/<name>/card → {persona, card, live, drift, appended_text, capability_map, import}. */
  function loadPage(name, force) {
    if (!name || !NAME_RE.test(name)) return;
    const cur = P.pages.get(name) || { data: null, error: null, loading: false, at: 0 };
    if (cur.loading || (!force && cur.data && Date.now() - cur.at < 2000)) return;
    cur.loading = true;
    P.pages.set(name, cur);
    SF.api('/api/personas/' + SF.enc(name) + '/card').then((r) => { cur.data = isObj(r) ? r : null; cur.error = null; cur.at = Date.now(); })
      .catch((e) => { cur.error = e; })
      .finally(() => { cur.loading = false; if (P.handle) P.handle.refresh(); });
  }
  const modelId = (row) => (row && isObj(row.binding) && typeof row.binding.model === 'string' ? row.binding.model : '');
  /** Whether a chat can start with the row, and why not (DESIGN §15: kind openwebui or snapshot, no error, an
   *  offline copy only with its chats enabled; the server still decides). */
  function canChat(row) {
    if (!row) return { ok: false, why: 'unknown persona' };
    if (row.kind === 'wake') return { ok: false, why: 'She wakes herself and is never a chat.' };
    if (row.error) return { ok: false, why: 'This persona file has a problem: ' + row.error };
    if (row.kind === 'snapshot' && row.offline === false) return { ok: false, why: 'Offline copy not enabled: turn its chats on first.' };
    return { ok: true, why: '' };
  }
  /** Text from the server as a sentence: a full stop added when it has none (it stays the server's words). */
  const sentence = SF.sentence;
  const shortV = (v) => (v === null || v === undefined || v === '' ? 'unknown' : String(v).slice(0, 12));
  const wakeHref = (name) => '#/schedule/persona/' + SF.enc(name);
  const personaHref = (row) => (row && row.kind === 'wake' ? wakeHref(row.name) : '#/personas/' + SF.enc(row ? row.name : ''));
  /** "Kate" for the text "answered by a newer Kate": Open WebUI's name when a chat has it, else the persona's. */
  const title = (s) => (s ? String(s).charAt(0).toUpperCase() + String(s).slice(1) : '');

  // ================================================================ small pieces
  function tag(text, cls, attrs) { return h('span', Object.assign({ class: 'tag ' + (cls || '') }, attrs || {}), text); }
  function kindTag(row) {
    const k = KIND[row.kind] || { word: row.kind, icon: 'persona', cls: 'tag-outline', title: '' };
    return tag([SF.icon(k.icon, 'icon-xs'), k.word], k.cls + ' pe-kind', { title: k.title, dataset: { kind: row.kind } });
  }
  /** A persona file's privacy line. Not for a wake persona: hers is set by the owner's clean tick on her page, and a
   *  privacy: key in her file means nothing (DESIGN §35). */
  function privacyTag(row) {
    if (row.kind === 'wake') return null;
    const clean = row.privacy === 'clean';
    return tag(clean ? 'clean' : 'private', clean ? 'tag-outline' : 'tag-outline pe-private', {
      title: clean ? 'Marked clean: what her replies suggest may be given to an outside agent.' : 'Private (the default): what her replies suggest is never given to an outside agent.',
      dataset: { privacy: clean ? 'clean' : 'private' },
    });
  }
  function operatorTag(row) {
    if (row.role !== 'operator') return null;
    return tag([SF.icon('shield', 'icon-xs'), 'Operator'], 'tag-accent pe-operator', { title: OPERATOR_HELP });
  }
  function operatorNote(row) {
    if (row.role !== 'operator') return null;
    return h('p', { class: 'pe-operator-note' }, SF.icon('shield', 'icon-xs'), h('span', null, h('strong', null, 'Operator. '), OPERATOR_HELP, ' The mark is set in Settings or on the Operator page.'));
  }
  const itemLink = (ref, cls) => SF.itemLink(ref, 'pe-item' + (cls ? ' ' + cls : ''));
  /** The drift chip: drift.kind in words, when it changed, and its tracker item. */
  function driftLine(drift) {
    if (!isObj(drift) || !drift.kind) return h('span', { class: 'sf-muted pe-drift' }, 'Not checked yet');
    const d = DRIFT[drift.kind] || [drift.kind, 'tag-outline', ''];
    const when = [drift.changed_at ? ['changed ', SF.timeEl(drift.changed_at)] : null, drift.changed_at && drift.seen_at ? ', ' : null,
      drift.seen_at ? ['checked ', SF.timeEl(drift.seen_at)] : null];
    return h('span', { class: 'pe-drift', dataset: { drift: drift.kind } }, h('span', { class: 'pe-drift-now' }, tag(d[0], d[1], { title: d[2] }), drift.item ? itemLink(drift.item) : null),
      drift.changed_at || drift.seen_at ? h('span', { class: 'sf-muted pe-drift-when' }, when) : null);
  }
  function chips(ids, cls) {
    return (ids || []).length ? h('span', { class: 'pe-chips' }, ids.map((x) => h('code', { class: 'pe-chip' + (cls ? ' ' + cls : '') }, String(x)))) : null;
  }
  function kv(pairs) {
    const dl = h('dl', { class: 'sf-kv pe-kv' });
    for (const [k, v] of pairs) {
      if (v === null || v === undefined || v === false || (Array.isArray(v) && !v.length)) continue;
      dl.appendChild(h('dt', null, k));
      dl.appendChild(h('dd', null, v));
    }
    return dl;
  }
  function section(id, titleText, sub, body, actions) {
    return h('section', { class: 'pe-sec', 'aria-labelledby': 'pe-' + id + '-t', dataset: { sec: id } },
      h('div', { class: 'pe-sec-head' }, h('h2', { class: 'pe-sec-title', id: 'pe-' + id + '-t', tabindex: '-1' }, titleText), h('span', { class: 'spacer' }), actions || null),
      sub ? h('p', { class: 'pe-sec-sub' }, sub) : null, body);
  }
  /** Links that help with a refusal: the provider and the key, the preset, the Doctor. */
  function helpLinks(msg) {
    const m = String(msg || '');
    const links = [];
    if (/OPENWEBUI_API_KEY|API keys?\b|this key|the key\b/i.test(m)) links.push(['#/setup/api-keys', 'Setup › API keys']);
    if (/preset|models\.|not on this machine|loopback/i.test(m)) links.push(['#/settings/routing', 'Settings › Routing (the openwebui preset)']);
    if (/provider|cannot reach|not on this machine|base_url/i.test(m)) links.push(['#/settings/providers', 'Settings › Providers']);
    if (/mcp_servers/.test(m)) links.push(['#/settings/mcp_servers', 'Settings › MCP servers']);
    links.push(['#/doctor', 'Doctor']);
    return h('span', { class: 'pe-help-links' }, links.map(([href, label], i) => [i ? ' · ' : '', h('a', { href }, label)]));
  }
  function errorBox(titleText, e) {
    const msg = e && e.message ? e.message : String(e || '');
    return h('div', { class: 'callout callout-err pe-err', role: 'alert' }, SF.icon('alert', 'icon-sm'),
      h('div', null, h('strong', null, titleText), ' ', h('span', { class: 'pe-err-text' }, msg), h('div', { class: 'pe-err-help' }, helpLinks(msg))));
  }

  // ================================================================ actions
  async function startChat(name, button) {
    if (button) SF.setBusy(button, true);
    try {
      const r = await SF.api('/api/chats', { method: 'POST', body: { persona: name } });
      if (!r || !r.chat || !r.chat.id) throw new Error('The server returned no chat.');
      SF.go('#/chat/' + SF.enc(r.chat.id));
    } catch (e) {
      SF.openModal({
        title: 'The chat did not start',
        description: 'Nothing was sent to a model. The harness says why:',
        body: [h('p', { class: 'pe-err-text pe-modal-err' }, e && e.message ? e.message : String(e)), h('p', { class: 'field-hint' }, helpLinks(e && e.message))],
        actions: [{ label: 'Close', value: 'cancel' }],
      });
    } finally {
      if (button && button.isConnected) SF.setBusy(button, false);
    }
  }
  const CHECK_WORDS = {
    new: 'first look: this is the baseline later checks compare with',
    none: 'no change',
    cosmetic: 'a cosmetic change only (name, description or pictures)',
    behaviour: 'what it does changed',
    unknown: 'may be cosmetic: this key cannot read the prompt and settings',
  };
  function checkWords(row) {
    if (row.error) return String(row.error);
    let words = CHECK_WORDS[row.kind] || String(row.kind || '');
    if (row.kind === 'behaviour' && row.version_id && row.version_id === row.previous_version_id) {
      words += ': Open WebUI lists it differently without a new version (the admin’s Model Defaults, for example)';
    } else if (row.kind === 'behaviour' || row.kind === 'cosmetic' || row.kind === 'unknown') {
      words += ' (version ' + shortV(row.previous_version_id) + ' → ' + shortV(row.version_id) + ')';
    }
    if (row.stale) words += '; this offline copy is of another version: re-import it to follow Open WebUI';
    return words;
  }
  /** POST /api/personas/check {persona?} → {checked: [...]}; persona.drift events follow for changes. */
  async function checkNow(name, button) {
    if (P.checking !== null) return;
    P.checking = name || '';
    if (button) SF.setBusy(button, true);
    if (P.handle) P.handle.refresh();
    try {
      const r = await SF.api('/api/personas/check', { method: 'POST', body: name ? { persona: name } : {} });
      P.check = { at: new Date().toISOString(), scope: name || '', rows: r && Array.isArray(r.checked) ? r.checked : [] };
      const changed = P.check.rows.filter((x) => x.kind === 'behaviour' || x.kind === 'unknown').length;
      SF.toast(changed ? SF.plural(changed, 'persona') + ' changed in Open WebUI: see the result.' : 'Checked: ' + SF.plural(P.check.rows.length, 'persona') + '.', { kind: changed ? 'info' : 'ok' });
    } catch (e) {
      P.check = { at: new Date().toISOString(), scope: name || '', error: e };
      SF.toastError('Check failed', e);
    } finally {
      P.checking = null;
      loadRows(true);
      if (name) loadPage(name, true);
      if (P.handle) P.handle.refresh();
    }
  }
  function checkResult(scope) {
    const c = P.check;
    if (!c || c.scope !== scope) return null;
    if (c.error) return errorBox('The check did not run:', c.error);
    const rows = c.rows || [];
    return h('div', { class: 'pe-check sf-card', role: 'status', dataset: { check: scope || 'all' } },
      h('div', { class: 'pe-check-head' }, SF.icon('refresh', 'icon-sm'), h('strong', null, 'Checked ', SF.timeEl(c.at)), h('span', { class: 'sf-muted' }, ' · ' + SF.plural(rows.length, 'persona') + ' against Open WebUI')),
      rows.length ? h('ul', { class: 'pe-check-list' }, rows.map((x) => h('li', { class: 'pe-check-row' + (x.error ? ' is-err' : x.kind === 'behaviour' || x.kind === 'unknown' ? ' is-warn' : ''), dataset: { persona: String(x.persona || '') } },
        h('span', { class: 'pe-check-name' }, h('a', { href: '#/personas/' + SF.enc(x.persona || '') }, String(x.persona || '?')), h('code', { class: 'pe-chip' }, String(x.model || ''))),
        h('span', { class: 'pe-check-words' }, checkWords(x)),
        x.item ? itemLink(x.item) : null)))
        : h('p', { class: 'sf-muted' }, 'No persona is bound to an Open WebUI workspace model.'));
  }
  /** The offline switch: POST /api/config {agents: {<name>: {offline: true|false}}} (decision e of §35). */
  async function setOffline(row, on, input) {
    input.disabled = true;
    try {
      await SF.api('/api/config', { method: 'POST', body: { agents: { [row.name]: { offline: !!on } } } });
      SF.toast(on ? 'Chats with this offline copy are on. Every reply says it came from the copy.' : 'Chats with this offline copy are off.', { kind: 'ok' });
    } catch (e) {
      input.checked = !on;
      SF.toastError('Could not change it', e);
    } finally {
      input.disabled = false;
      loadRows(true);
    }
  }
  function offlineSwitch(row) {
    if (row.kind !== 'snapshot') return null;
    const id = 'pe-off-' + row.name;
    const inp = h('input', { type: 'checkbox', id, checked: row.offline === true, dataset: { focus: 'offline:' + row.name } });
    inp.addEventListener('change', () => setOffline(row, inp.checked, inp));
    // The label asks; the tick says whether it is allowed (a label that states "on" would contradict an unticked box).
    return h('div', { class: 'pe-switch' }, h('label', { class: 'check', for: id }, inp, h('span', null, 'Allow chats with this offline copy')),
      h('div', { class: 'field-hint' }, 'It runs on this machine’s own model, never through Open WebUI, and labels every reply. Not ticked: no chat runs on it.'));
  }

  // ================================================================ the list
  function personaCard(row) {
    const can = canChat(row);
    const tid = 'pe-card-' + row.name + '-t';
    const actions = [];
    if (row.kind === 'wake') {
      actions.push(h('a', { class: 'btn btn-sm', href: wakeHref(row.name), dataset: { focus: 'page:' + row.name } }, SF.icon('bell', 'icon-sm'), 'Her page'));
    } else {
      if (can.ok) actions.push(SF.btn('Chat', { cls: 'btn-sm btn-primary', icon: 'chat', dataset: { focus: 'chat:' + row.name, action: 'chat' }, onClick: (e) => startChat(row.name, e.currentTarget) }));
      actions.push(h('a', { class: 'btn btn-sm', href: '#/personas/' + SF.enc(row.name), dataset: { focus: 'card:' + row.name, action: 'card' } }, SF.icon('file', 'icon-sm'), 'Profile card'));
    }
    const id = modelId(row);
    const facts = [];
    if (row.kind !== 'wake') facts.push(['Runs on', row.model ? h('code', null, String(row.model)) : h('span', { class: 'sf-muted' }, 'no models entry')]);
    if (id) facts.push([row.kind === 'snapshot' ? 'Copied from' : 'Workspace model', h('span', null, h('code', null, id), row.provider ? h('span', { class: 'sf-muted' }, ' on ' + row.provider) : null)]);
    if (row.kind === 'snapshot' && isObj(row.binding)) facts.push(['Copy of', 'version ' + shortV(row.binding.version_id) + (row.binding.imported_at ? ', imported ' + String(row.binding.imported_at).slice(0, 10) : '')]);
    if (row.kind === 'wake' && isObj(row.source) && row.source.model) facts.push(['Soul from', h('span', null, 'Open WebUI’s ', h('code', null, String(row.source.model)), ', version ' + shortV(row.source.version_id))]);
    if (id) facts.push(['Profile card', row.card ? tag('imported', 'tag-ok') : tag('not imported yet', 'tag-outline')]);
    if (id) facts.push(['Open WebUI', driftLine(row.drift)]);
    const binding = isObj(row.binding) ? row.binding : {};
    const extra = [];
    if (row.kind === 'openwebui') extra.push(tag(binding.knowledge ? 'knowledge on' : 'knowledge off', binding.knowledge ? 'tag-warn' : 'tag-outline', { title: binding.knowledge ? 'Her Open WebUI knowledge is sent on every call: Open WebUI then runs retrieval each time, with its own task and embedding models.' : 'Off unless the persona file says knowledge: true.' }));
    if (row.kind === 'openwebui' && binding.filters === false) extra.push(tag('default filters off', 'tag-outline', { title: 'The persona file says filters: false.' }));
    if (row.kind === 'snapshot' && row.offline === false) extra.push(tag('offline copy not enabled', 'tag-warn'));
    return h('article', { class: 'sf-card pe-card' + (row.error ? ' has-error' : ''), 'aria-labelledby': tid, dataset: { persona: row.name, kind: row.kind } },
      h('div', { class: 'pe-card-head' },
        h('span', { class: 'pe-avatar', 'aria-hidden': 'true' }, SF.icon(row.kind === 'wake' ? 'bell' : 'persona')),
        h('div', { class: 'pe-card-titles' }, h('h3', { class: 'pe-card-title', id: tid }, String(row.label || row.name)), h('div', { class: 'pe-card-name mono' }, row.name))),
      h('div', { class: 'sf-row pe-tags' }, kindTag(row), privacyTag(row), operatorTag(row), extra),
      row.description ? h('p', { class: 'pe-desc' }, String(row.description)) : null,
      operatorNote(row),
      row.error ? h('div', { class: 'callout callout-err' }, SF.icon('alert', 'icon-sm'), h('div', null, h('strong', null, 'Cannot start: '), String(row.error))) : null,
      facts.length ? kv(facts) : null,
      offlineSwitch(row),
      h('div', { class: 'pe-actions' }, actions));
  }
  function renderList(root, handle) {
    const busy = P.checking === '';
    const check = SF.btn('Check now', { cls: busy ? 'is-busy' : '', icon: 'refresh', disabled: busy, dataset: { focus: 'check-all', action: 'check' },
      onClick: (e) => checkNow(null, e.currentTarget) });
    const imp = SF.btn('Import', { cls: 'btn-primary', icon: 'download', dataset: { focus: 'import', action: 'import' }, onClick: () => openImport({}) });
    const nodes = [SF.viewHead('Personas', 'Talk to your Open WebUI personas from here, keep offline copies of them, and see the personas that wake themselves.', h('div', { class: 'sf-row' }, check, imp))];
    nodes.push(checkResult(''));
    if (P.error && !P.rows) { nodes.push(SF.unavailable(P.error, 'Personas')); SF.rebuild(root, nodes); return; }
    if (!P.rows) { nodes.push(SF.skeleton(6)); SF.rebuild(root, nodes); return; }
    if (!P.rows.length) {
      nodes.push(SF.emptyState('persona', 'No personas yet', 'Apply the openwebui preset (Settings › Routing) and import your Open WebUI workspace model’s profile card: then you can chat with her here.',
        [SF.btn('Import a profile', { cls: 'btn-primary', icon: 'download', onClick: () => openImport({}) })]));
    }
    for (const [kind, titleText, sub] of GROUPS) {
      const rows = P.rows.filter((r) => r.kind === kind);
      if (!rows.length) continue;
      nodes.push(h('section', { class: 'pe-group', 'aria-labelledby': 'pe-g-' + kind, dataset: { group: kind } },
        h('h2', { class: 'pe-group-title', id: 'pe-g-' + kind }, titleText, h('span', { class: 'count-pill' }, String(rows.length))),
        h('p', { class: 'pe-group-sub' }, sub),
        h('div', { class: 'sf-cards pe-cards', role: 'list' }, rows.map((r) => h('div', { role: 'listitem', class: 'pe-cell' }, personaCard(r))))));
    }
    const other = P.rows.filter((r) => !KIND[r.kind]);
    if (other.length) nodes.push(h('div', { class: 'sf-cards pe-cards' }, other.map(personaCard)));
    nodes.push(h('section', { class: 'wz-proc-card pe-proc', 'aria-labelledby': 'pe-proc-t' }, h('h2', { class: 'wz-proc-title', id: 'pe-proc-t' }, 'How personas work'),
      SF.procedureList([
        ['Kate runs through Open WebUI.', 'Her prompt, settings, skills and filters stay there and apply on every call; the harness only adds a short note after her prompt, shown word for word on her profile card.'],
        ['The profile card reads her.', 'Import it from Open WebUI’s Export file or straight from Open WebUI to see what reaches a chat here and what only her own Open WebUI chats have.'],
        ['An offline copy is your choice.', 'It runs on this machine’s own model, does not follow later changes and labels every reply; switch its chats off at any time.'],
        ['Check now looks for changes.', 'A change that alters what she does opens one item on the Housekeeping Tracker, which links back to her card.'],
      ])));
    SF.rebuild(root, nodes);
  }

  // ================================================================ the profile card page
  function settingValue(v) {
    if (typeof v === 'string') return h('code', { class: 'pe-raw' }, v);   // exactly as stored: a backslash-n is two characters
    if (Array.isArray(v) && v.every((x) => typeof x !== 'object' || x === null)) return h('span', { class: 'pe-chips' }, v.map((x) => h('code', { class: 'pe-chip pe-raw' }, typeof x === 'string' ? x : JSON.stringify(x))));
    if (typeof v === 'object' && v !== null) return h('pre', { class: 'sf-pre pe-json' }, JSON.stringify(v, null, 2));
    return h('code', null, String(v));
  }
  function partsTable(parts, caption) {
    const rows = (parts || []).filter(isObj);
    if (!rows.length) return null;
    const wrap = h('div', { class: 'table-wrap sf-scroll-x pe-table' },
      h('table', { class: 'table' }, h('caption', { class: 'sr-only' }, caption),
        h('thead', null, h('tr', null, h('th', { scope: 'col' }, 'Part'), h('th', { scope: 'col' }, 'Through the API'), h('th', { scope: 'col' }, 'Note'))),
        h('tbody', null, rows.map((r) => h('tr', { dataset: { part: String(r.part || '') } },
          h('td', null, h('strong', null, String(r.part || '')), chips(r.ids)),
          h('td', null, r.through_api ? tag('yes', 'tag-ok') : tag('no', 'tag-outline')),
          h('td', { class: 'pe-note' }, r.note ? String(r.note) : ''))))));
    SF.trackFades(wrap);
    return wrap;
  }
  function baseModelText(b) {
    if (!isObj(b)) return null;
    return h('span', null, h('code', null, String(b.id || '?')), b.listed === false ? h('span', { class: 'pe-warn-text' }, ' (Open WebUI does not list it for this key)')
      : h('span', { class: 'sf-muted' }, (b.connection_type ? ', connection ' + b.connection_type : ', no connection type') + (b.owned_by ? ', owned by ' + b.owned_by : '')));
  }
  function cardSections(card, live, page, row) {
    const out = [];
    const copy = !!row && row.kind === 'snapshot';
    const sys = isObj(card.system) ? card.system : {};
    const ph = (sys.variables || []).concat(sys.unknown_variables || []);
    const filters = isObj(card.filters) ? card.filters : {};
    const lv = isObj(live) && live.listed ? live : null;
    out.push(section('card', 'Profile card', 'What the preset is, read from Open WebUI. It never holds her prompt’s text.', h('div', { class: 'sf-card' }, kv([
      ['Name', String(card.name || '')],
      ['Model id', h('code', null, String(card.model_id || ''))],
      ['Description', card.description ? h('span', { class: 'pe-desc' }, String(card.description)) : null],
      ['Version', h('span', null, h('code', { title: String(card.version_id || '') }, shortV(card.version_id)), lv && lv.version_id && lv.version_id !== card.version_id
        ? h('span', { class: 'pe-warn-text' }, ' · changed in Open WebUI since this card (now ', h('code', null, shortV(lv.version_id)), ')') : lv ? h('span', { class: 'sf-muted' }, ' · the same as Open WebUI lists now') : null)],
      ['Imported', h('span', null, SF.timeEl(card.imported_at), ' from ' + (card.source === 'api' ? 'Open WebUI (its export, with the owner’s key)' : 'an Export file'))],
      ['Open WebUI', card.open_webui_version ? 'version ' + card.open_webui_version : (lv && lv.open_webui_version ? 'version ' + lv.open_webui_version + ' (now)' : 'version not recorded')],
      ['Base model', lv && lv.base_model ? baseModelText(lv.base_model) : card.base_model_id ? h('code', null, String(card.base_model_id)) : 'none'],
      ['System prompt', h('span', null, SF.fmtNum(sys.chars || 0) + ' characters, sha256 ', h('code', { title: String(sys.sha256 || '') }, String(sys.sha256 || '').slice(0, 16) + '…'),
        ph.length ? [', placeholders ', chips(ph.map((x) => '{{' + x + '}}'))] : ', no placeholders',
        (sys.unknown_variables || []).length ? h('span', { class: 'pe-warn-text' }, ' (an offline copy cannot fill ' + sys.unknown_variables.map((x) => '{{' + x + '}}').join(', ') + ')') : null)],
      ['Stream', card.stream === null || card.stream === undefined ? 'not set' : (card.stream ? 'Stream Chat Response on' : 'Stream Chat Response off') + ' (informational: the harness never sends it)'],
      ['Function calling', card.function_calling ? h('code', null, String(card.function_calling)) : 'not set'],
      ['Model controls', chips(card.model_controls) || 'none'],
      ['Knowledge', (card.knowledge || []).length ? h('span', { class: 'pe-chips' }, card.knowledge.map((k) => h('span', { class: 'pe-chip' }, String(k.name || k.id), h('span', { class: 'sf-muted' }, ' (' + String(k.type || '') + ')')))) : 'none'],
      ['Filters', (filters.always || []).length || (filters.default_on || []).length ? h('span', null, (filters.always || []).length ? ['always: ', chips(filters.always)] : null, (filters.default_on || []).length ? [' default on: ', chips(filters.default_on)] : null) : 'none'],
      ['Skills', chips(card.skills) || 'none'],
      ['Tools', (card.tools || []).length ? h('ul', { class: 'pe-tools' }, card.tools.map((t) => h('li', null, h('code', null, String(t.id)), ' ', tag(t.kind === 'mcp' ? 'MCP server' : 'tool', 'tag-outline'), h('span', { class: 'sf-muted' }, ' ' + String(t.harness || ''))))) : 'none'],
      ['Actions', chips(card.actions) || 'none'],
      ['Features', chips(card.features) || 'none'],
      ['Built-in tools', chips(card.builtin_tools) || 'none'],
      ['Terminal', card.terminal ? h('code', null, String(card.terminal)) : 'none'],
    ]))));
    const parts = lv && Array.isArray(lv.parts) ? lv.parts : card.parts;
    // An offline copy never goes through Open WebUI: this table is about her chats that do, not about the copy.
    const partsTitle = copy ? 'What reaches ' + String(card.name || title(row.name)) + '’s chats through Open WebUI' : 'What reaches a chat through the API';
    out.push(section('parts', partsTitle, copy ? 'About her chats through Open WebUI’s API, not this copy: what this copy has is in the “An imported offline copy” column of What runs where.'
      : lv && Array.isArray(lv.parts) ? 'As Open WebUI lists her now, with this persona file’s knowledge and filters lines.'
        : 'As the card saw it: knowledge is off unless the persona file says knowledge: true.', partsTable(parts, partsTitle)));
    const settings = isObj(card.settings) ? card.settings : {};
    const keys = Object.keys(settings);
    out.push(section('settings', 'Settings', card.defaults_known === false ? 'Open WebUI’s merge of the preset’s settings, without Open WebUI’s global defaults (this key could not read them). Each model control’s default option is included.'
      : 'Open WebUI’s merge of its global defaults and the preset’s settings, with each model control’s default option.',
      keys.length ? h('div', { class: 'sf-card' }, kv(keys.map((k) => [h('code', null, k), settingValue(settings[k])]))) : h('p', { class: 'sf-muted' }, 'No settings of its own.')));
    if ((card.notes || []).length) out.push(section('notes', 'Notes', null, h('ul', { class: 'pe-notes' }, card.notes.map((n) => h('li', null, String(n))))));
    return out;
  }
  function appendedSection(text, row) {
    if (typeof text !== 'string') return null;
    return section('appended', 'What the harness adds after her prompt', row && row.kind === 'snapshot'
      ? 'Word for word. The copy’s own prompt comes first; the harness adds only this after it. <the chat’s folder> stands for each chat’s own folder.'
      : 'Word for word: Open WebUI puts her prompt first and this after it, in one system message. <the chat’s folder> stands for each chat’s own folder.',
    h('pre', { class: 'sf-pre pe-appended', tabindex: '0', 'aria-label': 'What the harness adds after her prompt' }, text));
  }
  /** The capability map (DESIGN §33): one column per surface, "Has" and "Does not have", never merged or totalled. */
  function capabilityMap(cmap) {
    if (!isObj(cmap) || !Array.isArray(cmap.surfaces)) return null;
    const partRow = (r) => {
      const note = r.note ? String(r.note) : '';
      const noteEl = note ? h('div', { class: 'pe-part-note' }, note, /mcp_servers\./.test(note) ? [' ', h('a', { href: '#/settings/mcp_servers' }, 'Settings › MCP servers')] : null) : null;
      return h('li', { class: 'pe-part', dataset: { part: String(r.part || '') } }, h('span', { class: 'pe-part-name' }, String(r.part || '')), chips(r.ids), noteEl);
    };
    const column = (s) => {
      const setUp = s.exists !== false;
      const names = Array.isArray(s.personas) ? s.personas : null;
      return h('section', { class: 'pe-surface' + (setUp ? '' : ' is-off'), 'aria-labelledby': 'pe-s-' + s.surface, dataset: { surface: String(s.surface || '') } },
        h('div', { class: 'pe-surface-head' }, SF.icon(SURFACE_ICON[s.surface] || 'layers', 'icon-sm'), h('h3', { class: 'pe-surface-title', id: 'pe-s-' + s.surface }, String(s.title || s.surface)),
          s.exists === false ? tag('not set up', 'tag-outline', { title: 'No persona makes this surface yet: the lists say what it would have.' }) : s.exists === null ? tag('not known', 'tag-outline') : null),
        names && names.length ? h('p', { class: 'pe-surface-who' }, 'Personas: ', names.map((n, i) => {
          const r = rowOf(n);
          return [i ? ', ' : '', h('a', { href: r ? personaHref(r) : '#/personas/' + SF.enc(n) }, n), r && r.path && s.exists === false ? h('span', { class: 'sf-muted' }, ' (' + r.path + ')') : null];
        })) : null,
        h('h4', { class: 'pe-list-title' }, SF.icon('check', 'icon-xs'), 'Has'),
        (s.available || []).length ? h('ul', { class: 'pe-parts' }, s.available.map(partRow)) : h('p', { class: 'sf-muted pe-none' }, 'Nothing of the persona’s own.'),
        h('h4', { class: 'pe-list-title is-not' }, SF.icon('ban', 'icon-xs'), 'Does not have'),
        (s.unavailable || []).length ? h('ul', { class: 'pe-parts is-not' }, s.unavailable.map(partRow)) : h('p', { class: 'sf-muted pe-none' }, 'Nothing it lacks is listed.'),
        s.note ? h('p', { class: 'pe-surface-note' }, String(s.note)) : null);
    };
    const del = Array.isArray(cmap.delegation) ? cmap.delegation.filter(isObj) : [];
    return section('map', 'What runs where', null, [
      h('p', { class: 'pe-map-note' }, SF.icon('info', 'icon-sm'), h('span', null, String(cmap.note || ''))),
      h('div', { class: 'pe-surfaces' }, cmap.surfaces.filter(isObj).map(column)),
      del.length ? h('div', { class: 'pe-delegation' }, h('h3', { class: 'pe-list-title' }, 'Between the surfaces'),
        h('ul', null, del.map((d) => h('li', { dataset: { route: String(d.route || '') } }, h('strong', null, (DELEGATION_WORDS[d.route] || title(d.route)) + ': '), String(d.text || ''))))) : null,
    ]);
  }
  function driftSection(drift) {
    if (!isObj(drift)) return null;
    const hist = Array.isArray(drift.history) ? drift.history.filter(isObj).slice().reverse() : [];
    return section('drift', 'Changes seen in Open WebUI', 'The drift check compares her live preset with the last one seen; it runs at a chat’s start, on a turn whose version changed, on Check now and daily.',
      h('div', { class: 'sf-card' }, kv([['Now', driftLine(drift)], ['Version seen', h('code', null, shortV(drift.version_id))]]),
        hist.length ? h('ul', { class: 'pe-history' }, hist.map((x) => h('li', null, SF.timeEl(x.at), ' version ', h('code', null, shortV(x.from)), ' → ', h('code', null, shortV(x.to)), ': ',
          (DRIFT[x.kind] || [String(x.kind || '')])[0], x.item ? [' · ', itemLink(x.item)] : null))) : null));
  }
  /** Whether what the owner imported is already of the version a drift saw (or newer than the change): the card,
   *  and for an offline copy the copy too. Importing does not touch the drift state, which stays until a check. */
  function importedSince(drift, card, row) {
    const after = (x) => { const a = Date.parse(x || ''), b = Date.parse(drift.changed_at || ''); return isFinite(a) && isFinite(b) && a > b; };
    const cardDone = !!card && ((!!card.version_id && card.version_id === drift.version_id) || after(card.imported_at));
    if (row.kind !== 'snapshot') return cardDone;
    const b = isObj(row.binding) ? row.binding : {};
    return cardDone && ((!!b.version_id && b.version_id === drift.version_id) || after(b.imported_at));
  }
  function liveBanners(row, page) {
    const out = [];
    const live = isObj(page.live) ? page.live : null;
    const card = isObj(page.card) ? page.card : null;
    const copy = row.kind === 'snapshot';
    const who = card ? String(card.name || title(row.name)) : title(row.name);
    if (live && live.error) out.push(h('div', { class: 'callout callout-err pe-live-err', role: 'alert' }, SF.icon('alert', 'icon-sm'),
      h('div', null, h('strong', null, 'Open WebUI did not answer: '), String(live.error), card ? ' The saved card below is as it was imported.' : '',
        copy ? ' This copy still runs: it never asks Open WebUI.' : '', h('div', { class: 'pe-err-help' }, helpLinks(live.error)))));
    else if (live && live.listed === false && copy) out.push(SF.callout('info', 'info', 'Open WebUI no longer lists ' + (modelId(row) || 'it') + ' for this key.', 'This copy still runs on this machine. To re-import it, use the Export file.'));
    else if (live && live.listed === false) out.push(SF.callout('warn', 'alert', 'Open WebUI does not list it for this key.', 'Its chats cannot start until it does: check the workspace model’s access in Open WebUI, or the key.'));
    // The base model's warnings are about chats that go through Open WebUI: an offline copy's words never do.
    if (!copy && live && Array.isArray(live.warnings)) for (const w of live.warnings) out.push(h('div', { class: 'callout callout-warn pe-warning' }, SF.icon('alert', 'icon-sm'), h('div', null, h('strong', null, 'Where her words go: '), sentence(w))));
    const drift = isObj(page.drift) ? page.drift : (isObj(row.drift) ? row.drift : null);
    if (drift && (drift.kind === 'behaviour' || drift.kind === 'unknown')) {
      const done = importedSince(drift, card, row);
      const next = done
        ? [' You imported ', copy ? 'this copy' : 'her card', ' after this change. Look at what changed below', copy ? '' : ', re-import any offline copy of her', ', then mark ',
          drift.item ? itemLink(drift.item) : 'its item', ' Proven on the Housekeeping Tracker.']
        : [drift.item ? [' See ', itemLink(drift.item), '.'] : null, ' Import her profile card again', copy ? ' and re-import this offline copy.' : '.'];
      out.push(h('div', { class: 'callout callout-warn pe-drift-banner' + (done ? ' is-imported' : ''), dataset: { imported: done ? '1' : '0' } }, SF.icon('alert', 'icon-sm'), h('div', { class: 'pe-grow' },
        h('strong', null, who + ' changed in Open WebUI' + (drift.changed_at ? ' ' : '')), drift.changed_at ? SF.timeEl(drift.changed_at) : null,
        drift.kind === 'unknown' ? ': it may be cosmetic; this key cannot read the prompt and settings.' : ': what she does changed.', next),
      done ? SF.btn('Check now', { cls: 'btn-sm', icon: 'refresh', disabled: P.checking === row.name, dataset: { focus: 'drift-check', action: 'check' }, onClick: (e) => checkNow(row.name, e.currentTarget) }) : null));
    }
    if (row.kind === 'snapshot' && live && live.listed && isObj(row.binding) && live.version_id && live.version_id !== row.binding.version_id) {
      out.push(h('div', { class: 'callout callout-info pe-stale' }, SF.icon('info', 'icon-sm'), h('div', null, h('strong', null, 'The live preset has changed since this copy: '),
        'version ' + shortV(row.binding.version_id) + ' → ' + shortV(live.version_id) + '. Re-import it to follow Open WebUI; it never switches by itself.')));
    }
    return out;
  }
  /** "On this page": a button per section that scrolls to its heading and moves focus there (no hash change). */
  function toc(secs) {
    const list = secs.filter((x) => x && x.dataset && x.dataset.sec);
    if (list.length < 3) return null;
    return h('nav', { class: 'pe-toc', 'aria-label': 'On this page' }, h('span', { class: 'pe-toc-label' }, 'On this page'), list.map((x) => {
      const head = x.querySelector('.pe-sec-title');
      return h('button', { type: 'button', class: 'btn btn-xs btn-ghost', dataset: { focus: 'toc:' + x.dataset.sec },
        onclick: () => { const t = document.getElementById('pe-' + x.dataset.sec + '-t'); if (t) { t.scrollIntoView({ block: 'start' }); t.focus({ preventScroll: true }); } } }, head ? head.textContent : x.dataset.sec);
    }));
  }
  function renderPage(root, route) {
    const asked = route.parts[0];
    const back = h('a', { class: 'pe-back', href: '#/personas', dataset: { focus: 'back' } }, SF.icon('chevron-left', 'icon-sm'), 'Personas');
    if (!P.rows) {
      SF.rebuild(root, [back, P.error ? SF.unavailable(P.error, 'Personas') : SF.skeleton(8)]);
      return;
    }
    const t = target(asked);
    const row = t.row;
    if (!row) {
      SF.rebuild(root, [back, SF.emptyState('persona', 'No persona called ' + asked, 'It is not among the personas the harness finds now, and no persona is bound to a workspace model of that id.',
        [h('a', { class: 'btn', href: '#/personas' }, 'All personas')])]);
      return;
    }
    if (row.kind === 'wake') {
      SF.rebuild(root, [back, SF.viewHead(String(row.label || row.name), String(row.description || '')), h('div', { class: 'sf-row pe-tags' }, kindTag(row), privacyTag(row), operatorTag(row)), operatorNote(row),
        SF.emptyState('bell', 'She wakes herself', 'Her soul, memory, thread and check-ins are on her own page in Messages & Wakes. She is never a chat.',
          [h('a', { class: 'btn btn-primary', href: wakeHref(row.name) }, 'Open her page'), activityLink(row.name)])]);
      return;
    }
    const pg = P.pages.get(row.name);
    if (!pg || (!pg.data && !pg.error)) loadPage(row.name);
    const can = canChat(row);
    const busy = P.checking === row.name;
    const id = modelId(row);
    const actions = h('div', { class: 'sf-row' },
      can.ok ? SF.btn('Chat', { cls: 'btn-primary', icon: 'chat', dataset: { focus: 'page-chat', action: 'chat' }, onClick: (e) => startChat(row.name, e.currentTarget) }) : null,
      SF.btn(row.card ? 'Import again' : 'Import', { icon: 'download', dataset: { focus: 'page-import', action: 'import' }, onClick: () => openImport({ model: id, persona: row }) }),   // row.provider: hers
      SF.btn('Check now', { icon: 'refresh', cls: busy ? 'is-busy' : '', disabled: busy, dataset: { focus: 'page-check', action: 'check' }, onClick: (e) => checkNow(row.name, e.currentTarget) }),
      activityLink(row.name));
    const nodes = [back, SF.viewHead(String(row.label || row.name), row.description ? String(row.description) : null, actions),
      h('div', { class: 'sf-row pe-tags' }, kindTag(row), privacyTag(row), operatorTag(row), row.card ? tag('profile card imported', 'tag-ok') : tag('no profile card yet', 'tag-outline'),
        id ? h('span', { class: 'sf-muted' }, 'workspace model ', h('code', { class: 'pe-chip' }, id)) : null, row.model && row.model !== id ? h('span', { class: 'sf-muted' }, 'runs on ', h('code', null, String(row.model))) : null),
      t.via ? SF.callout('info', 'info', 'Workspace model ' + t.via + ':', 'this is the persona bound to it' + (t.others && t.others.length ? ' (also: ' + t.others.map((x) => x.name).join(', ') + ')' : '') + '.') : null,
      operatorNote(row),
      row.error ? h('div', { class: 'callout callout-err' }, SF.icon('alert', 'icon-sm'), h('div', null, h('strong', null, 'This persona cannot start: '), String(row.error))) : null,
      !can.ok && !row.error ? SF.callout('warn', 'alert', 'No chats yet:', can.why) : null,
      row.kind === 'snapshot' ? h('div', { class: 'sf-card pe-offline-card' }, offlineSwitch(row)) : null,
      checkResult(row.name)];
    if (pg && pg.error && !pg.data) {
      nodes.push(errorBox('Could not load the profile card:', pg.error));
    } else if (!pg || !pg.data) {
      nodes.push(SF.skeleton(10));
    } else {
      const page = pg.data;
      nodes.push(...liveBanners(row, page));
      const secs = [];
      if (isObj(page.card)) {
        const [cardSec, ...rest] = cardSections(page.card, page.live, page, row);
        secs.push(cardSec, appendedSection(page.appended_text, row), capabilityMap(page.capability_map || page.card.capability_map), ...rest);
      } else {
        secs.push(SF.emptyState('file', 'No profile card yet', 'Import the profile card to see what runs where: what her own Open WebUI chats have, what a chat here has, and what an offline copy would have.',
          [SF.btn('Import the profile card', { cls: 'btn-primary', icon: 'download', onClick: () => openImport({ model: id, persona: row }) })]), appendedSection(page.appended_text, row));
      }
      secs.push(driftSection(page.drift));
      if (isObj(page.import)) {
        secs.push(section('before-import', 'Changes before import', 'What changed in Open WebUI before this profile card was first imported, as the operator reported it.',
          SF.beforeImport(page.import, { persona: row.name, state: biState(row.name), bare: true, onChange: (imp) => { pg.data.import = imp; if (P.handle) P.handle.refresh(); loadPage(row.name, true); } })));
      }
      nodes.push(toc(secs), ...secs);
    }
    SF.rebuild(root, nodes);
  }

  /** DESIGN §36: what she did, on the Operator area's activity page (#/activity), the last 7 days. */
  function activityLink(name) {
    return h('a', { class: 'btn', href: '#/activity?agent=' + SF.enc(name) + '&since=7d', dataset: { focus: 'page-activity', action: 'activity' }, title: 'What she did: her turns, actions and receipts' }, SF.icon('clock', 'icon-sm'), 'Activity');
  }

  /** The attach form's state of one persona's "Changes before import" (DESIGN §15): kept across renders. */
  function biState(name) {
    if (!P.beforeImport.has(name)) P.beforeImport.set(name, { ids: '', by: 'owner', error: null, open: false });
    return P.beforeImport.get(name);
  }

  // ================================================================ import (DESIGN §15 "Import", §35 "A new persona from an Open WebUI profile")
  /** An Export file's records without the cosmetic pictures (the server takes at most 1 MB). */
  function stripRecords(data) {
    const list = Array.isArray(data) ? data : isObj(data) ? [data] : null;
    if (!list || !list.every(isObj)) throw new Error('This is not an Open WebUI Export file: it should be a JSON list of models.');
    return list.map((r) => {
      const c = Object.assign({}, r);
      delete c.background_image_data;
      if (isObj(c.meta)) { c.meta = Object.assign({}, c.meta); delete c.meta.profile_image_url; delete c.meta.background_image_data; }
      return c;
    });
  }
  const isLocalUrl = (u) => /^https?:\/\/(127\.\d+\.\d+\.\d+|localhost|\[::1\])(:\d+)?(\/|$)/i.test(String(u || ''));
  /** A dialog's error with the server's own words on a line of their own, scrolled into view. */
  function showErr(ctl, lead, msg, more) {
    ctl.showError([lead ? h('span', null, lead) : null, msg ? h('span', { class: 'pe-err-text pe-err-server' }, msg) : null, more || null]);
    const box = ctl.dlg.querySelector('.form-error');
    if (box) box.scrollIntoView({ block: 'nearest' });
  }
  function openImport(pre) {
    pre = pre || {};
    if (document.querySelector('dialog[open]')) return;
    const st = { records: null, fileName: '', listing: null, listingError: null, listedFor: undefined, cfg: null, warned: '', candidates: null, replacing: false };
    const uid = 'pe-imp';
    // where from
    const fromName = uid + '-from';
    const fromFile = h('input', { type: 'radio', name: fromName, value: 'file', checked: true, dataset: { from: 'file' } });
    const fromLive = h('input', { type: 'radio', name: fromName, value: 'live', dataset: { from: 'live' } });
    const fileInput = h('input', { type: 'file', id: uid + '-file', accept: '.json,application/json', class: 'input pe-file' });
    const fileModel = h('select', { class: 'select', id: uid + '-file-model' });
    const fileInfo = h('div', { class: 'field-hint', role: 'status' }, 'In Open WebUI: Workspace › Models, the model’s menu, Export. Its pictures are left out before anything is sent.');
    const liveModel = h('select', { class: 'select', id: uid + '-live-model' });
    const liveInfo = h('div', { class: 'field-hint', role: 'status' }, 'Reads Open WebUI’s list with the key the harness has (OPENWEBUI_API_KEY).');
    const liveModelText = h('input', { class: 'input mono-input', type: 'text', id: uid + '-live-id', placeholder: 'the workspace model’s id, e.g. kate', hidden: true });
    // Several Open WebUI providers (kind openwebui): which one to read. Without a choice the server reads the default
    // one (personas.client), so Import again names the persona's own provider.
    const provSel = h('select', { class: 'select', id: uid + '-provider' });
    const provField = h('div', { hidden: true, dataset: { slot: 'provider' } }, SF.field('Open WebUI', provSel, 'The provider to read it from: each has its own Open WebUI and key.'));
    const provider = () => (!provField.hidden && provSel.value ? provSel.value : (pre.persona && typeof pre.persona.provider === 'string' && pre.persona.provider) || null);
    const filePane = h('div', { class: 'pe-imp-pane', dataset: { pane: 'file' } }, SF.field('Export file', fileInput), fileInfo, h('div', { hidden: true, dataset: { slot: 'file-model' } }, SF.field('Model in the file', fileModel)));
    const livePane = h('div', { class: 'pe-imp-pane', dataset: { pane: 'live' }, hidden: true }, provField, SF.field('Workspace model', liveModel), liveModelText, liveInfo);
    // what to make
    const whatName = uid + '-what';
    const whatCard = h('input', { type: 'radio', name: whatName, value: 'card', checked: true, dataset: { what: 'card' } });
    const whatWake = h('input', { type: 'radio', name: whatName, value: 'wake', dataset: { what: 'wake' } });
    const snapOn = h('input', { type: 'checkbox', id: uid + '-snap', dataset: { snapshot: '1' } });   // never pre-checked
    const snapName = h('input', { class: 'input mono-input', type: 'text', id: uid + '-snap-name', maxlength: 41, autocomplete: 'off', spellcheck: 'false', placeholder: 'kate-offline' });
    const snapBase = h('select', { class: 'select', id: uid + '-snap-base' }, h('option', { value: '' }, 'Pick it for me (the model Open WebUI’s base model maps to)'));
    const snapUser = h('input', { class: 'input', type: 'text', id: uid + '-snap-user', maxlength: 80, autocomplete: 'off', placeholder: 'optional' });
    const snapFields = h('div', { class: 'pe-snap-fields', hidden: true },
      SF.field('Name of the copy', snapName, 'a-z, 0-9 and -, up to 41 characters; it becomes a persona and a models entry.'),
      SF.field('Runs on', snapBase, 'One of this machine’s own models, never Open WebUI or an API.'),
      SF.field('Your name for {{USER_NAME}}', snapUser, 'Optional: what the copy calls you where her prompt says {{USER_NAME}}.'));
    const wakeName = h('input', { class: 'input mono-input', type: 'text', id: uid + '-wake-name', maxlength: 41, autocomplete: 'off', spellcheck: 'false', placeholder: 'e.g. mira' });
    const wakeDisplay = h('input', { class: 'input', type: 'text', id: uid + '-wake-display', maxlength: 60, autocomplete: 'off', placeholder: 'optional, e.g. Mira' });
    const wakeDesc = h('input', { class: 'input', type: 'text', id: uid + '-wake-desc', maxlength: 200, autocomplete: 'off', placeholder: 'optional: one line about her' });
    const wakeUser = h('input', { class: 'input', type: 'text', id: uid + '-wake-user', maxlength: 80, autocomplete: 'off', placeholder: 'optional' });
    const wakeKey = h('input', { class: 'input mono-input', type: 'password', id: uid + '-wake-key', autocomplete: 'off', spellcheck: 'false', placeholder: 'sk-…' });
    const wakeKeyField = h('div', { hidden: true }, SF.field('Open WebUI key for this import', wakeKey, 'A key with write access to this model. It is used for this one call, never saved, and cleared from this form after.'));
    const wakeFields = h('div', { class: 'pe-wake-fields', hidden: true },
      SF.field('Her name', wakeName, 'a-z, 0-9 and -, not an agent’s name.'), SF.field('Display name', wakeDisplay), SF.field('Description', wakeDesc),
      SF.field('Your name for {{USER_NAME}}', wakeUser, 'Optional, kept privately for her turns.'), wakeKeyField,
      h('p', { class: 'field-hint' }, 'She is private until you tick her instructions clean on her page, and she is never a chat.'));
    const cardFields = h('div', { class: 'pe-card-fields' },
      h('label', { class: 'check pe-snap-check', for: uid + '-snap' }, snapOn, h('span', null, 'Also make an offline copy that runs on this machine’s own model, not through Open WebUI; it does not follow later changes')),
      snapFields);
    const radioCard = (inp, titleText, desc) => h('label', { class: 'radio-card' }, inp, h('span', null, h('span', { class: 'radio-card-title' }, titleText), h('span', { class: 'radio-card-desc', style: { display: 'block' } }, desc)));
    const body = [
      h('div', { class: 'field' }, h('span', { class: 'field-label', id: uid + '-from-l' }, 'Where from'),
        h('div', { class: 'radio-cards', role: 'radiogroup', 'aria-labelledby': uid + '-from-l' },
          radioCard(fromFile, 'Upload the Export file', 'Open WebUI’s workspace Export (or Export all): works with any key, nothing is asked of Open WebUI.'),
          radioCard(fromLive, 'Read it from Open WebUI', 'Asks Open WebUI for its export with the harness’s key; the key needs write access to the model.'))),
      filePane, livePane,
      h('div', { class: 'field' }, h('span', { class: 'field-label', id: uid + '-what-l' }, 'What to make'),
        h('div', { class: 'radio-cards', role: 'radiogroup', 'aria-labelledby': uid + '-what-l' },
          radioCard(whatCard, 'A profile card', 'Saves what the preset is and what reaches a chat here. Nothing changes in Open WebUI.'),
          radioCard(whatWake, 'A persona that wakes herself', 'Make a persona that wakes herself from this profile: her soul is the preset’s prompt.'))),
      cardFields, wakeFields,
    ];
    const isLive = () => fromLive.checked;
    const isWake = () => whatWake.checked;
    const chosenModel = () => (isLive() ? (liveModelText.hidden ? liveModel.value : liveModelText.value.trim()) : fileModel.value);
    const sync = () => {
      filePane.hidden = isLive(); livePane.hidden = !isLive();
      cardFields.hidden = isWake(); wakeFields.hidden = !isWake();
      snapFields.hidden = !snapOn.checked || isWake();
      wakeKeyField.hidden = !(isWake() && isLive());
      if (snapOn.checked && !snapName.value && chosenModel()) snapName.value = (chosenModel().toLowerCase().replace(/[^a-z0-9-]+/g, '-').replace(/^-+/, '').slice(0, 32) || 'copy') + '-offline';
    };
    for (const el of [fromFile, fromLive, whatCard, whatWake, snapOn]) el.addEventListener('change', () => { st.warned = ''; sync(); if (isLive() && !st.listing && !st.listingError) loadListing(); });
    fileModel.addEventListener('change', () => { st.warned = ''; });
    provSel.addEventListener('change', () => { st.warned = ''; st.listing = null; st.listingError = null; loadListing(); });
    liveModel.addEventListener('change', () => { st.warned = ''; });
    fileInput.addEventListener('change', async () => {
      st.records = null;
      const f = fileInput.files && fileInput.files[0];
      fileModel.parentNode.parentNode.hidden = true;
      if (!f) return;
      try {
        const raw = await f.text();
        st.records = stripRecords(JSON.parse(raw));
        st.fileName = f.name;
        SF.clear(fileModel);
        for (const r of st.records) fileModel.appendChild(h('option', { value: String(r.id || '') }, String(r.name || r.id || '?') + (r.id ? ' (' + r.id + ')' : ' (no id)')));
        const want = pre.model && st.records.some((r) => r.id === pre.model) ? pre.model : String(st.records[0] && st.records[0].id || '');
        fileModel.value = want;
        fileModel.parentNode.parentNode.hidden = st.records.length < 2;
        fileInfo.textContent = SF.plural(st.records.length, 'model') + ' in ' + f.name + (st.records.length === 1 ? ': ' + String(st.records[0].name || st.records[0].id || '') : ': pick one below') + '. Pictures left out.';
        sync();
      } catch (e) {
        fileInfo.textContent = 'This file could not be read: ' + (e && e.message ? e.message : String(e));
      }
    });
    async function loadListing() {
      liveInfo.textContent = 'Asking Open WebUI…';
      const prov = provider();
      st.listedFor = prov;
      try {
        const r = await SF.api('/api/openwebui/models' + (prov ? '?provider=' + SF.enc(prov) : ''));
        if (st.listedFor !== prov) return;   // another provider was picked meanwhile
        st.listing = r && Array.isArray(r.models) ? r.models : [];
        st.listingError = null;
        SF.clear(liveModel);
        const presets = st.listing.filter((m) => m && m.preset);
        for (const m of presets) {
          liveModel.appendChild(h('option', { value: String(m.id) }, String(m.name || m.id) + ' (' + m.id + ') · version ' + shortV(m.version_id) + (m.card ? ' · card saved' : '') + ((m.personas || []).length ? ' · used by ' + m.personas.join(', ') : '')));
        }
        if (pre.model && presets.some((m) => m.id === pre.model)) liveModel.value = pre.model;
        liveModel.parentNode.hidden = !presets.length;
        liveModelText.hidden = !!presets.length;
        const via = r && r.provider ? ' (provider ' + r.provider + ')' : '';
        liveInfo.textContent = presets.length ? 'Open WebUI ' + (r.open_webui_version || '') + via + ' lists ' + SF.plural(presets.length, 'workspace model') + ' for the harness’s key (only workspace models can be imported).'
          : 'Open WebUI' + via + ' lists no workspace model for this key: type the model’s id.';
        sync();
      } catch (e) {
        if (st.listedFor !== prov) return;
        st.listingError = e;
        liveModel.parentNode.hidden = true;
        liveModelText.hidden = false;
        SF.clear(liveInfo);
        SF.add(liveInfo, ['Could not read Open WebUI’s list: ', h('span', { class: 'pe-err-text' }, e.message), '. ', isWake() ? 'With a key pasted below you can still type the model’s id. ' : '', helpLinks(e.message)]);
      }
    }
    SF.api('/api/config').then((cfg) => {
      st.cfg = isObj(cfg) ? cfg : null;
      fillBases(null);
      fillProviders();
    }).catch(() => {});
    /** The providers of kind openwebui (personas.openwebui_providers): a choice only when there are several. */
    function fillProviders() {
      const provs = st.cfg && isObj(st.cfg.providers) ? st.cfg.providers : {};
      const marked = Object.keys(provs).filter((k) => isObj(provs[k]) && provs[k].kind === 'openwebui').sort();
      if (marked.length < 2) return;
      const want = pre.persona && marked.indexOf(pre.persona.provider) >= 0 ? pre.persona.provider : marked.indexOf('openwebui') >= 0 ? 'openwebui' : marked[0];
      SF.clear(provSel);
      for (const k of marked) provSel.appendChild(h('option', { value: k }, k + (provs[k].base_url ? ' (' + provs[k].base_url + ')' : '')));
      provSel.value = want;
      provField.hidden = false;
      if (isLive() && st.listedFor !== undefined && st.listedFor !== provider()) { st.listing = null; st.listingError = null; loadListing(); }
    }
    function localKeys() {
      const cfg = st.cfg || {};
      const provs = isObj(cfg.providers) ? cfg.providers : {};
      return Object.keys(isObj(cfg.models) ? cfg.models : {}).filter((k) => { const p = provs[cfg.models[k].provider] || {}; return isLocalUrl(p.base_url) && p.kind !== 'openwebui'; }).sort();
    }
    function fillBases(cands) {
      const keep = snapBase.value;
      SF.clear(snapBase);
      snapBase.appendChild(h('option', { value: '' }, cands ? 'Pick one of these' : 'Pick it for me (the model Open WebUI’s base model maps to)'));
      const keys = cands && cands.length ? cands : localKeys();
      for (const k of keys) snapBase.appendChild(h('option', { value: k }, k + (st.cfg && st.cfg.models && st.cfg.models[k] ? ' (' + st.cfg.models[k].model + ')' : '')));
      snapBase.value = keys.indexOf(keep) >= 0 ? keep : '';
    }
    /** A warning before sending a snapshot name the config or an agent already has, and before replacing this model's
     *  own copy (personas.write_snapshot replaces it whole and turns its chats on again). */
    function nameWarning(name, model) {
      const own = (P.rows || []).find((r) => r.name === name && r.kind === 'snapshot' && modelId(r) === model);
      st.replacing = !!own;
      if (own) {
        const v = isObj(own.binding) ? own.binding.version_id : null;
        return 'This replaces the offline copy ' + name + ' (version ' + shortV(v) + ') with a new copy and turns its chats on' + (own.offline === false ? ' (they are off now)' : '') + '. Press Import again to confirm, or choose another name.';
      }
      if (st.cfg && isObj(st.cfg.models) && st.cfg.models[name]) return 'models.' + name + ' already exists: agents routed to it would move onto the copy, so the harness will refuse it. Choose another name, or press Import to ask anyway.';
      if (SF.agents().some((a) => a.name === name) || (P.rows || []).some((r) => r.name === name)) return 'An agent or persona called ' + name + ' already exists, so the harness will refuse it. Choose another name, or press Import to ask anyway.';
      return '';
    }
    function buildBody() {
      st.replacing = false;
      const model = chosenModel();
      if (!model) throw new Error(isLive() ? 'Pick the workspace model to import.' : 'Choose the Export file first.');
      let b;
      if (isWake()) {
        const name = wakeName.value.trim();
        if (!NAME_RE.test(name) || RESERVED.indexOf(name) >= 0) { wakeName.focus(); throw new Error('Her name: a-z, 0-9 and -, up to 41 characters, starting with a letter or digit (not import or check).'); }
        b = { name, model_id: model };
        if (isLive()) {
          if (!wakeKey.value.trim()) { wakeKey.focus(); throw new Error('Paste an Open WebUI key with write access to this model (it is used for this call only).'); }
          b.source = 'openwebui'; b.key = wakeKey.value.trim();
          if (provider()) b.provider = provider();
        } else b.export = st.records;
        if (wakeUser.value.trim()) b.user_name = wakeUser.value.trim();
        if (wakeDesc.value.trim()) b.description = wakeDesc.value.trim();
        if (wakeDisplay.value.trim()) b.display = wakeDisplay.value.trim();
      } else {
        b = isLive() ? { source: 'openwebui', model_id: model } : { export: st.records, model_id: model };
        if (isLive() && provider()) b.provider = provider();
        if (snapOn.checked) {
          const name = snapName.value.trim();
          if (!NAME_RE.test(name) || RESERVED.indexOf(name) >= 0) { snapName.focus(); throw new Error('Name of the copy: a-z, 0-9 and -, up to 41 characters, starting with a letter or digit.'); }
          b.snapshot = { name };
          if (snapBase.value) b.snapshot.model = snapBase.value;
          if (snapUser.value.trim()) b.snapshot.user_name = snapUser.value.trim();
          const w = nameWarning(name, model);
          if (w && st.warned !== name) { st.warned = name; snapName.focus(); throw new Error(w); }
        }
      }
      if (!isLive() && bodyBytes(b) > MAX_BODY) throw new Error(TOO_BIG);
      return b;
    }
    SF.openModal({
      title: 'Import from Open WebUI',
      description: 'Read a workspace model of Open WebUI (Kate, for example) into the harness. Nothing is written to Open WebUI.',
      body, wide: true, dismissable: false,
      actions: [{ label: 'Cancel', value: 'cancel' }, { label: 'Import', value: 'ok', primary: true, icon: 'download' }],
      onOpen: () => { if (pre.live) { fromLive.checked = true; sync(); loadListing(); } },
      onSubmit: async (v, ctl) => {
        const b = buildBody();
        let r;
        try {
          r = await SF.api('/api/personas/import', { method: 'POST', body: b });
        } catch (e) {
          if (b.key) wakeKey.value = '';
          const msg = e && e.message ? e.message : String(e);
          const maps = /maps to (.+?) of the harness's local models/.exec(msg);
          if (e.status === 400 && maps) {
            const cands = maps[1] === 'none' ? null : maps[1].split(/,\s*/).filter(Boolean);
            fillBases(cands);
            snapBase.focus();
            showErr(ctl, 'Which of this machine’s models should the copy run on? Pick one under “Runs on” and press Import again.', msg);
            return false;
          }
          if (e.status === 413) {
            showErr(ctl, 'Nothing was imported. ' + TOO_BIG, msg);
            return false;
          }
          if (isWake() && e.status === 409) {   // a wake persona of that name: her re-import is on her page
            const name = b.name;
            wakeName.focus(); wakeName.select();
            const link = h('a', { class: 'btn btn-xs', href: wakeHref(name), onclick: () => ctl.close(null) }, SF.icon('bell', 'icon-xs'), 'Her page');
            showErr(ctl, 'She already wakes herself here: re-import her soul on her page, or choose another name.', msg, h('span', { class: 'pe-err-server' }, link));
            return false;
          }
          if (e.status === 400 && /already exists|exists: choose another name/.test(msg)) {
            const field = isWake() ? wakeName : snapName;
            if (!isWake() && !snapName.value) snapName.value = chosenModel() + '-offline';
            field.focus(); field.select();
            showErr(ctl, 'Choose another name.', msg);
            return false;
          }
          if (e.status === 502 && /does not let this key export/.test(msg)) {
            const use = SF.btn('Use the Export file instead', { cls: 'btn-xs', onClick: () => { fromFile.checked = true; sync(); fileInput.focus(); ctl.clearError(); } });
            showErr(ctl, 'This key can read but not export: upload the Export file instead. ', msg, h('span', { class: 'pe-err-server' }, use));
            return false;
          }
          showErr(ctl, 'Nothing was imported.', msg, helpLinks(msg));
          return false;
        }
        if (b.key) wakeKey.value = '';
        await loadRows(true);
        if (isWake()) {
          SF.toast('She wakes herself now. She stays private until you tick her instructions clean on her page.', { kind: 'ok', timeout: 8000 });
          SF.go(wakeHref(r && r.persona ? r.persona : b.name));
          return r;
        }
        const made = r && r.snapshot;
        const card = r && r.card;
        SF.toast(made ? 'Profile card saved, and the offline copy ' + made.persona + (st.replacing ? ' replaced' : ' made') + ': its chats are on.' : 'Profile card saved.', { kind: 'ok', timeout: 7000 });
        const model = card ? card.model_id : b.model_id;
        P.pages.clear();
        const bound = made ? rowOf(made.persona) : target(model).row;
        if (bound) SF.go('#/personas/' + SF.enc(bound.name));
        else if (card) setTimeout(() => showLooseCard(card), 0);   // after the dialog closed and gave focus back
        return r;
      },
    });
  }
  /** A card whose workspace model no persona is bound to yet: shown in the panel, from the import's answer. */
  function showLooseCard(card) {
    SF.drawer.open({
      key: 'pe-card:' + card.model_id, title: String(card.name || card.model_id), eyebrow: h('span', { class: 'tag tag-ok' }, 'Profile card saved'), wide: true,
      sub: 'No persona is bound to Open WebUI’s ' + card.model_id + ' yet. A persona file with “openwebui: ' + card.model_id + '” (and its models entry) makes it one; until then this is what the card says.',
      body: [...cardSections(card, null, null), capabilityMap(card.capability_map)],
    });
  }

  // ================================================================ chats (SF.extend: chatStart, chatHead, chatBanner, chatReply)
  /** New chat's persona start: POST /api/chats {persona} for an Open WebUI persona or an enabled offline copy. */
  SF.extend('chatStart', () => {
    const sel = h('select', { class: 'select', id: 'nc-persona' });
    const note = h('div', { class: 'field-hint', role: 'status' });
    const failed = () => !P.rows && !!P.error;   // GET /api/personas refused (a persona file that does not parse: 400)
    const fill = () => {
      const keep = sel.value;
      SF.clear(sel);
      SF.clear(note);
      if (failed()) {
        sel.disabled = true;
        SF.add(note, [h('strong', null, 'Your personas could not be loaded. '), h('span', { class: 'pe-err-text' }, P.error.message || String(P.error)), ' ', h('a', { href: '#/personas' }, 'Personas'), ' · ', h('a', { href: '#/doctor' }, 'Doctor')]);
        const dlg = sel.closest('dialog');
        const radio = dlg ? dlg.querySelector('input[data-start="persona"]') : null;
        if (radio && !radio.checked) { radio.disabled = true; if (radio.closest('label')) radio.closest('label').classList.add('is-disabled'); }
        return;
      }
      sel.disabled = false;
      const rows = P.rows || [];
      const ok = rows.filter((r) => canChat(r).ok);
      for (const r of ok) sel.appendChild(h('option', { value: r.name }, String(r.label || r.name)));
      if (ok.some((r) => r.name === keep)) sel.value = keep;
      const no = rows.filter((r) => r.kind !== 'wake' && !canChat(r).ok);
      const wakes = rows.filter((r) => r.kind === 'wake').map((r) => r.name);
      SF.add(note, P.rows ? [ok.length ? 'Her chat has no repository and nothing in it goes to the factory. ' : 'No persona can start a chat yet. ',
        no.length ? ['Not startable: ', no.map((r, i) => [i ? '; ' : '', h('strong', null, r.name), ' (' + canChat(r).why.replace(/[.]$/, '') + ')']), '. '] : '',
        wakes.length ? wakes.join(', ') + (wakes.length > 1 ? ' wake themselves and are never a chat: see Messages & Wakes. ' : ' wakes herself and is never a chat: see Messages & Wakes. ') : '',
        h('a', { href: '#/personas' }, 'Personas')] : 'Loading your personas…');
    };
    return {
      id: 'persona', title: 'With a persona', desc: failed() ? 'Your personas could not be loaded: see the Personas page.' : 'Kate through Open WebUI, or an offline copy on this machine. No repository.',
      disabled: failed() || (!!P.rows && !(P.rows || []).some((r) => canChat(r).ok)),
      // Every persona file is offered here or nowhere: the No repository start leaves these names out.
      claims: () => (P.rows || []).map((r) => r.name),
      fields: () => { fill(); if (!P.rows || Date.now() - P.at > 5000) loadRows(true).then(fill); return [SF.field('Persona', sel), note]; },
      body: () => {
        if (failed()) throw new Error('Your personas could not be loaded: ' + (P.error.message || String(P.error)));
        if (!sel.value) throw new Error('Pick a persona: import one on the Personas page first.');
        return { persona: sel.value };
      },
      onError: (e) => h('span', { class: 'pe-err-help' }, 'Nothing was sent to a model. ', helpLinks(e && e.message)),
    };
  });
  /** A persona chat's head (DESIGN §15 "A persona chat's header"): label, offline-copy badge, version, base model,
   *  and what does not come through the API. */
  SF.extend('chatHead', (chat) => {
    const p = isObj(chat.persona) ? chat.persona : null;
    if (!p) return null;
    const row = rowOf(chat.agent);
    const out = [h('span', { class: 'tag tag-accent pe-chat-label', title: 'What answers in this chat' }, SF.icon('persona', 'icon-xs'), String(p.label || chat.agent))];
    if (p.type === 'snapshot') out.push(tag('offline copy', 'tag-local', { title: 'It runs on this machine’s own model, not through Open WebUI, and does not follow later changes.' }));
    if (p.version_id !== undefined) out.push(h('span', { class: 'meta-item pe-chat-version', title: 'Version ' + String(p.version_id || 'unknown') }, 'version ', h('code', null, shortV(p.version_id))));
    if (isObj(p.base_model)) out.push(h('span', { class: 'meta-item pe-chat-base' }, 'base model ' + String(p.base_model.id || '?') + (p.base_model.listed === false ? ' (not listed for this key)' : ', connection ' + String(p.base_model.connection_type || 'not labelled'))));
    if (Array.isArray(p.not_through_api) && p.not_through_api.length) out.push(tag('not available here: ' + p.not_through_api.join(', '), 'tag-outline pe-chat-not', { title: 'Her own Open WebUI chats have these; a chat through the API does not.' }));
    if (row && row.role === 'operator') out.push(tag([SF.icon('shield', 'icon-xs'), 'Operator'], 'tag-outline', { title: OPERATOR_HELP }));
    out.push(h('a', { class: 'meta-item', href: '#/personas/' + SF.enc(chat.agent) }, SF.icon('file', 'icon-xs'), 'Profile card'));
    return out;
  });
  const DISMISS_KEY = 'sf.personas.dismissed';
  function textHash(s) { let x = 5381; s = String(s); for (let i = 0; i < s.length; i++) x = ((x << 5) + x + s.charCodeAt(i)) | 0; return (x >>> 0).toString(36); }
  function dismissed(chatId) {
    try { const all = JSON.parse(SF.storage.get(DISMISS_KEY) || '{}'); return isObj(all) && Array.isArray(all[chatId]) ? all[chatId] : []; } catch (e) { return []; }
  }
  function dismiss(chatId, hash, btn) {
    // A keyboard user stays in the banners: focus moves to the next warning's button (else the chat's title) before
    // the head is rebuilt, which keeps the focused control by its data-focus key.
    if (btn && document.activeElement === btn) {
      const buttons = Array.from(document.querySelectorAll('.pe-chat-warning [data-focus^="pe-dismiss-"]'));
      const i = buttons.indexOf(btn);
      const next = buttons[i + 1] || buttons[i - 1] || (btn.closest('.chat-head') || document).querySelector('.chat-title');
      if (next) next.focus({ preventScroll: true });
    }
    let all = {};
    try { all = JSON.parse(SF.storage.get(DISMISS_KEY) || '{}') || {}; } catch (e) { all = {}; }
    if (!isObj(all)) all = {};
    all[chatId] = Array.from(new Set((Array.isArray(all[chatId]) ? all[chatId] : []).concat([hash]))).slice(-20);
    const keys = Object.keys(all);
    if (keys.length > 50) for (const k of keys.slice(0, keys.length - 50)) delete all[k];
    SF.storage.set(DISMISS_KEY, JSON.stringify(all));
    SF.refreshJobs();
  }
  /** Banners under a chat's head: her warnings (dismissible per chat, back when the text changes), a drift that
   *  changed what she does, a stale offline copy, and a turn refused because the agent became a persona. */
  SF.extend('chatBanner', (chat) => {
    const out = [];
    const p = isObj(chat.persona) ? chat.persona : null;
    const who = p ? String(p.name || title(chat.agent)) : '';
    if (p && Array.isArray(p.warnings)) {
      const gone = dismissed(chat.id);
      p.warnings.forEach((w, i) => {
        const hash = textHash(w);
        if (gone.indexOf(hash) >= 0) return;
        const x = SF.btn('', { cls: 'btn-ghost btn-icon btn-xs', icon: 'x', aria: 'Dismiss this warning for this chat', title: 'Dismiss for this chat', dataset: { focus: 'pe-dismiss-' + i }, onClick: (e) => dismiss(chat.id, hash, e.currentTarget) });
        // Open on a wide screen; on a phone one line until opened (the choice is kept while the chat re-renders).
        const key = chat.id + ':' + hash;
        const open = P.warnOpen.has(key) ? P.warnOpen.get(key) : !SF.isNarrow();
        const det = h('details', { class: 'pe-grow pe-warn-details', open, dataset: { focus: 'pe-warn-' + i } },
          h('summary', null, h('strong', null, 'Where your words go: '), h('span', { class: 'pe-warn-first' }, String(w))), h('p', { class: 'pe-warn-full' }, sentence(w), ' The chat still runs.'));
        det.addEventListener('toggle', () => P.warnOpen.set(key, det.open));
        out.push(h('div', { class: 'callout callout-warn pe-chat-warning', dataset: { warning: String(i) } }, SF.icon('alert', 'icon-sm'), det, x));
      });
    }
    const drift = p && isObj(p.drift) ? p.drift : null;
    if (drift && (drift.kind === 'behaviour' || drift.kind === 'unknown')) {
      out.push(h('div', { class: 'callout callout-warn pe-chat-drift' }, SF.icon('alert', 'icon-sm'), h('div', null,
        h('strong', null, who + ' changed in Open WebUI'), drift.changed_at ? [' ', SF.timeEl(drift.changed_at)] : null,
        drift.kind === 'unknown' ? ' (it may be cosmetic: this key cannot read the prompt and settings)' : '', drift.item ? ['; see ', itemLink(drift.item)] : null, '.')));
    }
    if (p && p.type === 'snapshot' && p.stale) {
      out.push(h('div', { class: 'callout callout-info pe-chat-stale' }, SF.icon('info', 'icon-sm'), h('div', null,
        h('strong', null, 'The live preset has changed since this copy'), ' (' + String(p.stale) + '): re-import it to follow Open WebUI. ', h('a', { href: '#/personas/' + SF.enc(chat.agent) }, 'Its page'))));
    }
    const err = chat.status === 'error' && typeof chat.error === 'string' ? chat.error : '';
    let m = /^agent '([a-z0-9][a-z0-9-]{0,40})' is now a persona: its chats run only as persona chats/.exec(err);
    if (m) {
      const name = m[1];
      out.push(h('div', { class: 'callout callout-info pe-chat-now' }, SF.icon('persona', 'icon-sm'), h('div', { class: 'pe-grow' },
        h('strong', null, name + ' is a persona now.'), ' Nothing was sent to a model. Her chats run only as persona chats, on this machine and on her own model.'),
      SF.btn('Start a chat with ' + name, { cls: 'btn-sm btn-primary', icon: 'chat', dataset: { focus: 'pe-start-' + name, action: 'persona-chat' }, onClick: (e) => startChat(name, e.currentTarget) })));
    }
    m = /^agent '([a-z0-9][a-z0-9-]{0,40})' is now a persona that wakes itself/.exec(err);
    if (m) {
      out.push(h('div', { class: 'callout callout-info pe-chat-now' }, SF.icon('bell', 'icon-sm'), h('div', { class: 'pe-grow' },
        h('strong', null, m[1] + ' wakes herself now.'), ' Nothing was sent to a model; this chat cannot go on with her. She talks to you in her own thread.'),
      h('a', { class: 'btn btn-sm', href: wakeHref(m[1]) }, 'Her page')));
    }
    m = /^([a-z0-9][a-z0-9-]{0,40}) is an imported copy, and its offline mode is not enabled/.exec(err);
    if (m) {
      out.push(h('div', { class: 'callout callout-info pe-chat-now' }, SF.icon('server', 'icon-sm'), h('div', { class: 'pe-grow' },
        h('strong', null, 'Chats with ' + m[1] + ' are off.'), ' Turn them on on its page, then send your message again.'),
      h('a', { class: 'btn btn-sm', href: '#/personas/' + SF.enc(m[1]) }, 'Its page')));
    }
    return out;
  });
  function windowText(w) {
    if (!isObj(w) || !w.started_at) return '';
    const a = String(w.started_at), b = String(w.ended_at || '');
    const t = (s) => (/T(\d\d:\d\d:\d\d(\.\d+)?)/.exec(s) || [null, s])[1];
    const day = a.slice(0, 10) !== b.slice(0, 10) && b ? ' (' + a.slice(0, 10) + ')' : '';
    return 'Asked ' + String(w.model || 'Open WebUI') + ' ' + t(a) + (b ? ' to ' + t(b) : '') + ' UTC' + day;
  }
  /** persona.turns' newest entry for transcript turn k: a turn that failed after its message was saved has an entry
   *  of its own, and the next turn's reply (its text folded into that message) carries the same k. */
  function turnEntry(turns, k) {
    for (let i = turns.length - 1; i >= 0; i--) if (turns[i].turn === k) return turns[i];
    return null;
  }
  /** Under each reply of a persona chat: the imported copy's label, a reply answered by a newer version, and the
   *  time window Open WebUI's own log can be matched with. A saved reply is matched by its transcript turn; a live
   *  run by its own persona.turn event (its NN is the log's number, not the transcript's), and only once it started:
   *  a refused turn sent nothing to a model and gets no line. */
  SF.extend('chatReply', (x) => {
    const chat = x.chat;
    const p = chat && isObj(chat.persona) ? chat.persona : null;
    if (!p) return null;
    const out = [];
    const run = x.run;
    const ran = !!run && (run.started === true || run.text === true);
    const label = x.message ? x.message.label : (p.type === 'snapshot' && ran ? 'from the imported copy: ' + String(p.label || chat.agent) : null);
    if (label) out.push(h('div', { class: 'pe-reply-label' }, SF.icon('server', 'icon-xs'), String(label)));
    const turns = Array.isArray(p.turns) ? p.turns.filter(isObj) : [];
    let cur = null;
    let prevV = null;
    if (x.message && Number.isInteger(x.turn)) {
      cur = turnEntry(turns, x.turn);
      const prev = turnEntry(turns, x.turn - 1);
      prevV = prev ? prev.version_id : null;
    } else if (ran) {
      const ev = P.turns.get(chat.id + ':' + run.name);
      if (ev) { cur = { turn: ev.turn, version_id: ev.version_id }; prevV = ev.previous_version_id; }
    }
    const who = String(p.name || title(chat.agent));
    if (cur) {
      if (cur.version_id === null && p.type === 'openwebui') out.push(h('div', { class: 'pe-reply-version is-unknown' }, SF.icon('info', 'icon-xs'), 'The version that answered could not be read.'));
      else if (prevV && cur.version_id && prevV !== cur.version_id) out.push(h('div', { class: 'pe-reply-version' }, SF.icon('alert', 'icon-xs'), 'Answered by a newer ' + who + ' (version ' + shortV(prevV) + ' → ' + shortV(cur.version_id) + ')'));
    }
    const win = (cur && isObj(cur.window) ? cur.window : null) || (ran ? P.windows.get(chat.id + ':' + run.name) : null);
    if (win) out.push(h('div', { class: 'pe-reply-window', title: 'Match it with Open WebUI’s own log' }, SF.icon('clock', 'icon-xs'), windowText(win)));
    return out.length ? h('div', { class: 'pe-reply' }, out) : null;
  });

  // ================================================================ the view
  P.handle = SF.registerView({
    id: 'personas',
    title: 'Personas',
    routes: ['personas'],
    nav: { label: 'Personas', icon: 'persona', order: 75 },
    home: {
      title: 'Personas', icon: 'persona', order: 30, href: '#/personas',
      text: 'Talk to your Open WebUI personas from here, keep offline copies of them, and meet the ones that wake themselves to check in.',
      routes: [{ to: 'messages', label: 'check-ins, notices' }, { to: 'tracker', label: 'Open WebUI changes' }],
      events: ['persona.drift', 'persona.updated', 'persona.imported', 'agent.updated'],
      /** GET /api/personas → {personas: [persona_view rows]} (server.api_personas, personas.persona_view). */
      async load() {
        const rows = (await loadRows(true)) || [];
        if (P.error && !P.rows) throw P.error;
        if (!rows.length) return { tone: null, text: 'no personas yet', detail: 'Import an Open WebUI profile to add one.' };
        const kinds = {};
        for (const p of rows) kinds[p.kind] = (kinds[p.kind] || 0) + 1;
        const errors = rows.filter((p) => p.error).length;
        const changed = rows.filter((p) => p.kind === 'openwebui' && p.drift && (p.drift.kind === 'behaviour' || p.drift.kind === 'unknown')).length;
        const COUNT_WORDS = { openwebui: ['through Open WebUI', 'through Open WebUI'], snapshot: ['offline copy', 'offline copies'], wake: ['wakes herself', 'wake themselves'] };
        const detail = Object.keys(kinds).map((k) => kinds[k] + ' ' + (COUNT_WORDS[k] ? COUNT_WORDS[k][kinds[k] === 1 ? 0 : 1] : k)).join(' · ');
        // "Needs you": a persona file that cannot start (the fix is in her file). A change in Open WebUI is the
        // tracker's item (system scope), listed there, so it is not listed twice.
        const needs = rows.filter((p) => p.error).map((p) => ({ key: 'persona:error:' + p.name, tone: 'warn', icon: 'persona', href: personaHref(p),
          text: (p.instance_name && p.instance_name !== p.name ? p.instance_name + ' (' + p.name + ')' : p.name) + ' cannot start', sub: sentence(p.error) }));
        if (changed) return { tone: 'warn', text: SF.plural(changed, 'persona') + ' changed in Open WebUI', detail, needs };
        if (errors) return { tone: 'warn', text: SF.plural(errors, 'persona') + ' cannot start', detail, needs };
        return { tone: 'ok', text: SF.plural(rows.length, 'persona'), detail, needs };
      },
    },
    start() { loadRows(true); },
    enter(route) {
      loadRows(!P.rows);
      const name = route.parts[0];
      if (name && P.rows) { const t = target(name); if (t.row && t.row.kind !== 'wake') loadPage(t.row.name, true); }
    },
    events: {
      'persona.drift': () => reloadSoon(),
      'persona.imported': () => reloadSoon(),
      'persona.updated': () => reloadSoon(),
      'agent.updated': () => reloadSoon(),
      'persona.turn': (ev) => {
        const d = ev.data || {};
        // Keyed by the run (its log stem): the event's turn is the transcript's, which a live run's NN does not
        // follow once a turn was refused (DESIGN §15; chats._transcript_turn).
        if (d.chat && d.run) { P.turns.set(d.chat + ':' + d.run, d); if (P.turns.size > 400) P.turns.delete(P.turns.keys().next().value); SF.refreshJobs(); }
      },
      'persona.window': (ev) => {
        const d = ev.data || {};
        if (d.chat && d.run) { P.windows.set(d.chat + ':' + d.run, d); if (P.windows.size > 400) P.windows.delete(P.windows.keys().next().value); SF.refreshJobs(); }
      },
    },
    render(root, route) {
      if (route.parts[0]) renderPage(root, route); else renderList(root);
    },
  });
  SF.onReconnect(() => { P.pages.clear(); loadRows(true); });
})(window.SF);
