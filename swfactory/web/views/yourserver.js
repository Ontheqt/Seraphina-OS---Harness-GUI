/* Seraphina OS Harness: Your server (DESIGN §42; docs/seraphina/YOUR_SERVER.md §1.3, §2, §3, §4, §7.2, §8, §10,
 * §11.5), phases P1a to P1c. This area only looks: it shows how the server and its apps are doing and changes nothing
 * on the server, except the two things the owner switches on himself and the page then names: refreshing Ubuntu's
 * list of updates, and putting a name on each app's current and previous version so a clean-up never deletes it. The
 * Maintain and Fix buttons (the design's P2) and root-access changes (P3) are not built, and no button here starts,
 * stops, restarts, updates or installs anything.
 * Routes: #/apps (the host strip, the first-run review list while nothing is confirmed, the app cards, Beszel, a "Not
 * shown" line; before Seraphina's system access is set up, one summary card), #/apps/review (the review list),
 * #/apps/<a-id> (one app: its card, parts, addresses, QR code and Add to Home Screen, should it run, maintenance,
 * backups and How to go back, its latest log lines, Ask about this app, what changed), #/server (what Seraphina may
 * do, the Check buttons, previous versions, Beszel, links), #/server/root (who can take full control, view only),
 * #/server/receipts (the records of changes), #/server/setup (the one paste, the same steps one at a time,
 * switching it off, removing it). Rail: "Your server", order 5, on the phone's bar in Plans' place (decision D21).
 * Home: its card, err when an app is down, human for the review list, warn for a new way to take full control.
 * Names, labels and addresses are shown as text, never markup; links pass SF.safeHref and open in a new tab with
 * rel="noopener noreferrer", never at Seraphina's own address. Every sentence a person reads is in WORDS, which the
 * word check of tests/test_yourserver.py reads (U8). */
