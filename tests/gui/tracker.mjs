// Checks for the Housekeeping Tracker (DESIGN §32, §15 "Housekeeping Tracker"; receipt items §36; drift sources §38 and
// §39's GUI notes; Grid receipts and Mermaid maps §26; product decisions §7): the view module swfactory/web/views/tracker.js
// against tests/gui_mock/tracker.py, run by tests/gui/smoke.mjs (AREA_SUITES) after the core checks, or alone with
// GUI_ONLY=tracker node tests/gui/smoke.mjs. Screenshots of every new screen, desktop 1440x900 and phone 390x844:
// t.areaShot(page, AREA, name) → $GUI_AREA_SHOTS/tracker/<name>.png.
//
// run(t) gets the harness of smoke.mjs: { browser, url, startMock, step, assert, canon, waitPost, shot, cleanShot,
// noOverflow, watch, allowHttp, newPage(label, {phone, width, height, colorScheme, mockUrl}), areaShot(page, area, name) }.
// t.newPage gives a signed-in page of its own ({ctx, page, url}); close its ctx when done.
//
// The suite runs on a mock of its own (t.startMock): it proves, drops, moves and approves items, and the other suites
// keep the seeded ledgers.

const AREA = 'tracker';
const J002 = 'job:my-app/002-rate-limit-signup-form';
const DECISION = J002 + '/I-3';
const enc = encodeURIComponent;
const itemUrl = (base, ref) => base + '/#/housekeeping/' + enc(ref);

/** The POST bodies the page sent to path (a RegExp), oldest first (the mock's request log). */
async function posted(page, base, re) {
  const r = await (await page.request.get(base + '/__mock/requests')).json();
  return r.requests.filter((x) => x.method === 'POST' && re.test(x.path)).map((x) => x.body);
}
/** The data-action values of the buttons in a container. */
const actions = (page, sel) => page.$$eval(sel + ' .tr-actions [data-action]', (els) => els.map((e) => e.dataset.action));
async function openItem(page, base, ref) {
  await page.goto(itemUrl(base, ref));
  await page.waitForSelector('#drawer.is-panel:not([hidden]) .tr-history, #drawer.is-panel:not([hidden]) .empty');
}
/** Go to a list hash and read its rows once the filter controls show `want` ({status, owner, kind, source, must,
 *  overdue}; the others empty) and the list is in. */
async function listOf(page, base, hash, want) {
  await page.goto(base + '/' + hash);
  await page.waitForFunction((w) => {
    const f = (sel) => document.querySelector('[data-filter="' + sel + '"]');
    if (!f('status') || document.querySelector('#tracker-root .skel-box')) return false;
    return f('status').value === (w.status || '') && f('owner').value === (w.owner || '') && f('kind').value === (w.kind || '')
      && f('source').value === (w.source || '') && f('must').checked === !!w.must && f('overdue').checked === !!w.overdue;
  }, want);
  return page.$$eval('.tr-row', (els) => els.map((e) => ({ ref: e.dataset.ref, text: e.textContent, must: !!e.querySelector('.tr-must') })));
}
/** In the page: some run row of the Live tab shows a Grid Card receipt. */
const shownGrid = () => Array.from(document.querySelectorAll('#panel-live .run .tr-grid')).some((e) => e.getBoundingClientRect().width > 0);
async function modalOk(page) {
  await page.click('dialog[open] button[data-action="ok"]');
}

