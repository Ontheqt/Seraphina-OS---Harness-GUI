/* Seraphina OS Harness: Backups (DESIGN §41 "GUI interface notes (disk protection)"; docs/seraphina/BACKUPS_SNAPRAID.md
 * §1.1, §4, §5, §6, §12.5). Two routes. On the protection service's route (the SnapRAID Daemon: view.route
 * "daemon", or "metrics" before Seraphina's link is set up) the card shows the service's facts, a run in progress, a
 * freeze and its ways out, and the buttons it offers (view.daemon.offers: absent when the link or the checks would
 * refuse); each press opens a go-ahead dialog with what will happen and one button, and the answer is shown in
 * DESIGN §40's words. Pages: #/backups/review (the frozen change list, a page at a time, with Bring files back for
 * the chosen entries) and #/backups/run/<number> (one step of the service, its messages as text). On the reader's
 * route (P1, read-only) Seraphina only watches SnapRAID and protects nothing.
 * Routes: #/backups (the disk protection card, the disks, who runs it, versioned backups, not covered, details),
 * #/backups/changes/<G-n> (a guard report and its change list, a page at a time, with links to its runs),
 * #/backups/run/<J-n> (one run of the reader and its output, reached from Details' recent runs or a guard report),
 * #/backups/setup (the copy blocks that install or remove the reader, each move a step of its own). Rail: "Backups",
 * order 45. Home: its card, amber until a versioned copy exists, err when the light is red.
 * Buttons that would protect, check for damage or bring files back are not drawn: their verbs are not allowed in
 * this phase. File names (a change list, a run's output) are shown as text, never markup, and the page keeps no copy.
 * Every sentence a person reads is in WORDS, which the word check of tests/test_snapraid.py reads (U7). */