'use strict';
(function (SF) {
  const WORDS = {
    title: 'Your server',
    sub: 'Every app with its address, and how your server is doing now.',
    homeText: 'Every app with its address, how your server is doing now, and who can take full control of it.',
    looksOnly: 'This area only looks. Seraphina reads how your server and its apps are doing and changes nothing.',
    hostTitle: 'Your server now',
    hostEmpty: 'No numbers yet.',
    apps: 'Apps',
    noApps: 'No apps found. Seraphina looks for Dockge stacks, other compose folders, containers and system services that you open in a browser.',
    summaryTitle: 'Your apps',
    setUp: 'Set it up',
    turnOn: 'Turn it on',
    turnOnAsk: 'Turn on Your server? Seraphina will read how your server and its apps are doing every few minutes. She changes nothing on the server.',
    turnedOn: 'Your server is on.',
    offTitle: 'Your server is off',
    offText: 'It is turned off in the settings. The numbers above need nothing installed.',
    accessTitle: 'Seraphina’s system access',
    accessOff: 'Seraphina’s system access is not set up yet. The numbers above need nothing installed.',
    open: 'Open', qr: 'QR code', hideQr: 'Hide the QR code', details: 'Details',
    checkNow: 'Check now', checking: 'Checking…', checked: 'Read again.',
    tone: { err: 'Down', warn: 'Needs a look', ok: 'Working', muted: 'Unknown', off: 'Off' },
    ram: 'RAM', cpu: 'CPU', gpu: 'GPU', ofServer: 'of the server',
    version: 'Version', items: 'open items', item: 'open item',
    noAddress: 'No address found.', typeOne: 'Type the one you use',
    confirmAsk: 'Is this the address you use for',
    yes: 'Yes', notThis: 'No, it’s another one', homeOnly: 'I only use it at home',
    outside: 'This address was changed outside Seraphina. Is it still right?',
    proposedLabel: 'Found:',
    homeOnlyTag: 'Only at home', tailnetTag: 'On your Tailscale network', gatewayTag: 'Through your login gateway', signIn: 'asks for your sign-in',
    reviewTitle: 'Check your apps once',
    reviewSub: 'Seraphina found these apps. Check the names and addresses; the ticked ones look certain. Untick or change anything that is wrong.',
    reviewDone: 'These are right',
    reviewSaved: 'Saved. The cards use these addresses now.',
    shouldRun: 'Should it run?', run: 'Yes, it should run', keepOff: 'No, keep it off', askMe: 'Not sure yet',
    oldCopyOf: 'This is an old copy of',
    notOldCopy: 'Not an old copy',
    name: 'Name', address: 'Address', otherAddress: 'Another address…', typed: 'The address you use',
    tailnetLine: 'Your Tailscale network:', thatsMine: 'That’s mine',
    tailnetSaved: 'Saved: addresses on this network can be opened from here.',
    shares: 'Shares its Tailscale connection with',
    oldCopies: 'Old copies', oldCopy: 'Old copy, stopped since', retire: 'Retire it', partOf: 'It is part of this app', keepIt: 'It is an old copy',
    retired: 'Retired', copyState: { proposed: 'Is this an old copy?', confirmed: 'Old copy', retired: 'Retired', part: 'Part of this app' }, bootsWarn: 'This old copy tries to start whenever the server starts. To stop that, run this in your terminal (Seraphina changes no container):',
    jobRuns: 'Job runs',
    notShown: 'Not shown:', hidden: 'hidden', oneOff: ['one-off run', 'one-off runs'], jobs: ['job run', 'job runs'], copiesShort: ['old copy', 'old copies'], unreadable: 'unreadable',
    parts: 'Parts', running: 'running', stopped: 'stopped', candidates: 'Addresses found',
    qrHelp: 'Scan this with your phone’s camera. It opens the app only while Tailscale is on on that phone.',
    addHome: 'Add it to your phone’s home screen', iphone: 'iPhone or iPad', android: 'Android',
    gatewayCert: 'This address uses your login gateway’s own certificate, so your phone will warn before opening it. Use the Tailscale address on your phone.',
    expectTitle: 'Should it be running?', itShouldRun: 'It should run', keepItOff: 'Keep it off', expectSaved: 'Saved.',
    maintenance: 'Maintenance', planned: 'Planned: Seraphina does not update apps yet; this is kept for when she does.',
    modeSaved: 'Saved.',
    phoneName: 'Its name may appear on your phone', phoneSaved: 'Saved.',
    backups: 'Backups', restore: 'Restore available:',
    rename: 'Rename', note: 'Your note', save: 'Save', saved: 'Saved.', hide: 'Hide this app', unhide: 'Show this app again',
    askTitle: 'Ask about this app',
    askSub: 'The facts below come first. A question goes, with these facts only, to the AI model on this server.',
    askWhat: 'What is this?', askHealthy: 'Is it healthy?', askChanged: 'What changed?',
    askPlaceholder: 'Why did it restart last night?', ask: 'Ask', asking: 'Asking…', aiLabel: 'AI, from the facts above',
    changes: 'What changed', noChanges: 'Nothing recorded yet.',
    updates: 'Updates',
    exposure: 'Who can reach it',
    managed: 'What manages it',
    beszelTitle: 'History (Beszel)', beszelLink: 'History in Beszel',
    beszelSteps: ['Seraphina makes a password for this login and keeps it. Copy it once:',
      'Open Beszel, open the menu at the top right and choose Users. Sign in there with Beszel’s admin login if it asks.',
      'Press New record. Email: anything you like, for example seraphina@localhost. Password: paste the one you copied, twice. Role: readonly. Press Create.',
      'In the same menu choose Systems, open your server’s row, add the new login under Users (leave the others there), and press Save.',
      'Back here, type the email you chose and press Check.'],
    makePassword: 'Make the password', beszelEmail: 'The login’s email', beszelLoginTitle: 'Give Seraphina a read-only Beszel login',
    serverTitle: 'Your server: system access',
    serverSub: 'What Seraphina may do on your server, and where her history comes from.',
    caller: 'It answers only Seraphina’s own account:',
    later: 'Installing updates, cleaning up and fixes come with later versions. They are not built here.',
    terminal: 'Open terminal (Cockpit)', noTerminal: 'Set Cockpit’s address in the links below to open its terminal from here.',
    links: 'Links', linksSub: 'The addresses of the tools Seraphina links to. Each must be on your Tailscale network, your home network or this machine.',
    linkDockge: 'Dockge', linkCockpit: 'Cockpit', linkBeszel: 'Beszel',
    setupTitle: 'Set up Seraphina’s system access',
    setupSub: 'A small program lets Seraphina look at your server’s layout, because only the administrator may. You install it with one paste in your own terminal; Seraphina never runs sudo.',
    stage: 'Prepare the files', staged: 'The files are ready in', checkIt: 'Check it',
    expect: 'Expect:', ifNot: 'If not:', noteLabel: 'Note:', copy: 'Copy', copied: 'Copied',
    detailsSteps: 'The same steps one at a time', removal: 'Switch it off or remove it',
    back: 'Back to Your server', backServer: 'Back',
    loadError: 'Could not load Your server:',
    unknownPage: 'This page is not here yet: it comes with a later version.',
    labLine: 'Not checked on your server yet',
    notFound: 'This app is not on your server any more.',
    allWorking: 'All {n} apps working.', someNeed: '{n} need you.', oneNeeds: '1 needs you.', appsDown: '{n} down.',
    homeOff: 'Off: turn it on to see your apps.', homeNoAccess: 'Your apps show once Seraphina’s system access is set up.',
    reviewNeed: 'Check {n} app names and addresses',
    stale: 'Last read',
    alsoMay: 'You allowed two more things:',
    mayTitle: 'What Seraphina may do', mayLead: 'Seraphina may:',
    checksTitle: 'Check your server', checksSub: 'Each check only looks: nothing on your server changes when you press one.',
    check: 'Check', checkAll: 'Check everything', notChecked: 'Not checked yet.', exactCommands: 'The exact commands',
    showHow: 'Show how', refreshList: 'Refresh the list', refreshing: 'Refreshing the list. This takes a minute.',
    biggest: 'Biggest folders on', measure: 'Measure', folders: 'Shown here only, never kept.',
    keepTitle: 'Previous versions', keepNow: 'Keep the current versions now',
    keepOn: 'Seraphina puts a name on each app’s current version, and the first time on the version before it, so a clean-up never deletes them. She adds it by herself after each update, and removes nothing.',
    keepOffText: 'Keeping previous versions is not switched on. To turn it on, tick it on the setup page and paste the line again.',
    keepLast: 'Last kept:', keepLooked: 'Last looked:', keepTried: 'Last try:',
    keepSome: 'Only some were kept: Docker did not add every name.', keepAlready: 'Every app’s current version was already kept.',
    keepFailed: 'Nothing was kept: Docker did not add the names, so a clean-up could still delete these versions.',
    keepUnknown: 'Seraphina does not know what it kept.',
    rootLink: 'Who can take full control', receiptsLink: 'Records of changes',
    rootTitle: 'Who can take full control of your server?', rootSub: 'Every way Seraphina can find, in plain words. This page only looks.',
    lookAgain: 'Look again', why: 'Why', since: 'Since', knownBox: 'I know about this', noteFor: 'Your note',
    newTag: 'New', knownTag: 'Known', wayTag: 'Full control', escapeTag: 'Only if something escapes',
    cantSee: 'What Seraphina can’t see from here', notRead: 'Not read this time:', goneRows: 'Gone since you last looked',
    rootWait: 'This page waits until these are true:', explainTitle: 'See the same list in your own terminal',
    changeLater: 'Changes', rootNever: 'Seraphina has not looked yet.',
    receiptsTitle: 'Records of changes', receiptsSub: 'Everything Seraphina’s system access changed, newest first, each with how to undo it.',
    noReceipts: 'Nothing has been changed yet.', wayBack: 'How to undo it:', verified: 'Checked:', notVerified: 'Not checked:',
    unknownOutcome: 'Seraphina does not know whether this ran. It is never sent again by itself.', more: 'Older records',
    outcome: { ok: 'Done', succeeded: 'Done', partly: 'Partly done', failed: 'Failed', refused: 'Refused', nothing: 'Nothing to do', unknown: 'Outcome unknown',
      nothing_verified: 'Reported, nothing checked', partly_verified: 'Reported, partly checked', reported_verified: 'Reported checked by the operator',
      outcome_unknown: 'Outcome unknown (reported)', reported_failed: 'Reported failed' },
    changeNames: { 'keep.images': 'Keeping previous versions', 'apt.refresh': 'Refreshing the list of updates' },
    // the changes whose failed record changed nothing: it has no way back, and its checks are about nothing
    failedNone: { 'keep.images': 'Docker did not add the names, so none were kept and there is nothing to undo.' },
    retry: 'Try again',
    logsTitle: 'Latest log lines', logsShow: 'Show the latest lines', logsOlder: 'Older lines', logsPart: 'Part', logsNone: 'No lines.',
    logsDockge: 'Open its logs in Dockge', logsCockpit: 'Open its logs in Cockpit', logsNote: 'Shown here only and never kept. Anything that looks like a password or a key is hidden before it reaches this page.',
    backTitle: 'How to go back', backCopy: 'Copy the line', backOpen: 'Open the stack in Dockge', backFrom: 'Kept version from',
    health: { healthy: 'Docker: healthy', unhealthy: 'Docker: unhealthy', starting: 'Docker: starting' },
    stageAlso: 'Also allow (each is off until you paste the line again):',
    inCockpit: 'Open in Cockpit', recordsLink: 'Records of changes', showRows: 'Show all {n}', securityTag: 'Security fix',
    logsMain: 'Its main part',
    checkTone: { err: 'Needs you', warn: 'Needs a look', ok: 'Fine', muted: 'Unknown', off: 'Off' },
  };
  const TONE_PILL = { err: 'tone-err', warn: 'tone-warn', ok: 'tone-ok', muted: 'tone-muted', off: 'tone-neutral' };
  // A record's outcome as a tone: the helper's own (ok, partly, failed, refused, nothing) and the operator's reported
  // ones (receipts.OUTCOMES, never green: SF.words.receiptTone).
  const OUTCOME_TONE = Object.assign({ ok: 'ok', succeeded: 'ok', partly: 'warn', failed: 'err', refused: 'err', nothing: 'muted', unknown: 'warn' }, SF.words.receiptTone);
  const Y = { view: null, error: null, loading: null, again: false, apps: null, appsError: null, app: null, appId: '', appError: null, appBusy: '', appAgain: false,
    review: null, reviewError: null, reviewBusy: false, rows: null, setup: null, setupError: null, checkWords: '', ask: { id: '', answer: null, busy: false },
    phone: 'iphone', qr: new Map(), busy: false, password: '', checks: null, checksError: null, poll: null,
    root: null, rootError: null, receipts: null, receiptsError: null, logs: { id: '', data: null, busy: false, member: '', memberApp: '' },
    stageClasses: null, mount: '' };

  // ---------------------------------------------------------------- data
  function load(handle) {
    if (Y.loading) { Y.again = true; return Y.loading; }
    Y.again = false;
    Y.loading = Promise.all([SF.api('/api/yourserver'), SF.api('/api/yourserver/apps')])
      .then(([v, a]) => { Y.view = SF.isObj(v) ? v : null; Y.apps = SF.isObj(a) ? a : null; Y.error = null; })
      .catch((e) => { Y.error = e; })
      .finally(() => { Y.loading = null; if (Y.again) { load(handle); return; } if (handle) handle.refresh(); });
    return Y.loading;
  }
  /** One app's page. A render asks only while nothing is loaded for id: an answer, or a failure, is kept until Try
   *  again, a new visit or an event asks again (force), so a failed page never asks again at every frame. One request
   *  per app at a time; a forced one asked meanwhile runs once that one is back. */
  async function loadApp(id, handle, force) {
    if (!force && Y.appId === id && (Y.app || Y.appError)) return;
    if (Y.appBusy === id) { if (force) Y.appAgain = true; return; }
    Y.appBusy = id;
    Y.appAgain = false;
    let answer = null;
    let error = null;
    try { answer = await SF.api('/api/yourserver/apps/' + SF.enc(id)); } catch (e) { error = e; }
    if (Y.appBusy !== id) return;   // the page moved to another app meanwhile (enter): this answer is not its
    Y.appBusy = '';
    Y.app = error ? null : answer;
    Y.appError = error;
    Y.appId = id;
    handle.refresh();
    if (Y.appAgain) { Y.appAgain = false; await loadApp(id, handle, true); }
  }
  /** The review list's row as Seraphina proposes it; touched names the fields the owner changed. */
  const freshRow = (r) => ({ app: r.app, ticked: !!r.ticked, name: r.name, url: r.url || '', typed: '', home_only: !!r.home_only, expect: r.expect || '', old_copy: '', touched: {} });
  /** keep: read the list again but keep what the owner changed in it, by app (after That's mine). One read at a time
   *  from the page, so a second answer never arrives after the owner began editing and resets the list. */
  async function loadReview(handle, keep) {
    const old = keep && Y.rows && Y.review ? new Map((Y.review.rows || []).map((r, i) => [r.app, Y.rows[i]])) : null;
    Y.reviewBusy = true;
    try {
      Y.review = await SF.api('/api/yourserver/review');
      Y.reviewError = null;
      Y.rows = old ? (Y.review.rows || []).map((r) => {
        const was = old.get(r.app);
        const row = freshRow(r);
        if (was) for (const k of Object.keys(was.touched || {})) { row[k] = was[k]; row.touched[k] = true; }
        return row;
      }) : null;
    } catch (e) { Y.reviewError = e; } finally { Y.reviewBusy = false; }
    handle.refresh();
  }
  async function loadSetup(handle) {
    try { Y.setup = await SF.api('/api/yourserver/setup'); Y.setupError = null; } catch (e) { Y.setupError = e; }
    handle.refresh();
  }
  async function post(path, body, button, done) {
    SF.setBusy(button, true);
    try { const r = await SF.api(path, { method: 'POST', body: body || {} }); if (done) await done(r); return r; } catch (e) { SF.toastError(null, e); return null; } finally { SF.setBusy(button, false); }
  }

  // ---------------------------------------------------------------- pieces
  const tag = (text, cls) => SF.h('span', { class: 'tag' + (cls ? ' ' + cls : '') }, String(text));
  /** An Open link: only a plain http(s) address (SF.safeHref), never at Seraphina's own host, in a new tab. */
  function openLink(url, label, cls, action) {
    const href = SF.safeHref(url);
    if (!href) return null;
    let host = '';
    try { host = new URL(href).hostname; } catch (e) { return null; }
    if (host === location.hostname) return null;
    return SF.h('a', { class: cls === 'link' ? 'ys-plain-link' : 'btn ' + (cls || 'btn-sm btn-primary'), href, target: '_blank', rel: 'noopener noreferrer',
      dataset: { action: action || 'open' } }, cls === 'link' ? null : SF.icon('external', 'icon-sm'), label || WORDS.open, cls === 'link' ? SF.icon('external', 'icon-xs') : null);
  }
  const nameOf = (id) => { const c = ((Y.apps || {}).apps || []).find((x) => x.id === id); return c ? c.name : id; };
  function section(id, title, sub, body) {
    return SF.h('section', { class: 'home-card ys-section', 'aria-labelledby': 'ys-' + id + '-t', dataset: { part: id } },
      SF.h('h2', { class: 'home-card-title', id: 'ys-' + id + '-t' }, title), sub ? SF.h('p', { class: 'muted' }, sub) : null, body);
  }
  function light(l) {
    l = l || { tone: 'muted', reason: '' };
    return SF.h('div', { class: 'ys-light', dataset: { tone: l.tone, code: l.code || '' } },
      SF.h('span', { class: 'pill ' + (TONE_PILL[l.tone] || 'tone-muted') }, WORDS.tone[l.tone] || l.tone),
      SF.h('span', { class: 'ys-reason' }, String(l.reason || '')));
  }
  function hostStrip(view) {
    const host = (view && view.host) || {};
    const items = host.items || [];
    return SF.h('section', { class: 'ys-strip', 'aria-label': WORDS.hostTitle, dataset: { part: 'host' } },
      SF.h('span', { class: 'ys-strip-title' }, WORDS.hostTitle),
      items.length ? items.map((i) => SF.h('span', { class: 'ys-chip', dataset: { key: i.key, tone: i.tone || 'ok' } }, String(i.words || ''))) : SF.h('span', { class: 'muted' }, WORDS.hostEmpty));
  }
  function looksOnly() {
    const extra = ((Y.view || {}).looks_extra || []);
    return SF.h('div', { class: 'ys-looks-box', dataset: { part: 'looks-only' } },
      SF.h('p', { class: 'ys-looks muted' }, SF.icon('eye', 'icon-sm'), ' ', WORDS.looksOnly),
      extra.length ? SF.h('p', { class: 'ys-looks muted', dataset: { part: 'looks-extra' } }, WORDS.alsoMay + ' ' + extra.map(String).join('; ') + '.') : null);
  }
  function qrGrid(code) {
    const size = Number(code.size) || 0;
    const quiet = 4;
    const n = size + 2 * quiet;
    const cells = [];
    for (let r = 0; r < n; r++) {
      const row = r >= quiet && r < quiet + size ? String(code.rows[r - quiet] || '') : '';
      for (let c = 0; c < n; c++) {
        const dark = row && c >= quiet && c < quiet + size && row.charAt(c - quiet) === '1';
        cells.push(SF.h('span', { class: dark ? 'ys-qr-d' : 'ys-qr-l' }));
      }
    }
    return SF.h('div', { class: 'ys-qr', role: 'img', 'aria-label': WORDS.qr, dataset: { size: String(size) }, style: { 'grid-template-columns': 'repeat(' + n + ', 1fr)' } }, cells);
  }
  async function toggleQr(id, handle) {
    if (Y.qr.has(id)) { Y.qr.delete(id); handle.refresh(); return; }
    try { Y.qr.set(id, await SF.api('/api/yourserver/apps/' + SF.enc(id) + '/qr')); } catch (e) { SF.toastError(null, e); }
    handle.refresh();
  }
  function addressRow(card, handle, full) {
    const a = card.address || {};
    const out = [];
    if (a.state === 'confirmed' && a.url) {
      out.push(SF.h('div', { class: 'ys-address', dataset: { state: 'confirmed' } },
        SF.h('span', { class: 'ys-url' }, String(a.url)),
        SF.h('span', { class: 'ys-actions' }, openLink(a.url),
          a.qr ? SF.btn(Y.qr.has(card.id) ? WORDS.hideQr : WORDS.qr, { cls: 'btn-sm', icon: 'grid', dataset: { action: 'qr', app: card.id }, onClick: () => toggleQr(card.id, handle) }) : null),
        a.home_only ? tag(WORDS.homeOnlyTag, 'tag-outline') : null,
        a.source === 'gateway' ? tag(WORDS.gatewayTag, 'tag-outline') : null));
      if (Y.qr.has(card.id)) out.push(SF.h('div', { class: 'ys-qr-box', dataset: { part: 'qr' } }, qrGrid(Y.qr.get(card.id)), SF.h('p', { class: 'muted' }, WORDS.qrHelp)));
    } else if (a.state === 'changed_outside' || a.state === 'refused') {
      out.push(SF.callout('warn', 'alert', null, String(a.words || WORDS.outside)));
      if (a.saved && a.state === 'changed_outside') out.push(addressAsk(card, a.saved, a.candidate, handle, true));
    } else if ((a.state === 'proposed' || a.state === 'changed') && a.proposed) {
      out.push(addressAsk(card, a.proposed, a.proposed_candidate, handle, false));
    } else {
      out.push(SF.h('div', { class: 'ys-address muted', dataset: { state: a.state || 'none' } }, WORDS.noAddress, ' ',
        SF.h('a', { href: '#/apps/' + SF.enc(card.id), dataset: { action: 'type-address' } }, WORDS.typeOne)));
    }
    return out;
  }
  /** "Is this the address you use for Photos? <url> [Yes] [No, it's another one] [I only use it at home]". For a saved
   *  address changed outside Seraphina, Yes and home-only name the address shown (set, which the server checks
   *  again), never a found one that may differ from it; No names no candidate, so only the saved address goes and
   *  the found one whose fingerprint it carries may still be proposed. */
  function addressAsk(card, url, candidate, handle, saved) {
    const reload = async () => { await load(handle); if (Y.appId === card.id) await loadApp(card.id, handle, true); };
    const path = '/api/yourserver/apps/' + SF.enc(card.id) + '/address';
    const homeOnly = !!(card.address || {}).home_only;
    const yes = SF.btn(WORDS.yes, { cls: 'btn-sm btn-primary', dataset: { action: 'confirm-address', app: card.id } });
    yes.addEventListener('click', () => post(path, saved ? { action: 'set', url, home_only: homeOnly } : { action: 'confirm', candidate }, yes, reload));
    const no = SF.btn(WORDS.notThis, { cls: 'btn-sm', dataset: { action: 'reject-address', app: card.id } });
    no.addEventListener('click', () => post(path, saved ? { action: 'reject' } : { action: 'reject', candidate }, no, reload));
    const home = SF.btn(WORDS.homeOnly, { cls: 'btn-sm', dataset: { action: 'home-address', app: card.id } });
    home.addEventListener('click', () => post(path, saved ? { action: 'set', url, home_only: true } : { action: 'home_only', candidate }, home, reload));
    return SF.h('div', { class: 'ys-ask-address', dataset: { state: saved ? 'saved' : 'proposed' } },
      SF.h('div', null, WORDS.confirmAsk + ' ', SF.h('strong', null, String(card.name)), '?'),
      SF.h('div', { class: 'ys-url' }, String(url)),
      SF.h('div', { class: 'actions ys-buttons' }, yes, no, home));
  }
  function numbersLine(card) {
    const n = card.numbers || {};
    const bits = [];
    if (n.ram_words) bits.push(WORDS.ram + ' ' + n.ram_words);
    if (n.cpu_percent !== null && n.cpu_percent !== undefined) bits.push(WORDS.cpu + ' ' + n.cpu_percent + '%');
    if (n.gpu_mib) bits.push(WORDS.gpu + ' ' + (n.gpu_mib / 1024).toFixed(1) + ' GB');
    return bits.length ? SF.h('div', { class: 'ys-numbers muted' }, bits.join(' · ')) : null;
  }
  function appCard(card, handle) {
    const managed = card.managed || {};
    const managedLink = managed.link ? openLink(managed.link, managed.words, 'link', 'manager') : null;
    const versionWords = [card.version && card.version.value ? WORDS.version + ' ' + card.version.value : null, card.version && card.version.words].filter(Boolean).join(' · ');
    const backup = ((card.backup || {}).lines || [])[0];
    return SF.h('article', { class: 'home-card ys-card' + (card.light && card.light.tone ? ' has-' + card.light.tone : ''), dataset: { app: card.id, tone: (card.light || {}).tone || '' } },
      SF.h('div', { class: 'home-card-head' }, SF.h('span', { class: 'home-card-icon' }, SF.icon(card.icon || 'server')),
        SF.h('h3', { class: 'home-card-title ys-name' }, SF.h('a', { href: '#/apps/' + SF.enc(card.id), dataset: { action: 'app' } }, String(card.name)))),
      light(card.light),
      addressRow(card, handle),
      numbersLine(card),
      versionWords ? SF.h('div', { class: 'ys-version muted' }, versionWords) : null,
      card.updates && (card.updates.kinds || []).length ? SF.h('div', { class: 'ys-updates', dataset: { part: 'updates', kind: card.updates.kinds[0].kind } }, SF.icon('download', 'icon-sm'), ' ', String(card.updates.words || '')) : null,
      card.health && WORDS.health[card.health] ? tag(WORDS.health[card.health], card.health === 'healthy' ? 'tag-ok' : 'tag-warn') : null,
      SF.h('div', { class: 'ys-managed muted' }, managedLink || String(managed.words || '')),
      card.maintenance ? SF.h('div', { class: 'ys-maintenance muted' }, String(card.maintenance.words || '')) : null,
      backup ? SF.h('div', { class: 'ys-backup muted' }, String(backup.words || '')) : null,
      card.exposure ? SF.callout('warn', 'shield', null, String(card.exposure)) : null,
      (card.shares_with || []).length ? SF.h('div', { class: 'muted ys-shares' }, WORDS.shares + ' ' + card.shares_with.map(nameOf).join(', ')) : null,
      card.items ? SF.h('a', { class: 'muted', href: '#/tracker' }, card.items + ' ' + (card.items === 1 ? WORDS.item : WORDS.items)) : null);
  }
  function notShownLine(apps) {
    const ns = (apps && apps.not_shown) || {};
    const bits = [];
    if ((ns.hidden || []).length) bits.push(ns.hidden.length + ' ' + WORDS.hidden);
    const count = (n, pair) => n + ' ' + pair[n === 1 ? 0 : 1];
    if (ns.old_copies) bits.push(count(ns.old_copies, WORDS.copiesShort));
    if (ns.retired) bits.push(ns.retired + ' ' + WORDS.retired.toLowerCase());
    if (ns.one_off) bits.push(count(ns.one_off, WORDS.oneOff));
    if (ns.job_runs) bits.push(count(ns.job_runs, WORDS.jobs));
    if (ns.unknown_format) bits.push(ns.unknown_format + ' ' + WORDS.unreadable);
    if (!bits.length) return null;
    return SF.h('p', { class: 'muted ys-not-shown', dataset: { part: 'not-shown' } }, WORDS.notShown + ' ' + bits.join(', ') + '.',
      (ns.hidden || []).length ? [' ', ns.hidden.map((h, i) => [i ? ', ' : '', SF.h('a', { href: '#/apps/' + SF.enc(h.id) }, String(h.name))])] : null);
  }
  function beszelCard(view, handle) {
    const b = view && view.beszel;
    if (!b) return null;
    const link = b.link ? openLink(b.link, WORDS.beszelLink, 'btn-sm', 'history') : null;
    let login = null;
    if (b.code === 'no_login' || b.login_error) {
      const make = SF.btn(WORDS.makePassword, { cls: 'btn-sm', icon: 'shield', dataset: { action: 'beszel-password' } });
      make.addEventListener('click', () => post('/api/yourserver/beszel', { action: 'password' }, make, async (r) => { Y.password = String(r.password || ''); handle.refresh(); }));
      const email = SF.h('input', { class: 'input input-sm', type: 'email', placeholder: 'seraphina@localhost', value: Y.beszelEmail || '', 'aria-label': WORDS.beszelEmail, dataset: { focus: 'beszel-email' } });
      email.addEventListener('input', () => { Y.beszelEmail = email.value; });   // a refresh keeps what was typed
      const check = SF.btn(WORDS.check, { cls: 'btn-sm', icon: 'check', dataset: { action: 'beszel-check' } });
      // The password is shown only this once: it goes only when the login works, so a failed Check leaves it to paste again.
      check.addEventListener('click', () => post('/api/yourserver/beszel', { action: 'check', email: email.value.trim() }, check, async (r) => {
        const seen = r.beszel || {};
        const works = !seen.login_error && (seen.code === 'ok' || seen.code === 'not_recording');
        if (works) Y.password = '';
        SF.toast(String(r.words || ''), { kind: works ? 'ok' : 'error' });
        await load(handle);
      }));
      const copy = Y.password ? SF.btn(WORDS.copy, { cls: 'btn-xs', icon: 'copy', dataset: { action: 'copy-password' }, onClick: () => SF.copyText(Y.password, WORDS.copied) }) : null;
      login = SF.h('details', { class: 'ys-beszel-steps', open: !!Y.password || !!Y.beszelOpen, dataset: { part: 'beszel-login' } }, SF.h('summary', null, String(b.note || WORDS.beszelLoginTitle)),
        SF.h('ol', null, WORDS.beszelSteps.map((s, i) => SF.h('li', null, s, i === 0 ? [' ', make, Y.password ? SF.h('code', { class: 'ys-mono', dataset: { part: 'password' } }, Y.password) : null, ' ', copy] : null,
          i === 4 ? SF.h('div', { class: 'actions ys-buttons' }, email, check) : null))));
    }
    if (login) login.addEventListener('toggle', () => { Y.beszelOpen = login.open; });   // a refresh keeps it as the owner left it
    return section('beszel', WORDS.beszelTitle, null, [light(b), b.login_error ? SF.h('p', { class: 'muted' }, String(b.login_error)) : null, login,
      link ? SF.h('div', { class: 'actions' }, link) : null]);
  }
  function summaryCard(view, handle) {
    const s = view.summary || {};
    const helper = view.helper || {};
    const setUp = SF.h('a', { class: 'btn btn-sm btn-primary', href: '#/server/setup', dataset: { action: 'set-up' } }, SF.icon('tool', 'icon-sm'), WORDS.setUp);
    const why = helper.state && helper.state !== 'absent' && helper.state !== 'ready' ? SF.callout(helper.state === 'unsafe' ? 'err' : 'warn', 'alert', null, String(helper.words || '')) : null;
    return SF.h('section', { class: 'home-card ys-summary', dataset: { part: 'summary', state: helper.state || '' } },
      SF.h('div', { class: 'home-card-head' }, SF.h('span', { class: 'home-card-icon' }, SF.icon('server')), SF.h('h2', { class: 'home-card-title' }, WORDS.summaryTitle)),
      SF.h('p', { class: 'ys-summary-words' }, String(s.words || WORDS.accessOff)), why, SF.h('div', { class: 'actions' }, setUp));
  }
  function offCard(handle) {
    const on = SF.btn(WORDS.turnOn, { icon: 'play', cls: 'btn-sm btn-primary', dataset: { action: 'turn-on' } });
    on.addEventListener('click', async () => {
      if ((await SF.confirmModal({ title: WORDS.turnOn, description: WORDS.turnOnAsk, confirm: WORDS.turnOn })) !== 'ok') return;
      await post('/api/config', { yourserver: { enabled: true } }, on, async () => { SF.toast(WORDS.turnedOn); await load(handle); });
    });
    return SF.h('section', { class: 'home-card ys-off', dataset: { part: 'off' } }, SF.h('h2', { class: 'home-card-title' }, WORDS.offTitle),
      SF.h('p', { class: 'muted' }, WORDS.offText), SF.h('div', { class: 'actions' }, on, SF.h('a', { class: 'btn btn-sm', href: '#/server/setup' }, WORDS.setUp)));
  }
  function tailnetLine(tailnet, handle) {
    if (!tailnet || tailnet.suffix || !tailnet.proposed) return null;
    const mine = SF.btn(WORDS.thatsMine, { cls: 'btn-sm btn-primary', dataset: { action: 'tailnet' } });
    mine.addEventListener('click', () => post('/api/config', { yourserver: { tailnet: { suffix: tailnet.proposed, confirmed_at: new Date().toISOString().slice(0, 19) + 'Z' } } }, mine,
      async () => { SF.toast(WORDS.tailnetSaved); await load(handle); if (Y.review) await loadReview(handle, true); }));
    return SF.h('div', { class: 'callout callout-info ys-tailnet', dataset: { part: 'tailnet' } }, SF.icon('cloud', 'icon-sm'),
      SF.h('div', null, WORDS.tailnetLine + ' ', SF.h('strong', null, String(tailnet.proposed)), ' ', mine));
  }

  // ---------------------------------------------------------------- the review list (§2.3)
  function reviewRows(review, handle) {
    if (!Y.rows) Y.rows = (review.rows || []).map(freshRow);
    const others = (review.rows || []).map((r) => [r.app, r.name]);
    const list = SF.h('ul', { class: 'check-list ys-review' }, (review.rows || []).map((r, i) => {
      const st = Y.rows[i];
      const touch = (...keys) => { for (const k of keys) st.touched[k] = true; };
      const tick = SF.h('input', { type: 'checkbox', checked: st.ticked, 'aria-label': String(r.name), dataset: { focus: 'tick-' + r.app, action: 'tick' } });
      tick.addEventListener('change', () => { st.ticked = tick.checked; touch('ticked'); });
      const name = SF.h('input', { class: 'input input-sm', value: st.name, 'aria-label': WORDS.name, maxlength: '80', dataset: { focus: 'name-' + r.app, field: 'name' } });
      name.addEventListener('input', () => { st.name = name.value; st.ticked = true; tick.checked = true; touch('name', 'ticked'); });
      // st.url is what the select shows: a found address, '' for none, or '__typed' for the typed box. The typed
      // address is sent only while it is the choice, so one typed and then left for a found address is never sent.
      const choices = [['', WORDS.noAddress]].concat([r.url].concat(r.others || []).filter(Boolean).map((u) => [u, u])).concat([['__typed', WORDS.otherAddress]]);
      const addr = SF.selectEl(choices, st.url, { 'aria-label': WORDS.address, dataset: { focus: 'addr-' + r.app, field: 'url' } });
      const typed = SF.h('input', { class: 'input input-sm', value: st.typed, placeholder: WORDS.typed, 'aria-label': WORDS.typed, hidden: st.url !== '__typed', dataset: { focus: 'typed-' + r.app, field: 'typed' } });
      addr.addEventListener('change', () => { st.url = addr.value; typed.hidden = addr.value !== '__typed'; st.ticked = true; tick.checked = true; touch('url', 'ticked'); });
      typed.addEventListener('input', () => { st.typed = typed.value.trim(); touch('typed'); });
      const home = SF.h('input', { type: 'checkbox', checked: st.home_only, dataset: { field: 'home_only' } });
      home.addEventListener('change', () => { st.home_only = home.checked; touch('home_only'); });
      const expect = SF.selectEl([['run', WORDS.run], ['off', WORDS.keepOff], ['', WORDS.askMe]], st.expect, { 'aria-label': WORDS.shouldRun, dataset: { field: 'expect' } });
      expect.addEventListener('change', () => { st.expect = expect.value; st.ticked = true; tick.checked = true; touch('expect', 'ticked'); });
      const copyOf = SF.selectEl([['', WORDS.notOldCopy]].concat(others.filter(([id]) => id !== r.app).map(([id, n]) => [id, WORDS.oldCopyOf + ' ' + n])), st.old_copy, { 'aria-label': WORDS.oldCopyOf, dataset: { field: 'old_copy' } });
      copyOf.addEventListener('change', () => { st.old_copy = copyOf.value; st.ticked = true; tick.checked = true; touch('old_copy', 'ticked'); });
      return SF.h('li', { class: 'check-row ys-row', dataset: { app: r.app, ticked: r.ticked ? '1' : '0' } }, tick,
        SF.h('div', { class: 'check-main ys-row-main' },
          SF.h('div', { class: 'ys-row-head' }, name, light(r.light)),
          SF.h('div', { class: 'ys-row-fields' }, addr, typed,
            SF.h('label', { class: 'ys-inline' }, home, ' ', WORDS.homeOnly), expect, copyOf),
          SF.h('div', { class: 'muted ys-row-sub' }, [r.words, r.managed].filter(Boolean).join(' · ')),
          (r.old_copies || []).length ? SF.h('div', { class: 'muted' }, WORDS.oldCopies + ': ' + r.old_copies.map((o) => o.name).join(', ')) : null));
    }));
    const done = SF.btn(WORDS.reviewDone, { cls: 'btn-primary', icon: 'check', dataset: { action: 'review-done' } });
    done.addEventListener('click', async () => {
      const rows = [];
      (review.rows || []).forEach((r, i) => {
        const st = Y.rows[i];
        if (!st.ticked) return;
        const row = { app: r.app };
        if (st.name && st.name !== r.name) row.name = st.name;
        const url = st.url === '__typed' ? st.typed : st.url;
        if (url) row.url = url;
        if (st.home_only) row.home_only = true;
        if (st.expect) row.expect = st.expect;
        if (st.old_copy) row.old_copy = st.old_copy;
        rows.push(row);
      });
      await post('/api/yourserver/review', { rows }, done, async () => { SF.toast(WORDS.reviewSaved); Y.rows = null; Y.review = null; await load(handle); SF.go('#/apps'); });
    });
    return [SF.h('p', null, String(review.words || '')), tailnetLine(review.tailnet, handle), list, SF.h('div', { class: 'actions', style: { 'margin-top': '10px' } }, done)];
  }

  // ---------------------------------------------------------------- pages
  /** A page that could not load, with Try again: a failed load is asked again only from here, a new visit or an event. */
  function failed(e, what, again) {
    return [SF.unavailable(e, what), SF.h('div', { class: 'actions' }, SF.btn(WORDS.retry, { cls: 'btn-sm', icon: 'refresh', dataset: { action: 'retry' }, onClick: again }))];
  }
  function head(handle) {
    const now = SF.btn(Y.busy ? WORDS.checking : WORDS.checkNow, { icon: 'refresh', cls: 'btn-sm', disabled: Y.busy, dataset: { action: 'check-now', focus: 'check-now' } });
    now.addEventListener('click', async () => {
      Y.busy = true; handle.refresh();
      await post('/api/yourserver/refresh', {}, now, async () => { SF.toast(WORDS.checked); await load(handle); });
      Y.busy = false; handle.refresh();
    });
    return SF.viewHead(WORDS.title, WORDS.sub, SF.h('div', { class: 'actions' }, now, SF.h('a', { class: 'btn btn-sm', href: '#/server', dataset: { action: 'server' } }, SF.icon('server', 'icon-sm'), WORDS.accessTitle)));
  }
  function appsPage(root, handle) {
    if (Y.error && !Y.view) { SF.rebuild(root, [SF.viewHead(WORDS.title, WORDS.sub), failed(Y.error, WORDS.title, () => load(handle))]); return; }
    if (!Y.view) { SF.rebuild(root, [SF.viewHead(WORDS.title, WORDS.sub), SF.skeleton(4)]); return; }
    const v = Y.view;
    const apps = Y.apps || {};
    const body = [head(handle), looksOnly(), hostStrip(v)];
    if (!v.enabled) body.push(offCard(handle));
    else if (!v.helper || v.helper.state !== 'ready' || v.summary) body.push(summaryCard(v, handle));
    else {
      if (apps.review && apps.review.pending) {
        body.push(SF.h('section', { class: 'home-card ys-review-card', dataset: { part: 'review' } }, SF.h('h2', { class: 'home-card-title' }, WORDS.reviewTitle),
          SF.h('p', { class: 'muted' }, WORDS.reviewSub), SF.h('a', { class: 'btn btn-primary btn-sm', href: '#/apps/review', dataset: { action: 'review' } }, SF.icon('check', 'icon-sm'), WORDS.reviewTitle)));
      }
      body.push(tailnetLine(v.tailnet, handle));
      if (apps.words) body.push(SF.callout(apps.docker_active === false ? 'warn' : 'info', 'info', null, String(apps.words)));
      if (apps.stale && apps.observed_at) body.push(SF.h('p', { class: 'muted ys-stale' }, WORDS.stale + ' ', SF.timeEl(apps.observed_at)));
      body.push(SF.h('div', { class: 'home-grid ys-grid', role: 'list', dataset: { part: 'apps' } }, (apps.apps || []).map((c) => SF.h('div', { class: 'home-cell', role: 'listitem' }, appCard(c, handle)))));
      body.push(notShownLine(apps));
      body.push(beszelCard(v, handle));
    }
    body.push(SF.h('p', { class: 'muted ys-lab', style: { 'font-size': 'var(--fs-sm)' } }, String(v.lab_line || '')));
    SF.rebuild(root, body);
  }
  function reviewPage(root, handle) {
    if (!Y.review && !Y.reviewError && !Y.reviewBusy) loadReview(handle);
    const back = SF.h('a', { class: 'btn btn-sm', href: '#/apps', dataset: { action: 'back' } }, SF.icon('chevron-left', 'icon-sm'), WORDS.back);
    if (Y.reviewError) { SF.rebuild(root, [SF.viewHead(WORDS.reviewTitle, WORDS.reviewSub, back), SF.unavailable(Y.reviewError, WORDS.reviewTitle)]); return; }
    if (!Y.review) { SF.rebuild(root, [SF.viewHead(WORDS.reviewTitle, WORDS.reviewSub, back), SF.skeleton(6)]); return; }
    SF.rebuild(root, [SF.viewHead(WORDS.reviewTitle, WORDS.reviewSub, back), looksOnly(), ...reviewRows(Y.review, handle)]);
  }
  function kvList(rows) {
    return SF.h('dl', { class: 'kv' }, rows.filter(([, v]) => v !== null && v !== undefined && v !== '').map(([k, v]) => [SF.h('dt', null, k), SF.h('dd', null, v instanceof Node ? v : String(v))]));
  }
  function appPage(root, id, handle) {
    loadApp(id, handle, false);
    const back = SF.h('a', { class: 'btn btn-sm', href: '#/apps', dataset: { action: 'back' } }, SF.icon('chevron-left', 'icon-sm'), WORDS.back);
    if (Y.appError && Y.appId === id) { SF.rebuild(root, [SF.viewHead(WORDS.title, null, back), Y.appError.status === 404 ? SF.emptyState('server', WORDS.notFound, null, null) : failed(Y.appError, WORDS.title, () => loadApp(id, handle, true))]); return; }
    if (!Y.app || Y.appId !== id) { SF.rebuild(root, [SF.viewHead(WORDS.title, null, back), SF.skeleton(6)]); return; }
    const d = Y.app;
    const card = d.app || {};
    const reload = async () => { await loadApp(id, handle, true); load(handle); };
    // the card, then the address
    const parts = SF.h('ul', { class: 'check-list ys-parts' }, (d.parts || []).map((p) => SF.h('li', { class: 'check-row', dataset: { part: p.key } },
      SF.h('span', { class: 'check-icon ' + (p.running ? 'ok' : 'warn') }, SF.icon(p.running ? 'check' : 'stop')),
      SF.h('div', { class: 'check-main' }, SF.h('div', { class: 'check-name' }, String(p.service || p.name || ''), ' ', tag(p.running ? WORDS.running : WORDS.stopped, p.running ? 'tag-ok' : 'tag-warn'), p.tailscale ? tag('Tailscale', 'tag-outline') : null),
        SF.h('div', { class: 'muted ys-mono' }, String(p.name || ''), ' · ', String(p.image || '')),
        (p.privileges || []).length ? SF.h('div', { class: 'muted' }, p.privileges.join(', ')) : null))));
    const candidates = (d.candidates || []).length ? SF.h('ul', { class: 'ys-candidates' }, d.candidates.map((c) => SF.h('li', { dataset: { candidate: c.id } },
      SF.h('span', { class: 'ys-url' }, String(c.url)), ' ', tag(c.words || c.source, 'tag-outline'), c.sign_in ? ' ' : null, c.sign_in ? tag(WORDS.signIn, 'tag-outline') : null,
      card.address && card.address.state !== 'confirmed' || (card.address && card.address.candidate !== c.id) ? SF.btn(WORDS.yes, { cls: 'btn-xs', dataset: { action: 'use-candidate', candidate: c.id },
        onClick: (e) => post('/api/yourserver/apps/' + SF.enc(id) + '/address', { action: 'confirm', candidate: c.id }, e.currentTarget, reload) }) : null))) : null;
    const typed = SF.h('input', { class: 'input input-sm', placeholder: WORDS.typed, 'aria-label': WORDS.typed, dataset: { focus: 'typed' } });
    const setTyped = SF.btn(WORDS.save, { cls: 'btn-sm', dataset: { action: 'set-address' } });
    setTyped.addEventListener('click', () => post('/api/yourserver/apps/' + SF.enc(id) + '/address', { action: 'set', url: typed.value.trim() }, setTyped, reload));
    // add to home screen
    let homeSteps = null;
    if (d.home_steps) {
      const pick = SF.selectEl([['iphone', WORDS.iphone], ['android', WORDS.android]], Y.phone, { 'aria-label': WORDS.addHome, dataset: { focus: 'phone' } });
      pick.addEventListener('change', () => { Y.phone = pick.value; handle.refresh(); });
      homeSteps = SF.h('div', { class: 'ys-home-steps', dataset: { part: 'home-steps' } }, SF.h('strong', null, WORDS.addHome), ' ', pick,
        SF.h('ol', null, (d.home_steps[Y.phone] || []).map((s) => SF.h('li', null, String(s)))));
    }
    // should it run, maintenance, phone name
    const expect = (card.expect || {});
    const runBtn = SF.btn(WORDS.itShouldRun, { cls: 'btn-sm' + (expect.state === 'run' ? ' btn-primary' : ''), dataset: { action: 'expect-run' } });
    runBtn.addEventListener('click', () => post('/api/yourserver/apps/' + SF.enc(id), { expect: 'run' }, runBtn, async () => { SF.toast(WORDS.expectSaved); await reload(); }));
    const offBtn = SF.btn(WORDS.keepItOff, { cls: 'btn-sm' + (expect.state === 'off' ? ' btn-primary' : ''), dataset: { action: 'expect-off' } });
    offBtn.addEventListener('click', () => post('/api/yourserver/apps/' + SF.enc(id), { expect: 'off' }, offBtn, async () => { SF.toast(WORDS.expectSaved); await reload(); }));
    const m = card.maintenance || {};
    const modes = (d.modes || []).map((k) => [k, (d.mode_words || {})[k] || k]);
    const mode = SF.selectEl(modes, m.mode || 'inherit', { 'aria-label': WORDS.maintenance, dataset: { focus: 'mode', field: 'maintenance' } });
    mode.addEventListener('change', () => post('/api/yourserver/apps/' + SF.enc(id), { maintenance: { mode: mode.value } }, null, async () => { SF.toast(WORDS.modeSaved); await reload(); }));
    const phone = SF.h('input', { type: 'checkbox', checked: !!card.phone_name, dataset: { focus: 'phone-name', field: 'phone_name' } });
    phone.addEventListener('change', () => post('/api/yourserver/apps/' + SF.enc(id), { phone_name: phone.checked }, null, async () => { SF.toast(WORDS.phoneSaved); await reload(); }));
    // rename, note, hide
    const name = SF.h('input', { class: 'input input-sm', value: card.name || '', maxlength: '80', 'aria-label': WORDS.rename, dataset: { focus: 'rename' } });
    const note = SF.h('input', { class: 'input input-sm', value: card.note || '', maxlength: '500', 'aria-label': WORDS.note, dataset: { focus: 'note' } });
    const save = SF.btn(WORDS.save, { cls: 'btn-sm', dataset: { action: 'save-name' } });
    save.addEventListener('click', () => post('/api/yourserver/apps/' + SF.enc(id), { name: name.value.trim() || null, note: note.value.trim() || null }, save, async () => { SF.toast(WORDS.saved); await reload(); }));
    const hidden = Y.apps && ((Y.apps.not_shown || {}).hidden || []).some((h) => h.id === id);
    const hide = SF.btn(hidden ? WORDS.unhide : WORDS.hide, { cls: 'btn-sm', dataset: { action: 'hide' } });
    hide.addEventListener('click', () => post('/api/yourserver/apps/' + SF.enc(id), { hidden: !hidden }, hide, reload));
    // old copies
    const copies = (card.old_copies || []).length ? SF.h('ul', { class: 'check-list ys-copies' }, card.old_copies.map((o) => SF.h('li', { class: 'check-row', dataset: { copy: o.key, state: o.state } },
      SF.h('div', { class: 'check-main' }, SF.h('div', { class: 'check-name' }, String(o.name || ''), ' ', tag(WORDS.copyState[o.state] || WORDS.copyState.confirmed, 'tag-outline')),
        o.stopped_at ? SF.h('div', { class: 'muted' }, WORDS.oldCopy + ' ', SF.timeEl(o.stopped_at)) : null,
        o.boots && o.fix ? SF.h('div', { class: 'ys-boots' }, SF.h('p', null, WORDS.bootsWarn), SF.h('pre', { class: 'code-block' }, String(o.fix)), SF.btn(WORDS.copy, { cls: 'btn-xs', icon: 'copy', onClick: () => SF.copyText(o.fix, WORDS.copied) })) : null,
        SF.h('div', { class: 'actions' },
          o.state !== 'retired' ? SF.btn(WORDS.retire, { cls: 'btn-xs', dataset: { action: 'retire' }, onClick: (e) => post('/api/yourserver/groups', { action: 'retire', app: id, member: o.key }, e.currentTarget, reload) }) : null,
          o.state === 'proposed' ? SF.btn(WORDS.keepIt, { cls: 'btn-xs', dataset: { action: 'keep' }, onClick: (e) => post('/api/yourserver/groups', { action: 'keep', app: id, member: o.key }, e.currentTarget, reload) }) : null,
          SF.btn(WORDS.partOf, { cls: 'btn-xs', dataset: { action: 'part' }, onClick: (e) => post('/api/yourserver/groups', { action: 'part', app: id, member: o.key }, e.currentTarget, reload) })))))) : null;
    // ask about this app
    const facts = d.facts || {};
    const question = SF.h('textarea', { class: 'input', rows: '2', placeholder: WORDS.askPlaceholder, 'aria-label': WORDS.askTitle, dataset: { focus: 'question' } });
    const askBtn = SF.btn(Y.ask.busy ? WORDS.asking : WORDS.ask, { cls: 'btn-sm btn-primary', dataset: { action: 'ask' }, disabled: Y.ask.busy });
    askBtn.addEventListener('click', async () => {
      if (!question.value.trim()) return;
      Y.ask = { id, answer: null, busy: true };
      handle.refresh();
      const r = await post('/api/yourserver/apps/' + SF.enc(id) + '/ask', { question: question.value.trim() }, askBtn);
      Y.ask = { id, answer: r, busy: false };
      handle.refresh();
    });
    const answer = Y.ask.id === id && Y.ask.answer ? SF.h('div', { class: 'ys-answer', dataset: { part: 'answer' } },
      Y.ask.answer.answer ? [tag(Y.ask.answer.label || WORDS.aiLabel, 'tag-accent'), SF.h('p', { class: 'ys-answer-text' }, String(Y.ask.answer.answer))] : SF.h('p', { class: 'muted' }, String(Y.ask.answer.words || ''))) : null;
    const what = facts.what || {};
    const healthy = facts.healthy || {};
    const changed = facts.changed || [];
    const backupLines = ((card.backup || {}).lines || []).map((l) => SF.h('li', { dataset: { line: l.key } }, String(l.words || '')));
    SF.rebuild(root, [
      SF.viewHead(String(card.name || id), null, back),
      looksOnly(),
      SF.h('section', { class: 'home-card ys-app', dataset: { app: id } }, light(card.light), addressRow(card, handle, true), numbersLine(card),
        d.gateway_cert ? SF.callout('info', 'info', null, WORDS.gatewayCert) : null, homeSteps,
        card.exposure ? SF.callout('warn', 'shield', null, String(card.exposure)) : null,
        kvList([[WORDS.managed, (card.managed || {}).link && openLink(card.managed.link, card.managed.words, 'link', 'manager') || (card.managed || {}).words],
          [WORDS.version, [card.version && card.version.value, card.version && card.version.words].filter(Boolean).join(' · ')], [WORDS.updates, (card.updates || {}).words],
          [WORDS.health.healthy.split(':')[0], card.health && WORDS.health[card.health] ? WORDS.health[card.health].split(': ')[1] : null]]),
        (card.links || {}).beszel ? openLink(card.links.beszel, WORDS.beszelLink, 'link', 'history') : null),
      section('addresses', WORDS.candidates, null, [candidates || SF.h('p', { class: 'muted' }, WORDS.noAddress), SF.h('div', { class: 'actions ys-buttons' }, typed, setTyped)]),
      section('expect', WORDS.expectTitle, null, SF.h('div', { class: 'actions ys-buttons' }, runBtn, offBtn)),
      section('parts', WORDS.parts, null, parts),
      copies ? section('copies', WORDS.oldCopies, null, copies) : null,
      d.job_runs && d.job_runs.length ? section('jobs', WORDS.jobRuns, null, SF.h('ul', null, d.job_runs.map((j) => SF.h('li', null, String(j.name || ''))))) : null,
      section('maintenance', WORDS.maintenance, m.planned ? WORDS.planned : null, [mode, SF.h('p', { class: 'muted' }, String(m.words || ''))]),
      section('backups', WORDS.backups, null, [SF.h('ul', { class: 'ys-backup-lines' }, backupLines), SF.h('p', { class: 'muted' }, WORDS.restore + ' ' + String((card.backup || {}).restore || '')), howBack(d)]),
      logsSection(id, d, handle),
      section('ask', WORDS.askTitle, WORDS.askSub, [
        SF.h('h3', { class: 'ys-h3' }, WORDS.askWhat), kvList([[WORDS.name, what.name], [WORDS.managed, what.managed_by], [WORDS.version, what.version], [WORDS.parts, (what.parts || []).map((p) => p.service || p.image).join(', ')]]),
        SF.h('h3', { class: 'ys-h3' }, WORDS.askHealthy), kvList([['', healthy.light], [WORDS.parts, (healthy.parts_running || 0) + ' / ' + (healthy.parts || 0) + ' ' + WORDS.running]]),
        SF.h('h3', { class: 'ys-h3' }, WORDS.askChanged), changed.length ? SF.h('ul', null, changed.slice(0, 8).map((c) => SF.h('li', null, String(c.kind || ''), ' ', c.at ? SF.timeEl(c.at) : null))) : SF.h('p', { class: 'muted' }, WORDS.noChanges),
        question, SF.h('div', { class: 'actions' }, askBtn), answer]),
      section('settings', WORDS.details, null, [SF.h('div', { class: 'ys-row-fields' }, name, note, save), SF.h('label', { class: 'ys-inline' }, phone, ' ', WORDS.phoneName), SF.h('div', { class: 'actions' }, hide)]),
      section('changes', WORDS.changes, null, (d.changes || []).length ? SF.h('ul', { class: 'ys-changes' }, d.changes.map((c) => SF.h('li', null, String(c.kind || ''), ' ', c.at ? SF.timeEl(c.at) : null))) : SF.h('p', { class: 'muted' }, WORDS.noChanges)),
    ]);
  }
  function serverPage(root, handle) {
    if (!Y.view && !Y.error && !Y.loading) load(handle);   // after a failure: Try again, a new visit or an event, never a render
    const back = SF.h('a', { class: 'btn btn-sm', href: '#/apps', dataset: { action: 'back' } }, SF.icon('chevron-left', 'icon-sm'), WORDS.back);
    if (!Y.view) { SF.rebuild(root, [SF.viewHead(WORDS.serverTitle, WORDS.serverSub, back), Y.error ? failed(Y.error, WORDS.title, () => load(handle)) : SF.skeleton(4)]); return; }
    const v = Y.view;
    const helper = v.helper || {};
    const links = (v.links || {});
    const cockpit = links.cockpit ? openLink(links.cockpit.replace(/\/+$/, '') + '/system/terminal', WORDS.terminal, 'btn-sm', 'terminal') : null;
    const fields = ['dockge', 'cockpit', 'beszel'].map((k) => {
      const input = SF.h('input', { class: 'input input-sm', value: links[k] || '', 'aria-label': WORDS['link' + k.charAt(0).toUpperCase() + k.slice(1)], dataset: { focus: 'link-' + k, link: k } });
      return SF.field(WORDS['link' + k.charAt(0).toUpperCase() + k.slice(1)], input);
    });
    const saveLinks = SF.btn(WORDS.save, { cls: 'btn-sm', dataset: { action: 'save-links' } });
    saveLinks.addEventListener('click', () => {
      const body = {};
      for (const f of root.querySelectorAll('[data-link]')) body[f.dataset.link] = f.value.trim() || null;
      post('/api/config', { yourserver: { links: body } }, saveLinks, async () => { SF.toast(WORDS.saved); await load(handle); });
    });
    const allow = v.allow || [];
    let keep = null;
    if (helper.state === 'ready') {
      const now = allow.includes('keep') ? SF.btn(WORDS.keepNow, { cls: 'btn-sm', icon: 'check', dataset: { action: 'keep-now' } }) : null;
      if (now) now.addEventListener('click', () => post('/api/yourserver/keep', {}, now, async (r) => { keepToast(r); await load(handle); }));
      keep = section('keep', WORDS.keepTitle, null, [SF.h('p', { class: 'muted' }, allow.includes('keep') ? WORDS.keepOn : WORDS.keepOffText), keepLast(v.keep), now ? SF.h('div', { class: 'actions' }, now) : null]);
    }
    SF.rebuild(root, [
      SF.viewHead(WORDS.serverTitle, WORDS.serverSub, back), looksOnly(), hostStrip(v),
      section('access', WORDS.accessTitle, null, [light({ tone: helper.state === 'ready' ? 'ok' : helper.state === 'unsafe' ? 'err' : 'muted', code: helper.state, reason: helper.words }),
        helper.caller ? SF.h('p', { class: 'muted' }, WORDS.caller + ' ' + String(helper.caller)) : null,
        (v.may || []).length ? SF.h('div', { dataset: { part: 'may' } }, SF.h('strong', null, WORDS.mayLead), SF.h('ul', null, v.may.map((m) => SF.h('li', null, String(m))))) : null,
        SF.h('p', { class: 'muted' }, WORDS.later),
        SF.h('div', { class: 'actions ys-buttons' }, SF.h('a', { class: 'btn btn-sm', href: '#/server/setup', dataset: { action: 'setup' } }, SF.icon('tool', 'icon-sm'), WORDS.setupTitle),
          SF.h('a', { class: 'btn btn-sm', href: '#/server/root', dataset: { action: 'root' } }, SF.icon('shield', 'icon-sm'), WORDS.rootLink),
          SF.h('a', { class: 'btn btn-sm', href: '#/server/receipts', dataset: { action: 'receipts' } }, SF.icon('file', 'icon-sm'), WORDS.recordsLink))]),
      checksSection(handle),
      keep,
      beszelCard(v, handle),
      section('terminal', WORDS.terminal, null, cockpit || SF.h('p', { class: 'muted' }, WORDS.noTerminal)),
      section('links', WORDS.links, WORDS.linksSub, [SF.h('div', { class: 'ys-links' }, fields), SF.h('div', { class: 'actions' }, saveLinks)]),
      section('lab', WORDS.labLine, null, SF.h('ul', null, (v.lab || []).map((c) => SF.h('li', { dataset: { lab: c.id } }, String(c.what || ''))))),
    ]);
  }
  /** What the keep pass just did, by its outcome. The answer's words say "already kept" whenever no name was added,
   *  so a pass Docker refused in part or in all is told in the page's own words, never in them. */
  function keepToast(r) {
    const o = ((r && r.keep) || {}).outcome;
    if (o === 'ok' || o === 'nothing') SF.toast(String(r.words || ''));
    else if (o === 'partly') SF.toast(WORDS.keepSome, { sticky: true });
    else SF.toast(o === 'failed' ? WORDS.keepFailed : WORDS.keepUnknown, { kind: 'error' });
  }
  /** The last keep pass by its outcome: "Last kept" only when it named something; a failed pass says nothing was kept. */
  function keepLast(k) {
    if (!k || !k.at) return null;
    const at = SF.timeEl(k.at);
    const o = k.outcome;
    if (o === 'ok' || o === 'partly') return SF.h('div', { class: 'muted', dataset: { part: 'keep-last', outcome: o } }, WORDS.keepLast + ' ', at, o === 'partly' ? ' ' + WORDS.keepSome : null);
    if (o === 'nothing') return SF.h('div', { class: 'muted', dataset: { part: 'keep-last', outcome: o } }, WORDS.keepLooked + ' ', at, ' ' + WORDS.keepAlready);
    return SF.h('div', { class: 'callout callout-warn', dataset: { part: 'keep-last', outcome: String(o || 'unknown') } }, SF.icon('alert', 'icon-sm'),
      SF.h('div', null, WORDS.keepTried + ' ', at, ' ' + (o === 'failed' ? WORDS.keepFailed : WORDS.keepUnknown)));
  }
  function block(b) {
    const text = (b.commands || []).join('\n');
    const copy = SF.btn(WORDS.copy, { cls: 'btn-xs', icon: 'copy', dataset: { action: 'copy', block: b.id } });
    copy.addEventListener('click', () => SF.copyText(text, WORDS.copied));
    return SF.h('li', { class: 'check-row ys-block', dataset: { block: b.id } },
      SF.h('span', { class: 'check-icon info' }, String(b.id)),
      SF.h('div', { class: 'check-main', style: { display: 'grid', gap: '6px', 'min-width': '0' } },
        SF.h('div', { class: 'check-name' }, String(b.title || '')),
        text ? SF.h('pre', { class: 'code-block ys-paste' }, text) : null,
        text ? SF.h('div', null, copy) : null,
        b.expect ? SF.h('div', null, SF.h('strong', null, WORDS.expect + ' '), String(b.expect)) : null,
        b.note ? SF.h('div', { class: 'muted' }, SF.h('strong', null, WORDS.noteLabel + ' '), String(b.note)) : null,
        b.if_not ? SF.h('div', { class: 'muted' }, SF.h('strong', null, WORDS.ifNot + ' '), String(b.if_not)) : null));
  }
  function setupPage(root, handle) {
    if (!Y.setup && !Y.setupError) loadSetup(handle);
    const back = SF.h('a', { class: 'btn btn-sm', href: '#/server', dataset: { action: 'back' } }, SF.icon('chevron-left', 'icon-sm'), WORDS.backServer);
    if (Y.setupError) { SF.rebuild(root, [SF.viewHead(WORDS.setupTitle, WORDS.setupSub, back), SF.unavailable(Y.setupError, WORDS.setupTitle)]); return; }
    if (!Y.setup) { SF.rebuild(root, [SF.viewHead(WORDS.setupTitle, WORDS.setupSub, back), SF.skeleton(4)]); return; }
    if (!Y.stageClasses) Y.stageClasses = new Set((Y.setup.classes || []).filter((c) => c.staged || c.on).map((c) => c.id));
    const classes = (Y.setup.classes || []).map((c) => {
      const box = SF.h('input', { type: 'checkbox', checked: Y.stageClasses.has(c.id), disabled: !!c.why_not, dataset: { cls: c.id, focus: 'class-' + c.id } });
      box.addEventListener('change', () => { if (box.checked) Y.stageClasses.add(c.id); else Y.stageClasses.delete(c.id); });
      return SF.h('li', null, SF.h('label', { class: 'ys-inline' }, box, ' ', String(c.words || c.id)), c.why_not ? SF.h('div', { class: 'muted ys-row-sub' }, String(c.why_not)) : null);
    });
    const stage = SF.btn(WORDS.stage, { icon: 'download', cls: 'btn-sm btn-primary', dataset: { action: 'stage' } });
    stage.addEventListener('click', () => post('/api/yourserver/setup/stage', { classes: Array.from(Y.stageClasses) }, stage, async (r) => { Y.setup = r; handle.refresh(); }));
    const check = SF.btn(WORDS.checkIt, { icon: 'check', cls: 'btn-sm', dataset: { action: 'check' } });
    check.addEventListener('click', () => post('/api/yourserver/setup/check', {}, check, async (r) => { Y.checkWords = String(r.words || ''); await load(handle); handle.refresh(); }));
    SF.rebuild(root, [
      SF.viewHead(WORDS.setupTitle, WORDS.setupSub, back), looksOnly(),
      classes.length ? SF.h('div', { class: 'ys-classes', dataset: { part: 'classes' } }, SF.h('p', null, WORDS.stageAlso), SF.h('ul', { class: 'ys-plain-list' }, classes)) : null,
      SF.h('div', { class: 'actions', style: { gap: '8px', 'flex-wrap': 'wrap' } }, stage, check),
      Y.setup.staged ? SF.h('p', { class: 'muted' }, WORDS.staged + ' ', SF.h('code', null, String(Y.setup.setup_dir || ''))) : null,
      Y.checkWords ? SF.callout('info', 'info', null, Y.checkWords) : null,
      SF.h('ol', { class: 'check-list ys-blocks', style: { 'margin-top': '12px' } }, (Y.setup.blocks || []).map(block)),
      SF.h('details', { class: 'ys-details', style: { 'margin-top': '12px' } }, SF.h('summary', { dataset: { action: 'details' } }, WORDS.detailsSteps),
        SF.h('ol', { class: 'check-list' }, (Y.setup.details || []).map(block))),
      section('removal', WORDS.removal, null, SF.h('ul', { class: 'check-list' }, (Y.setup.removal || []).map(block))),
    ]);
  }

  // ---------------------------------------------------------------- the Check buttons (§7.2)
  async function loadChecks(handle) {
    try { Y.checks = await SF.api('/api/yourserver/checks'); Y.checksError = null; } catch (e) { Y.checksError = e; }
    handle.refresh();
    const c = Y.checks || {};
    if (((c.running || []).length || (c.refresh && c.refresh.job)) && !Y.poll) {
      Y.poll = setTimeout(() => { Y.poll = null; if (handle.isShown()) loadChecks(handle); }, 1500);
    }
  }
  function rowList(rows, kind) {
    if (!(rows || []).length) return null;
    const list = SF.h('ul', { class: 'check-list ys-check-rows', dataset: { rows: kind || '' } }, rows.map((r) => SF.h('li', { class: 'check-row', dataset: { key: String(r.key || ''), tone: r.tone || '' } },
      SF.h('span', { class: 'check-icon ' + ({ err: 'err', warn: 'warn', ok: 'ok' }[r.tone] || 'info') }, SF.icon(r.tone === 'ok' ? 'check' : r.tone === 'err' ? 'alert' : r.tone === 'warn' ? 'info' : 'circle')),
      SF.h('div', { class: 'check-main' }, SF.h('div', { class: 'check-name' }, String(r.words || '')),
        r.sub ? SF.h('div', { class: 'muted ys-row-sub' }, String(r.sub)) : null,
        r.link ? openLink(r.link, WORDS.inCockpit, 'link', 'cockpit') : null,
        r.security ? tag(WORDS.securityTag, 'tag-warn') : null,
        r.app ? SF.h('a', { href: '#/apps/' + SF.enc(r.app), class: 'muted', dataset: { action: 'app' } }, WORDS.open) : null))));
    if (rows.length <= 6) return list;
    return SF.h('details', { class: 'ys-details', dataset: { part: 'rows-' + (kind || '') } }, SF.h('summary', null, WORDS.showRows.replace('{n}', rows.length)), list);
  }
  function checkBlock(c, handle) {
    const res = c.result || {};
    const run = SF.btn(c.state === 'running' ? WORDS.checking : WORDS.check, { cls: 'btn-sm', icon: 'refresh', disabled: c.state === 'running' || c.needs_access, dataset: { action: 'run-check', check: c.id } });
    run.addEventListener('click', () => post('/api/yourserver/checks/' + SF.enc(c.id), {}, run, async (r) => { Y.checks = r; await loadChecks(handle); }));
    const extra = [];
    if (c.id === 'updates') {
      const refresh = (Y.checks || {}).refresh || {};
      if (refresh.allowed) {
        const again = SF.btn(refresh.job ? WORDS.refreshing : WORDS.refreshList, { cls: 'btn-sm', icon: 'download', disabled: !!refresh.job, dataset: { action: 'refresh-list' } });
        again.addEventListener('click', () => post('/api/yourserver/checks/updates', { refresh: true }, again, async (r) => { Y.checks = r; await loadChecks(handle); }));
        extra.push(again);
      } else if (refresh.words) extra.push(SF.h('span', { class: 'muted ys-row-sub' }, String(refresh.words)));
    }
    if (c.id === 'disk') {
      const disks = (Y.checks || {}).disks || [];
      if (disks.length) {
        const pick = SF.selectEl(disks.map((d) => [d.mount, d.words]), Y.mount || disks[0].mount, { 'aria-label': WORDS.biggest, dataset: { focus: 'du-mount' } });
        pick.addEventListener('change', () => { Y.mount = pick.value; });
        const go = SF.btn(WORDS.measure, { cls: 'btn-sm', dataset: { action: 'du' } });
        go.addEventListener('click', () => post('/api/yourserver/checks/disk', { mount: pick.value }, go, async (r) => { Y.checks = r; await loadChecks(handle); }));
        extra.push(SF.h('span', { class: 'ys-inline' }, WORDS.biggest + ' ', pick, ' ', go));
      }
    }
    const folders = res.folders && (res.folders.rows || []).length ? SF.h('div', { class: 'ys-folders', dataset: { part: 'folders' } }, SF.h('p', { class: 'muted' }, WORDS.folders),
      SF.h('ul', { class: 'ys-mono-list' }, res.folders.rows.map((f) => SF.h('li', null, SF.h('span', { class: 'ys-mono' }, String(f.path)), ' ', String(f.words || ''))))) : null;
    const blocks = (res.blocks || []).length ? SF.h('details', { class: 'ys-details', dataset: { part: 'family-blocks' } }, SF.h('summary', { dataset: { action: 'show-how' } }, WORDS.showHow),
      SF.h('ol', { class: 'check-list' }, res.blocks.map(block))) : null;
    return SF.h('li', { class: 'home-card ys-check', dataset: { check: c.id, state: c.state, tone: c.tone || '' } },
      SF.h('div', { class: 'ys-check-head' }, SF.h('span', { class: 'ys-check-n' }, String(c.number)), SF.h('h3', { class: 'ys-h3' }, String(c.title)),
        SF.h('span', { class: 'pill ' + (TONE_PILL[c.tone] || 'tone-muted') }, c.state === 'never' ? WORDS.notChecked : (WORDS.checkTone[c.tone] || c.tone)), run),
      SF.h('p', { class: 'muted ys-check-what' }, String(c.what || '')),
      SF.h('p', { class: 'ys-check-words', dataset: { part: 'check-words' } }, String(c.words || '')), c.at ? SF.h('div', { class: 'muted' }, WORDS.stale + ' ', SF.timeEl(c.at)) : null,
      extra.length ? SF.h('div', { class: 'actions ys-buttons' }, extra) : null,
      rowList(res.rows, c.id), folders, blocks,
      SF.h('details', { class: 'ys-details' }, SF.h('summary', null, WORDS.exactCommands), SF.h('ul', { class: 'ys-mono-list' }, (c.commands || []).map((x) => SF.h('li', { class: 'ys-mono' }, String(x))))));
  }
  function checksSection(handle) {
    if (!Y.checks && !Y.checksError) loadChecks(handle);
    const all = SF.btn(WORDS.checkAll, { cls: 'btn-sm btn-primary', icon: 'refresh', dataset: { action: 'check-all' } });
    all.addEventListener('click', async () => {
      SF.setBusy(all, true);
      try { for (const c of (Y.checks || {}).checks || []) { if (!c.needs_access) await SF.api('/api/yourserver/checks/' + SF.enc(c.id), { method: 'POST', body: {} }); } } catch (e) { SF.toastError(null, e); } finally { SF.setBusy(all, false); }
      await loadChecks(handle);
    });
    const body = Y.checksError ? SF.unavailable(Y.checksError, WORDS.checksTitle) : !Y.checks ? SF.skeleton(3) :
      SF.h('ol', { class: 'ys-checks' }, (Y.checks.checks || []).map((c) => checkBlock(c, handle)));
    return section('checks', WORDS.checksTitle, WORDS.checksSub, [SF.h('div', { class: 'actions' }, all), body]);
  }

  // ---------------------------------------------------------------- who can take full control (§8, view only)
  async function loadRoot(handle) {
    try { Y.root = await SF.api('/api/yourserver/root'); Y.rootError = null; } catch (e) { Y.rootError = e; }
    handle.refresh();
    if (Y.root && Y.root.running && !Y.poll) Y.poll = setTimeout(() => { Y.poll = null; if (handle.isShown()) loadRoot(handle); }, 2000);
  }
  function rootRow(r, handle) {
    const known = SF.h('input', { type: 'checkbox', checked: !!r.known, dataset: { focus: 'known-' + r.id, field: 'known' } });
    const note = SF.h('input', { class: 'input input-sm', value: r.note || '', maxlength: '300', placeholder: WORDS.noteFor, 'aria-label': WORDS.noteFor, dataset: { focus: 'note-' + r.id } });
    const save = SF.btn(WORDS.save, { cls: 'btn-xs', dataset: { action: 'mark', row: r.id } });
    save.addEventListener('click', () => post('/api/yourserver/root/' + SF.enc(r.id) + '/mark', { known: known.checked, note: note.value.trim() || null }, save, async (v) => { Y.root = v; SF.toast(WORDS.saved); handle.refresh(); }));
    const changes = (r.p3 || []).map((c) => SF.btn(c.label, { cls: 'btn-xs', disabled: true, dataset: { p3: c.id }, title: (Y.root || {}).p3_why || '' }));
    return SF.h('li', { class: 'check-row ys-root-row', dataset: { row: r.id, way: r.way ? '1' : '0', new: r.new ? '1' : '0', known: r.known ? '1' : '0' } },
      SF.h('span', { class: 'check-icon ' + (r.way ? (r.known ? 'info' : 'warn') : 'ok') }, SF.icon(r.way ? 'shield' : 'info')),
      SF.h('div', { class: 'check-main ys-row-main' },
        SF.h('div', { class: 'check-name' }, String(r.words || ''), ' ', r.way ? tag(WORDS.wayTag, 'tag-warn') : r.escape ? tag(WORDS.escapeTag, 'tag-outline') : null,
          r.new ? tag(WORDS.newTag, 'tag-accent') : null, r.known ? tag(WORDS.knownTag, 'tag-ok') : null),
        SF.h('details', { class: 'ys-details' }, SF.h('summary', { dataset: { action: 'why' } }, WORDS.why),
          SF.h('p', { class: 'ys-mono' }, String(r.why || '')), r.since ? SF.h('div', { class: 'muted' }, WORDS.since + ' ', SF.timeEl(r.since)) : null,
          changes.length ? SF.h('div', { class: 'actions ys-buttons', dataset: { part: 'p3' } }, SF.h('span', { class: 'muted' }, WORDS.changeLater + ':'), changes) : null),
        SF.h('div', { class: 'ys-row-fields' }, SF.h('label', { class: 'ys-inline' }, known, ' ', WORDS.knownBox), note, save)));
  }
  function rootPage(root, handle) {
    if (!Y.root && !Y.rootError) loadRoot(handle);
    const back = SF.h('a', { class: 'btn btn-sm', href: '#/server', dataset: { action: 'back' } }, SF.icon('chevron-left', 'icon-sm'), WORDS.backServer);
    if (Y.rootError) { SF.rebuild(root, [SF.viewHead(WORDS.rootTitle, WORDS.rootSub, back), SF.unavailable(Y.rootError, WORDS.rootTitle)]); return; }
    if (!Y.root) { SF.rebuild(root, [SF.viewHead(WORDS.rootTitle, WORDS.rootSub, back), SF.skeleton(5)]); return; }
    const v = Y.root;
    const body = [SF.viewHead(WORDS.rootTitle, WORDS.rootSub, back), looksOnly()];
    if (!v.available) {
      body.push(SF.callout('warn', 'shield', WORDS.rootWait, null), SF.h('ul', { class: 'ys-reasons', dataset: { part: 'reasons' } }, (v.reasons || []).map((r) => SF.h('li', { dataset: { code: r.code } }, String(r.words || '')))));
      if (v.explain) body.push(section('explain', WORDS.explainTitle, null, SF.h('ol', { class: 'check-list ys-blocks' }, block(v.explain))));
      SF.rebuild(root, body);
      return;
    }
    const again = SF.btn(v.running ? WORDS.checking : WORDS.lookAgain, { cls: 'btn-sm btn-primary', icon: 'refresh', disabled: !!v.running, dataset: { action: 'look-again' } });
    again.addEventListener('click', () => post('/api/yourserver/root/check', {}, again, async (r) => { Y.root = r; await loadRoot(handle); }));
    body.push(SF.h('p', { class: 'ys-headline', dataset: { part: 'headline' } }, String(v.headline || WORDS.rootNever)), SF.h('div', { class: 'actions' }, again));
    if (v.p3_why) body.push(SF.h('p', { class: 'muted', dataset: { part: 'p3-why' } }, String(v.p3_why)));
    for (const g of v.groups || []) body.push(section('group-' + g.id, g.title, null, SF.h('ul', { class: 'check-list ys-root-rows' }, g.rows.map((r) => rootRow(r, handle)))));
    if ((v.gone || []).length) body.push(section('gone', WORDS.goneRows, null, SF.h('ul', null, v.gone.map((g) => SF.h('li', null, SF.timeEl(g.gone), g.note ? ' · ' + String(g.note) : null)))));
    body.push(section('cant-see', WORDS.cantSee, null, [SF.h('ul', { class: 'ys-cant' }, (v.cannot_check || []).map((x) => SF.h('li', null, String(x)))),
      (v.unchecked || []).length ? SF.h('p', { class: 'muted' }, WORDS.notRead + ' ' + v.unchecked.join('; ')) : null]));
    SF.rebuild(root, body);
  }

  // ---------------------------------------------------------------- records of changes (§6.10)
  async function loadReceipts(handle, cursor) {
    try {
      const r = await SF.api('/api/yourserver/receipts' + (cursor ? '?cursor=' + SF.enc(cursor) : ''));
      Y.receipts = cursor && Y.receipts ? Object.assign({}, r, { receipts: (Y.receipts.receipts || []).concat(r.receipts || []) }) : r;
      Y.receiptsError = null;
    } catch (e) { Y.receiptsError = e; }
    handle.refresh();
  }
  /** A record's outcome in words, never only as a colour. */
  function outcomePill(o) {
    const key = o ? String(o) : 'unknown';
    return SF.h('span', { class: 'pill no-dot tone-' + (OUTCOME_TONE[key] || 'warn'), dataset: { part: 'outcome' } }, WORDS.outcome[key] || key);
  }
  /** One record of a change. The system access's own record says what was done only when it did something (Done or
   *  Partly done); otherwise it names the change, since its words are written as done whatever the outcome. A failed
   *  keep pass added no name: it says so instead of a way back and checks about names that do not exist. */
  function receiptRow(x) {
    const mine = x.source === 'system';
    const did = !mine || ((x.outcome === 'ok' || x.outcome === 'partly') && x.words && x.words !== x.verb);
    const what = did ? String(x.words || '') : String(WORDS.changeNames[x.verb] || x.verb || x.words || '');
    const none = mine && x.outcome === 'failed' ? WORDS.failedNone[x.verb] : null;
    return SF.h('li', { class: 'check-row', dataset: { receipt: String(x.id || ''), source: x.source || '', outcome: String(x.outcome || 'unknown') } },
      SF.h('span', { class: 'check-icon ' + ({ ok: 'ok', err: 'err' }[OUTCOME_TONE[x.outcome]] || 'warn') }, SF.icon('file')),
      SF.h('div', { class: 'check-main' }, SF.h('div', { class: 'check-name' }, SF.h('span', { dataset: { part: 'what' } }, what), ' ', outcomePill(x.outcome)),
        SF.h('div', { class: 'muted' }, String(x.id || ''), ' · ', x.time ? SF.timeEl(x.time) : null, ' · ', String(x.label || '')),
        none ? SF.h('div', { dataset: { part: 'none' } }, none) : null,
        !none && x.way_back ? SF.h('div', null, SF.h('strong', null, WORDS.wayBack + ' '), String(x.way_back)) : null,
        (x.not_verified || []).length ? SF.h('div', { class: 'muted' }, WORDS.notVerified + ' ' + x.not_verified.join('; ')) : null,
        !none && (x.verified || []).length ? SF.h('div', { class: 'muted' }, WORDS.verified + ' ' + x.verified.join('; ')) : null));
  }
  function receiptsPage(root, handle) {
    if (!Y.receipts && !Y.receiptsError) loadReceipts(handle);
    const back = SF.h('a', { class: 'btn btn-sm', href: '#/server', dataset: { action: 'back' } }, SF.icon('chevron-left', 'icon-sm'), WORDS.backServer);
    if (Y.receiptsError) { SF.rebuild(root, [SF.viewHead(WORDS.receiptsTitle, WORDS.receiptsSub, back), SF.unavailable(Y.receiptsError, WORDS.receiptsTitle)]); return; }
    if (!Y.receipts) { SF.rebuild(root, [SF.viewHead(WORDS.receiptsTitle, WORDS.receiptsSub, back), SF.skeleton(3)]); return; }
    const r = Y.receipts;
    const list = (r.receipts || []).length ? SF.h('ul', { class: 'check-list ys-receipts' }, r.receipts.map(receiptRow)) :
      SF.emptyState('file', WORDS.noReceipts, null, null);
    const more = r.next ? SF.btn(WORDS.more, { cls: 'btn-sm', dataset: { action: 'more' }, onClick: () => loadReceipts(handle, r.next) }) : null;
    SF.rebuild(root, [SF.viewHead(WORDS.receiptsTitle, WORDS.receiptsSub, back), looksOnly(),
      (r.unknown || []).length ? SF.h('div', { class: 'callout callout-warn', dataset: { part: 'unknown' } }, SF.icon('alert', 'icon-sm'), SF.h('div', null, WORDS.unknownOutcome,
        SF.h('ul', null, r.unknown.map((u) => SF.h('li', null, String(WORDS.changeNames[u.verb] || u.verb || ''), u.at ? [' · ', SF.timeEl(u.at)] : null))))) : null,
      r.problem ? SF.callout('warn', 'alert', null, String(r.problem)) : null, list, more ? SF.h('div', { class: 'actions' }, more) : null]);
  }

  // ---------------------------------------------------------------- one app's logs (Check 8) and How to go back
  /** The part chosen for id's logs: only one chosen on this app and still one of its parts, else its main part (''). */
  function logsPart(id, d) {
    const chosen = Y.logs.memberApp === id ? Y.logs.member : '';
    return chosen && (d.parts || []).some((p) => p.key === chosen) ? chosen : '';
  }
  async function loadLogs(id, d, handle, before) {
    const part = logsPart(id, d);
    Y.logs = Object.assign({}, Y.logs, { id, busy: true });
    handle.refresh();
    try {
      const q = '?lines=200' + (part ? '&member=' + SF.enc(part) : '') + (before ? '&before=' + SF.enc(before) : '');
      const r = await SF.api('/api/yourserver/apps/' + SF.enc(id) + '/logs' + q);
      if (before && Y.logs.data && r.lines) r.lines = r.lines.concat(Y.logs.data.lines || []);
      Y.logs = Object.assign({}, Y.logs, { id, data: r, busy: false });
    } catch (e) { SF.toastError(null, e); Y.logs = Object.assign({}, Y.logs, { busy: false }); }
    handle.refresh();
  }
  function logsSection(id, d, handle) {
    const mine = Y.logs.id === id ? Y.logs.data : null;
    const show = SF.btn(Y.logs.busy ? WORDS.checking : WORDS.logsShow, { cls: 'btn-sm', icon: 'file', disabled: Y.logs.busy, dataset: { action: 'logs' } });
    show.addEventListener('click', () => loadLogs(id, d, handle));
    const parts = (d.parts || []).filter((p) => p.key);
    const pick = parts.length > 1 ? SF.selectEl([['', WORDS.logsMain]].concat(parts.map((p) => [p.key, String(p.service || p.name || p.key)])), logsPart(id, d), { 'aria-label': WORDS.logsPart, dataset: { focus: 'logs-part' } }) : null;
    if (pick) pick.addEventListener('change', () => { Y.logs.member = pick.value; Y.logs.memberApp = id; });
    const body = [SF.h('div', { class: 'actions ys-buttons' }, pick ? SF.h('span', { class: 'ys-inline' }, WORDS.logsPart + ' ', pick) : null, show)];
    if (mine && mine.available === false) {
      body.push(SF.callout('info', 'info', null, String(mine.words || '')), SF.h('ul', { class: 'ys-reasons' }, (mine.reasons || []).map((r) => SF.h('li', null, String(r.words || '')))));
      const links = mine.links || {};
      body.push(SF.h('div', { class: 'actions ys-buttons' }, links.dockge ? openLink(links.dockge, WORDS.logsDockge, 'btn-sm', 'logs-dockge') : null, links.cockpit ? openLink(links.cockpit, WORDS.logsCockpit, 'btn-sm', 'logs-cockpit') : null));
    } else if (mine) {
      body.push(SF.h('p', { class: 'muted' }, WORDS.logsNote));
      body.push((mine.lines || []).length ? SF.h('ol', { class: 'ys-log', dataset: { part: 'log-lines' } }, mine.lines.map((l) => SF.h('li', { dataset: { stream: l.stream || '' } },
        SF.h('span', { class: 'ys-log-at muted' }, String(l.at || '').replace('T', ' ').slice(0, 19)), ' ', SF.h('span', { class: 'ys-log-text' }, String(l.text || ''))))) : SF.h('p', { class: 'muted' }, WORDS.logsNone));
      if (mine.more && mine.before) body.push(SF.btn(WORDS.logsOlder, { cls: 'btn-sm', dataset: { action: 'logs-older' }, onClick: () => loadLogs(id, d, handle, mine.before) }));
    }
    return section('logs', WORDS.logsTitle, null, body);
  }
  function howBack(d) {
    const hb = d.how_back || {};
    if (!hb.available) return SF.h('p', { class: 'muted', dataset: { part: 'how-back' } }, String(hb.words || ''));
    return SF.h('div', { class: 'ys-how-back', dataset: { part: 'how-back' } }, SF.h('h3', { class: 'ys-h3' }, WORDS.backTitle),
      (hb.services || []).map((sv) => SF.h('div', { class: 'ys-back-service', dataset: { service: String(sv.service || '') } },
        SF.h('div', { class: 'muted' }, WORDS.backFrom + ' ' + String(sv.date || '')),
        SF.h('ol', null, (sv.steps || []).map((x) => SF.h('li', null, String(x)))),
        SF.h('pre', { class: 'code-block ys-paste' }, String(sv.kept_line || '')),
        SF.h('div', { class: 'actions ys-buttons' }, SF.btn(WORDS.backCopy, { cls: 'btn-xs', icon: 'copy', dataset: { action: 'copy-kept' }, onClick: () => SF.copyText(sv.kept_line, WORDS.copied) }),
          hb.link ? openLink(hb.link, WORDS.backOpen, 'btn-xs', 'open-stack') : null))),
      SF.h('p', { class: 'muted' }, String(hb.data || '')));
  }

  // ---------------------------------------------------------------- Home
  function homeStatus(v, apps) {
    const needs = (v.needs || []).map((n) => Object.assign({ icon: n.tone === 'err' ? 'alert' : n.tone === 'human' ? 'user' : 'server', sub: n.sub || WORDS.title }, n));
    if (!v.enabled) return { tone: null, text: WORDS.homeOff, needs: [] };
    if (!v.helper || v.helper.state !== 'ready') {
      const bad = v.helper && ['unsafe', 'refused', 'unreachable'].includes(v.helper.state);
      return { tone: bad ? 'err' : 'todo', text: bad ? String(v.helper.words || '') : WORDS.homeNoAccess, needs };
    }
    const count = (v.apps_summary || {}).count || 0;
    const tones = (v.apps_summary || {}).by_tone || {};
    const down = tones.err || 0;
    const human = needs.filter((n) => n.tone === 'human').length;
    const waiting = needs.length;
    let text = down ? WORDS.appsDown.replace('{n}', down) : WORDS.allWorking.replace('{n}', count);
    if (waiting) text += ' ' + (waiting === 1 ? WORDS.oneNeeds : WORDS.someNeed.replace('{n}', waiting));
    const tone = down ? 'err' : human ? 'human' : needs.some((n) => n.tone === 'warn') ? 'warn' : 'ok';
    return { tone, text, needs };
  }

  /** The open app page reads its app again on an event about it (any app for yourserver.updated): its light, parts and
   *  numbers come from its own answer, which the list's reload does not touch. The page keeps showing until it is back. */
  function reloadOpenApp(ev, handle) {
    const r = handle.route();
    const sub = r && r.name === 'apps' ? (r.parts[0] || '') : '';
    if (!/^a-[a-z0-9][a-z0-9-]{0,30}$/.test(sub) || Y.appId !== sub) return;
    const about = ev && ev.data ? ev.data.app : null;   // yourserver.app {app, tone, code}
    if (about && about !== sub) return;
    loadApp(sub, handle, true);
  }

  SF.registerView({
    id: 'yourserver',
    title: 'Your server',
    routes: ['apps', 'server'],
    nav: { label: 'Your server', icon: 'server', order: 5, phone: true },
    home: {
      title: 'Your server', icon: 'server', order: 5, href: '#/apps', text: WORDS.homeText,
      routes: [{ to: 'tracker', label: 'apps down' }],
      events: ['yourserver.updated', 'yourserver.app'],
      async load() {
        const v = await SF.api('/api/yourserver');
        return homeStatus(SF.isObj(v) ? v : {});
      },
    },
    enter(route, prev, handle) {
      const sub = route.parts[0] || '';
      if (route.name === 'apps' && !sub) load(handle);
      if (route.name === 'apps' && sub === 'review') { Y.review = null; Y.reviewError = null; Y.rows = null; }
      if (route.name === 'apps' && sub && sub !== 'review' && (!prev || prev.parts[0] !== sub)) {
        Y.app = null; Y.appId = ''; Y.appError = null; Y.appBusy = ''; Y.appAgain = false;
        Y.logs = { id: '', data: null, busy: false, member: '', memberApp: '' };
        if (!Y.apps) load(null);
      }
      if (route.name === 'server' && !sub) load(handle);
      if (route.name === 'server' && sub === 'setup') { Y.setup = null; Y.setupError = null; Y.checkWords = ''; Y.stageClasses = null; }
      if (route.name === 'server' && !sub) { Y.checks = null; Y.checksError = null; }
      if (route.name === 'server' && sub === 'root') { Y.root = null; Y.rootError = null; if (!Y.view) load(null); }
      if (route.name === 'server' && sub === 'receipts') { Y.receipts = null; Y.receiptsError = null; if (!Y.view) load(null); }
    },
    render(root, route, handle) {
      const sub = route.parts[0] || '';
      if (route.name === 'apps' && !sub) return appsPage(root, handle);
      if (route.name === 'apps' && sub === 'review') return reviewPage(root, handle);
      if (route.name === 'apps' && /^a-[a-z0-9][a-z0-9-]{0,30}$/.test(sub)) return appPage(root, sub, handle);
      if (route.name === 'server' && !sub) return serverPage(root, handle);
      if (route.name === 'server' && sub === 'setup') return setupPage(root, handle);
      if (route.name === 'server' && sub === 'root') return rootPage(root, handle);
      if (route.name === 'server' && sub === 'receipts') return receiptsPage(root, handle);
      SF.rebuild(root, [SF.viewHead(WORDS.title, WORDS.sub), SF.emptyState('server', WORDS.unknownPage, null, [SF.h('a', { class: 'btn btn-sm', href: '#/apps' }, WORDS.back)])]);
      return undefined;
    },
    events: {
      'yourserver.updated': (ev, handle) => { if (handle.isShown()) { load(handle); reloadOpenApp(null, handle); } },
      'yourserver.app': (ev, handle) => { if (handle.isShown()) { load(handle); reloadOpenApp(ev, handle); } },
      'yourserver.check': (ev, handle) => { if (handle.isShown() && Y.checks) loadChecks(handle); },
      'system.job': (ev, handle) => { if (handle.isShown()) { if (Y.checks) loadChecks(handle); if (Y.root) loadRoot(handle); } },
    },
  });
})(window.SF);
