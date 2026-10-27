/* Seraphina OS Harness: The Codex worker (DESIGN §44; docs/seraphina/BROWSER_AND_CODEX.md §3.3, §3.6, §3.7, §3.12,
 * §4.4), phase BC1b. A factory job whose builder runs on Codex: Codex edits, the harness commits and re-runs the
 * repository's tests; every Codex run has a receipt the harness wrote.
 * Routes: #/codex (on or off, the pin, the Codex user, the Responses checks, and the owner's setup blocks: nothing is
 * applied by the harness). Rail: "Codex", order 78, shown once the worker is on. Home: no card.
 * In the job drawer (SF.extend, SF.registerJobTab): a "Codex" chip and the outcome on a Codex run's header (runHead);
 * under the head (jobHead) the reconcile of a run that did not end cleanly (what it left, never replayed) and
 * "Verify in the browser" on a job with a preview while its worktree exists; a "Receipts" tab with each run's
 * receipt: the outcome, the paths, the harness commit, the write-policy, secret-scan and verify checks, the home's
 * check and the hash chain. Paths and commands are raw text through SF.h; a receipt holds hashes, never page or model
 * text. Every sentence a person reads is in WORDS. */
'use strict';
(function (SF) {
  const h = SF.h;
  const WORDS = {
    title: 'Codex',
    sub: 'A factory job whose builder runs on Codex: Codex edits, the harness commits and re-runs the tests.',
    on: 'On', off: 'Off',
    offText: 'It is off in the settings (codex.enabled). A job whose builder is routed to codex-worker does not start.',
    pin: 'Pinned Codex', user: 'Runs as', noUser: 'the harness’s own user (no Codex user yet: the test suite’s containment, not the server’s)',
    checks: 'Responses checks (in a swap window, under a custody request lease)', noChecks: 'None yet: the codex model line is a blocker until one passes.',
    setup: 'The owner’s setup blocks', setupText: 'Nothing here is applied by the harness: each block is yours to paste, and the doctor checks after.',
    expect: 'Expect', undo: 'Undo', then: 'Then, only when that matched',
    risk: 'Residual risk',
    codexChip: 'Codex', codexTitle: 'This run is Codex under its own sandbox; the harness commits',
    outcome: { completed: 'completed', failed: 'failed', declined: 'declined', cancelled: 'cancelled', timed_out: 'timed out', unknown: 'outcome unknown' },
    reconcileTitle: 'The Codex run ended',
    reconcileText: 'Nothing was committed and nothing is sent again. Retry starts a fresh run on the worktree as it is.',
    verify: 'Verify in the browser',
    verifyTitle: 'Open a browsing chat on this job’s preview (its own namespace, no network)',
    verifyNote: 'Never a merge gate: the receipt goes beside the job’s criteria for the approver or you.',
    verifyOpened: 'A browsing chat on the job’s preview is open.',
    tab: 'Receipts',
    none: 'No Codex run in this job yet.',
    chainOk: 'The receipts’ hash chain holds.', chainBroken: 'The receipts’ hash chain is broken at',
    commit: 'harness commit', noCommit: 'no commit',
    paths: 'changed', home: 'home checked', homeBad: 'home not checked',
    check: { write_policy: 'write policy', secret_scan: 'secret scan', verify: 'verify' },
    receipt: 'receipt', show: 'Show the receipt', loading: 'Loading…', loadError: 'Could not load:',
    exit: 'exit',
    lastVerify: 'Last verify',
  };
  const TONE = { completed: 'tag-ok', failed: 'tag-err', declined: 'tag-err', cancelled: 'tag-warn', timed_out: 'tag-warn', unknown: 'tag-warn' };
  const C = { state: null, error: null, receipts: new Map(), loading: new Map() };
  function tag(text, cls, title) { return h('span', { class: 'tag ' + (cls || ''), title: title || null }, text); }
  const isCodexJob = (job) => SF.isObj(job) && SF.isObj(job.routing) && SF.isObj(job.routing.builder) &&
    (job.routing.builder.driver === 'codex-worker' || (job.routing.builder.escalate || []).some((r) => SF.isObj(r) && r.driver === 'codex-worker'));
  const keyOf = (job) => job.repo + '/' + job.id;
  const sigOf = (job) => [(job.runs || []).length, job.stage, job.updated_at].join('|');

  function receipts(job, onDone) {
    const key = keyOf(job);
    const have = C.receipts.get(key);
    if (have && have.sig === sigOf(job)) return have.data;
    if (!C.loading.has(key)) {
      C.loading.set(key, SF.api('/api/jobs/' + SF.enc(job.repo) + '/' + SF.enc(job.id) + '/receipts')
        .then((r) => { C.receipts.set(key, { sig: sigOf(job), data: r, error: null }); })
        .catch((e) => { C.receipts.set(key, { sig: sigOf(job), data: null, error: e }); })
        .finally(() => { C.loading.delete(key); if (onDone) onDone(); }));
    }
    return have ? have.data : null;
  }

  // ---------------------------------------------------------------- a Codex run's header
  SF.extend('runHead', (run) => {
    if (!run || !SF.isObj(run.start) || !SF.isObj(run.start.codex)) return null;
    const out = [tag(WORDS.codexChip, 'tag-outline cx-chip', WORDS.codexTitle + ' (' + String(run.start.codex.version || '') + ', ' + String(run.start.codex.permissions_profile || '') + ')')];
    const outcome = SF.isObj(run.end) ? String(run.end.outcome || '') : '';
    if (outcome && outcome !== 'completed') out.push(tag(WORDS.outcome[outcome] || outcome, (TONE[outcome] || 'tag-outline') + ' cx-outcome'));
    return out;
  });

  // ---------------------------------------------------------------- under the job's head
  SF.extend('jobHead', (job) => {
    if (!isCodexJob(job) && !SF.isObj(job.reconcile)) return null;
    const out = [];
    const rec = SF.isObj(job.reconcile) ? job.reconcile : null;
    if (rec && (job.stage === 'failed' || job.stage === 'cancelled' || job.stage === 'needs-human')) {
      const outcome = String(rec.outcome || '');
      out.push(h('div', { class: 'cx-reconcile', dataset: { part: 'reconcile', outcome } },
        SF.callout('warn', 'alert', WORDS.reconcileTitle + ' ' + (WORDS.outcome[outcome] || outcome) + ':',
          [String(rec.summary || ''), '. ', WORDS.reconcileText,
            rec.diff_sha256 ? [' ', h('code', { class: 'cx-hash', title: 'sha256 of the diff it left' }, String(rec.diff_sha256).slice(0, 12))] : null])));
    }
    const data = receipts(job, () => SF.refreshJobs());
    const offer = data && SF.isObj(data.preview) ? data.preview : null;
    if (offer && offer.offered) {
      const b = SF.btn(WORDS.verify, { cls: 'btn-sm', icon: 'eye', title: WORDS.verifyTitle });
      b.dataset.action = 'verify-in-browser';
      b.addEventListener('click', async () => {
        SF.setBusy(b, true);
        try {
          const r = await SF.api('/api/jobs/' + SF.enc(job.repo) + '/' + SF.enc(job.id) + '/verify-in-browser', { method: 'POST', body: {} });
          SF.toast(WORDS.verifyOpened, { kind: 'ok' });
          if (r && r.chat && r.chat.id) SF.go('#/chat/' + SF.enc(String(r.chat.id)));
        } catch (e) { SF.toastError(null, e); } finally { SF.setBusy(b, false); }
      });
      out.push(h('div', { class: 'cx-verify', dataset: { part: 'verify' } }, b, h('span', { class: 'muted cx-note' }, WORDS.verifyNote)));
    }
    return out.length ? out : null;
  });

  // ---------------------------------------------------------------- the Receipts tab
  function checkTag(c) {
    const kind = String(c.kind || '');
    const ok = kind === 'verify' ? c.check === 'ok' : c.ok === true;
    const label = WORDS.check[kind] || kind;
    const extra = kind === 'verify' && c.exit !== undefined && c.exit !== null ? ' · ' + WORDS.exit + ' ' + String(c.exit) : '';
    return tag((ok ? '✓ ' : '✗ ') + label + extra, ok ? 'tag-ok' : 'tag-err', c.reason ? String(c.reason) : null);
  }
  function receiptRow(job, r) {
    const outcome = String(r.outcome || '');
    const codex = SF.isObj(r.codex) ? r.codex : {};
    const details = h('details', { class: 'cx-raw' }, h('summary', null, WORDS.show));
    details.addEventListener('toggle', async () => {
      if (!details.open || details.dataset.loaded) return;
      details.dataset.loaded = '1';
      const pre = h('pre', { class: 'cx-json' }, WORDS.loading);
      details.appendChild(pre);
      try {
        const full = await SF.api('/api/jobs/' + SF.enc(job.repo) + '/' + SF.enc(job.id) + '/receipts/' + SF.enc(String(r.run)));
        pre.textContent = JSON.stringify(full.receipt, null, 2);
      } catch (e) { pre.textContent = WORDS.loadError + ' ' + String(e.message || e); }
    });
    return h('li', { class: 'cx-receipt', dataset: { run: String(r.run || ''), outcome } },
      h('div', { class: 'cx-receipt-head' },
        h('code', { class: 'cx-run' }, String(r.run || '')),
        tag(WORDS.outcome[outcome] || outcome || '…', TONE[outcome] || 'tag-outline'),
        r.commit ? tag(WORDS.commit + ' ' + String(r.commit).slice(0, 10), 'tag-outline cx-commit') : tag(WORDS.noCommit, 'tag-outline'),
        tag(r.home_verified ? WORDS.home : WORDS.homeBad, r.home_verified ? 'tag-outline' : 'tag-warn'),
        h('span', { class: 'spacer' }), SF.timeEl(r.ended || r.started, 'muted')),
      h('div', { class: 'cx-checks' }, (r.checks || []).map(checkTag)),
      (r.paths || []).length ? h('div', { class: 'cx-paths' }, h('span', { class: 'muted' }, WORDS.paths + ' '),
        (r.paths || []).slice(0, 20).map((p) => h('code', { class: 'cx-path' }, String(p)))) : null,
      h('div', { class: 'cx-meta muted' }, 'Codex ' + String(codex.version || '?') + ' · ' + String(codex.surface || '') + ' · ' +
        String(codex.permissions_profile || '') + ' · ' + String(codex.approval_policy || '') + (r.provider ? ' · ' : ''),
        r.provider ? h('code', null, String(r.provider)) : null, ' · ' + WORDS.receipt + ' ', h('code', { class: 'cx-hash' }, String(r.sha256 || '').slice(0, 12))),
      details);
  }
  function renderTab(panel, current, ref) {
    // The job may not have reached the page yet (a drawer opened from a link): its ref is enough to ask.
    const job = current || { repo: ref.repo, id: ref.id, runs: [], stage: '', updated_at: '' };
    const have = C.receipts.get(keyOf(job));
    const data = receipts(job, () => renderTab(panel, SF.job(job.repo, job.id), ref));
    if (have && have.error && !data) { SF.rebuild(panel, [SF.callout('err', 'alert', WORDS.loadError, String(have.error.message || have.error))]); return; }
    if (!data) { SF.rebuild(panel, [SF.skeleton(3)]); return; }
    const list = data.receipts || [];
    const broken = data.chain_broken || [];
    const verify = SF.isObj(data.verify) ? data.verify : null;
    SF.rebuild(panel, [
      list.length ? h('ul', { class: 'cx-receipts' }, list.map((r) => receiptRow(job, r))) : SF.emptyState('layers', WORDS.none, null, null, true),
      verify ? h('p', { class: 'cx-last-verify muted' }, WORDS.lastVerify + ': ', tag(verify.check === 'ok' ? '✓ ok' : '✗ failed', verify.check === 'ok' ? 'tag-ok' : 'tag-err'),
        ' ', h('code', null, String(verify.commit || '').slice(0, 10)), verify.exit !== undefined ? ' · ' + WORDS.exit + ' ' + String(verify.exit) : null) : null,
      list.length ? h('p', { class: broken.length ? 'cx-chain is-broken' : 'cx-chain muted' },
        broken.length ? [WORDS.chainBroken + ' ', h('code', null, broken.join(', '))] : WORDS.chainOk) : null,
    ]);
  }
  SF.registerJobTab({ id: 'receipts', label: WORDS.tab, icon: 'shield', render: renderTab });

  // ---------------------------------------------------------------- the page
  function load(handle) {
    return SF.api('/api/codex').then((r) => { C.state = SF.isObj(r) ? r : null; C.error = null; })
      .catch((e) => { C.error = e; })
      .finally(() => { if (handle) { handle.refresh(); handle.setNav(!!(C.state && C.state.enabled)); } });
  }
  function blockCard(b) {
    const lines = (xs) => (xs || []).map((line) => h('code', { class: 'cx-cmd' }, String(line)));
    return h('li', { class: 'cx-block', dataset: { block: String(b.id || '') } },
      h('strong', null, String(b.title || '')),
      (b.commands || []).length ? h('div', { class: 'cx-cmds' }, lines(b.commands)) : null,
      (b.then || []).length ? [h('div', { class: 'muted' }, WORDS.then), h('div', { class: 'cx-cmds' }, lines(b.then))] : null,
      h('div', { class: 'cx-kv' }, h('span', { class: 'muted' }, WORDS.expect + ' '), String(b.expect || '')),
      b.note ? h('p', { class: 'muted cx-note' }, String(b.note)) : null,
      b.if_not ? h('div', { class: 'cx-kv' }, h('span', { class: 'muted' }, WORDS.undo + ' '), h('code', null, String(b.if_not))) : null);
  }
  function page(root) {
    if (C.error && !C.state) { SF.rebuild(root, [SF.viewHead(WORDS.title, WORDS.sub), SF.callout('err', 'alert', WORDS.loadError, String(C.error.message || C.error))]); return; }
    if (!C.state) { SF.rebuild(root, [SF.viewHead(WORDS.title, WORDS.sub), SF.skeleton(6)]); return; }
    const st = C.state;
    const checks = SF.isObj(st.checks) ? Object.entries(st.checks) : [];
    SF.rebuild(root, [
      SF.viewHead(WORDS.title, WORDS.sub, tag(st.enabled ? WORDS.on : WORDS.off, st.enabled ? 'tag-ok' : 'tag-outline')),
      st.enabled ? null : SF.callout('info', 'info', WORDS.off + '.', WORDS.offText),
      h('section', { class: 'card cx-card', dataset: { part: 'worker' } },
        h('div', { class: 'cx-kv' }, h('span', { class: 'muted' }, WORDS.pin + ' '), h('code', null, String(st.pin || '')), st.binary ? [' ', h('code', { class: 'cx-path' }, String(st.binary))] : null),
        h('div', { class: 'cx-kv' }, h('span', { class: 'muted' }, WORDS.user + ' '), st.user ? h('code', null, String(st.user) + ' (' + String(st.via || '') + ')') : WORDS.noUser)),
      h('section', { class: 'card cx-card', dataset: { part: 'checks' } }, h('h2', { class: 'card-title' }, WORDS.checks),
        checks.length ? h('ul', { class: 'cx-checklist' }, checks.map(([server, c]) => h('li', null,
          tag(c.ok ? '✓ passed' : '✗ not passed', c.ok ? 'tag-ok' : 'tag-err'), ' ', h('code', null, String(server)), ' ',
          h('span', { class: 'muted' }, 'HTTP ' + String(c.status) + ' · ' + String(c.at || ''))))) : h('p', { class: 'muted' }, WORDS.noChecks)),
      h('section', { class: 'card cx-card', dataset: { part: 'setup' } }, h('h2', { class: 'card-title' }, WORDS.setup),
        h('p', { class: 'muted' }, WORDS.setupText), h('ol', { class: 'cx-blocks' }, (st.setup || []).map(blockCard)),
        st.risk ? SF.callout('info', 'info', WORDS.risk + ':', String(st.risk)) : null),
    ]);
  }

  SF.registerView({
    id: 'codex',
    title: 'Codex',
    routes: ['codex'],
    nav: { label: 'Codex', icon: 'layers', order: 78, hidden: true },
    start(handle) { load(handle); },
    enter(route, prev, handle) { load(handle); },
    render(root) { page(root); },
  });
})(window.SF);