'use strict';
(function (SF) {
  const WORDS = {
    title: 'Backups',
    sub: 'What could bring your files back, and what could not.',
    homeText: 'Says how your files are protected now, and what nothing protects yet.',
    card: 'Disk protection',
    watching: 'Seraphina is only watching: nothing is protected by it yet.',
    checkNow: 'Check now',
    checkHealth: 'Read the disks’ health',
    checkChanges: 'Check for changes now',
    seeChanges: 'See what changed',
    checking: 'Checking…',
    busy: 'Another check is running: try again when it ends.',
    started: 'Checking now. The card updates when it is done.',
    setUp: 'Set it up',
    turnOn: 'Turn it on',
    turnOnAsk: 'Turn on disk protection reading? Seraphina will read the disks’ state every hour and their health once a day. She changes nothing on them.',
    turnedOn: 'Disk protection reading is on.',
    learnMore: 'What it is',
    learnMoreText: 'The design is written in docs/seraphina/BACKUPS_SNAPRAID.md in the harness.',
    readerHint: 'The setup page has the steps; you run each one yourself.',
    disks: 'Disks',
    disksEmpty: 'No disks read yet.',
    disk: 'Disk',
    protection: 'Protection disk',
    notMounted: 'not mounted',
    otherFs: 'a different filesystem',
    files: 'files',
    free: 'free',
    healthOk: 'healthy',
    healthFail: 'failing',
    healthUnknown: 'health unknown',
    healthUnread: 'health not read yet',
    chance: 'chance of failing this year',
    asleep: 'asleep',
    awake: 'awake',
    whoRuns: 'Who runs it',
    noScheduler: 'Nothing else on this machine starts it on a timetable.',
    schedulerFound: 'Started by another program on this machine:',
    outsideRuns: 'Runs that started outside Seraphina:',
    versioned: 'Versioned backups',
    versionedNone: 'No versioned backup exists yet.',
    notCovered: 'Not covered',
    notCoveredSub: 'Nothing here can bring these back:',
    details: 'Details',
    detailsSub: 'The names SnapRAID uses, its settings and the reader’s recent runs.',
    runs: 'Recent runs of the reader',
    noRuns: 'The reader has not run yet.',
    runsError: 'Could not load the runs:',
    labDetails: 'Safety checks not run on this server yet',
    labNotRun: 'not run:',
    guardRuns: 'What it ran:',
    sandboxFix: 'Turn it on in Settings',
    report: 'A report only: in this phase Seraphina runs nothing, so nothing is held or protected.',
    verdict: { would_hold: 'A run now would wait for you to look first.', would_wait: 'A run now would wait until another job ends.',
      would_run: 'A run now would go ahead.', nothing_to_do: 'Nothing changed: there is nothing new to protect.', error: 'The check could not finish.' },
    changesTitle: 'What changed',
    changesSub: 'Since the last run, as the last check for changes found it.',
    noReport: 'No check for changes yet.',
    checksTitle: 'The checks, in order',
    perDisk: 'On each disk',
    added: 'added', removed: 'removed', updated: 'changed', moved: 'moved', copied: 'copied', restored: 'came back', replaced: 'replaced by a similar file',
    lost: 'would lose their kept version',
    allDisks: 'All disks', allKinds: 'Every kind', search: 'Search names',
    groups: 'Where most changes are',
    under: 'under',
    top: 'the top folder',
    noEntries: 'Nothing matches.',
    prev: 'Previous', next: 'Next',
    of: 'of',
    back: 'Back to Backups',
    runTitle: 'A reading run',
    runOutput: 'What it printed',
    commandLine: 'Exactly what ran',
    more: 'Show more',
    noOutput: 'It printed nothing.',
    setupTitle: 'Set up disk protection reading',
    setupSub: 'A small program reads SnapRAID for Seraphina, because only the administrator may. You install it with these steps; Seraphina never runs sudo.',
    stage: 'Prepare the files',
    staged: 'The files are ready in',
    check: 'Check it',
    expect: 'Expect:', ifNot: 'If not:', note: 'Note:',
    removal: 'Remove it',
    removalSub: 'Turn the reading off first. This removes the reader and nothing else.',
    copy: 'Copy',
    copied: 'Copied',
    thenStep: 'Then, only once it shows what you expect:',
    unknownPage: 'This page is not here yet: it comes with a later version.',
    loadError: 'Could not load disk protection:',
    tone: { err: 'Problem', warn: 'Needs a look', ok: 'Healthy', muted: 'Unknown' },
    result: { pass: 'passed', hold: 'would hold', wait: 'would wait', note: 'note', skipped: 'not reached', error: 'failed' },
    homeNoVersioned: 'No versioned backup exists yet: disk protection is not a backup.',
    service: 'The protection service',
    answering: 'is answering', notAnswering: 'is not answering',
    passwordOn: 'asks for a password', passwordOff: 'accepts commands without a password',
    version: 'Version', runsSnapraid: 'running SnapRAID',
    nextRun: 'Next run:', noWindow: 'nothing has scheduled a run',
    lastRun: 'Last run:', restarted: 'The protection service restarted',
    noLink: 'To see more and to ask for runs, set up Seraphina’s link.',
    linkReadOnly: 'Seraphina may only read the protection service: allowing more is a step on the setup page.',
    running: 'Running now:', timeLeft: 'about', minutesLeft: 'minutes left',
    stopSafely: 'Stop safely',
    protectNow: 'Protect the latest changes',
    skipNext: 'Skip the next run', dontSkip: 'Don’t skip',
    repairIt: 'Repair it',
    bringBack: 'Bring files back',
    frozenTitle: 'Waiting for you',
    frozenText: 'The protection service stopped before changing anything, so the protection still has these files. Choose a way out after you looked at the list.',
    waysOut: 'Your ways out',
    wayBring: 'Bring back the files deleted by mistake: choose them in the list.',
    wayAccept: 'Protect these changes anyway',
    wayTonight: 'Not tonight: skip the next run while you look.',
    wayLimits: 'Change the limits yourself, in the protection service’s settings:',
    limitsCopy: 'sudoedit /etc/snapraidd.conf\nsudo systemctl reload snapraidd',
    doing: { sync: 'protecting the latest changes', scrub: 'checking for hidden damage', fix: 'bringing files back', up: 'starting the disks', report: 'writing its report' },
    deleted: 'deleted', changed: 'changed', addedSince: 'added',
    requests: 'What you asked for',
    // after DESIGN §40's word for the outcome (SF.words.outcome, read in outcomeWords)
    outcomeMore: { succeeded: 'the protection service took it.', failed: 'it was not done.', unknown: 'Check before trying again.' },
    stateWords: { preview: 'waiting for your go-ahead', sent: 'sent', refused: 'refused', cancelled: 'cancelled', expired: 'too old', void: 'no longer valid' },
    approveTitle: { stop: 'Stop safely', hold_off: 'Skip the next run', maintenance: 'Protect the latest changes', accept: 'Protect these changes anyway', undelete: 'Bring files back', heal: 'Repair hidden damage' },
    goAhead: 'Go ahead',
    whatHappens: 'What will happen',
    whatYouSaw: 'What Seraphina showed you',
    typedAsk: 'Type the number of changes to confirm:',
    changedSince: 'Something changed since you looked. Look again.',
    filesChosen: 'files and folders chosen',
    asOf: 'as they were at the last run',
    onDisk: 'on',
    stepsOf: 'steps',
    runsTitle: 'Recent runs of the protection service',
    startedBy: 'started by',
    stepTitle: 'A step of the protection service',
    messages: 'What it said',
    noMessages: 'It said nothing.',
    reviewTitle: 'What changed since the last run',
    reviewSub: 'What the protection service found when it looked, not now. Names stay on this page: Seraphina keeps no copy.',
    reviewCut: 'The protection service keeps only the first 100,000 names: the counts are complete, the list is not.',
    choose: 'Choose', chosen: 'chosen',
    oneDisk: 'Files come back one disk at a time: bring these back first, then choose from another disk.',
    allChanges: 'Every kind',
    limitsLabel: 'Its limits',
    verdictWords: { done: 'Done.', warning: 'Done, with a warning.', frozen: 'Stopped for you to look at the changes.', failed: 'It did not finish.', stopped: 'Stopped safely.', did_nothing: 'It was cancelled because a disk is not healthy. It did nothing.', unknown: 'Seraphina could not tell how it ended.', running: 'Running.', partial: 'Stopped part way: some changes are not protected yet.' },
    whoStarts: 'What starts protection runs',
    windowLine: 'The protection service, at its scheduled time:',
    countUnknown: 'Seraphina cannot see what else might start protection runs.',
    labService: 'Not checked against a real protection service yet',
    linkSetup: 'Seraphina’s link to the protection service',
    linkSetupSub: 'A small program the system starts for each request holds the protection service’s password, so Seraphina never does. You install it with these steps.',
    linkStage: 'Prepare the link’s files',
    linkCheck: 'Check the link',
    serviceLines: 'The lines the protection service’s settings need:',
  };
  const ERR_CODES = new Set(['disk_missing', 'protection_missing', 'disk_uuid_changed', 'smart_fail', 'bad_blocks', 'content_problem', 'not_protected_days']);
  const WARN_CODES = new Set(['never_protected', 'old', 'check_overdue', 'outside', 'stale', 'unreachable', 'unsafe', 'interrupted',
    'health_unknown', 'health_unread', 'health_old', 'health_failed', 'status_failed', 'failure_likely', 'almost_full', 'list_room_low',
    'not_set_up', 'taking_out', 'read_with_care', 'frozen', 'partial', 'unknown_outcome', 'lapsed', 'two_schedulers', 'no_scheduler',
    'beyond_loopback', 'down', 'report_failed']);
  // The protection service's red codes (snapraidd.REASONS): Home shows them as problems
  ['auth_off', 'open_beyond', 'failing', 'degraded', 'engine_old', 'frozen_long'].forEach((c) => ERR_CODES.add(c));
  // A reason that starts with a name keeps its capital after "Disk protection: " on Home.
  const lowerFirst = (text) => (/^(Seraphina|SnapRAID)\b/.test(text) ? text : text.charAt(0).toLowerCase() + text.slice(1));
  // draft: the search words typed and not yet applied, kept across a redraw (an event can redraw the page while typing)
  const B = { view: null, error: null, loading: null, notice: '', guard: null, guardKey: '', guardError: null, query: { disk: '', kind: '', q: '', offset: 0 }, draft: null, again: false,
    run: null, runKey: '', runText: '', runOffset: 0, runMore: false, setup: null, setupError: null, checkWords: '',
    detailsOpen: false, jobs: null, jobsError: null, jobsLoading: false,
    review: null, reviewKey: '', reviewError: null, rquery: { disk: '', change: '', q: '', offset: 0 }, rdraft: null, picked: new Map(),
    druns: null, druns_error: null, step: null, stepKey: '' };

  // ---------------------------------------------------------------- data
  // A load asked for while one is on its way runs once more after it: the first may have left before the change.
  function load(handle) {
    if (B.loading) { B.again = true; return B.loading; }
    B.again = false;
    B.loading = SF.api('/api/snapraid').then((v) => { B.view = SF.isObj(v) ? v : null; B.error = null; })
      .catch((e) => { B.error = e; })
      .finally(() => {
        B.loading = null;
        if (B.again) { load(handle); return; }
        if (handle) handle.refresh();
        pollWhileChecking(handle);
      });
    return B.loading;
  }
  let pollTimer = null;
  function pollWhileChecking(handle) {
    if (pollTimer || !B.view || !B.view.refreshing) return;
    pollTimer = setTimeout(() => { pollTimer = null; load(handle); }, 3000);
  }
  async function refresh(what, button, handle) {
    SF.setBusy(button, true);
    try {
      const r = await SF.api('/api/snapraid/refresh', { method: 'POST', body: { what } });
      if (r && r.busy) SF.toast(WORDS.busy);
      else SF.toast(WORDS.started);
      await load(handle);
    } catch (e) { SF.toastError(null, e); } finally { SF.setBusy(button, false); }
  }
  async function loadGuard(id, handle) {
    const q = B.query;
    const key = id + '|' + q.disk + '|' + q.kind + '|' + q.q + '|' + q.offset;
    if (B.guardKey === key && B.guard) return;
    B.guardKey = key;
    const params = ['offset=' + q.offset].concat(['disk', 'kind', 'q'].filter((k) => q[k]).map((k) => k + '=' + SF.enc(q[k])));
    try {
      B.guard = await SF.api('/api/snapraid/guard/' + SF.enc(id) + '?' + params.join('&'));
      B.guardError = null;
    } catch (e) { B.guard = null; B.guardError = e; }
    handle.refresh();
  }
  async function loadRun(id, handle, more) {
    if (!more && B.runKey === id && B.run) return;
    const offset = more ? B.runOffset : 0;
    try {
      const r = await SF.api('/api/snapraid/jobs/' + SF.enc(id) + '?log=1&offset=' + offset);
      B.run = r.job || {};
      const log = r.log || {};
      B.runText = (more ? B.runText : '') + String(log.text || '');
      // the reader counts the bytes it read: decoded text that was not UTF-8 has another length
      B.runOffset = Number.isFinite(Number(log.next_offset)) ? Number(log.next_offset) : (Number(log.offset) || 0) + new TextEncoder().encode(String(log.text || '')).length;
      B.runMore = !!log.more;
      B.runKey = id;
    } catch (e) { B.run = { error: e.message || String(e) }; B.runKey = id; }
    handle.refresh();
  }
  async function loadJobs(handle) {
    if (B.jobsLoading) return;
    B.jobsLoading = true;
    try { const r = await SF.api('/api/snapraid/jobs'); B.jobs = (r && r.jobs) || []; B.jobsError = null; } catch (e) { B.jobsError = e; }
    B.jobsLoading = false;
    handle.refresh();
  }
  async function loadSetup(handle) {
    try { B.setup = await SF.api('/api/snapraid/setup'); B.setupError = null; } catch (e) { B.setupError = e; }
    handle.refresh();
  }

  // ---------------------------------------------------------------- pieces
  const tag = (text, cls) => SF.h('span', { class: 'tag' + (cls ? ' ' + cls : '') }, String(text));
  /** An answer to a go-ahead in DESIGN §40's words (SF.words.outcome), then what it means here: "Succeeded: the
   * protection service took it.", "Failed: it was not done.", "Outcome unknown: it may or may not have happened. Check
   * before trying again." With rest, only the part after the word (the word is drawn beside it as a pill). */
  function outcomeWords(outcome, rest) {
    const word = SF.words.outcome[outcome];
    const more = WORDS.outcomeMore[outcome];
    if (!word || !more) return '';
    if (outcome === 'unknown') return rest ? more : word + '. ' + more;
    return rest ? more.charAt(0).toUpperCase() + more.slice(1) : word + ': ' + more;
  }
  const TONE_TAG = { err: 'tag-err', warn: 'tag-warn', ok: 'tag-ok', muted: 'tag-outline' };
  const fmtBytes = (n) => {
    if (!(n >= 0)) return '';
    const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
    let i = 0; let v = n;
    while (v >= 1000 && i < units.length - 1) { v /= 1000; i++; }
    return (v >= 10 || i === 0 ? Math.round(v) : v.toFixed(1)) + ' ' + units[i];
  };
  function section(id, title, sub, body) {
    return SF.h('section', { class: 'home-card', 'aria-labelledby': 'bk-' + id + '-t', dataset: { part: id }, style: { 'margin-top': '14px' } },
      SF.h('h2', { class: 'home-card-title', id: 'bk-' + id + '-t' }, title),
      sub ? SF.h('p', { class: 'muted' }, sub) : null, body);
  }
  function light(view) {
    const l = (view && view.light) || { tone: 'muted', reason: '' };
    return SF.h('div', { class: 'bk-light', dataset: { tone: l.tone, code: l.code || '' }, style: { display: 'flex', gap: '10px', 'align-items': 'flex-start', 'flex-wrap': 'wrap' } },
      SF.h('span', { class: 'pill tone-' + (l.tone === 'muted' ? 'none' : l.tone), dataset: { focus: 'light' } }, WORDS.tone[l.tone] || l.tone),
      SF.h('div', { style: { 'min-width': '0', flex: '1 1 260px' } },
        SF.h('strong', { class: 'bk-reason' }, String(l.reason || '')),
        l.note ? SF.h('div', { class: 'muted bk-note' }, String(l.note)) : null,
        l.last_known ? SF.h('div', { class: 'muted bk-last-known' }, String(l.last_known)) : null));
  }
  function buttons(view, handle) {
    const out = [];
    if (view.state === 'ready') {
      const busy = !!view.refreshing;
      const now = SF.btn(busy ? WORDS.checking : WORDS.checkNow, { icon: 'refresh', cls: 'btn-sm', dataset: { action: 'check-status', focus: 'check-status' }, disabled: busy });
      now.addEventListener('click', () => refresh('status', now, handle));
      const health = SF.btn(WORDS.checkHealth, { icon: 'gauge', cls: 'btn-sm', dataset: { action: 'check-health', focus: 'check-health' }, disabled: busy });
      health.addEventListener('click', () => refresh('health', health, handle));
      const diff = SF.btn(WORDS.checkChanges, { icon: 'search', cls: 'btn-sm', dataset: { action: 'check-diff', focus: 'check-diff' }, disabled: busy });
      diff.addEventListener('click', () => refresh('diff', diff, handle));
      out.push(now, health, diff);
      if (view.guard && view.guard.guard) out.push(SF.h('a', { class: 'btn btn-sm', href: '#/backups/changes/' + SF.enc(view.guard.guard), dataset: { action: 'see-changes', focus: 'see-changes' } }, SF.icon('list', 'icon-sm'), WORDS.seeChanges));
    } else if (view.state === 'no_reader') {
      out.push(SF.h('a', { class: 'btn btn-sm btn-primary', href: '#/backups/setup', dataset: { action: 'set-up' } }, SF.icon('tool', 'icon-sm'), WORDS.setUp));
    } else if (view.state === 'off') {
      const on = SF.btn(WORDS.turnOn, { icon: 'play', cls: 'btn-sm btn-primary', dataset: { action: 'turn-on' } });
      on.addEventListener('click', async () => {
        if ((await SF.confirmModal({ title: WORDS.turnOn, description: WORDS.turnOnAsk, confirm: WORDS.turnOn })) !== 'ok') return;
        SF.setBusy(on, true);
        try { await SF.api('/api/config', { method: 'POST', body: { snapraid: { enabled: true } } }); SF.toast(WORDS.turnedOn); await load(handle); } catch (e) { SF.toastError(null, e); } finally { SF.setBusy(on, false); }
      });
      out.push(on, SF.h('a', { class: 'btn btn-sm', href: '#/backups/setup', dataset: { action: 'set-up' } }, WORDS.setUp));
    }
    return out.length ? SF.h('div', { class: 'actions', style: { 'flex-wrap': 'wrap', gap: '8px' } }, out) : null;
  }
  function protectionCard(view, handle) {
    const facts = view.facts || {};
    const warnings = (view.warnings || []).map((w) => SF.h('div', { class: 'callout callout-warn', dataset: { warning: w.code } }, SF.icon('alert', 'icon-sm'),
      SF.h('div', null, String(w.words || ''), ' ', SF.h('a', { href: '#/settings' }, WORDS.sandboxFix))));
    const body = [
      light(view),
      SF.h('div', { class: 'bk-sentences', style: { display: 'grid', gap: '6px' } }, (view.sentences || []).map((s, i) => SF.h('p', { dataset: { sentence: String(i + 1) } }, String(s)))),
      view.state === 'ready' ? SF.callout('info', 'info', null, String(view.undo_line || WORDS.watching)) : null,
      view.waiting_words ? SF.h('p', { class: 'muted bk-waiting', dataset: { part: 'waiting' } }, String(view.waiting_words) + ': ' + WORDS.checking) : null,
      (facts.words || []).length ? SF.h('ul', { class: 'bk-facts', style: { margin: '0', 'padding-left': '18px' } }, facts.words.map((w) => SF.h('li', null, String(w)))) : null,
      view.state === 'absent' ? SF.h('p', { class: 'muted' }, WORDS.learnMoreText) : null,
      view.state === 'no_reader' ? readerFacts(view) : null,
      warnings,
      buttons(view, handle),
      view.state === 'ready' && view.lab_line ? SF.h('p', { class: 'muted bk-lab', style: { 'font-size': 'var(--fs-sm)' } }, String(view.lab_line)) : null,
    ];
    return SF.h('section', { class: 'home-card' + (view.light && view.light.tone !== 'muted' ? ' has-' + view.light.tone : ''), 'aria-labelledby': 'bk-card-t', dataset: { part: 'protection', state: view.state || '' } },
      SF.h('div', { class: 'home-card-head' }, SF.h('span', { class: 'home-card-icon' }, SF.icon('layers')), SF.h('h2', { class: 'home-card-title', id: 'bk-card-t' }, WORDS.card)), body);
  }
  function readerFacts(view) {
    const s = view.snapraid || {};
    const rows = [];
    if (s.version) rows.push(['SnapRAID', s.version]);
    if (s.readable_disks !== null && s.readable_disks !== undefined) rows.push([WORDS.disks, String(s.readable_disks)]);
    return SF.h('div', null, rows.length ? SF.h('dl', { class: 'kv' }, rows.map(([k, v]) => [SF.h('dt', null, k), SF.h('dd', null, v)])) : null,
      SF.h('p', { class: 'muted' }, WORDS.readerHint));
  }
  function diskRows(view) {
    const disks = view.disks || [];
    if (!disks.length) return SF.h('p', { class: 'muted' }, WORDS.disksEmpty);
    return SF.h('ul', { class: 'check-list bk-disks' }, disks.map((d) => {
      const bad = !d.mounted || d.uuid_ok === false || d.health === 'fail' || d.health === 'prefail';
      const unknown = d.health === 'unknown' || !d.health;
      const name = d.label || (d.role === 'data' ? WORDS.disk + ' ' + d.number : WORDS.protection);
      const parts = [];
      if (!d.mounted) parts.push(WORDS.notMounted);
      else if (d.uuid_ok === false) parts.push(WORDS.otherFs);
      if (d.files !== null && d.files !== undefined) parts.push(SF.fmtNum(d.files) + ' ' + WORDS.files);
      if (d.free_bytes) parts.push(fmtBytes(d.free_bytes) + ' ' + WORDS.free);
      parts.push(d.health === 'ok' ? WORDS.healthOk : d.health === 'fail' || d.health === 'prefail' ? WORDS.healthFail : d.health === 'unknown' ? WORDS.healthUnknown : WORDS.healthUnread);
      if (typeof d.probability === 'number') parts.push(Math.round(d.probability * 100) + '% ' + WORDS.chance);
      if (d.power === 'standby') parts.push(WORDS.asleep); else if (d.power === 'active') parts.push(WORDS.awake);
      return SF.h('li', { class: 'check-row', dataset: { disk: d.name } },
        SF.h('span', { class: 'check-icon ' + (bad ? 'err' : unknown ? 'warn' : 'ok') }, SF.icon(bad ? 'x' : unknown ? 'help' : 'check')),
        SF.h('div', { class: 'check-main' },
          SF.h('div', { class: 'check-name' }, name, ' ', tag(d.name, 'tag-mono tag-outline')),
          SF.h('div', { class: 'muted', style: { 'word-break': 'break-word' } }, parts.join(' · ')),
          d.mount ? SF.h('div', { class: 'check-detail' }, String(d.mount) + (d.model ? '  ' + d.model : '') + (d.serial ? '  ' + d.serial : '')) : null));
    }));
  }
  function whoRuns(view) {
    const found = view.schedulers || [];
    const outside = view.outside || [];
    return [
      found.length ? SF.h('div', null, SF.h('p', null, WORDS.schedulerFound),
        SF.h('ul', { class: 'bk-schedulers' }, found.map((f) => SF.h('li', null, SF.h('code', null, String(f.place || '')), ' ', (f.schedule || []).length ? tag((f.schedule || []).join(' '), 'tag-mono') : null)))) : SF.h('p', { class: 'muted' }, WORDS.noScheduler),
      outside.length ? SF.h('div', null, SF.h('p', null, WORDS.outsideRuns), SF.h('ul', { class: 'bk-outside' }, outside.map((o) => SF.h('li', null, SF.timeEl(o.modified || o.seen_at))))) : null,
    ];
  }
  function runsList(handle) {
    if (B.jobsError) return SF.h('p', { class: 'muted' }, WORDS.runsError + ' ' + (B.jobsError.message || String(B.jobsError)));
    if (!B.jobs) return SF.skeleton(2);
    if (!B.jobs.length) return SF.h('p', { class: 'muted' }, WORDS.noRuns);
    return SF.h('ul', { class: 'check-list bk-runs' }, B.jobs.slice(0, 20).map((j) => SF.h('li', { class: 'check-row', dataset: { run: j.job } },
      SF.h('div', { class: 'check-main' },
        SF.h('div', { class: 'check-name' }, SF.h('a', { href: '#/backups/run/' + SF.enc(j.job), dataset: { action: 'open-run' } }, String(j.job)), ' ', tag(j.command || j.verb || '', 'tag-mono tag-outline')),
        SF.h('div', { class: 'muted' }, String(j.words || ''), j.started ? ' · ' : '', j.started ? SF.timeEl(j.started) : null)))));
  }
  function details(view, handle) {
    const rows = [];
    const s = view.snapraid || {};
    const helper = view.helper || {};
    if (s.binary) rows.push(['SnapRAID', (s.version || '?') + '  ' + s.binary]);
    if (helper.path) rows.push(['Reader', helper.path]);
    if (helper.sha256) rows.push(['sha256', helper.sha256 + (helper.matches_release === false ? '  (not this release’s)' : '')]);
    rows.push(['Allowed', (view.allowed || []).join(', ')]);
    const facts = (view.facts || {}).status || {};
    if (facts.block_size) rows.push(['Block size', fmtBytes(facts.block_size)]);
    if ((view.facts || {}).last_run) rows.push(['Last run (inferred)', view.facts.last_run.at]);
    const problems = (view.conf_problems || []).map((p) => SF.h('li', null, String(p.words || '')));
    const lab = (view.lab || []).map((c) => SF.h('li', { dataset: { lab: c.id } }, SF.h('strong', null, String(c.id) + ': '), String(c.what || ''), SF.h('span', { class: 'muted' }, ' (' + WORDS.labNotRun + ' ' + String(c.why || '') + ')')));
    const toggled = (e) => { B.detailsOpen = e.target.open; if (B.detailsOpen && !B.jobs) loadJobs(handle); };
    return SF.h('details', { class: 'bk-details', open: B.detailsOpen, ontoggle: toggled }, SF.h('summary', { dataset: { action: 'details' } }, WORDS.details),
      SF.h('p', { class: 'muted' }, WORDS.detailsSub),
      SF.h('dl', { class: 'kv' }, rows.map(([k, v]) => [SF.h('dt', null, k), SF.h('dd', { class: 'mono' }, String(v))])),
      problems.length ? SF.h('ul', { class: 'bk-problems' }, problems) : null,
      SF.h('h3', { class: 'home-card-title', style: { 'margin-top': '12px' } }, WORDS.runs),
      B.detailsOpen ? runsList(handle) : null,
      lab.length ? [SF.h('h3', { class: 'home-card-title', style: { 'margin-top': '12px' } }, WORDS.labDetails), SF.h('ul', { class: 'bk-lab-list' }, lab)] : null,
      SF.h('p', null, SF.h('a', { href: '#/backups/setup' }, WORDS.setupTitle)));
  }

  // ---------------------------------------------------------------- the protection service's route (§4, §5, §6)
  const isDaemon = (v) => !!v && (v.route === 'daemon' || v.route === 'metrics') && SF.isObj(v.daemon);
  const minutes = (s) => Math.max(1, Math.round((Number(s) || 0) / 60));
  // One go-ahead per press (§6.1): a preview first, then a dialog with what will happen and one button. The answer is
  // shown in §40's words; 409 "changed" sends the owner back to look again.
  async function act(action, extra, button, handle) {
    SF.setBusy(button, true);
    let preview;
    try { preview = await SF.api('/api/snapraid/actions', { method: 'POST', body: Object.assign({ action }, extra || {}) }); } catch (e) { SF.toastError(null, e); SF.setBusy(button, false); return; }
    SF.setBusy(button, false);
    const typed = preview.typed ? SF.h('input', { class: 'input', type: 'number', inputmode: 'numeric', 'aria-label': WORDS.typedAsk, dataset: { focus: 'typed' } }) : null;
    const result = await SF.confirmModal({
      title: WORDS.approveTitle[action] || action, confirm: WORDS.goAhead, icon: 'check',
      body: SF.h('div', { class: 'bk-approval', dataset: { approval: preview.approval, action } }, showsBody(action, preview.shows || {}),
        typed ? SF.h('label', { class: 'field' }, SF.h('span', null, WORDS.typedAsk), typed) : null),
      onSubmit: async () => {
        const body = { shown_sha256: preview.shown_sha256 };
        if (typed) body.typed = Number(typed.value);
        try { return await SF.api('/api/snapraid/actions/' + SF.enc(preview.approval) + '/approve', { method: 'POST', body }); } catch (e) {
          if (e && e.status === 409) throw new Error(WORDS.changedSince);
          throw e;
        }
      },
    });
    if (result && SF.isObj(result)) SF.toast(outcomeWords(result.outcome) || String(result.words || ''));
    else SF.api('/api/snapraid/actions/' + SF.enc(preview.approval) + '/cancel', { method: 'POST', body: {} }).catch(() => {});
    await load(handle);
  }
  function showsBody(action, s) {
    const rows = [];
    const line = (text, part) => SF.h('p', { dataset: part ? { part } : {} }, String(text));
    if (action === 'stop') {
      rows.push(line(WORDS.running + ' ' + String(s.what || '') + ' (' + String(s.of || '') + ')' + (s.progress !== undefined && s.progress !== null ? ', ' + s.progress + '%' : '') + '.', 'what'), line(s.line || '', 'line'));
    } else if (action === 'hold_off') {
      rows.push(line((s.enabled ? WORDS.skipNext : WORDS.dontSkip) + (s.window_words ? ': ' + s.window_words : '') + '.', 'what'));
      (s.lines || []).forEach((t) => rows.push(line(t)));
    } else if (action === 'maintenance' || action === 'accept') {
      const c = s.counts || {};
      rows.push(line(SF.fmtNum(c.removed || 0) + ' ' + WORDS.deleted + ', ' + SF.fmtNum(c.updated || 0) + ' ' + WORDS.changed + ', ' + SF.fmtNum(c.added || 0) + ' ' + WORDS.addedSince + '.', 'counts'));
      if (s.limits_line) rows.push(line(s.limits_line, 'limits'));
      const per = s.per_disk || {};
      Object.keys(per).forEach((disk) => rows.push(line(diskWord(disk) + ': ' + Object.entries(per[disk]).map(([k, n]) => SF.fmtNum(n) + ' ' + (WORDS[k] || k)).join(', '))));
      if (s.accept_line) rows.push(SF.callout('warn', 'alert', null, String(s.accept_line)));
    } else if (action === 'undelete') {
      rows.push(line(SF.fmtNum(s.count || 0) + ' ' + WORDS.filesChosen + ', ' + WORDS.asOf + '.', 'count'));
      (s.lines || []).forEach((t) => rows.push(line(t)));
    } else {
      rows.push(line(String(s.line || ''), 'what'));
    }
    return [SF.h('h3', { class: 'home-card-title' }, WORDS.whatHappens), rows];
  }
  const diskWord = (name) => (/^d?(\d+)$/.test(String(name)) ? WORDS.disk + ' ' + String(name).replace(/^d/, '') : String(name));
  function daemonCard(v, handle) {
    const d = v.daemon;
    const offers = d.offers || {};
    const btn = (label, action, icon, extra, cls) => {
      const b = SF.btn(label, { icon, cls: 'btn-sm' + (cls ? ' ' + cls : ''), dataset: { action: 'act-' + action, focus: 'act-' + action } });
      b.addEventListener('click', () => act(action, extra, b, handle));
      return b;
    };
    const undo = [String(v.undo_line || '')];
    if (offers.hold_off) undo.push(' ', d.hold_off ? btn(WORDS.dontSkip, 'hold_off', 'play', { enabled: false }) : btn(WORDS.skipNext, 'hold_off', 'clock', { enabled: true }));
    const facts = [];
    facts.push(SF.h('li', { dataset: { fact: 'service' } }, WORDS.service + ' ' + (d.answering ? WORDS.answering : WORDS.notAnswering) + ' and ' + (d.auth_on === false ? WORDS.passwordOff : WORDS.passwordOn) + '. ' + WORDS.version + ' ' + String(d.version || '?') + ', ' + WORDS.runsSnapraid + ' ' + String(d.engine || '?') + '.'));
    if (d.limits_line) facts.push(SF.h('li', { dataset: { fact: 'limits' } }, String(d.limits_line)));
    facts.push(SF.h('li', { dataset: { fact: 'window' } }, WORDS.nextRun + ' ' + String((d.window || {}).words || WORDS.noWindow) + '.'));
    if (d.last_run) facts.push(SF.h('li', { dataset: { fact: 'last-run' } }, WORDS.lastRun + ' ' + (WORDS.verdictWords[d.last_run.verdict] || d.last_run.verdict)));
    if (d.restarted_at) facts.push(SF.h('li', { dataset: { fact: 'restarted' } }, WORDS.restarted + ' ', SF.timeEl(new Date(d.restarted_at * 1000).toISOString()), '.'));
    const running = d.active ? SF.h('div', { class: 'callout callout-info bk-running', dataset: { part: 'running' } }, SF.icon('refresh', 'icon-sm'),
      SF.h('div', null, WORDS.running + ' ' + String(WORDS.doing[d.active.command] || d.active.command) +
        (d.active.progress !== null && d.active.progress !== undefined ? ', ' + d.active.progress + '%' : '') + (d.active.eta ? ', ' + WORDS.timeLeft + ' ' + minutes(d.active.eta) + ' ' + WORDS.minutesLeft : '') + '.',
        offers.stop ? [' ', btn(WORDS.stopSafely, 'stop', 'stop')] : null)) : null;
    const frozen = d.frozen ? SF.h('div', { class: 'callout callout-warn bk-frozen', dataset: { part: 'frozen' } }, SF.icon('alert', 'icon-sm'),
      SF.h('div', { style: { display: 'grid', gap: '6px', 'min-width': '0' } },
        SF.h('strong', null, WORDS.frozenTitle + ': ' + SF.fmtNum((d.frozen.counts || {}).removed || 0) + ' ' + WORDS.deleted + ', ' + SF.fmtNum((d.frozen.counts || {}).updated || 0) + ' ' + WORDS.changed + '.'),
        SF.h('p', null, WORDS.frozenText),
        SF.h('div', { class: 'actions', style: { gap: '8px', 'flex-wrap': 'wrap' } },
          SF.h('a', { class: 'btn btn-sm btn-primary', href: '#/backups/review', dataset: { action: 'review', focus: 'review' } }, SF.icon('list', 'icon-sm'), WORDS.seeChanges),
          offers.accept ? btn(WORDS.wayAccept, 'accept', 'check') : null,
          offers.hold_off && !d.hold_off ? btn(WORDS.skipNext, 'hold_off', 'clock', { enabled: true }) : null),
        SF.h('details', { class: 'bk-ways' }, SF.h('summary', null, WORDS.waysOut),
          SF.h('ol', null, SF.h('li', null, WORDS.wayBring), SF.h('li', null, WORDS.wayAccept + '.'), SF.h('li', null, WORDS.wayTonight),
            SF.h('li', null, WORDS.wayLimits, SF.h('pre', { class: 'code-block' }, WORDS.limitsCopy)))))) : null;
    const buttonsRow = [];
    if (offers.maintenance) buttonsRow.push(btn(WORDS.protectNow, 'maintenance', 'shield', null, 'btn-primary'));
    if (offers.heal) buttonsRow.push(btn(WORDS.repairIt, 'heal', 'tool'));
    if (offers.undelete && !d.frozen) buttonsRow.push(SF.h('a', { class: 'btn btn-sm', href: '#/backups/review', dataset: { action: 'bring-back' } }, SF.icon('download', 'icon-sm'), WORDS.bringBack));
    const now = SF.btn(v.refreshing ? WORDS.checking : WORDS.checkNow, { icon: 'refresh', cls: 'btn-sm', dataset: { action: 'check-status', focus: 'check-status' } });
    now.addEventListener('click', () => refresh('status', now, handle));
    buttonsRow.push(now);
    const link = d.link || {};
    const notes = [];
    if (!link.installed) notes.push(SF.callout('info', 'info', null, SF.h('span', null, WORDS.noLink, ' ', SF.h('a', { href: '#/backups/setup', dataset: { action: 'set-up-link' } }, WORDS.setUp))));
    else if ((link.allow || []).indexOf('act') < 0) notes.push(SF.h('p', { class: 'muted bk-read-only' }, WORDS.linkReadOnly));
    const asked = (v.actions || []).filter((a) => a.state !== 'preview' && a.state !== 'cancelled').slice(-3).reverse();
    return SF.h('section', { class: 'home-card' + (v.light && v.light.tone !== 'muted' ? ' has-' + v.light.tone : ''), 'aria-labelledby': 'bk-card-t', dataset: { part: 'protection', state: v.state || '', route: v.route } },
      SF.h('div', { class: 'home-card-head' }, SF.h('span', { class: 'home-card-icon' }, SF.icon('layers')), SF.h('h2', { class: 'home-card-title', id: 'bk-card-t' }, WORDS.card)),
      light(v),
      SF.h('div', { class: 'bk-sentences', style: { display: 'grid', gap: '6px' } }, (v.sentences || []).map((t, i) => SF.h('p', { dataset: { sentence: String(i + 1) } }, String(t)))),
      SF.h('div', { class: 'callout callout-info bk-undo', dataset: { part: 'undo' } }, SF.icon('info', 'icon-sm'), SF.h('div', null, undo)),
      SF.h('ul', { class: 'bk-facts bk-service', style: { margin: '0', 'padding-left': '18px' } }, facts),
      running, frozen,
      asked.length ? SF.h('div', { class: 'bk-asked', dataset: { part: 'asked' } }, SF.h('strong', null, WORDS.requests), SF.h('ul', null, asked.map((a) => SF.h('li', { dataset: { approval: a.approval, outcome: a.outcome || '' } },
        String(WORDS.approveTitle[a.action] || a.action) + ': ',
        a.outcome ? SF.h('span', { class: 'pill no-dot tone-' + (SF.words.outcomeTone[a.outcome] || 'warn') }, SF.words.outcome[a.outcome] || String(a.outcome)) : null,
        String(a.outcome ? ' ' + outcomeWords(a.outcome, true) : (WORDS.stateWords[a.state] || a.state)) + (a.words && a.outcome !== 'succeeded' ? ' ' + String(a.words) : ''))))) : null,
      notes,
      SF.h('div', { class: 'actions', style: { 'flex-wrap': 'wrap', gap: '8px' } }, buttonsRow));
  }
  function daemonDisks(v) {
    const disks = (v.daemon || {}).disks || [];
    if (!disks.length) return SF.h('p', { class: 'muted' }, WORDS.disksEmpty);
    const words = { passed: WORDS.healthOk, pending: WORDS.healthUnknown, unknown: WORDS.healthUnknown, corrupt: 'hidden damage found', degraded: 'missing or not answering', prefail: WORDS.healthFail, failing: WORDS.healthFail };
    return SF.h('ul', { class: 'check-list bk-disks' }, disks.map((d) => {
      const bad = ['degraded', 'prefail', 'failing', 'corrupt'].indexOf(d.health) >= 0;
      return SF.h('li', { class: 'check-row', dataset: { disk: d.name } },
        SF.h('span', { class: 'check-icon ' + (bad ? 'err' : d.health === 'passed' ? 'ok' : 'warn') }, SF.icon(bad ? 'x' : d.health === 'passed' ? 'check' : 'help')),
        SF.h('div', { class: 'check-main' }, SF.h('div', { class: 'check-name' }, String(d.label || d.name), ' ', tag(d.name, 'tag-mono tag-outline')),
          SF.h('div', { class: 'muted' }, words[d.health] || WORDS.healthUnread)));
    }));
  }
  function daemonWho(v) {
    const s = (v.daemon || {}).schedulers || {};
    const rows = [];
    if (s.window) rows.push(SF.h('li', null, WORDS.windowLine + ' ', tag(String(s.window), 'tag-mono')));
    (s.places || []).filter((p) => p.counted).forEach((p) => rows.push(SF.h('li', null, SF.h('code', null, String(p.place || '')))));
    return [s.count === null || s.count === undefined ? SF.h('p', { class: 'muted' }, WORDS.countUnknown) : null,
      rows.length ? SF.h('ul', { class: 'bk-schedulers' }, rows) : SF.h('p', { class: 'muted' }, WORDS.noScheduler)];
  }
  async function loadRuns(handle) {
    try { const r = await SF.api('/api/snapraid/runs'); B.druns = (r && r.runs) || []; B.druns_error = null; } catch (e) { B.druns_error = e; }
    handle.refresh();
  }
  function daemonRuns(handle) {
    if (B.druns_error) return SF.h('p', { class: 'muted' }, WORDS.runsError + ' ' + (B.druns_error.message || String(B.druns_error)));
    if (!B.druns) { loadRuns(handle); return SF.skeleton(2); }
    if (!B.druns.length) return SF.h('p', { class: 'muted' }, WORDS.noRuns);
    return SF.h('ul', { class: 'check-list bk-druns' }, B.druns.slice(0, 10).map((r) => SF.h('li', { class: 'check-row', dataset: { verdict: r.verdict } },
      SF.h('div', { class: 'check-main' },
        SF.h('div', { class: 'check-name' }, String(r.words || r.high_command || ''), ' ', tag(WORDS.verdictWords[r.verdict] || r.verdict, r.verdict === 'done' ? 'tag-ok' : 'tag-warn')),
        SF.h('div', { class: 'muted' }, WORDS.startedBy + ' ' + String(r.started_by || '') + ' · ' + WORDS.stepsOf + ': ',
          (r.numbers || []).map((n, i) => [i ? ', ' : '', SF.h('a', { href: '#/backups/run/' + n, dataset: { step: String(n) } }, String(n))]))))));
  }
  function daemonMain(root, v, handle) {
    const d = v.daemon;
    const lab = (d.lab || []).map((c) => SF.h('li', { dataset: { lab: c.id } }, SF.h('strong', null, String(c.id) + ': '), String(c.what || '')));
    SF.rebuild(root, [
      SF.viewHead(WORDS.title, WORDS.sub),
      daemonCard(v, handle),
      section('disks', WORDS.disks, null, daemonDisks(v)),
      section('who', WORDS.whoStarts, null, daemonWho(v)),
      (d.link || {}).installed ? section('runs', WORDS.runsTitle, null, daemonRuns(handle)) : null,
      section('versioned', WORDS.versioned, WORDS.versionedNone, SF.h('p', null, String((v.versioned || {}).words || ''))),
      section('not-covered', WORDS.notCovered, WORDS.notCoveredSub, SF.h('ul', { class: 'bk-not-covered' }, (v.not_covered || []).map((t) => SF.h('li', null, String(t))))),
      lab.length ? section('lab', WORDS.labService, null, SF.h('ul', { class: 'bk-lab-list' }, lab)) : null,
    ]);
  }
  async function loadReview(handle) {
    const q = B.rquery;
    const key = q.disk + '|' + q.change + '|' + q.q + '|' + q.offset;
    if (B.reviewKey === key && B.review) return;
    B.reviewKey = key;
    const params = ['offset=' + q.offset].concat(['disk', 'change', 'q'].filter((k) => q[k]).map((k) => k + '=' + SF.enc(q[k])));
    try { B.review = await SF.api('/api/snapraid/review?' + params.join('&')); B.reviewError = null; } catch (e) { B.review = null; B.reviewError = e; }
    handle.refresh();
  }
  function reviewPage(root, handle) {
    loadReview(handle);
    const back = SF.h('a', { class: 'btn btn-sm', href: '#/backups', dataset: { action: 'back' } }, SF.icon('chevron-left', 'icon-sm'), WORDS.back);
    if (B.reviewError) { SF.rebuild(root, [SF.viewHead(WORDS.reviewTitle, WORDS.reviewSub, back), SF.unavailable(B.reviewError, WORDS.reviewTitle)]); return; }
    if (!B.review) { SF.rebuild(root, [SF.viewHead(WORDS.reviewTitle, WORDS.reviewSub, back), SF.skeleton(6)]); return; }
    const r = B.review;
    const per = r.per_disk || {};
    const perDisk = SF.h('ul', { class: 'bk-per-disk' }, Object.keys(per).map((disk) => SF.h('li', null, SF.h('strong', null, diskWord(disk)), ': ',
      Object.entries(per[disk]).map(([k, n]) => SF.fmtNum(n) + ' ' + (WORDS[k] || k)).join(', '))));
    const q = B.rquery;
    const diskSel = SF.selectEl([['', WORDS.allDisks]].concat(Object.keys(per).map((n) => [n, diskWord(n)])), q.disk, { 'aria-label': WORDS.allDisks, dataset: { focus: 'r-disk' } });
    const changeSel = SF.selectEl([['', WORDS.allChanges]].concat(['removed', 'updated', 'added', 'moved', 'copied', 'restored'].map((k) => [k, WORDS[k]])), q.change, { 'aria-label': WORDS.allChanges, dataset: { focus: 'r-change' } });
    const search = SF.h('input', { class: 'input input-sm', type: 'search', value: B.rdraft === null ? q.q : B.rdraft, placeholder: WORDS.search, 'aria-label': WORDS.search, dataset: { focus: 'r-q' } });
    search.addEventListener('input', () => { B.rdraft = search.value; });
    const apply = () => { B.rquery = { disk: diskSel.value, change: changeSel.value, q: search.value.trim(), offset: 0 }; B.rdraft = null; B.reviewKey = ''; loadReview(handle); };
    diskSel.addEventListener('change', apply); changeSel.addEventListener('change', apply);
    search.addEventListener('keydown', (e) => { if (e.key === 'Enter') apply(); });
    const total = Number(r.total) || 0;
    const start = Number(r.offset) || 0;
    const entries = r.entries || [];
    const offers = ((B.view || {}).daemon || {}).offers || {};
    const list = entries.length ? SF.h('ul', { class: 'check-list bk-changes' }, entries.map((e) => {
      const key = e.disk + '\u0000' + e.path;
      // one disk per undelete: the protection service would bring back every chosen name on every chosen disk
      const otherDisk = B.picked.size > 0 && !Array.from(B.picked.values()).some((p) => p.disk === e.disk);
      const box = e.change === 'removed' && offers.undelete ? SF.h('input', { type: 'checkbox', checked: B.picked.has(key), disabled: otherDisk, title: otherDisk ? WORDS.oneDisk : null, 'aria-label': WORDS.choose, dataset: { pick: '1' } }) : null;
      if (box) box.addEventListener('change', () => { if (box.checked) B.picked.set(key, { disk: e.disk, path: e.path }); else B.picked.delete(key); handle.refresh(); });
      return SF.h('li', { class: 'check-row bk-change', dataset: { kind: e.change } }, box,
        SF.h('div', { class: 'check-main' }, SF.h('div', { style: { display: 'flex', gap: '6px', 'flex-wrap': 'wrap', 'align-items': 'baseline' } },
          tag(WORDS[e.change] || e.change, e.change === 'removed' || e.change === 'updated' ? 'tag-warn' : ''), tag(diskWord(e.disk), 'tag-outline'),
          SF.h('span', { class: 'bk-path', style: { 'word-break': 'break-all', 'white-space': 'pre-wrap', 'min-width': '0' } }, String(e.path || '')))));
    })) : SF.h('p', { class: 'muted' }, WORDS.noEntries);
    const pager = SF.h('div', { class: 'actions bk-pager', style: { gap: '8px', 'flex-wrap': 'wrap', 'align-items': 'center' } },
      SF.h('span', { class: 'muted bk-range' }, total ? (start + 1) + '–' + (start + entries.length) + ' ' + WORDS.of + ' ' + SF.fmtNum(total) : '0'),
      SF.btn(WORDS.prev, { cls: 'btn-sm', disabled: start <= 0, dataset: { action: 'prev' }, onClick: () => { B.rquery.offset = Math.max(0, start - 500); loadReview(handle); } }),
      SF.btn(WORDS.next, { cls: 'btn-sm', disabled: start + entries.length >= total, dataset: { action: 'next' }, onClick: () => { B.rquery.offset = start + 500; loadReview(handle); } }));
    const bring = SF.btn(WORDS.bringBack + (B.picked.size ? ' (' + SF.fmtNum(B.picked.size) + ' ' + WORDS.chosen + ')' : ''), { icon: 'download', cls: 'btn-sm btn-primary', disabled: !B.picked.size, dataset: { action: 'act-undelete' } });
    bring.addEventListener('click', () => act('undelete', { paths: Array.from(B.picked.values()) }, bring, { refresh: () => handle.refresh() }).then(() => { B.picked.clear(); handle.refresh(); }));
    const accept = offers.accept ? SF.btn(WORDS.wayAccept, { icon: 'check', cls: 'btn-sm', dataset: { action: 'act-accept' } }) : null;
    if (accept) accept.addEventListener('click', () => act('accept', null, accept, { refresh: () => handle.refresh() }));
    SF.rebuild(root, [
      SF.viewHead(WORDS.reviewTitle, WORDS.reviewSub, back),
      r.cut ? SF.callout('warn', 'alert', null, WORDS.reviewCut) : null,
      section('per-disk', WORDS.perDisk, null, perDisk),
      section('list', WORDS.changesTitle, null, [SF.h('div', { class: 'actions', style: { gap: '8px', 'flex-wrap': 'wrap' } }, diskSel, changeSel, search), list, pager,
        B.picked.size && offers.undelete ? SF.h('p', { class: 'muted bk-one-disk', style: { 'margin-top': '10px' } }, WORDS.oneDisk) : null,
        SF.h('div', { class: 'actions', style: { gap: '8px', 'flex-wrap': 'wrap', 'margin-top': '10px' } }, offers.undelete ? bring : null, accept)]),
    ]);
  }
  async function loadStep(number, handle) {
    if (B.stepKey === number && B.step) return;
    B.stepKey = number;
    try { B.step = await SF.api('/api/snapraid/runs/' + SF.enc(number)); } catch (e) { B.step = { error: e.message || String(e) }; }
    handle.refresh();
  }
  function stepPage(root, number, handle) {
    loadStep(number, handle);
    const back = SF.h('a', { class: 'btn btn-sm', href: '#/backups', dataset: { action: 'back' } }, SF.icon('chevron-left', 'icon-sm'), WORDS.back);
    const r = B.stepKey === number ? B.step : null;
    if (!r) { SF.rebuild(root, [SF.viewHead(WORDS.stepTitle, null, back), SF.skeleton(3)]); return; }
    if (r.error) { SF.rebuild(root, [SF.viewHead(WORDS.stepTitle, null, back), SF.callout('err', 'alert', WORDS.loadError, r.error)]); return; }
    const t = r.task || {};
    SF.rebuild(root, [
      SF.viewHead(WORDS.stepTitle + ' ' + number, null, back),
      SF.h('dl', { class: 'kv' }, [['Step', t.command], ['Of', t.high_command], ['Status', t.status], ['Exit', t.exit_code], ['Message', t.exit_msg]]
        .filter(([, val]) => val !== undefined && val !== null).map(([k, val]) => [SF.h('dt', null, k), SF.h('dd', { class: 'mono' }, String(val))])),
      section('messages', WORDS.messages, null, (r.messages || []).length ? SF.h('pre', { class: 'code-block bk-messages', style: { 'max-height': '60vh' } }, (r.messages || []).map((m) => String(m.level || '') + ': ' + String(m.text || '')).join('\n')) : SF.h('p', { class: 'muted' }, WORDS.noMessages)),
    ]);
  }

  // ---------------------------------------------------------------- pages
  function mainPage(root, handle) {
    if (B.error && !B.view) { SF.rebuild(root, [SF.viewHead(WORDS.title, WORDS.sub), SF.unavailable(B.error, WORDS.card)]); return; }
    if (!B.view) { SF.rebuild(root, [SF.viewHead(WORDS.title, WORDS.sub), SF.skeleton(4)]); return; }
    const v = B.view;
    if (isDaemon(v)) { daemonMain(root, v, handle); return; }
    SF.rebuild(root, [
      SF.viewHead(WORDS.title, WORDS.sub),
      protectionCard(v, handle),
      v.state === 'ready' ? section('disks', WORDS.disks, null, diskRows(v)) : null,
      v.state === 'ready' ? section('who', WORDS.whoRuns, null, whoRuns(v)) : null,
      section('versioned', WORDS.versioned, WORDS.versionedNone, SF.h('p', null, String((v.versioned || {}).words || ''))),
      section('not-covered', WORDS.notCovered, WORDS.notCoveredSub, SF.h('ul', { class: 'bk-not-covered' }, (v.not_covered || []).map((t) => SF.h('li', null, String(t))))),
      v.state === 'ready' ? section('details', WORDS.details, null, details(v, handle)) : null,
    ]);
  }
  function changesPage(root, route, handle) {
    const id = route.parts[1] || '';
    loadGuard(id, handle);
    const back = SF.h('a', { class: 'btn btn-sm', href: '#/backups', dataset: { action: 'back' } }, SF.icon('chevron-left', 'icon-sm'), WORDS.back);
    if (B.guardError) { SF.rebuild(root, [SF.viewHead(WORDS.changesTitle, WORDS.changesSub, back), SF.unavailable(B.guardError, WORDS.changesTitle)]); return; }
    if (!B.guard) { SF.rebuild(root, [SF.viewHead(WORDS.changesTitle, WORDS.changesSub, back), SF.skeleton(6)]); return; }
    const g = B.guard.guard || {};
    const page = B.guard.page || {};
    const numbers = g.numbers || {};
    const diskName = (name) => (numbers[name] ? WORDS.disk + ' ' + numbers[name] : WORDS.protection);
    const counts = (g.counts || {}).disks || {};
    const checks = SF.h('ul', { class: 'check-list bk-checks' }, (g.checks || []).map((c) => SF.h('li', { class: 'check-row', dataset: { check: c.id, result: c.result } },
      SF.h('span', { class: 'check-icon ' + ({ pass: 'ok', hold: 'err', error: 'err', wait: 'warn', note: 'info' }[c.result] || 'info') }, SF.icon({ pass: 'check', hold: 'x', error: 'x', wait: 'clock' }[c.result] || 'info')),
      SF.h('div', { class: 'check-main' }, SF.h('div', { class: 'check-name' }, c.id + ' ', tag(WORDS.result[c.result] || c.result)), SF.h('div', null, String(c.words || ''))))));
    const perDisk = SF.h('ul', { class: 'bk-per-disk' }, Object.keys(counts).map((name) => {
      const c = counts[name];
      const bits = ['added', 'removed', 'updated', 'moved', 'replaced'].filter((k) => c[k]).map((k) => SF.fmtNum(c[k]) + ' ' + WORDS[k]);
      return SF.h('li', null, SF.h('strong', null, diskName(name)), ': ', bits.join(', ') || '0', c.lost ? ' (' + SF.fmtNum(c.lost) + ' ' + WORDS.lost + ')' : '');
    }));
    const q = B.query;
    const diskSel = SF.selectEl([['', WORDS.allDisks]].concat(Object.keys(counts).map((n) => [n, diskName(n)])), q.disk, { 'aria-label': WORDS.allDisks, dataset: { focus: 'f-disk' } });
    const kindSel = SF.selectEl([['', WORDS.allKinds]].concat(['removed', 'updated', 'added', 'moved', 'replaced'].map((k) => [k, WORDS[k]])), q.kind, { 'aria-label': WORDS.allKinds, dataset: { focus: 'f-kind' } });
    const search = SF.h('input', { class: 'input input-sm', type: 'search', value: B.draft === null ? q.q : B.draft, placeholder: WORDS.search, 'aria-label': WORDS.search, dataset: { focus: 'f-q' } });
    search.addEventListener('input', () => { B.draft = search.value; });
    const apply = () => { B.query = { disk: diskSel.value, kind: kindSel.value, q: search.value.trim(), offset: 0 }; B.draft = null; B.guardKey = ''; loadGuard(id, handle); };
    diskSel.addEventListener('change', apply); kindSel.addEventListener('change', apply);
    search.addEventListener('keydown', (e) => { if (e.key === 'Enter') apply(); });
    const total = Number(page.total) || 0;
    const start = Number(page.offset) || 0;
    const entries = page.entries || [];
    const list = entries.length ? SF.h('ul', { class: 'check-list bk-changes' }, entries.map((e) => SF.h('li', { class: 'check-row bk-change', dataset: { kind: e.kind } },
      SF.h('div', { class: 'check-main' },
        SF.h('div', { style: { display: 'flex', gap: '6px', 'flex-wrap': 'wrap', 'align-items': 'baseline' } }, tag(WORDS[e.kind] || e.kind, e.kind === 'removed' || e.kind === 'updated' ? 'tag-warn' : ''), tag(diskName(e.disk), 'tag-outline'),
          SF.h('span', { class: 'bk-path', style: { 'word-break': 'break-all', 'white-space': 'pre-wrap', 'min-width': '0' } }, String(e.path || ''))),
        e.from ? SF.h('div', { class: 'muted bk-from', style: { 'word-break': 'break-all' } }, String(e.from)) : null,
        e.replaced_by ? SF.h('div', { class: 'muted bk-replaced', style: { 'word-break': 'break-all' } }, WORDS.replaced + ': ' + String(e.replaced_by)) : null,
        e.size ? SF.h('div', { class: 'muted' }, fmtBytes(e.size)) : null)))) : SF.h('p', { class: 'muted' }, WORDS.noEntries);
    const pager = SF.h('div', { class: 'actions bk-pager', style: { gap: '8px', 'flex-wrap': 'wrap', 'align-items': 'center' } },
      SF.h('span', { class: 'muted bk-range' }, total ? (start + 1) + '–' + (start + entries.length) + ' ' + WORDS.of + ' ' + SF.fmtNum(total) : '0'),
      SF.btn(WORDS.prev, { cls: 'btn-sm', disabled: start <= 0, dataset: { action: 'prev' }, onClick: () => { B.query.offset = Math.max(0, start - 500); loadGuard(id, handle); } }),
      SF.btn(WORDS.next, { cls: 'btn-sm', disabled: start + entries.length >= total, dataset: { action: 'next' }, onClick: () => { B.query.offset = start + 500; loadGuard(id, handle); } }));
    const groups = (page.groups || []).slice(0, 8);
    SF.rebuild(root, [
      SF.viewHead(WORDS.changesTitle, WORDS.changesSub, back),
      SF.h('section', { class: 'home-card', dataset: { part: 'verdict', verdict: g.verdict || '' } }, SF.h('strong', null, WORDS.verdict[g.verdict] || WORDS.noReport), SF.h('p', { class: 'muted' }, WORDS.report), g.at ? SF.timeEl(g.at) : null),
      section('checks', WORDS.checksTitle, null, [checks, (g.runs || []).length ? SF.h('p', { class: 'muted bk-guard-runs' }, WORDS.guardRuns + ' ',
        (g.runs || []).map((job, i) => [i ? ', ' : '', SF.h('a', { href: '#/backups/run/' + SF.enc(job), dataset: { run: job } }, String(job))])) : null]),
      section('per-disk', WORDS.perDisk, null, perDisk),
      groups.length ? section('groups', WORDS.groups, null, SF.h('ul', { class: 'bk-groups' }, groups.map((gr) => SF.h('li', null, SF.fmtNum(gr.count) + ' ' + (WORDS[gr.kind] || gr.kind) + ' ' + WORDS.under + ' ', SF.h('span', { style: { 'word-break': 'break-all' } }, gr.folder ? String(gr.folder) + '/' : WORDS.top), ' (' + diskName(gr.disk) + ')')))) : null,
      section('list', WORDS.changesTitle, null, [SF.h('div', { class: 'actions', style: { gap: '8px', 'flex-wrap': 'wrap' } }, diskSel, kindSel, search), list, pager]),
    ]);
  }
  function runPage(root, route, handle) {
    const id = route.parts[1] || '';
    loadRun(id, handle, false);
    const back = SF.h('a', { class: 'btn btn-sm', href: '#/backups', dataset: { action: 'back' } }, SF.icon('chevron-left', 'icon-sm'), WORDS.back);
    const r = B.runKey === id ? B.run : null;
    if (!r) { SF.rebuild(root, [SF.viewHead(WORDS.runTitle, null, back), SF.skeleton(4)]); return; }
    if (r.error) { SF.rebuild(root, [SF.viewHead(WORDS.runTitle, null, back), SF.callout('err', 'alert', WORDS.loadError, r.error)]); return; }
    const more = SF.btn(WORDS.more, { cls: 'btn-sm', dataset: { action: 'more' }, onClick: () => loadRun(id, handle, true) });
    SF.rebuild(root, [
      SF.viewHead(WORDS.runTitle + ' ' + id, String(r.words || ''), back),
      SF.h('dl', { class: 'kv' }, [['Verb', r.verb], ['Command', r.command], ['Outcome', r.outcome], ['Exit', r.exit], ['Started', r.started], ['Ended', r.ended],
        [WORDS.commandLine, (r.argv || []).join(' ')]]
        .map(([k, v]) => [SF.h('dt', null, k), SF.h('dd', { class: 'mono', style: { 'word-break': 'break-all' }, dataset: k === WORDS.commandLine ? { part: 'argv' } : {} }, v === null || v === undefined ? '' : String(v))])),
      section('output', WORDS.runOutput, null, [SF.h('pre', { class: 'code-block bk-output', style: { 'max-height': '60vh' } }, B.runText || WORDS.noOutput), B.runMore ? more : null]),
    ]);
  }
  // A block's commands install and check; its then step (a second Copy) moves the file into place, chained on that
  // check, so even a block pasted whole never moves an unchecked file.
  function block(b) {
    const text = (b.commands || []).join('\n');
    const then = (b.then || []).join('\n');
    const copy = SF.btn(WORDS.copy, { cls: 'btn-xs', icon: 'copy', dataset: { action: 'copy', block: b.id } });
    copy.addEventListener('click', () => SF.copyText(text, WORDS.copied));
    const copyThen = SF.btn(WORDS.copy, { cls: 'btn-xs', icon: 'copy', dataset: { action: 'copy-then', block: b.id } });
    copyThen.addEventListener('click', () => SF.copyText(then, WORDS.copied));
    return SF.h('li', { class: 'check-row bk-block', dataset: { block: b.id } },
      SF.h('span', { class: 'check-icon info' }, String(b.id)),
      SF.h('div', { class: 'check-main', style: { display: 'grid', gap: '6px', 'min-width': '0' } },
        SF.h('div', { class: 'check-name' }, String(b.title || '')),
        text ? SF.h('pre', { class: 'code-block' }, text) : null,
        text ? SF.h('div', null, copy) : null,
        b.expect ? SF.h('div', null, SF.h('strong', null, WORDS.expect + ' '), String(b.expect)) : null,
        then ? SF.h('div', { class: 'bk-then', style: { display: 'grid', gap: '6px' } }, SF.h('strong', null, WORDS.thenStep), SF.h('pre', { class: 'code-block' }, then), SF.h('div', null, copyThen)) : null,
        b.note ? SF.h('div', { class: 'muted' }, SF.h('strong', null, WORDS.note + ' '), String(b.note)) : null,
        b.if_not ? SF.h('div', { class: 'muted' }, SF.h('strong', null, WORDS.ifNot + ' '), SF.h('code', null, String(b.if_not))) : null));
  }
  function setupPage(root, handle) {
    if (!B.setup && !B.setupError) loadSetup(handle);
    const back = SF.h('a', { class: 'btn btn-sm', href: '#/backups', dataset: { action: 'back' } }, SF.icon('chevron-left', 'icon-sm'), WORDS.back);
    if (B.setupError) { SF.rebuild(root, [SF.viewHead(WORDS.setupTitle, WORDS.setupSub, back), SF.unavailable(B.setupError, WORDS.setupTitle)]); return; }
    if (!B.setup) { SF.rebuild(root, [SF.viewHead(WORDS.setupTitle, WORDS.setupSub, back), SF.skeleton(5)]); return; }
    const stage = SF.btn(WORDS.stage, { icon: 'download', cls: 'btn-sm btn-primary', dataset: { action: 'stage' } });
    stage.addEventListener('click', async () => {
      SF.setBusy(stage, true);
      try { B.setup = await SF.api('/api/snapraid/setup/stage', { method: 'POST', body: {} }); handle.refresh(); } catch (e) { SF.toastError(null, e); } finally { SF.setBusy(stage, false); }
    });
    const check = SF.btn(WORDS.check, { icon: 'check', cls: 'btn-sm', dataset: { action: 'check' } });
    check.addEventListener('click', async () => {
      SF.setBusy(check, true);
      try { const r = await SF.api('/api/snapraid/setup/check', { method: 'POST', body: {} }); B.checkWords = String(r.words || ''); handle.refresh(); } catch (e) { SF.toastError(null, e); } finally { SF.setBusy(check, false); }
    });
    SF.rebuild(root, [
      SF.viewHead(WORDS.setupTitle, WORDS.setupSub, back),
      SF.h('div', { class: 'actions', style: { gap: '8px', 'flex-wrap': 'wrap' } }, stage, check),
      B.setup.staged ? SF.h('p', { class: 'muted' }, WORDS.staged + ' ', SF.h('code', null, String(B.setup.setup_dir || ''))) : null,
      B.checkWords ? SF.callout('info', 'info', null, B.checkWords) : null,
      SF.h('ol', { class: 'check-list bk-blocks', style: { 'margin-top': '12px' } }, (B.setup.blocks || []).map(block)),
      section('removal', WORDS.removal, WORDS.removalSub, SF.h('ul', { class: 'check-list' }, (B.setup.removal || []).map(block))),
      B.setup.link ? linkSetup(B.setup.link, handle) : null,
    ]);
  }
  function linkSetup(link, handle) {
    const stage = SF.btn(WORDS.linkStage, { icon: 'download', cls: 'btn-sm', dataset: { action: 'link-stage' } });
    stage.addEventListener('click', async () => {
      SF.setBusy(stage, true);
      try { B.setup = await SF.api('/api/snapraid/setup/link-stage', { method: 'POST', body: {} }); handle.refresh(); } catch (e) { SF.toastError(null, e); } finally { SF.setBusy(stage, false); }
    });
    const check = SF.btn(WORDS.linkCheck, { icon: 'check', cls: 'btn-sm', dataset: { action: 'link-check' } });
    check.addEventListener('click', async () => {
      SF.setBusy(check, true);
      try { const r = await SF.api('/api/snapraid/setup/link-check', { method: 'POST', body: {} }); B.checkWords = String(r.words || ''); handle.refresh(); } catch (e) { SF.toastError(null, e); } finally { SF.setBusy(check, false); }
    });
    return section('link', WORDS.linkSetup, WORDS.linkSetupSub, [
      SF.h('div', { class: 'actions', style: { gap: '8px', 'flex-wrap': 'wrap' } }, stage, check),
      SF.h('ol', { class: 'check-list bk-link-blocks', style: { 'margin-top': '12px' } }, (link.blocks || []).map(block)),
      SF.h('p', null, WORDS.serviceLines), SF.h('pre', { class: 'code-block bk-daemon-lines' }, (link.daemon_lines || []).join('\n')),
      SF.h('ul', { class: 'check-list' }, (link.removal || []).map(block)),
    ]);
  }

  // ---------------------------------------------------------------- Home
  function homeStatus(v) {
    const l = v.light || { tone: 'muted', reason: '' };
    const needs = [];
    const lead = String(l.reason || '');
    if (ERR_CODES.has(l.code)) needs.push({ key: 'backups:' + l.code, tone: 'err', icon: 'alert', href: '#/backups', text: lead, sub: WORDS.card });
    else if (WARN_CODES.has(l.code)) needs.push({ key: 'backups:' + l.code, tone: 'warn', icon: 'alert', href: '#/backups', text: lead, sub: WORDS.card });
    for (const w of v.warnings || []) needs.push({ key: 'backups:' + w.code, tone: 'warn', icon: 'shield', href: '#/backups', text: String(w.words || ''), sub: WORDS.card });
    return { tone: l.tone === 'err' ? 'err' : 'warn', text: WORDS.card + ': ' + lowerFirst(lead).replace(/\.$/, ''), detail: WORDS.homeNoVersioned, needs };
  }

  SF.registerView({
    id: 'backups',
    title: 'Backups',
    routes: ['backups'],
    nav: { label: 'Backups', icon: 'layers', order: 45 },
    home: {
      title: 'Backups', icon: 'layers', order: 45, href: '#/backups', text: WORDS.homeText,
      routes: [{ to: 'tracker', label: 'disk health findings' }],
      events: ['snapraid.updated', 'snapraid.guard', 'snapraid.action', 'snapraid.run', 'snapraid.daemon'],
      async load() {
        const v = await SF.api('/api/snapraid');
        return homeStatus(SF.isObj(v) ? v : {});
      },
    },
    enter(route, prev, handle) {
      const sub = route.parts[0] || '';
      if (!sub) load(handle);
      if (sub === 'changes' && (!prev || prev.parts[1] !== route.parts[1])) { B.query = { disk: '', kind: '', q: '', offset: 0 }; B.draft = null; B.guardKey = ''; B.guard = null; }
      if (sub === 'run') { B.runKey = ''; B.run = null; B.stepKey = ''; B.step = null; }
      if (sub === 'review') { B.rquery = { disk: '', change: '', q: '', offset: 0 }; B.rdraft = null; B.reviewKey = ''; B.review = null; if (!B.view) load(null); }
      if (!sub) { B.druns = null; B.druns_error = null; }
      if (!sub) { B.jobs = null; B.jobsError = null; if (B.detailsOpen) loadJobs(handle); }
      if (sub === 'setup') { B.setup = null; B.setupError = null; B.checkWords = ''; }
    },
    render(root, route, handle) {
      const sub = route.parts[0] || '';
      if (!sub) return mainPage(root, handle);
      if (sub === 'changes' && route.parts[1]) return changesPage(root, route, handle);
      if (sub === 'run' && /^\d+$/.test(route.parts[1] || '')) return stepPage(root, route.parts[1], handle);
      if (sub === 'run' && route.parts[1]) return runPage(root, route, handle);
      if (sub === 'review') return reviewPage(root, handle);
      if (sub === 'setup') return setupPage(root, handle);
      SF.rebuild(root, [SF.viewHead(WORDS.title, WORDS.sub), SF.emptyState('layers', WORDS.unknownPage, null, [SF.h('a', { class: 'btn btn-sm', href: '#/backups' }, WORDS.back)])]);
      return undefined;
    },
    events: {
      'snapraid.updated': (ev, handle) => { if (handle.isShown()) load(handle); },
      'snapraid.guard': (ev, handle) => { if (handle.isShown()) load(handle); },
      'snapraid.action': (ev, handle) => { if (handle.isShown()) load(handle); },
      'snapraid.run': (ev, handle) => { B.druns = null; if (handle.isShown()) load(handle); },
      'snapraid.daemon': (ev, handle) => { if (handle.isShown()) load(handle); },
    },
  });
})(window.SF);