export async function run(t) {
  const { step, assert, noOverflow, canon } = t;
  const m = await t.startMock();
  const base = m.url;
  const d = await t.newPage(AREA, { mockUrl: base });
  const p = await t.newPage(AREA + '-phone', { phone: true, mockUrl: base });
  await d.ctx.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: base });
  const page = d.page;
  try {
    await step(AREA + ': the list: title, scope tabs, counts, Waiting for you first, item text as text, at 1440 px and 390 px with no sideways scroll', async () => {
      const api = await (await page.request.get(base + '/api/items?waiting=1&limit=200')).json();
      for (const [pg, tag] of [[page, 'desktop'], [p.page, 'phone']]) {
        await pg.goto(base + '/#/housekeeping');
        await pg.waitForSelector('#view-tracker:not([hidden]) #tracker-root .tr-row');
        assert(await pg.textContent('#tracker-root .view-title') === 'Housekeeping Tracker', 'title');
        assert(/until it is proven done\.$/.test(await pg.textContent('#tracker-root .view-sub')), 'the subtitle of §15');
        const tabs = await pg.$$eval('#tracker-root .tr-scope', (els) => els.map((e) => e.textContent));
        assert(canon(tabs) === canon(['All', 'Jobs', 'Chats', 'Plans', 'Follow-ups', 'System']), 'scope tabs: ' + tabs);
        assert(await pg.getAttribute('.tr-scope.is-on', 'aria-current') === 'page', 'the current scope is marked');
        const waiting = await pg.textContent('.tr-count[data-count="waiting"] strong');
        assert(Number(waiting) === api.counts.waiting, 'the waiting count is counts.waiting: ' + waiting);
        const groups = await pg.$$eval('#tracker-root .tr-group .tr-h2', (els) => els.map((e) => e.textContent));
        assert(/^Waiting for you \(\d+\)$/.test(groups[0]), 'Waiting for you comes first: ' + groups);
        assert(await pg.locator('#tracker-root img, #tracker-root script, #tracker-root iframe').count() === 0, 'no markup from item text is rendered');
        assert((await pg.textContent('#tracker-root')).includes("Should a blocked signup say when to retry? <img src=x onerror=alert('item')> (product)"), 'a hostile title shows as written');
        await noOverflow(pg, AREA + ' list ' + tag);
        if (tag === 'desktop') {
          assert(await pg.getAttribute('.rail-item[data-view="tracker"]', 'aria-current') === 'page', 'the rail marks it');
          assert(await pg.textContent('.rail-item[data-view="tracker"] .rail-badge') === String(api.counts.waiting), 'the rail badge counts counts.waiting');
        } else {
          assert(!(await pg.locator('.tr-filter-box').getAttribute('open')), 'on a phone the filters start folded');
        }
        await t.areaShot(pg, AREA, 'tracker-' + tag);
      }
    });

    await step(AREA + ': filters: a scope tab asks the server for that scope; source finds receipts (reported by the operator), drift sources and persona drift; owner, kind, must-do and overdue narrow the list', async () => {
      await page.goto(base + '/#/housekeeping');
      await page.waitForSelector('.tr-row');
      const sys = page.waitForRequest((r) => /\/api\/items\?/.test(r.url()) && /scope=system/.test(r.url()));
      await page.click('.tr-scope[data-scope="system"]');
      await sys;
      await page.waitForFunction(() => location.hash === '#/housekeeping?scope=system');
      await page.waitForSelector('.tr-row');
      const refs = await page.$$eval('.tr-row', (els) => els.map((e) => e.dataset.ref));
      assert(refs.length && refs.every((r) => /^system:S-\d+$/.test(r)), 'only system items: ' + refs);
      await page.selectOption('[data-filter="source"]', 'receipt');
      await page.waitForFunction(() => /source=receipt/.test(location.hash) && document.querySelectorAll('.tr-row').length === 3);
      const rec = await page.$$eval('.tr-row', (els) => els.map((e) => [e.dataset.ref, e.textContent]));
      assert(rec.length === 3 && rec.every(([, txt]) => txt.includes('Reported by the operator')), 'the receipt items say reported by the operator: ' + JSON.stringify(rec));
      assert(rec.some(([, txt]) => txt.includes('May not be hers')), 'an untrusted receipt item says May not be hers');
      // DESIGN §40: an item about an outcome nobody knows carries the amber words, never done until a person checks it
      const unknown = rec.find(([ref]) => ref === 'system:S-9');
      assert(unknown && unknown[1].includes('Outcome unknown: check before retrying (receipt r-20261025-013)') && await page.locator('.tr-row[data-ref="system:S-9"] .tr-unknown.tag-warn').count() === 1, 'the unknown outcome item is amber: ' + JSON.stringify(unknown));
      await page.selectOption('[data-filter="source"]', 'drift-source');
      await page.waitForFunction(() => /source=drift-source/.test(location.hash) && Array.from(document.querySelectorAll('.tr-row')).every((r) => /drift: checker/.test(r.textContent)));
      const drift = await page.$$eval('.tr-row', (els) => els.map((e) => e.textContent));
      assert(drift.length >= 1 && drift.every((x) => x.includes('drift: checker') && /finding (image|nginx)/.test(x)), 'drift-source items name the source and the key: ' + drift);
      await page.selectOption('[data-filter="source"]', 'drift');
      await page.waitForFunction(() => /source=drift(&|$)/.test(location.hash) && Array.from(document.querySelectorAll('.tr-row')).every((r) => /persona drift/.test(r.textContent)));
      const persona = await page.$$eval('.tr-row', (els) => els.map((e) => e.textContent));
      assert(persona.length === 1 && persona[0].includes('persona drift'), 'persona drift: ' + persona);
      const owners = await listOf(page, base, '#/housekeeping?owner=' + enc('agent:qwen-code'), { owner: 'agent:qwen-code' });
      assert(owners.length === 2 && owners.every((r) => r.text.includes('owner agent qwen-code')), 'an outside agent owner reads “agent qwen-code”: ' + owners.length);
      const decisions = await listOf(page, base, '#/housekeeping?kind=decision&status=all', { kind: 'decision', status: 'all' });
      assert(decisions.length === 3 && decisions.every((r) => r.text.includes('Product decision')), 'three product decisions in all: ' + decisions.length);
      const must = await listOf(page, base, '#/housekeeping?must=1', { must: true });
      assert(must.length > 3 && must.every((r) => r.must), 'must-do only');
      const overdue = await listOf(page, base, '#/housekeeping?overdue=1', { overdue: true });
      assert(canon(overdue.map((r) => r.ref)) === canon(['repo:my-app/F-4']), 'overdue only: ' + overdue.map((r) => r.ref));
      const waitReq = page.waitForRequest((r) => /\/api\/items\?/.test(r.url()) && /waiting=1/.test(r.url()));
      await page.goto(base + '/#/housekeeping');
      await page.waitForSelector('.tr-row');
      await page.selectOption('[data-filter="status"]', 'waiting');
      await waitReq;
      await page.waitForSelector('.tr-row');
      assert(await page.locator('.tr-row:not(.is-waiting)').count() === 0, 'Waiting for you lists only waiting rows');
    });

    await step(AREA + ': an outside call whose outcome is unknown (DESIGN §40): the push after a merge and her receipt each open an item worded "Outcome unknown: check before retrying", amber, and it stays open until a person checks it', async () => {
      await openItem(page, base, 'system:S-10');
      const text = await page.textContent('#drawer');
      assert(/Outcome unknown: check before retrying \(push of main to origin, job my-app\/001-add-dark-mode-toggle\)/.test(text), 'the title: ' + text.slice(0, 300));
      assert(await page.locator('#drawer .tr-unknown.tag-warn').count() === 1 && await page.locator('#drawer .callout-warn').filter({ hasText: 'Outcome unknown: it may or may not have happened.' }).count() === 1, 'the amber chip and the banner');
      assert(/Look at the remote \(git ls-remote, or its web page\) before anyone pushes again\./.test(text) && /It stays open until a person checks it\./.test(text), 'what to do: ' + text.slice(0, 600));
      assert(/the push after job my-app\/001-add-dark-mode-toggle/.test(text), 'its source in words');
      await page.waitForTimeout(300);   // the drawer's slide-in, so the shot shows it whole
      await t.areaShot(page, AREA, 'tracker-outcome-unknown-desktop');
      await page.keyboard.press('Escape');
    });

    await step(AREA + ': System tab: drift sources with their status words, never “no drift” unless checked; Check now shows opened, reopened and closed refs; the role manifest is read-only', async () => {
      await page.goto(base + '/#/housekeeping?scope=system');
      await page.waitForSelector('.tr-drift[data-source="checker"]');
      const cards = await page.$$eval('.tr-drift', (els) => els.map((e) => ({ s: e.dataset.source, pill: e.querySelector('.pill').textContent, v: e.querySelector('.tr-drift-verdict').textContent })));
      const by = Object.fromEntries(cards.map((c) => [c.s, c]));
      assert(by.checker.pill === 'Checked' && by.inventory.pill === 'Cannot read' && by['gpu-watch'].pill === 'Check in progress' && by['backup-checker'].pill === 'Not checked yet', 'status words: ' + JSON.stringify(cards));
      assert(cards.filter((c) => c.s !== 'checker').every((c) => /^Drift unknown/.test(c.v)), 'no other status says anything about drift');
      assert((await page.textContent('.tr-drift[data-source="inventory"]')).includes("unknown schemaVersion '3'"), 'cannot read shows its detail');
      await t.areaShot(page, AREA, 'system-drift-desktop');
      const r = await t.waitPost(page, /\/api\/drift-sources\/checker\/check$/, () => page.click('.tr-drift[data-source="checker"] [data-action="drift-check"]'));
      assert(r.status() === 200, 'check ' + r.status());
      await page.waitForSelector('.tr-drift[data-source="checker"] .tr-res[data-res="reopened"] a');
      const line = await page.textContent('.tr-drift[data-source="checker"] .tr-drift-result');
      assert(/opened 1 \(system:S-\d+\)/.test(line) && /reopened 1 \(system:S-2\)/.test(line) && /closed 1 \(system:S-1\)/.test(line), 'the result as refs: ' + line);
      assert(/role manifest is newer than this observation: kept/.test(line), 'profiles unchanged reads as kept');
      await page.click('.tr-drift[data-source="checker"] [data-action="drift-manifest"]');
      await page.waitForSelector('dialog[open] .tr-table tbody tr');
      assert(/never applied/.test(await page.textContent('dialog[open] .modal-desc')), 'the manifest is labelled generated, never applied');
      assert(await page.locator('dialog[open] .tr-table tbody tr').count() === 2 && await page.locator('dialog[open] b').count() === 0, 'its roles as a table of text');
      await page.keyboard.press('Escape');
      await p.page.goto(base + '/#/housekeeping?scope=system');
      await p.page.waitForSelector('.tr-drift');
      await noOverflow(p.page, AREA + ' system phone');
      await t.areaShot(p.page, AREA, 'system-drift-phone');
    });

    await step(AREA + ': an item’s panel: evidence (a claimed command is “claimed, not re-run”, commits ✓/✗ from the check), history oldest first, only a person’s buttons; Esc goes back to the row', async () => {
      await page.goto(base + '/#/housekeeping');
      await page.waitForSelector('.tr-row[data-ref="' + J002 + '/I-1"]');
      await page.focus('.tr-row[data-ref="' + J002 + '/I-1"]');
      await page.keyboard.press('Enter');
      await page.waitForSelector('#drawer.is-panel:not([hidden]) .tr-history');
      assert(await page.evaluate(() => location.hash) === '#/housekeeping/' + encodeURIComponent('job:my-app/002-rate-limit-signup-form/I-1'), 'the panel has its own route');
      const ev = await page.$$eval('#drawer .tr-ev', (els) => els.map((e) => [e.dataset.kind, e.textContent]));
      assert(ev.some(([k, x]) => k === 'commit' && x.includes('✓ checked')), 'a checked commit: ' + JSON.stringify(ev));
      assert(ev.some(([k, x]) => k === 'command' && x.includes('claimed, not re-run') && x.includes('pytest -q tests/test_signup.py')), 'a claimed command');
      const hist = await page.$$eval('#drawer .tr-history > li .tr-h-move', (els) => els.map((e) => e.textContent));
      assert(hist[0] === 'added as To do' && /To do → Waiting for a check/.test(hist[hist.length - 1]), 'history oldest first: ' + hist);
      assert(canon(await actions(page, '#drawer')) === canon(['verify', 'notmet', 'drop', 'unblock', 'assign']), 'a done must-do item: ' + await actions(page, '#drawer'));
      await t.areaShot(page, AREA, 'item-panel-desktop');
      await page.keyboard.press('Escape');
      await page.waitForFunction(() => document.getElementById('drawer').hidden && location.hash === '#/housekeeping');
      await page.waitForFunction((ref) => document.activeElement && document.activeElement.dataset.ref === ref, J002 + '/I-1', { timeout: 3000 });
      await openItem(page, base, J002 + '/I-6');
      assert((await page.textContent('#drawer .tr-ev[data-kind="commit"]')).includes('✗ check failed'), 'a failed commit check');
      await openItem(page, base, DECISION);
      assert(/A person must answer this/.test(await page.textContent('#drawer')), 'a decision says a person must answer it');
      assert(canon(await actions(page, '#drawer')) === canon(['verify', 'drop']), 'a decision: yes or no only: ' + await actions(page, '#drawer'));
      assert(await page.locator('#drawer img').count() === 0 && (await page.textContent('#drawer-title')).includes('<img src=x'), 'its (product) line is text');
      await openItem(page, base, 'job:my-app/001-add-dark-mode-toggle/I-1');
      assert((await actions(page, '#drawer')).length === 0, 'a proven item of an ended job offers nothing (no reopen there)');
      await openItem(page, base, 'job:my-app/002-rate-limit-signup-form/I-8');
      const later = await page.textContent('#drawer .tr-later');
      assert(/Later: moved to repo:my-app\/F-1/.test(later) && await page.getAttribute('#drawer .tr-later a', 'href') === '#/housekeeping/' + encodeURIComponent('repo:my-app/F-1'), 'Later names where it went: ' + later);
      await openItem(p.page, base, J002 + '/I-1');
      await noOverflow(p.page, AREA + ' panel phone');
      await t.areaShot(p.page, AREA, 'item-panel-phone');
    });

    await step(AREA + ': receipt items: “From receipt …, reported by the operator”, Not verified above Verified, “reported” never ✓, “She reports this checked: confirm or reopen”, May not be hers', async () => {
      await page.goto(base + '/#/housekeeping?scope=system');
      await page.waitForSelector('.tr-row[data-ref="system:S-5"]');
      await page.click('.tr-row[data-ref="system:S-5"]');
      await page.waitForSelector('#drawer.is-panel:not([hidden]) .tr-history');
      assert(await page.locator('.tr-scope.is-on[data-scope="system"]').count() === 1, 'the list behind the panel stays the one it was opened from');
      const cols = await page.$$eval('#drawer .tr-receipt-col h4', (els) => els.map((e) => e.textContent));
      assert(canon(cols) === canon(['Not verified (1)', 'Verified (1)']), 'not verified first: ' + cols);
      assert(/From receipt r-20261022-002, reported by the operator/.test(await page.textContent('#drawer .tr-receipt-head')), 'where it came from');
      assert(/She reports this checked:/.test(await page.textContent('#drawer .tr-receipt-box')), 'a done receipt item asks to confirm or reopen');
      assert(await page.locator('#drawer .tr-receipt-box .tag-ok').count() === 0, 'never a check mark');
      assert(canon(await actions(page, '#drawer')) === canon(['verify', 'notmet', 'drop', 'assign']), 'confirm, reopen, drop: ' + await actions(page, '#drawer'));
      assert(/Confirm her check/.test(await page.textContent('#drawer [data-action="verify"]')), 'confirm her check');
      await t.areaShot(page, AREA, 'receipt-item-desktop');
      await page.keyboard.press('Escape');
      await page.waitForFunction(() => location.hash === '#/housekeeping?scope=system' && document.getElementById('drawer').hidden);
      await openItem(page, base, 'system:S-4');
      assert(/Nothing verified/.test(await page.textContent('#drawer .tr-receipt-box')), 'an empty Verified says Nothing verified');
      assert(/May not be hers/.test(await page.textContent('#drawer')), 'untrusted: may not be hers');
      assert(await page.locator('#drawer b').count() === 0 && (await page.textContent('#drawer')).includes('<b>now</b>'), 'receipt text stays text');
      await openItem(p.page, base, 'system:S-5');
      await noOverflow(p.page, AREA + ' receipt phone');
      await t.areaShot(p.page, AREA, 'receipt-item-phone');
    });

    await step(AREA + ': a person’s actions post the documented bodies; a reason is asked for; the server’s refusal is shown verbatim, as text', async () => {
      await openItem(page, base, 'repo:my-app/F-3');
      await page.click('#drawer [data-action="dismiss"]');
      await page.waitForSelector('dialog[open] textarea');
      await page.fill('dialog[open] textarea', 'covered by the load-test job');
      const r1 = await t.waitPost(page, /\/api\/items\/dismiss$/, () => modalOk(page));
      assert(r1.status() === 200, 'dismiss ' + r1.status());
      await page.waitForSelector('#drawer .tr-pill:has-text("Dropped")');
      let body = (await posted(page, base, /\/api\/items\/dismiss$/)).pop();
      assert(canon(body) === canon({ ref: 'repo:my-app/F-3', reason: 'covered by the load-test job' }), 'dismiss body: ' + JSON.stringify(body));
      await openItem(page, base, 'repo:my-app/F-4');
      await page.click('#drawer [data-action="verify"]');
      await page.waitForSelector('dialog[open] textarea');
      await page.fill('dialog[open] textarea', 'rotated it in the vault');
      await t.waitPost(page, /\/api\/items\/verify$/, () => modalOk(page));
      await page.waitForSelector('#drawer .tr-pill:has-text("Proven")');
      body = (await posted(page, base, /\/api\/items\/verify$/)).pop();
      assert(canon(body) === canon({ ref: 'repo:my-app/F-4', note: 'rotated it in the vault' }), 'verify body: ' + JSON.stringify(body));
      assert((await page.textContent('#drawer .tr-ev[data-kind="human"]')).includes('rotated it in the vault'), 'a person’s check becomes evidence');
      await openItem(page, base, 'job:my-app/003-fix-the-flaky-checkout-test/I-1');
      await page.click('#drawer [data-action="defer"]');
      await page.waitForSelector('dialog[open] textarea');
      await modalOk(page);
      await page.waitForSelector('dialog[open] .form-error:not([hidden])');
      assert(/Write a reason/.test(await page.textContent('dialog[open] .form-error')), 'Later needs a reason');
      await page.fill('dialog[open] textarea', 'the build is blocked on CI');
      await t.waitPost(page, /\/api\/items\/defer$/, () => modalOk(page));
      await page.waitForSelector('#drawer .tr-later a');
      body = (await posted(page, base, /\/api\/items\/defer$/)).pop();
      assert(canon(body) === canon({ ref: 'job:my-app/003-fix-the-flaky-checkout-test/I-1', reason: 'the build is blocked on CI' }), 'defer body (no date: the server’s follow-up default): ' + JSON.stringify(body));
      assert(/Later: moved to repo:my-app\/F-\d+/.test(await page.textContent('#drawer .tr-later')), 'a job item moved to the follow-ups');
      // a refusal: the server's words, verbatim and as text
      await page.route('**/api/items/drop', (route) => route.fulfill({ status: 400, contentType: 'application/json', body: JSON.stringify({ error: 'only a person can drop <b>I-6</b>: refused by the rules' }) }));
      t.allowHttp(true);
      await openItem(page, base, J002 + '/I-6');
      await page.click('#drawer [data-action="drop"]');
      await page.fill('dialog[open] textarea', 'not needed');
      await modalOk(page);
      await page.waitForSelector('dialog[open] .form-error:not([hidden])');
      const err = await page.textContent('dialog[open] .form-error');
      assert(err.includes('only a person can drop <b>I-6</b>: refused by the rules') && await page.locator('dialog[open] .form-error b').count() === 0, 'the refusal verbatim: ' + err);
      await page.keyboard.press('Escape');
      await page.keyboard.press('Escape');
      t.allowHttp(false);
      await page.unroute('**/api/items/drop');
      // the overflow item is a group with Dismiss all (a bulk dismiss)
      await openItem(page, base, 'repo:my-app/F-7');
      assert(await page.locator('#drawer .tr-folded li').count() === 3, 'the folded sentences');
      assert(!(await actions(page, '#drawer')).includes('dismiss'), 'the group is dismissed with Dismiss all only');
      await page.click('#drawer [data-action="dismiss-all"]');
      await page.waitForSelector('dialog[open] input');
      await t.waitPost(page, /\/api\/items\/dismiss$/, () => modalOk(page));
      body = (await posted(page, base, /\/api\/items\/dismiss$/)).pop();
      assert(canon(body) === canon({ refs: ['repo:my-app/F-7'], reason: 'not needed' }), 'bulk dismiss body: ' + JSON.stringify(body));
      // make-job: a follow-up becomes a job; a follow-up a live job works on cannot
      await openItem(page, base, 'repo:my-app/F-5');
      assert(await page.locator('#drawer [data-action="make-job"][disabled]').count() === 1, 'make-job is disabled while job 003 works on it');
      await openItem(page, base, 'repo:my-app/F-1');
      const mj = await t.waitPost(page, /\/api\/items\/make-job$/, () => page.click('#drawer [data-action="make-job"]'));
      const made = await mj.json();
      assert(mj.status() === 200 && made.job && made.item.status === 'doing' && made.item.links.job === made.job.id, 'make-job: ' + JSON.stringify(made).slice(0, 200));
    });

    await step(AREA + ': the job drawer’s Items tab: product decisions to confirm (never pre-checked, the (product) line as text), confirm and reopen, groups, notices, unparsed lines, + Item', async () => {
      await page.goto(base + '/#/job/my-app/002-rate-limit-signup-form/items');
      await page.waitForSelector('#panel-items .tr-decisions');
      assert(await page.getAttribute('#tab-items', 'aria-selected') === 'true', 'the Items tab is open from its hash');
      const lines = await page.$$eval('#panel-items .tr-decision', (els) => els.map((e) => ({ done: e.classList.contains('is-done'), text: e.querySelector('.tr-line').textContent })));
      assert(lines.length === 3 && lines.filter((l) => l.done).length === 1, 'three marked lines, one confirmed: ' + JSON.stringify(lines));
      assert(await page.locator('#panel-items .tr-decision input:checked').count() === 0, 'never pre-checked');
      assert(lines[0].text === "Should a blocked signup say when to retry? <img src=x onerror=alert('item')> (product)" && await page.locator('#panel-items img').count() === 0, 'the (product) line shows escaped');
      assert(await page.locator('[data-action="confirm-selected"][disabled]').count() === 1, 'Confirm selected waits for a tick');
      assert(/Confirm all \(2\)/.test(await page.textContent('[data-action="confirm-all"]')), 'Confirm all (N)');
      const groups = await page.$$eval('#panel-items .tr-group .tr-h4', (els) => els.map((e) => e.textContent));
      assert(/^Must-do, not proven \(5\)/.test(groups[0]) && groups.some((g) => /^Suggestions \(1\)/.test(g)), 'groups: ' + groups);
      assert((await page.textContent('#panel-items .tr-notices')).includes('evidence check failed'), 'notices for the next prompt');
      assert((await page.textContent('#panel-items .tr-unparsed')).includes("<script>alert('unparsed')</script>") && await page.locator('#panel-items script').count() === 0, 'unparsed lines as text');
      await t.areaShot(page, AREA, 'job-items-desktop');
      await page.check('#panel-items .tr-decision input[data-line="0"]');
      const c = await t.waitPost(page, /\/confirm$/, () => page.click('[data-action="confirm-selected"]'));
      assert(c.status() === 200, 'confirm ' + c.status());
      const cb = (await posted(page, base, /\/confirm$/)).pop();
      assert(canon(cb) === canon({ lines: [lines[0].text] }), 'confirm body: ' + JSON.stringify(cb));
      await page.waitForFunction(() => document.querySelectorAll('#panel-items .tr-decision.is-done').length === 2);
      await page.waitForFunction(() => /^Must-do, not proven \(4\)/.test((document.querySelector('#panel-items .tr-group .tr-h4') || {}).textContent || ''));
      await page.click('#panel-items .tr-decision[data-line="0"] [data-action="reopen-decision"]');
      await page.fill('dialog[open] textarea', 'the copy needs another look');
      await t.waitPost(page, /\/api\/items\/reopen$/, () => modalOk(page));
      const rb = (await posted(page, base, /\/api\/items\/reopen$/)).pop();
      assert(canon(rb) === canon({ ref: DECISION, reason: 'the copy needs another look' }), 'reopen body: ' + JSON.stringify(rb));
      await page.waitForFunction(() => document.querySelectorAll('#panel-items .tr-decision.is-done').length === 1);
      await page.click('#panel-items [data-ref="' + J002 + '/I-6"]');
      await page.waitForSelector('#panel-items .tr-inline .tr-ev[data-kind="commit"]');
      await page.click('#panel-items .tr-inline [data-action="item-history"]');
      await page.waitForSelector('#panel-items .tr-inline .tr-history');
      await page.click('#panel-items [data-action="add-job-item"]');
      await page.fill('dialog[open] input[type="text"]', 'Log every blocked attempt');
      await page.check('dialog[open] input[type="checkbox"]');
      const a = await t.waitPost(page, /\/api\/jobs\/my-app\/002-rate-limit-signup-form\/items$/, () => modalOk(page));
      assert(a.status() === 201, 'add ' + a.status());
      assert(canon((await posted(page, base, /\/items$/)).pop()) === canon({ title: 'Log every blocked attempt', blocking: true }), 'the + Item body');
      await page.waitForFunction(() => /^Must-do, not proven \(6\)/.test((document.querySelector('#panel-items .tr-group .tr-h4') || {}).textContent || ''));
      await p.page.goto(base + '/#/job/my-app/002-rate-limit-signup-form/items');
      await p.page.waitForSelector('#panel-items .tr-decisions');
      await noOverflow(p.page, AREA + ' job items phone');
      await t.areaShot(p.page, AREA, 'job-items-phone');
    });

    await step(AREA + ': a product decision answered No: the line reads “Answered no”, leaves the counts and Confirm all (which then names the other lines), and Reopen brings the question back', async () => {
      const J2 = '/api/jobs/my-app/002-rate-limit-signup-form';
      await page.goto(base + '/#/job/my-app/002-rate-limit-signup-form/items');
      await page.waitForSelector('#panel-items .tr-decisions [data-action="confirm-all"]');
      const asm = await (await page.request.get(base + J2 + '/assumptions')).json();
      assert(asm.unconfirmed.length === 2, 'two lines wait: ' + JSON.stringify(asm.unconfirmed));
      await page.click('#panel-items [data-ref="' + DECISION + '"]');
      await page.click('#panel-items [data-focus="j:' + DECISION + ':drop"]');
      await page.fill('dialog[open] textarea', 'No: the plain message is enough');
      await t.waitPost(page, /\/api\/items\/drop$/, () => modalOk(page));
      await page.waitForSelector('#panel-items .tr-decision.is-refused');
      const refused = await page.textContent('#panel-items .tr-decision.is-refused');
      assert(/Answered no: No: the plain message is enough/.test(refused) && await page.locator('#panel-items .tr-decision.is-refused input').count() === 0, 'answered no, with no box: ' + refused);
      assert(/Confirm all \(1\)/.test(await page.textContent('[data-action="confirm-all"]')), 'Confirm all leaves the No out');
      await page.waitForFunction(() => /1 product decision to confirm/.test((document.querySelector('#drawer-head .tr-head-block') || {}).textContent || ''));
      const onRow = await page.waitForFunction((r) => (document.activeElement.dataset.focus || '').indexOf('j:' + r + ':') === 0, DECISION, { timeout: 5000 }).then(() => true).catch(() => false);
      assert(onRow, 'keyboard focus stays on the item’s row (its Reopen): ' + await page.evaluate(() => document.activeElement.dataset.focus || document.activeElement.tagName));
      await t.areaShot(page, AREA, 'decision-answered-no-desktop');
      await p.page.goto(base + '/#/job/my-app/002-rate-limit-signup-form/items');
      await p.page.waitForSelector('#panel-items .tr-decision.is-refused');
      await p.page.locator('#panel-items .tr-decisions').scrollIntoViewIfNeeded();
      await noOverflow(p.page, AREA + ' decision answered no phone');
      await t.areaShot(p.page, AREA, 'decision-answered-no-phone');
      const c = await t.waitPost(page, /002-rate-limit-signup-form\/confirm$/, () => page.click('[data-action="confirm-all"]'));
      assert(c.status() === 200, 'confirm ' + c.status());
      const cb = (await posted(page, base, /002-rate-limit-signup-form\/confirm$/)).pop();
      const other = asm.unconfirmed.filter((l) => !/Should a blocked signup/.test(l));
      assert(canon(cb) === canon({ lines: other }), 'Confirm all names the other lines, not {all: true}: ' + JSON.stringify(cb));
      const after = await (await page.request.get(base + J2 + '/assumptions')).json();
      assert(after.unconfirmed.length === 1 && /Should a blocked signup/.test(after.unconfirmed[0]), 'the No is not confirmed: ' + JSON.stringify(after));
      await page.waitForFunction(() => !document.querySelector('#drawer-head .tr-head-block'));
      await page.click('#panel-items .tr-decision.is-refused [data-action="reopen-decision"]');
      await page.fill('dialog[open] textarea', 'ask again');
      await t.waitPost(page, /\/api\/items\/reopen$/, () => modalOk(page));
      await page.waitForFunction(() => !document.querySelector('#panel-items .tr-decision.is-refused') && document.querySelectorAll('#panel-items .tr-decision input[type="checkbox"]').length === 1);
      await page.waitForSelector('#drawer-head .tr-head-block');
    });

    await step(AREA + ': job cards carry items chips (open, must-do, suggested, decisions to answer), the drawer head says a person must answer, and the Live tab shows each factory-line run’s Grid Card receipt as delivered', async () => {
      await page.goto(base + '/#/factory');
      await page.waitForSelector('.card[data-job="my-app/002-rate-limit-signup-form"] .tr-card-items');
      const chip = await page.$eval('.card[data-job="my-app/002-rate-limit-signup-form"] .tr-card-items', (e) => [e.textContent, e.className]);
      assert(/\d+ open · \d+ must-do/.test(chip[0]) && /tag-err/.test(chip[1]), 'a stopped job with must-do items reads red: ' + chip);
      assert(/1 suggested/.test(await page.textContent('.card[data-job="my-app/002-rate-limit-signup-form"] .tr-card-sugg')), 'suggested');
      await page.waitForSelector('.card[data-job="my-app/004-render-user-notes"] .tr-card-confirm');
      assert(/1 to answer/.test(await page.textContent('.card[data-job="my-app/004-render-user-notes"] .tr-card-confirm')), 'a card badge while a product decision waits');
      await page.goto(base + '/#/job/my-app/004-render-user-notes');
      await page.waitForSelector('#drawer-head .tr-head-block');
      assert(/A person must answer this:/.test(await page.textContent('#drawer-head .tr-head-block')), 'the drawer head');
      await page.goto(base + '/#/job/my-app/002-rate-limit-signup-form');
      await page.waitForFunction(shownGrid);
      const runs = await page.$$eval('#panel-live .run', (els) => els.map((e) => ({ run: e.dataset.run, grid: e.querySelector('.tr-grid') ? e.querySelector('.tr-grid').textContent : null, title: e.querySelector('.tr-grid') ? e.querySelector('.tr-grid').title : '' })));
      assert(runs.length && runs.every((r) => r.grid && /card delivered/.test(r.grid) && /[0-9a-f]{12}/.test(r.grid) && /\d bytes/.test(r.grid)), 'every factory-line run shows its receipt: ' + JSON.stringify(runs.slice(0, 2)));
      assert(runs.every((r) => /Delivered is not proof/.test(r.title) && !/\bread\b(?! or)/.test(r.grid)), 'labelled delivered, never read');
      await t.areaShot(page, AREA, 'run-receipts-desktop');
      await p.page.goto(base + '/#/job/my-app/002-rate-limit-signup-form/live');   // the same job: name the tab
      await p.page.waitForFunction(shownGrid);
      const phoneChip = await p.page.$eval('#panel-live .run .tr-grid', (e) => e.innerText);
      assert(/delivered/.test(phoneChip) && /[0-9a-f]{7}/.test(phoneChip), 'on a phone the receipt still says delivered: ' + phoneChip);
      const cut = await p.page.$$eval('#panel-live .run-head', (els) => els.filter((e) => e.scrollWidth > e.clientWidth + 1).length);
      assert(cut === 0, 'no run row is cut off on a phone: ' + cut);
      await noOverflow(p.page, AREA + ' live phone');
      await t.areaShot(p.page, AREA, 'run-receipts-phone');
    });

    await step(AREA + ': Procedures: each process map offers Copy Mermaid (GET /api/docs/maps’ source) and Download .mmd (the same text)', async () => {
      await page.goto(base + '/#/procedures/factory-line');
      await page.waitForSelector('figure[data-map="factory-line"] .tr-mermaid [data-action="copy-mermaid"]');
      const maps = await (await page.request.get(base + '/api/docs/maps')).json();
      const line = maps.find((x) => x.id === 'factory-line');
      const href = await page.getAttribute('figure[data-map="factory-line"] [data-action="download-mmd"]', 'href');
      assert(href === '/api/docs/maps/factory-line.mmd' && await page.getAttribute('figure[data-map="factory-line"] [data-action="download-mmd"]', 'download') === 'factory-line.mmd', 'download link: ' + href);
      const file = await page.request.get(base + href);
      assert(file.status() === 200 && (await file.text()) === line.mermaid && /attachment; filename="factory-line.mmd"/.test(file.headers()['content-disposition'] || ''), 'the .mmd is the map’s source');
      await page.click('figure[data-map="factory-line"] [data-action="copy-mermaid"]');
      await page.waitForSelector('.toast-ok');
      assert(await page.evaluate(() => navigator.clipboard.readText()) === line.mermaid, 'Copy puts the Mermaid source on the clipboard');
      assert(await page.locator('figure[data-map="plan-flow"] .tr-mermaid [data-action="download-mmd"]').count() === 1, 'the plan flow too');
      await page.locator('figure[data-map="factory-line"]').scrollIntoViewIfNeeded();
      await t.areaShot(page, AREA, 'procedures-mermaid-desktop');
      await p.page.goto(base + '/#/procedures/factory-line');
      await p.page.waitForSelector('figure[data-map="factory-line"] .tr-mermaid .btn');
      await p.page.locator('figure[data-map="factory-line"]').scrollIntoViewIfNeeded();
      await noOverflow(p.page, AREA + ' procedures phone');
      await t.areaShot(p.page, AREA, 'procedures-mermaid-phone');
    });

    await step(AREA + ': live: items.updated refreshes the list (a newly folded suggestion), items.carried says where items went; approve with must-do items moves them on only after the box is ticked', async () => {
      await page.goto(base + '/#/housekeeping?scope=followups&status=all');
      await page.waitForSelector('.tr-row[data-ref="repo:my-app/F-7"]');
      const folded = await (await page.request.post(base + '/__mock/tracker/fold', { data: { text: 'Think about <b>CSV</b> quoting' } })).json();
      await page.waitForSelector('.tr-row[data-ref="' + folded.item.ref + '"]', { timeout: 8000 });   // the group F-7 was dismissed: a new one
      assert(/1 more suggestion \(this scope already holds 300/.test(await page.textContent('.tr-row[data-ref="' + folded.item.ref + '"]')), 'a new overflow group');
      await page.request.post(base + '/__mock/publish', { data: { type: 'items.carried', data: { ref: J002, count: 2, to: 'follow-ups', reason: 'merged' }, repo: 'my-app', job: '002-rate-limit-signup-form' } });
      await page.waitForSelector('.toast:has-text("2 open items moved to follow-ups.")');
      await page.goto(base + '/#/job/my-app/002-rate-limit-signup-form/items');
      await page.waitForSelector('#panel-items .tr-carry');
      assert(/Approve, at the top, lists them/.test(await page.textContent('#panel-items .tr-carry')), 'the Items tab says how to approve with them open');
      await page.click('#drawer-head [data-action="approve"]');
      await page.waitForSelector('dialog[open] .tr-carry-list li');
      const n = await page.locator('dialog[open] .tr-carry-list li').count();
      await modalOk(page);
      await page.waitForSelector('dialog[open] .form-error:not([hidden])');
      assert(/^Tick “Move these/.test(await page.textContent('dialog[open] .form-error')), 'the box is required');
      await page.check('dialog[open] input[data-carry-ok]');
      const ap = await t.waitPost(page, /\/approve$/, () => modalOk(page));
      assert(ap.status() === 200, 'approve ' + ap.status());
      const body = (await posted(page, base, /\/approve$/)).pop();
      assert(Array.isArray(body.carry_items) && body.carry_items.length === n && body.carry_items.includes('I-3'), 'carry_items names every must-do id: ' + JSON.stringify(body));
      await page.waitForSelector('.toast:has-text("moved to follow-ups")');
    });

    await step(AREA + ': the Approve dialog when its items cannot be read: it says so, sends nothing, and reads them again', async () => {
      await page.goto(base + '/#/job/my-app/004-render-user-notes');
      await page.waitForSelector('#drawer-head [data-action="approve"]');
      let fail = true;
      t.allowHttp(true);
      await page.route('**/api/jobs/my-app/004-render-user-notes/items', (route) => {
        if (fail && route.request().method() === 'GET') { fail = false; return route.fulfill({ status: 503, contentType: 'application/json', body: '{"error":"temporarily unavailable"}' }); }
        return route.continue();
      });
      const before = (await posted(page, base, /004-render-user-notes\/approve$/)).length;
      await page.click('#drawer-head [data-action="approve"]');
      await page.waitForFunction(() => /could not be read \(temporarily unavailable\)/.test((document.querySelector('dialog[open] .tr-carry-note') || {}).textContent || ''));
      assert(await page.locator('dialog[open] [data-action="carry-reload"]').count() === 1, 'Read them again');
      await modalOk(page);
      await page.waitForSelector('dialog[open] .tr-carry-check:not([hidden])');
      assert(/could not be read, so nothing was sent/.test(await page.textContent('dialog[open] .form-error')), 'the dialog says nothing was sent');
      assert((await posted(page, base, /004-render-user-notes\/approve$/)).length === before, 'no approve was posted');
      await page.unroute('**/api/jobs/my-app/004-render-user-notes/items');
      t.allowHttp(false);
      await page.click('dialog[open] button[data-action="cancel"]');
      await page.waitForSelector('dialog[open]', { state: 'detached' });
    });

    await step(AREA + ': the core Approve dialog lists the must-do items that are not proven (an unanswered product decision said apart) and sends them as carry_items only once the box is ticked; a 409 naming must-do items rebuilds the list and asks again in plain words', async () => {
      const J4 = '/api/jobs/my-app/004-render-user-notes';
      const items = (await (await page.request.get(base + J4 + '/items')).json()).items.filter((r) => r.blocking && !['verified', 'dropped', 'deferred'].includes(r.status));
      await page.goto(base + '/#/job/my-app/004-render-user-notes');
      await page.click('#drawer-head [data-action="approve"]');
      await page.waitForSelector('dialog[open] .tr-carry-check:not([hidden])');
      const listed = await page.$$eval('dialog[open] .tr-carry-row code', (els) => els.map((e) => e.textContent));
      assert(canon(listed) === canon(items.map((r) => r.id)), 'the dialog lists every must-do item not proven: ' + listed);
      assert(await page.locator('dialog[open] input[data-carry-ok]:checked').count() === 0, 'the box starts unticked');
      assert(await page.locator('dialog[open] .tr-carry-field img').count() === 0 && (await page.textContent('dialog[open] .tr-carry-field')).includes("<img src=x onerror=alert('decision')>"), 'item titles stay text');
      const dec = await page.textContent('dialog[open] .tr-carry-dec');
      assert(/1 product decision has no answer:/.test(dec) && /ordinary follow-up/.test(dec) && await page.locator('dialog[open] [data-action="answer-first"]').count() === 1, 'the unanswered decision is said apart: ' + dec);
      assert(await page.locator('dialog[open] .tr-carry-row.is-decision').count() === 1, 'and marked in the list');
      const before = (await posted(page, base, /004-render-user-notes\/approve$/)).length;
      await modalOk(page);
      await page.waitForSelector('dialog[open] .form-error:not([hidden])');
      assert(/Tick “Move these 2 must-do items to follow-ups”/.test(await page.textContent('dialog[open] .form-error')), 'the box is required: ' + await page.textContent('dialog[open] .form-error'));
      assert((await posted(page, base, /004-render-user-notes\/approve$/)).length === before, 'nothing was posted');
      await page.waitForTimeout(250);   // the dialog's entrance
      await t.areaShot(page, AREA, 'approve-dialog-desktop');
      await p.page.goto(base + '/#/job/my-app/004-render-user-notes');
      await p.page.click('#drawer-head [data-action="approve"]');
      await p.page.waitForSelector('dialog[open] .tr-carry-check:not([hidden])');
      await p.page.waitForTimeout(250);   // the dialog's entrance
      await noOverflow(p.page, AREA + ' approve dialog phone');
      await t.areaShot(p.page, AREA, 'approve-dialog-phone');
      await p.page.keyboard.press('Escape');
      // the server's 409 (a must-do item was added meanwhile): the list is rebuilt from open_blocking, the box asked
      // again, in plain words (no API field, no CLI flag), and no developer-worded toast
      const named = items.map((r) => r.id).concat(['I-9']);
      t.allowHttp(true);
      await page.route('**' + J4 + '/approve', (route) => route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ error: '3 must-do items are not proven: ' + named.join(', ') + '. Approving moves them to follow-ups unproven: name every one in carry_items (factory approve --carry-open-items), or prove them first', open_blocking: named }) }));
      await page.check('dialog[open] input[data-carry-ok]');
      await modalOk(page);
      await page.waitForFunction(() => /The server found 3 must-do items not proven/.test((document.querySelector('dialog[open] .form-error') || {}).textContent || ''));
      const msg = await page.textContent('dialog[open] .form-error');
      assert(!/carry_items|--carry/.test(msg) && /tick “Move these 3 must-do items to follow-ups”/.test(msg), 'plain words: ' + msg);
      await page.waitForFunction(() => document.querySelectorAll('dialog[open] .tr-carry-row').length === 3);
      assert(/named by the server/.test(await page.textContent('dialog[open] .tr-carry-row[data-id="I-9"]')), 'an id only the 409 named is listed too');
      assert(await page.locator('dialog[open] input[data-carry-ok]:checked').count() === 0, 'the box is asked again');
      assert(await page.locator('.toast-error:has-text("carry_items")').count() === 0, 'no developer-worded toast');
      await t.areaShot(page, AREA, 'approve-409-desktop');
      await page.unroute('**' + J4 + '/approve');
      t.allowHttp(false);
      await page.check('dialog[open] input[data-carry-ok]');
      const ap = await t.waitPost(page, /004-render-user-notes\/approve$/, () => modalOk(page));
      assert(ap.status() === 200, 'approve ' + ap.status());
      const body = (await posted(page, base, /004-render-user-notes\/approve$/)).pop();
      assert(canon(body) === canon({ carry_items: named }), 'carry_items: ' + JSON.stringify(body));
      await page.waitForSelector('.toast:has-text("2 open items moved to follow-ups.")');
      const f = (await (await page.request.get(base + '/api/repos/my-app/followups')).json()).items;
      assert(items.every((r) => f.some((x) => (x.source || {}).ref === r.ref && /human override while open/.test((x.source || {}).reason || ''))), 'they are follow-ups now');
    });

    await step(AREA + ': outside agents: a token is made with a valid name, shown once with its MCP entry and AGENTS.md line, listed without its secret, and revoked after a confirm', async () => {
      await page.goto(base + '/#/housekeeping');
      await page.waitForSelector('#tracker-root [data-action="outside-agents"]');
      await page.click('#tracker-root [data-action="outside-agents"]');
      await page.waitForSelector('#drawer.is-panel:not([hidden]) .tr-tokens');
      assert(await page.evaluate(() => location.hash) === '#/housekeeping/agents', 'the panel has its own route');
      assert(/only its own items/.test(await page.textContent('#drawer')), 'it says what a token reads');
      await page.fill('#drawer [data-focus="token-name"]', 'Bad Name');
      await page.click('#drawer [data-action="make-token"]');
      await page.waitForSelector('#drawer .tr-make .form-error:not([hidden])');
      assert((await posted(page, base, /\/api\/agent-tokens$/)).length === 0, 'a bad name is not sent');
      await page.fill('#drawer [data-focus="token-name"]', 'claude-laptop');
      const mk = await t.waitPost(page, /\/api\/agent-tokens$/, () => page.click('#drawer [data-action="make-token"]'));
      const made = await mk.json();
      assert(mk.status() === 201 && /^sfi_/.test(made.token), 'made: ' + mk.status());
      await page.waitForSelector('#drawer .tr-made [data-block="mcp"]');
      assert((await page.textContent('#drawer [data-block="token"] pre')) === made.token, 'the token is shown');
      const mcp = JSON.parse(await page.textContent('#drawer [data-block="mcp"] pre'));
      const entry = mcp.mcpServers['sf-items'];
      assert(entry.command === made.connect.sf_items && canon(entry.args) === canon(['mcp']) && entry.env.SF_ITEMS_TOKEN === made.token && entry.env.SF_ITEMS_URL === made.connect.url, 'the MCP entry from connect: ' + JSON.stringify(mcp));
      assert(/sf-items add/.test(await page.textContent('#drawer [data-block="line"] pre')), 'the AGENTS.md line');
      assert((await page.$$eval('#drawer .tr-token', (els) => els.map((e) => e.dataset.token))).includes('claude-laptop'), 'listed');
      assert(!(await page.textContent('.toast:last-child')).includes(made.token), 'the token is never in a toast');
      await t.areaShot(page, AREA, 'outside-agents-desktop');
      await page.click('#drawer [data-action="token-done"]');
      await page.waitForFunction(() => !document.querySelector('#drawer .tr-made'));
      assert(!(await page.textContent('#drawer')).includes(made.token), 'shown once: gone after “I have copied it”');
      await page.click('#drawer .tr-token[data-token="claude-laptop"] [data-action="revoke-token"]');
      await page.waitForSelector('dialog[open]');
      await t.waitPost(page, /\/api\/agent-tokens\/claude-laptop\/revoke$/, () => modalOk(page));
      await page.waitForFunction(() => !document.querySelector('#drawer .tr-token[data-token="claude-laptop"]'));
      await page.keyboard.press('Escape');
      await page.waitForFunction(() => document.getElementById('drawer').hidden && location.hash === '#/housekeeping');
      await p.page.goto(base + '/#/housekeeping/agents');
      await p.page.waitForSelector('#drawer .tr-tokens');
      await p.page.fill('#drawer [data-focus="token-name"]', 'qwen-phone');
      await p.page.click('#drawer [data-action="make-token"]');
      await p.page.waitForSelector('#drawer .tr-made [data-block="mcp"]');
      await noOverflow(p.page, AREA + ' outside agents phone');
      await t.areaShot(p.page, AREA, 'outside-agents-phone');
    });

    await step(AREA + ': a run’s own suggestion group, the active filter, a busy drift check that acts on nothing, and a list that stays fresh while another view shows', async () => {
      await openItem(page, base, 'job:my-app/003-fix-the-flaky-checkout-test/I-3');
      assert(await page.locator('#drawer .tr-folded li').count() === 2 && /group of 2/.test(await page.textContent('#drawer .tr-chips')), 'a group of two suggestions');
      assert(await page.locator('#drawer b').count() === 0 && (await page.textContent('#drawer .tr-folded')).includes('<b>seed</b>'), 'its lines are text');
      assert(canon(await actions(page, '#drawer')) === canon(['accept', 'dismiss', 'verify', 'block', 'assign']), 'tracked or dismissed as one: ' + await actions(page, '#drawer'));
      const rows = await listOf(page, base, '#/housekeeping?status=active', { status: 'active' });
      assert(rows.length && rows.every((r) => /^(To do|Working on it)/.test(r.text)), 'to do or working on it');
      await page.goto(base + '/#/housekeeping?scope=system');
      await page.waitForSelector('.tr-drift[data-source="gpu-watch"]');
      await t.waitPost(page, /\/api\/drift-sources\/gpu-watch\/check$/, () => page.click('.tr-drift[data-source="gpu-watch"] [data-action="drift-check"]'));
      await page.waitForSelector('.toast:has-text("gpu-watch")');
      assert(await page.locator('.tr-drift[data-source="gpu-watch"] .tr-drift-result').count() === 0, 'a check that read nothing shows no result');
      assert(/^Drift unknown/.test(await page.textContent('.tr-drift[data-source="gpu-watch"] .tr-drift-verdict')), 'and says drift is unknown');
      // items.updated while Home shows: the badge follows, and the list is read again when the tracker opens
      await page.goto(base + '/#/home');
      await page.waitForSelector('#home-root');
      const badge0 = Number(await page.textContent('.rail-item[data-view="tracker"] .rail-badge'));
      const r = await (await page.request.post(base + '/api/system/items', { data: { title: 'Check the UPS battery date' } })).json();
      await page.waitForFunction((n) => Number((document.querySelector('.rail-item[data-view="tracker"] .rail-badge') || {}).textContent) === n + 1, badge0, { timeout: 8000 });
      await page.click('.rail-item[data-view="tracker"]');
      await page.waitForSelector('.tr-row[data-ref="' + r.item.ref + '"]', { timeout: 8000 });
    });

    await step(AREA + ': the item panel keeps keyboard focus: an action lands on the next action, an items.updated of another scope changes nothing; a moved Later item opens its follow-up; outside agents only for System items and follow-ups', async () => {
      const made = await (await page.request.post(base + '/__mock/tracker/fold', { data: { text: 'Keyboard check: <b>bold</b> stays text' } })).json();
      const ref = made.item.ref;   // a group of suggestions in the follow-ups
      await openItem(page, base, ref);
      await page.evaluate(() => { window.__titles = []; const el = document.getElementById('drawer-title'); new MutationObserver(() => window.__titles.push(el.textContent)).observe(el, { childList: true, characterData: true, subtree: true }); });
      await page.focus('#drawer [data-action="accept"]');
      await page.keyboard.press('Enter');
      await page.waitForSelector('#drawer .tr-pill:has-text("To do")');
      await page.waitForFunction((r) => (document.activeElement.dataset.focus || '').indexOf('p:' + r + ':') === 0, ref, { timeout: 5000 });
      const focus1 = await page.evaluate(() => document.activeElement.dataset.focus);
      assert(!(await page.evaluate(() => window.__titles)).some((x) => /Loading/.test(x)), 'never “Loading…” while it is read again');
      await page.request.post(base + '/__mock/publish', { data: { type: 'items.updated', data: { ref: 'system', summary: {}, changed: ['S-1'] } } });
      await page.waitForTimeout(1200);
      assert(await page.evaluate(() => document.activeElement.dataset.focus) === focus1, 'another scope’s items.updated leaves focus where it was');
      // owners: an outside agent only where its token can read the item
      await page.click('#drawer [data-action="assign"]');
      await page.waitForSelector('dialog[open] select');
      const fu = await page.$$eval('dialog[open] select option', (o) => o.map((x) => x.value));
      assert(fu.includes('agent:qwen-code'), 'a follow-up can go to an outside agent: ' + fu);
      await page.click('dialog[open] button[data-action="cancel"]');
      await openItem(page, base, 'job:my-app/003-fix-the-flaky-checkout-test/I-3');
      await page.click('#drawer [data-action="assign"]');
      await page.waitForSelector('dialog[open] select');
      const jo = await page.$$eval('dialog[open] select option', (o) => o.map((x) => x.value));
      assert(!jo.some((v) => /^agent:/.test(v)), 'a job’s item never offers an outside agent: ' + jo);
      assert(/move this item to the follow-ups first/.test(await page.textContent('dialog[open] .modal-body')), 'and says how');
      await page.click('dialog[open] button[data-action="cancel"]');
      await openItem(page, base, J002 + '/I-8');
      const acts = await page.$$eval('#drawer .tr-actions [data-action]', (els) => els.map((e) => [e.dataset.action, e.getAttribute('href')]));
      assert(acts.length === 1 && acts[0][0] === 'open-followup' && acts[0][1] === '#/housekeeping/' + enc('repo:my-app/F-1'), 'a moved Later item: Open the follow-up, never Reopen: ' + JSON.stringify(acts));
      await page.goto(base + '/#/housekeeping/agents');
      await page.waitForSelector('#drawer .tr-tokens');
      assert(/among the System items and the follow-ups/.test(await page.textContent('#drawer')), 'the agents page says what a token reads');
    });

    await step(AREA + ': a chat: its suggestions under the reply that made them (Track it / Not a promise), its Items chip and panel with + Item', async () => {
      const C1 = 'chat:c-1a2b3c4d5e/C-1';
      await page.goto(base + '/#/chat/c-1a2b3c4d5e');
      await page.waitForSelector('.msg-assistant .tr-turn-sugg[data-ref="' + C1 + '"]');
      assert(/1 open · 1 suggested/.test(await page.textContent('.chat-head .tr-scope-chip')), 'the chip counts the chat’s items');
      assert(await page.locator('.msg-assistant .tr-turn [data-action="track-it"]').count() === 1 && await page.locator('.msg-assistant .tr-turn [data-action="not-a-promise"]').count() === 1, 'Track it and Not a promise');
      await t.areaShot(page, AREA, 'chat-reply-desktop');
      // Not a promise: one click, the server's own reason (stubbed here, so the suggestion stays for Track it)
      t.allowHttp(true);
      let sent = null;
      await page.route('**/api/items/dismiss', (route) => { sent = route.request().postDataJSON(); route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ item: { ref: C1, status: 'dropped' } }) }); });
      await page.click('.tr-turn [data-action="not-a-promise"]');
      await page.waitForSelector('.toast:has-text("dismissed: not a promise")');
      assert(canon(sent) === canon({ ref: C1 }), 'Not a promise sends only the ref: ' + JSON.stringify(sent));
      await page.unroute('**/api/items/dismiss');
      t.allowHttp(false);
      await page.focus('.tr-turn [data-action="track-it"]');
      await t.waitPost(page, /\/api\/items\/accept$/, () => page.keyboard.press('Enter'));
      await page.waitForFunction(() => /From this reply: C-1 To do/.test((document.querySelector('.msg-assistant .tr-turn') || {}).textContent || ''));
      assert(canon((await posted(page, base, /\/api\/items\/accept$/)).pop()) === canon({ ref: C1 }), 'Track it sends the ref');
      assert(await page.evaluate(() => (document.activeElement.dataset.focus || '') === 'showi:chat:c-1a2b3c4d5e/C-1'), 'focus stays on the tracked item');
      await page.click('.chat-head .tr-scope-chip');
      await page.waitForSelector('#drawer .tr-list [data-ref="' + C1 + '"]');
      assert(await page.textContent('#drawer-title') === 'Items of this chat', 'its panel');
      await page.click('#drawer [data-action="add-chat-item"]');
      await page.fill('dialog[open] input[type="text"]', 'Write down the cookie flags');
      const a = await t.waitPost(page, /\/api\/chats\/c-1a2b3c4d5e\/items$/, () => modalOk(page));
      assert(a.status() === 201 && canon((await posted(page, base, /\/api\/chats\/c-1a2b3c4d5e\/items$/)).pop()) === canon({ title: 'Write down the cookie flags' }), '+ Item posts its title');
      await page.waitForFunction(() => /Write down the cookie flags/.test(document.getElementById('drawer').textContent));
      await page.waitForTimeout(350);   // the panel's entrance
      await t.areaShot(page, AREA, 'chat-items-desktop');
      await page.keyboard.press('Escape');
      await page.waitForFunction(() => document.getElementById('drawer').hidden && document.activeElement && document.activeElement.dataset.action === 'chat-items');
      await p.page.goto(base + '/#/chat/c-1a2b3c4d5e');
      await p.page.waitForSelector('.msg-assistant .tr-turn');
      await p.page.locator('.msg-assistant .tr-turn').scrollIntoViewIfNeeded();
      await noOverflow(p.page, AREA + ' chat phone');
      await t.areaShot(p.page, AREA, 'chat-reply-phone');
      await p.page.click('.chat-head .tr-scope-chip');
      await p.page.waitForSelector('#drawer .tr-list .tr-row');
      await noOverflow(p.page, AREA + ' chat items phone');
      await p.page.waitForTimeout(350);   // the panel's entrance
      await t.areaShot(p.page, AREA, 'chat-items-phone');
      await p.page.keyboard.press('Escape');
    });

    await step(AREA + ': a plan: the roll-up chip in its detail opens its own items and its jobs’', async () => {
      await page.goto(base + '/#/plans/my-app/P001-csv-export-for-reports');
      await page.waitForSelector('.plan-detail .tr-scope-chip:not([hidden])');
      assert(/Items: 1 open/.test(await page.textContent('.plan-detail .tr-scope-chip')), 'the chip: ' + await page.textContent('.plan-detail .tr-scope-chip'));
      await page.click('.plan-detail .tr-scope-chip');
      await page.waitForSelector('#drawer .tr-list [data-ref="plan:my-app/P001-csv-export-for-reports/I-1"]');
      const jobs = await page.$$eval('#drawer a.tr-row', (els) => els.map((e) => e.getAttribute('href')));
      assert(jobs.length === 4 && jobs.every((h) => /^#\/job\/my-app\/00[5-8]-[^/]+\/items$/.test(h)), 'its jobs link to their Items tabs: ' + jobs);
      assert(await page.locator('#drawer [data-action="add-chat-item"]').count() === 0, 'a person adds no plan item (the server refuses it)');
      await page.waitForTimeout(350);   // the panel's entrance
      await t.areaShot(page, AREA, 'plan-rollup-desktop');
      await page.keyboard.press('Escape');
      await p.page.goto(base + '/#/plans/my-app/P001-csv-export-for-reports');
      await p.page.waitForSelector('.plan-detail .tr-scope-chip:not([hidden])');
      await p.page.click('.plan-detail .tr-scope-chip');
      await p.page.waitForSelector('#drawer .tr-list .tr-row');
      await noOverflow(p.page, AREA + ' plan rollup phone');
      await p.page.waitForTimeout(350);   // the panel's entrance
      await t.areaShot(p.page, AREA, 'plan-rollup-phone');
      await p.page.keyboard.press('Escape');
    });

    await step(AREA + ': the Grid Card has a Housekeeping Tracker node: its waiting count, labelled routes in (a job’s open items) and out (make a job), and its procedure card', async () => {
      await page.goto(base + '/#/grid');
      await page.waitForSelector('.grid-map .map-node[data-node="tracker"]');
      const node = await page.textContent('.grid-map .map-node[data-node="tracker"]');
      assert(/Housekeeping Tracker/.test(node) && /\d+ waiting/.test(node), 'the node and its count: ' + node);
      const lab = await page.$$eval('.grid-map .map-label', (els) => els.map((e) => e.textContent));
      assert(lab.includes('make a job') && lab.some((l) => /open items when a/.test(l)), 'its routes are labelled: ' + lab);
      const fit = await page.evaluate(() => { const r = document.querySelector('.grid-map svg').getBoundingClientRect(); return [r.bottom, window.innerHeight]; });
      assert(fit[0] <= fit[1], 'the map still fits 1440x900: ' + fit);
      await page.click('.grid-map .map-node[data-node="tracker"]');
      await page.waitForSelector('.grid-card[data-node="tracker"] ol.proc li');
      assert(await page.getAttribute('.grid-card [data-action="open"]', 'href') === '#/housekeeping' && /Open Housekeeping Tracker/.test(await page.textContent('.grid-card [data-action="open"]')), 'Open goes to the tracker');
      await t.areaShot(page, AREA, 'grid-node-desktop');
      await p.page.goto(base + '/#/grid');
      await p.page.waitForSelector('.grid-list .grid-row[data-node="tracker"]');
      await p.page.evaluate(() => document.querySelector('.grid-list .grid-row[data-node="tracker"]').scrollIntoView({ block: 'center' }));   // the list is drawn again live
      assert(/make a job → Queued/.test(await p.page.textContent('.grid-list-sec:has(.grid-row[data-node="tracker"])')), 'the phone list names the route');
      await noOverflow(p.page, AREA + ' grid phone');
      await p.page.goto(base + '/#/grid/tracker');   // on a phone its card opens as a sheet
      await p.page.waitForSelector('.grid-card[data-node="tracker"] ol.proc li');
      await p.page.waitForTimeout(300);
      await t.areaShot(p.page, AREA, 'grid-node-phone');
    });

    await step(AREA + ': a drift source that sent only an inventory is not “no drift”; persona drift links the personas bound to the Open WebUI model', async () => {
      await page.route('**/api/drift-sources', (route) => route.fulfill({ json: { sources: [{ name: 'inventory-only', url: 'https://127.0.0.1:8443/inventory', host_header: null, status: 'ok', detail: null,
        observed_at: new Date(Date.now() - 60000).toISOString(), next_at: new Date(Date.now() + 1200000).toISOString(), present: [], items: [], last_result: null, manifest: true }] } }));
      await page.goto(base + '/#/housekeeping?scope=system');
      await page.reload();   // the sources were read before: read them again, from the stub
      await page.waitForSelector('.tr-drift[data-source="inventory-only"]');
      const v = await page.$eval('.tr-drift-verdict', (e) => [e.textContent, e.className]);
      assert(/^Inventory only so far/.test(v[0]) && !/is-ok/.test(v[1]), 'not a green “no drift”: ' + v);
      assert(/Contract proposed/.test(await page.textContent('.tr-drift-sec')), 'the contract is labelled proposed');
      await t.areaShot(page, AREA, 'drift-inventory-only-desktop');
      await page.unroute('**/api/drift-sources');
      await page.route('**/api/personas', (route) => route.fulfill({ json: { personas: [
        { name: 'kate', label: 'Kate through Open WebUI', binding: { type: 'openwebui', model: 'kate' }, drift: null },
        { name: 'kate-work', label: 'Kate at work', binding: { type: 'openwebui', model: 'kate' }, drift: null },
        { name: 'nova', label: 'Nova', binding: { type: 'openwebui', model: 'nova-qwen3' }, drift: null }] } }));
      await openItem(page, base, 'system:S-3');
      await page.waitForSelector('#drawer .tr-persona-links a[href="#/personas/kate-work"]');
      const links = await page.$$eval('#drawer .tr-persona-links a', (els) => els.map((e) => e.getAttribute('href')));
      assert(canon(links) === canon(['#/personas/kate', '#/personas/kate-work']), 'every persona bound to the model, by name: ' + links);
      await page.unroute('**/api/personas');
    });

    await step(AREA + ': a job’s stage change without an item change (Retry) updates the list and the badge', async () => {
      await page.goto(base + '/#/housekeeping?status=waiting');
      const I3 = 'job:my-app/003-fix-the-flaky-checkout-test/I-3';
      await page.waitForSelector('.tr-row[data-ref="' + I3 + '"]');
      const r = await page.request.post(base + '/api/jobs/my-app/003-fix-the-flaky-checkout-test/retry', { data: {} });
      assert(r.status() === 200, 'retry ' + r.status());
      await page.waitForSelector('.tr-row[data-ref="' + I3 + '"]', { state: 'detached', timeout: 8000 });
      const want = (await (await page.request.get(base + '/api/items?waiting=1&limit=200')).json()).counts.waiting;
      await page.waitForFunction((n) => Number((document.querySelector('.rail-item[data-view="tracker"] .rail-badge') || {}).textContent) === n, want, { timeout: 8000 });
    });
  } finally {
    await d.ctx.close();
    await p.ctx.close();
    m.proc.kill();
  }
}
