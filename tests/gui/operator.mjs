// Checks for the Operator (DESIGN §36 "GUI interface notes (operator persona)"; the status of GPU custody §37, drift
// sources §38 and error capture §39 from the GUI notes at the end of §39): the view module swfactory/web/views/operator.js
// against tests/gui_mock/operator.py, run by tests/gui/smoke.mjs (AREA_SUITES) after the core checks, or alone with
// GUI_ONLY=operator node tests/gui/smoke.mjs. Screenshots of every new screen, desktop 1440x900 and phone 390x844:
// t.areaShot(page, AREA, name) → $GUI_AREA_SHOTS/operator/<name>.png.
//
// run(t) gets the harness of smoke.mjs: { browser, url, startMock, step, assert, canon, waitPost, shot, cleanShot,
// noOverflow, watch, allowHttp, newPage(label, {phone, width, height, colorScheme, mockUrl}), areaShot(page, area, name) }.
// t.newPage gives a signed-in page of its own ({ctx, page, url}); close its ctx when done.
//
// GET /api/custody and GET /api/drift-sources are the routing and tracker areas' to mock once merged (they come first in
// gui_mock.AREAS): the custody and drift steps route the page's requests to this area's own rows
// (/__mock/operator/custody, /__mock/operator/drift-sources), so their checks hold whichever area answers.

const AREA = 'operator';
const AUTH = { Authorization: 'Bearer test-token' };

async function hook(t, method, name, body) {
  const r = await fetch(t.url + '/__mock/operator/' + name, { method, headers: { ...AUTH, 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
  if (!r.ok) throw new Error('mock hook ' + name + ': ' + r.status);
  return r.json();
}
const scenario = (t, name) => hook(t, 'POST', 'scenario', { name });
async function api(t, method, p, body) {
  const r = await fetch(t.url + p, { method, headers: { ...AUTH, 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
  return { status: r.status, json: await r.json().catch(() => null) };
}
const publish = (t, ev) => fetch(t.url + '/__mock/publish', { method: 'POST', headers: { ...AUTH, 'Content-Type': 'application/json' }, body: JSON.stringify(ev) });
const focusKey = (page) => page.evaluate(() => { const a = document.activeElement; return a ? (a.dataset && a.dataset.focus) || a.id || a.tagName : null; });
/** Scroll the first match of sel into view inside the page (a locator's scroll fails when a refresh redraws it). */
const scrollTo = (page, sel) => page.evaluate((x) => { const el = document.querySelector(x); if (el) el.scrollIntoView({ block: 'center' }); }, sel);
/** Count the page's GET requests whose path and query match re, from now on: {n, stop()}. */
function countGets(page, re) {
  const c = { n: 0 };
  const fn = (r) => { if (r.method() === 'GET' && re.test(new URL(r.url()).pathname + new URL(r.url()).search)) c.n += 1; };
  page.on('request', fn);
  c.stop = () => page.off('request', fn);
  return c;
}

/** Route the page's /api/custody and /api/drift-sources to this area's rows (see the header). */
async function pinFixtures(t, page) {
  const custody = await hook(t, 'GET', 'custody');
  const drift = await hook(t, 'GET', 'drift-sources');
  await page.route(/\/api\/custody(\?.*)?$/, (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(custody) }));
  await page.route(/\/api\/drift-sources$/, (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(drift) }));
}

/** #/activity?<query> in a fresh document (a hash change within the view keeps the last query's page until the
 *  new answer is drawn), once its answer is shown. */
async function openActivity(t, page, query) {
  await page.goto(t.url + '/#/activity?' + query);
  await page.reload();
  await page.waitForSelector('#operator-root .op-act-who, #operator-root .op-act-err');
  await page.waitForFunction(() => { const a = document.querySelector('#operator-root .op-act'); return a && a.getAttribute('aria-busy') === 'false'; });
}
async function openOperator(t, page, hash) {
  const target = t.url + '/' + (hash || '#/operator');
  if (page.url() === target) await page.reload(); else await page.goto(target);
  await page.waitForSelector('#view-operator:not([hidden]) #operator-root .view-title');
}
/** Go to Home and count its Needs-you items of key, once the operator card was loaded afresh. */
async function homeNeed(t, page, key) {
  const fresh = page.waitForResponse((r) => new URL(r.url()).pathname === '/api/operator', { timeout: 8000 }).catch(() => null);
  await page.goto(t.url + '/#/home');
  await fresh;
  await page.waitForSelector('#view-home:not([hidden]) .home-card[data-section="operator"]');
  await page.waitForTimeout(250);
  if (!await page.locator('.needs-item[data-need="' + key + '"]').count() && await page.locator('[data-action="needs-more"]').count()) {
    if (/Show all/.test(await page.textContent('[data-action="needs-more"]'))) {
      await page.click('[data-action="needs-more"]');
      await page.waitForFunction(() => !/Show all/.test((document.querySelector('[data-action="needs-more"]') || {}).textContent || ''));
    }
  }
  return page.locator('.needs-item[data-need="' + key + '"]').count();
}

export async function run(t) {
  const { step, assert, noOverflow } = t;
  await scenario(t, 'normal');
  const d = await t.newPage(AREA);
  const p = await t.newPage(AREA + '-phone', { phone: true });
  await d.ctx.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: t.url });
  try {
    await step(AREA + ': the Operator page: who she is, the guarantee note in plain words, and her receipts, at 1440 px and 390 px with no sideways scroll', async () => {
      for (const [page, tag] of [[d.page, 'desktop'], [p.page, 'phone']]) {
        await openOperator(t, page);
        await page.waitForSelector('#operator-root .op-rcpt');
        if (tag === 'desktop') assert(await page.getAttribute('.rail-item[data-view="operator"]', 'aria-current') === 'page', 'the rail marks it');
        assert(/Operator · Seraphina OS Harness$/.test(await page.title()), 'the page title names the view only: ' + await page.title());
        const who = await page.textContent('#operator-root [data-card="who"]');
        assert(/kate/.test(who) && /\/home\/operator\/receipts/.test(who) && /10 receipts read/.test(who), 'who, the folder and the last scan: ' + who);
        const note = await page.textContent('#operator-root [data-card="guarantee"]');
        assert(/She acts outside the harness\./.test(note) && /never approves or blocks her actions/.test(note) && /Its guarantees are for its own agents\./.test(note), 'the guarantee note: ' + note);
        assert(await page.locator('#operator-root .op-banner').count() === 0, 'no banner while all is well');
        await noOverflow(page, AREA + ' ' + tag);
        await t.areaShot(page, AREA, 'operator-receipts-' + tag);
      }
      const page = d.page;
      await page.click('[data-disclosure="compare"] > summary');
      const rows = await page.$$eval('.op-cmp tbody tr', (trs) => trs.map((tr) => tr.textContent));
      assert(rows.length === 6 && /She holds root/.test(rows[0]) && /never tests it/.test(rows[4]), 'the comparison table: ' + rows.length);
      await p.page.click('[data-disclosure="compare"] > summary');
      await noOverflow(p.page, AREA + ' compare phone');
      await scrollTo(p.page, '[data-disclosure="compare"]');
      await t.areaShot(p.page, AREA, 'operator-guarantee-phone');
    });

    await step(AREA + ': each receipt says "Reported by the operator", not verified before verified, never a green outcome, her undo words as the server gives them; cannot-read files, flags and refused files are listed; her text stays text', async () => {
      const page = d.page;
      const info = await page.$$eval('#operator-root .op-rcpt', (els) => els.map((li) => ({
        id: li.dataset.receipt || null, outcome: li.dataset.outcome || null, unread: li.classList.contains('is-unread'),
        label: !!li.querySelector('.op-label'), text: li.textContent,
        undo: (li.querySelector('.op-undo-chip .tag') || { textContent: null }).textContent,
        order: (() => { const nv = li.querySelector('.op-nv'); const v = li.querySelector('.op-v'); return nv && v ? !!(nv.compareDocumentPosition(v) & Node.DOCUMENT_POSITION_FOLLOWING) : null; })(),
        green: !!li.querySelector('.tone-ok, .tag-ok'), imgs: li.querySelectorAll('img, script, b').length,
      })));
      assert(info.length === 16, 'every file of the index: ' + info.length);
      assert(info.every((r) => r.label), 'every row is labelled');
      assert(info.filter((r) => !r.unread).every((r) => r.order === true), 'not verified comes before verified on every read receipt');
      assert(info.every((r) => !r.green), 'no outcome is green');
      assert(['nothing_verified', 'partly_verified', 'reported_verified', 'outcome_unknown', 'reported_failed'].every((s) => info.some((r) => r.outcome === s)), 'every outcome: ' + info.map((r) => r.outcome));
      const nothing = await page.getAttribute('.op-rcpt[data-outcome="nothing_verified"] .pill', 'class');
      assert(/tone-warn/.test(nothing), 'nothing verified is amber: ' + nothing);
      // DESIGN §40: her receipt's own outcome, in words beside the checks; unknown amber, failed red, never green
      const unknownPills = await page.$$eval('.op-rcpt[data-receipt="r-20261025-013"] .op-rcpt-chips > .pill', (els) => els.map((e) => [e.className, e.textContent]));
      assert(/tone-warn/.test(unknownPills[0][0]) && /^outcome unknown \(reported\): it may or may not have happened/.test(unknownPills[0][1]), 'outcome unknown, amber: ' + JSON.stringify(unknownPills));
      assert(/tone-warn/.test(unknownPills[1][0]) && /^The change: outcome unknown: it may or may not have happened; check before retrying$/.test(unknownPills[1][1]), 'her reported outcome in words: ' + JSON.stringify(unknownPills));
      const failedPills = await page.$$eval('.op-rcpt[data-receipt="r-20261025-014"] .op-rcpt-chips > .pill', (els) => els.map((e) => [e.className, e.textContent]));
      assert(/tone-err/.test(failedPills[0][0]) && /^reported failed: the operator says the change did not happen/.test(failedPills[0][1]) && /The change: reported failed/.test(failedPills[1][1]), 'reported failed is red: ' + JSON.stringify(failedPills));
      const okPills = await page.$$eval('.op-rcpt[data-receipt="r-20261025-011"] .op-rcpt-chips > .pill', (els) => els.map((e) => [e.className, e.textContent]));
      assert(!/tone-ok/.test(okPills.map((x) => x[0]).join(' ')) && /The change: reported succeeded; the other side's response is recorded/.test(okPills[1][1]), 'reported succeeded is never green: ' + JSON.stringify(okPills));
      assert(await page.getAttribute('.op-rcpt[data-receipt="r-20261025-013"] .op-item', 'href') === '#/housekeeping/system%3AS-9', 'an unknown outcome links its tracker item');
      // the undo chip is undo.words verbatim (her report), never the GUI's own words or a v2 badge
      const served = (await api(t, 'GET', '/api/operator/receipts?limit=200')).json.receipts.filter((r) => r.status === 'read');
      for (const r of info.filter((x) => !x.unread)) {
        const want = served.find((s) => s.id === r.id).undo.words;
        assert(r.undo === want, r.id + ': the undo chip is undo.words: ' + r.undo + ' vs ' + want);
      }
      assert(info.every((r) => r.imgs === 0) && info.some((r) => /<img src=x onerror=alert\('intent'\)>/.test(r.text)) && info.some((r) => /<script>alert\('x'\)<\/script>/.test(r.text)), 'markup in her intent stays text');
      const unread = info.filter((r) => r.unread).map((r) => r.text).join(' | ');
      assert(/nested more than 64 levels deep/.test(unread) && /NaN is not JSON/.test(unread) && /could not be read \(PermissionError\)/.test(unread) && /unknown schema 'kate-change-receipt\/2'/.test(unread), 'cannot-read reasons: ' + unread);
      const all = info.map((r) => r.text).join(' | ');
      for (const chip of ['Changed after it was first read', 'File gone', 'Same id as the receipt in 2026-10/r-20261022-006.json', 'May not be hers', 'reversibility unknown',
        'reported as going back by itself if the server does not start', 'reported as not undoable', 'as written: undo-reboot', 'as written: undo-by-hand'])
        assert(all.indexOf(chip) >= 0, 'chip: ' + chip);
      assert(await page.getAttribute('.op-rcpt[data-receipt="r-20261021-001"] .op-item', 'href') === '#/housekeeping/system%3AS-4', 'her unchecked claim links to its tracker item');
      await page.click('[data-disclosure="refused"] > summary');
      const refused = await page.textContent('[data-disclosure="refused"]');
      assert(/Not read \(2\)/.test(refused) && /latest\.json/.test(refused) && /a symbolic link: refused/.test(refused), 'refused: ' + refused);
      assert(!/root-only/.test(refused), 'an unreadable folder is not among the refused: ' + refused);
      // her ruling (26 Oct): what the harness may not read is named apart and left as it is; every receipt names its actor
      const locked = await page.textContent('#operator-root .op-unreadable');
      assert(/1 place in her folder the harness may not read: change-receipts\/root-only\/\. They are left as they are: the harness never changes their owner, mode or access/.test(locked), 'unreadable: ' + locked);
      assert(info.every((r) => /by \S|actor not stated/.test(r.text)), 'every row names its actor or says it is not stated');
      await page.click('.op-filter[data-filter="cannot"]');
      await page.waitForFunction(() => document.querySelectorAll('#operator-root .op-rcpt').length === 4);
      assert(await page.getAttribute('.op-filter[data-filter="cannot"]', 'aria-pressed') === 'true', 'the filter says it is on');
      await page.click('.op-filter[data-filter="unverified"]');
      await page.waitForFunction(() => document.querySelectorAll('#operator-root .op-rcpt').length === 4);
      await page.click('.op-filter[data-filter="all"]');
      await page.waitForFunction(() => document.querySelectorAll('#operator-root .op-rcpt').length === 16);
    });

    await step(AREA + ': a receipt with its own outcome (operator-receipt/1, DESIGN §40): unknown says check before retrying and shows the response she recorded (status, ids, body hash), as text; failed is never green', async () => {
      const page = d.page;
      await page.goto(t.url + '/#/operator/receipt/r-20261025-013');
      await page.waitForSelector('#drawer .op-panel[data-receipt="r-20261025-013"]');
      const text = await page.textContent('#drawer .op-panel');
      const order = ['Outcome unknown: check before retrying.', 'What it was for', 'Not verified (1)', 'the settings arrived on the backup host', 'Verified, as she reports (1)', 'How the change ended, as she reports',
        'outcome unknown: it may or may not have happened; check before retrying', 'Status', '504', 'Ids it returned', 'none', 'Answer’s sha256'];
      let at = -1;
      for (const w of order) { const i = text.indexOf(w, at + 1); assert(i > at, 'in order: ' + w); at = i; }
      assert(await page.locator('#drawer .op-panel i, #drawer .op-panel .tone-ok').count() === 0 && /<i>now<\/i>/.test(text), 'her text stays text, nothing green');
      await t.areaShot(page, AREA, 'operator-receipt-unknown-desktop');
      await page.goto(t.url + '/#/operator/receipt/r-20261025-014');
      await page.waitForSelector('#drawer .op-panel[data-receipt="r-20261025-014"]');
      const failed = await page.textContent('#drawer .op-panel');
      assert(/reported failed: the operator says the change did not happen/.test(failed) && /409/.test(failed) && /conflict-7 <b>busy<\/b>/.test(failed) && /no body/.test(failed), 'the refusal she recorded: ' + failed.slice(0, 400));
      assert(await page.locator('#drawer .op-panel b, #drawer .op-panel .tone-ok').count() === 0, 'ids stay text, nothing green');
      await page.keyboard.press('Escape');
      await page.waitForFunction(() => document.getElementById('drawer').hidden);
    });

    await step(AREA + ': a receipt opens in the panel (#/operator/receipt/<id>): label, not verified then verified ("Nothing verified"), the undo class as written and as read, the rollback, before and after side by side as data; Esc gives focus back', async () => {
      const page = d.page;
      await page.focus('.op-rcpt[data-receipt="r-20261022-002"] .op-rcpt-id');
      await page.keyboard.press('Enter');
      await page.waitForFunction(() => location.hash === '#/operator/receipt/r-20261022-002');
      await page.waitForSelector('#drawer.is-panel:not([hidden]) .op-panel[data-receipt="r-20261022-002"]');
      const text = await page.textContent('#drawer .op-panel');
      assert(/Reported by the operator\./.test(text), 'labelled');
      const order = ['Not verified (1)', 'nginx reload not verified', 'Verified', 'Nothing verified', 'Can it be undone?', 'reported as undoable at once', 'undo-instant', 'undo_instantly', 'Rollback, as she wrote it', 'Put fullchain.pem.bak back'];
      let at = -1;
      for (const s of order) { const i = text.indexOf(s, at + 1); assert(i > at, 'in order: ' + s); at = i; }
      assert(await page.locator('#drawer .op-panel img, #drawer .op-panel script').count() === 0, 'her markup stays text');
      const ba = await page.$$eval('#drawer .op-ba pre', (els) => els.map((e) => e.textContent));
      assert(ba.length === 2 && /Old Home Lab CA/.test(ba[0]) && /Home Lab Intermediate CA/.test(ba[1]) && ba[0].length > 4000, 'before and after as JSON, large: ' + ba.map((x) => x.length));
      assert(await page.getAttribute('#drawer .op-item', 'href') === '#/housekeeping/system%3AS-5', 'its item on the tracker');
      assert(!/img src/.test(await page.title()), 'no receipt text in the page title');
      await noOverflow(page, AREA + ' panel desktop');
      await t.areaShot(page, AREA, 'operator-receipt-desktop');
      await page.keyboard.press('Escape');
      await page.waitForFunction(() => location.hash === '#/operator' && document.getElementById('drawer').hidden);
      await page.waitForFunction(() => document.activeElement && document.activeElement.closest('.op-rcpt') && document.activeElement.closest('.op-rcpt').dataset.receipt === 'r-20261022-002', null, { timeout: 3000 });
      // a before too large to keep: its keys and a note; a time written without a zone; a duplicate id
      await page.goto(t.url + '/#/operator/receipt/r-20261023-007');
      await page.waitForSelector('#drawer .op-panel[data-receipt="r-20261023-007"]');
      const big = await page.textContent('#drawer .op-panel');
      assert(/Too large for the harness to keep/.test(big) && /_omitted/.test(big) && /not a UUID/.test(big), 'a before that was too large to keep');
      // the route leaving the receipt for another tab closes the panel (the app only closes it at its back hash)
      await page.evaluate(() => { location.hash = '#/operator/drift'; });
      await page.waitForFunction(() => document.getElementById('drawer').hidden && (document.querySelector('.op-page') || {}).dataset?.tab === 'drift');
      assert(!await page.evaluate(() => document.getElementById('app').hasAttribute('inert')), 'the page behind is usable again');
      await page.goto(t.url + '/#/operator/receipt/r-20261022-006');
      await page.waitForSelector('#drawer .op-panel[data-receipt="r-20261022-006"]');
      const six = await page.textContent('#drawer .op-panel');
      assert(/time has no zone: read as UTC/.test(six) && /copies\/r-20261022-006\.json/.test(six) && /Verified, as she reports \(2\)/.test(six), 'warnings and the duplicate file');
      await p.page.goto(t.url + '/#/operator/receipt/r-20261022-002');
      await p.page.waitForSelector('#drawer .op-panel[data-receipt="r-20261022-002"]');
      await noOverflow(p.page, AREA + ' panel phone');
      await t.areaShot(p.page, AREA, 'operator-receipt-phone');
      await scrollTo(p.page, '#drawer .op-ba');
      await t.areaShot(p.page, AREA, 'operator-receipt-before-after-phone');
      await p.page.keyboard.press('Escape');
      await page.goto(t.url + '/#/operator');
    });

    await step(AREA + ': "What she did in the hour around it" closes the panel and opens the Activity query, usable at once; a reopened receipt is read again after a scan', async () => {
      const page = d.page;
      for (const [pg, tag] of [[page, 'desktop'], [p.page, 'phone']]) {
        await openOperator(t, pg);
        await pg.waitForSelector('.op-rcpt[data-receipt="r-20261022-002"] .op-rcpt-id');
        await pg.click('.op-rcpt[data-receipt="r-20261022-002"] .op-rcpt-id');
        await pg.waitForSelector('#drawer .op-panel [data-action="around"]');
        await pg.click('#drawer .op-panel [data-action="around"]');
        await pg.waitForFunction(() => /^#\/activity\?agent=kate&since=/.test(location.hash) && /until=/.test(location.hash));
        await pg.waitForFunction(() => document.getElementById('drawer').hidden && !document.getElementById('app').hasAttribute('inert'));
        await pg.waitForSelector('#operator-root .op-act-who');
        assert(await focusKey(pg) === 'tab-activity', tag + ': focus on the Activity tab, not lost: ' + await focusKey(pg));
        await pg.keyboard.press('Escape');
        await pg.waitForTimeout(200);
        assert(/^#\/activity\?/.test(await pg.evaluate(() => location.hash)), tag + ': Esc keeps the query the link opened');
      }
      // a scan changes a receipt after its panel was closed: reopening reads it again (no stale copy)
      await openOperator(t, page);
      await page.waitForSelector('.op-rcpt[data-receipt="r-20261025-011"] .op-rcpt-id');
      const gets = countGets(page, /^\/api\/operator\/receipts\/r-20261025-011$/);
      await page.click('.op-rcpt[data-receipt="r-20261025-011"] .op-rcpt-id');
      await page.waitForSelector('#drawer .op-panel[data-receipt="r-20261025-011"]');
      await page.keyboard.press('Escape');
      await page.waitForFunction(() => document.getElementById('drawer').hidden);
      await publish(t, { type: 'operator.receipts', data: { read: 8, cannot_read: 4, refused: 2, new: 0, changed: 1, gone: 0, items: [] } });
      await page.waitForTimeout(300);
      await page.click('.op-rcpt[data-receipt="r-20261025-011"] .op-rcpt-id');
      await page.waitForSelector('#drawer .op-panel[data-receipt="r-20261025-011"]');
      await page.waitForFunction(() => !document.querySelector('#drawer .skel-box'));
      gets.stop();
      assert(gets.n === 2, 'the receipt was read again on reopening: ' + gets.n);
      await page.keyboard.press('Escape');
      await page.waitForFunction(() => document.getElementById('drawer').hidden);
    });

    await step(AREA + ': Scan now reads her folder (POST /api/operator/receipts/scan) and keeps keyboard focus; the new receipt and the event refresh the list, and the toast names counts, never her words', async () => {
      const page = d.page;
      await scenario(t, 'normal');
      await openOperator(t, page);
      await page.waitForSelector('#operator-root .op-rcpt');
      await hook(t, 'POST', 'new-receipt');
      await page.focus('[data-action="scan"]');
      await t.waitPost(page, /\/api\/operator\/receipts\/scan$/, () => page.keyboard.press('Enter'));
      await page.waitForSelector('.toast-ok .toast-msg');
      const toast = await page.textContent('.toast-ok .toast-msg');
      assert(/^Scanned: 1 new receipt\.$/.test(toast), 'toast: ' + toast);
      await page.waitForSelector('.op-rcpt[data-receipt="r-20261025-012"]');
      assert(/Turn on the nightly restore drill/.test(await page.textContent('.op-rcpt[data-receipt="r-20261025-012"]')), 'the new receipt is listed');
      assert(await focusKey(page) === 'scan', 'focus stays on Scan now: ' + await focusKey(page));
    });

    await step(AREA + ': banners: a broken one-operator rule, a mark that holds nothing, a folder the harness cannot read or can write (red), a failed scan, no folder yet (not an alarm), older receipts past the limit; plain callouts announced once; Home lists what needs the owner', async () => {
      const page = d.page;
      const banner = async (name, sel) => {
        await scenario(t, 'normal');
        await scenario(t, name);
        await openOperator(t, page);
        await page.waitForSelector('#operator-root .op-banner[data-banner="' + sel + '"]');
        return page.textContent('#operator-root .op-banner[data-banner="' + sel + '"]');
      };
      let txt = await banner('rule-problem', 'problem');
      assert(/at most one persona is the operator unless operator\.several lists every one of them \(not listed: kate, seraphina\)\. Until it is fixed, nobody is the operator\./.test(txt), 'rule, its own sentence: ' + txt);
      assert(await page.locator('[data-banner="problem"] [data-action="fix-rule"]').count() === 1, 'the rule banner offers a way out');
      txt = await banner('not-operator', 'not-operator');
      assert(/kate-offline: marked, but not the operator: see the doctor/.test(txt), 'not the operator: ' + txt);
      txt = await banner('unreadable', 'scan-problem');
      assert(/The harness cannot read the receipts directory: the harness may not read \/home\/operator\/receipts: give its user read access\. What was read before is still shown\./.test(txt), 'unreadable: ' + txt);
      assert(await homeNeed(t, page, 'operator:unreadable') === 1, 'Home: a set folder the harness cannot read needs the owner');
      assert(/The harness cannot read her receipts folder/.test(await page.textContent('.needs-item[data-need="operator:unreadable"]')), 'the need in plain words');
      // a failed scan (GET /api/operator/receipts scan.problem) shows on the Receipts tab, its words as they come
      txt = await banner('scan-fails', 'scan-failed');
      assert(/The last scan did not finish: the scan failed \(ItemsBusy\): what was read before is shown\. Scan now tries again\./.test(txt), 'a failed scan: ' + txt);
      // no folder yet: a note, not "cannot read", and nothing for Home's Needs you
      txt = await banner('no-folder', 'no-folder');
      assert(/No receipts folder yet\./.test(txt) && await page.locator('[data-banner="scan-problem"], [data-banner="scan-failed"]').count() === 0, 'no folder is a note: ' + txt);
      assert(/callout-info/.test(await page.getAttribute('[data-banner="no-folder"]', 'class')), 'and not a warning');
      await t.areaShot(page, AREA, 'operator-no-folder-desktop');
      await openOperator(t, p.page);
      await p.page.waitForSelector('#operator-root .op-banner[data-banner="no-folder"]');
      await noOverflow(p.page, AREA + ' no folder phone');
      await t.areaShot(p.page, AREA, 'operator-no-folder-phone');
      assert(await homeNeed(t, page, 'operator:unreadable') === 0, 'Home: no folder is not "could not be read"');
      assert(/No receipts folder set yet\./.test(await page.textContent('.home-card[data-section="operator"]')), 'the card says so');
      txt = await banner('writable', 'writable');
      assert(/The harness’s own user can write this directory, so a file put there would read as hers\. Her items from it are marked untrusted\. Give the directory to her user/.test(txt), 'writable: ' + txt);
      assert(/callout-err/.test(await page.getAttribute('.op-banner[data-banner="writable"]', 'class')), 'the writable banner is red');
      assert(await page.locator('#operator-root [role="alert"]').count() === 0, 'banners are plain callouts (a refresh would read a role=alert out again)');
      // an unrelated failed call: no banner is re-inserted; only the Errors tab's count changes
      await page.evaluate(() => {
        window.__bannerAdds = 0;
        new MutationObserver((ms) => { for (const m of ms) for (const n of m.addedNodes) if (n.nodeType === 1 && (n.matches('[data-banner]') || n.querySelector('[data-banner]'))) window.__bannerAdds += 1; })
          .observe(document.getElementById('operator-root'), { childList: true, subtree: true });
      });
      const badge = () => Number((document.querySelector('.op-tab[data-tab="errors"] .op-tab-n') || {}).textContent || 0);
      const before = await page.evaluate(badge);
      for (let i = 0; i < 3; i++) {
        await publish(t, { type: 'agent.error', agent: 'builder', repo: 'my-app', job: '003-fix-the-flaky-checkout-test',
          data: { kind: 'tool', name: 'Read', id: 'call_banner_' + i, output: 'no such file', output_truncated: false, started_at: new Date(Date.now() - 1000).toISOString(), failed_at: new Date(Date.now() + i).toISOString(), elapsed_ms: 1000 } });
      }
      await page.waitForFunction((n) => Number((document.querySelector('.op-tab[data-tab="errors"] .op-tab-n') || {}).textContent || 0) === n + 3, before);
      assert(await page.evaluate(() => window.__bannerAdds) === 0, 'no banner re-inserted for an unrelated event: ' + await page.evaluate(() => window.__bannerAdds));
      await scenario(t, 'past-limit');
      await openOperator(t, page);
      await page.waitForSelector('#operator-root .op-banner[data-banner="past-limit"]');
      assert(/3 older receipts were not read \(operator\.max_receipts\)/.test(await page.textContent('.op-banner[data-banner="past-limit"]')), 'past the limit');
      await page.click('[data-disclosure="refused"] > summary');
      assert(/past operator\.max_receipts \(5000\): an older file, not read/.test(await page.textContent('[data-disclosure="refused"]')), 'the older files are listed as not read');
      await noOverflow(page, AREA + ' banners');
      await page.evaluate(() => document.getElementById('main').scrollTo(0, 0));
      await t.areaShot(page, AREA, 'operator-banners-desktop');
      await openOperator(t, p.page);
      await p.page.waitForSelector('#operator-root .op-banner[data-banner="writable"]');
      await noOverflow(p.page, AREA + ' banners phone');
      await t.areaShot(p.page, AREA, 'operator-banners-phone');
      for (const [pg, tag] of [[page, 'desktop'], [p.page, 'phone']]) {
        assert(await homeNeed(t, pg, 'operator:writable') === 1, tag + ': Home: the folder the harness can write needs the owner');
        assert(await pg.getAttribute('.needs-item[data-need="operator:writable"]', 'href') === '#/operator', 'it opens the Operator page');
        await scrollTo(pg, '.needs-item[data-need="operator:writable"]');
        await t.areaShot(pg, AREA, 'operator-home-needs-' + tag);
      }
      await scenario(t, 'normal');
    });

    await step(AREA + ': Choose the operator: Open WebUI and wake personas only, read fresh each time; a second one asks "Allow several operators?" with one "Keep only" per name, each saving what it says; marking off saves role null; no change says so', async () => {
      const page = d.page;
      await openOperator(t, page);
      await page.waitForSelector('[data-action="mark"]');
      const personas = countGets(page, /^\/api\/personas$/);
      await page.click('[data-action="mark"]');
      await page.waitForSelector('dialog.modal .op-mark-list');
      assert(personas.n === 1, 'the personas are read when the dialog opens: ' + personas.n);
      assert(await page.isChecked('input[data-persona="kate"]'), 'kate is marked');
      assert(await page.isDisabled('input[data-persona="kate-offline"]'), 'an imported copy cannot be the operator');
      assert(/does not approve or block her actions/.test(await page.textContent('dialog.modal')), 'the help text');
      await t.areaShot(page, AREA, 'operator-mark-dialog-desktop');
      await page.click('dialog.modal button[data-action="ok"]');
      await page.waitForSelector('.toast-info .toast-msg');
      assert(/^Nothing changed: kate is the operator\.$/.test(await page.textContent('.toast-info .toast-msg')), 'Save without a change says so');
      await page.click('[data-action="mark"]');
      await page.waitForSelector('dialog.modal .op-mark-list');
      assert(personas.n === 2, 'and again on the next opening: ' + personas.n);
      personas.stop();
      await page.check('input[data-persona="seraphina"]');
      t.allowHttp(true);
      await page.click('dialog.modal button[data-action="ok"]');
      await page.waitForSelector('dialog.modal >> text=Allow several operators?');
      t.allowHttp(false);
      assert(await page.locator('dialog.modal').count() === 1, 'the mark dialog closed before the question');
      const keeps = await page.$$eval('dialog.modal button[data-action^="keep:"]', (els) => els.map((e) => [e.dataset.action, e.textContent.trim()]));
      assert(t.canon(keeps) === t.canon([['keep:seraphina', 'Keep only seraphina'], ['keep:kate', 'Keep only kate']]), 'one keep button per name, the new one first: ' + JSON.stringify(keeps));
      await t.areaShot(page, AREA, 'operator-several-dialog-desktop');
      const resp = await t.waitPost(page, /\/api\/config$/, () => page.click('dialog.modal button[data-action="several"]'));
      const body = JSON.parse(resp.request().postData());
      assert(t.canon(body) === t.canon({ agents: { seraphina: { role: 'operator' } }, operator: { several: ['kate', 'seraphina'] } }), 'saves the mark with operator.several: ' + JSON.stringify(body));
      await page.waitForFunction(() => !document.querySelector('dialog.modal'));
      await page.waitForSelector('.op-who-list li[data-operator="seraphina"]');
      // two operators: the head names both, one activity link each
      const head = await page.textContent('#operator-root .view-head');
      assert(/What kate and seraphina report they did on the server/.test(head), 'the head names every operator: ' + head);
      const links = await page.$$eval('#operator-root .view-head [data-activity]', (els) => els.map((e) => [e.textContent.trim(), e.getAttribute('href')]));
      assert(t.canon(links) === t.canon([['What kate did this week', '#/activity?agent=kate&since=7d'], ['What seraphina did this week', '#/activity?agent=seraphina&since=7d']]), 'one link per operator: ' + JSON.stringify(links));
      await t.areaShot(page, AREA, 'operator-several-head-desktop');
      await page.click('[data-action="mark"]');
      await page.waitForSelector('dialog.modal .op-mark-list');
      await page.uncheck('input[data-persona="seraphina"]');
      const off = await t.waitPost(page, /\/api\/config$/, () => page.click('dialog.modal button[data-action="ok"]'));
      assert(t.canon(JSON.parse(off.request().postData())) === t.canon({ agents: { seraphina: { role: null } } }), 'off is role null');
      await page.waitForFunction(() => !document.querySelector('.op-who-list li[data-operator="seraphina"]'));
      // the new mark sorts before the current operator: "Keep only kate" keeps kate (and says so)
      await scenario(t, 'normal');
      await api(t, 'POST', '/api/config', { agents: { kate: { role: null }, seraphina: { role: 'operator' } } });
      await openOperator(t, page);
      await page.waitForSelector('.op-who-list li[data-operator="seraphina"]');
      await page.click('[data-action="mark"]');
      await page.waitForSelector('dialog.modal input[data-persona="kate"]');
      await page.check('input[data-persona="kate"]');
      t.allowHttp(true);
      await page.click('dialog.modal button[data-action="ok"]');
      await page.waitForSelector('dialog.modal button[data-action="keep:kate"]');
      t.allowHttp(false);
      assert(/“Keep only kate”: kate is the only operator, and seraphina loses the mark\./.test(await page.textContent('dialog.modal')), 'the words say what the button does');
      const keep = await t.waitPost(page, /\/api\/config$/, () => page.click('dialog.modal button[data-action="keep:kate"]'));
      assert(t.canon(JSON.parse(keep.request().postData())) === t.canon({ agents: { kate: { role: 'operator' }, seraphina: { role: null } } }), 'keep kate saves kate: ' + keep.request().postData());
      await page.waitForSelector('.toast-ok .toast-msg >> text=kate is the operator.');
      assert(t.canon((await api(t, 'GET', '/api/operator')).json.operators) === t.canon(['kate']), 'the server has kate alone');
      // the phone: the dialog and the question
      await scenario(t, 'normal');
      await openOperator(t, p.page);
      await p.page.waitForSelector('[data-action="mark"]');
      await p.page.click('[data-action="mark"]');
      await p.page.waitForSelector('dialog.modal .op-mark-list');
      await t.areaShot(p.page, AREA, 'operator-mark-dialog-phone');
      await p.page.check('input[data-persona="seraphina"]');
      t.allowHttp(true);
      await p.page.click('dialog.modal button[data-action="ok"]');
      await p.page.waitForSelector('dialog.modal button[data-action="several"]');
      t.allowHttp(false);
      await noOverflow(p.page, AREA + ' several dialog phone');
      await t.areaShot(p.page, AREA, 'operator-several-dialog-phone');
      await p.page.keyboard.press('Escape');
      await p.page.waitForSelector('.toast-info .toast-msg >> text=Nothing changed');
    });

    await step(AREA + ': a broken one-operator rule can be fixed from the page: "Keep one or allow several…" saves operator.several; a failed persona read is an error with Try again, never "No personas yet"', async () => {
      const page = d.page;
      await scenario(t, 'rule-problem');
      await openOperator(t, page);
      await page.waitForSelector('[data-banner="problem"] [data-action="fix-rule"]');
      await page.click('[data-action="fix-rule"]');
      await page.waitForSelector('dialog.modal button[data-action="several"]');
      const fix = await t.waitPost(page, /\/api\/config$/, () => page.click('dialog.modal button[data-action="several"]'));
      assert(t.canon(JSON.parse(fix.request().postData())) === t.canon({ operator: { several: ['kate', 'seraphina'] } }), 'allows both: ' + fix.request().postData());
      await page.waitForFunction(() => !document.querySelector('[data-banner="problem"]'));
      assert(t.canon((await api(t, 'GET', '/api/operator')).json.operators) === t.canon(['kate', 'seraphina']), 'both are operators now');
      // the same from the dialog: Save with both still checked asks the question instead of closing silently
      await scenario(t, 'normal');
      await scenario(t, 'rule-problem');
      await openOperator(t, page);
      await page.waitForSelector('[data-banner="problem"]');
      await page.click('[data-action="mark"]');
      await page.waitForSelector('dialog.modal .op-mark-list');
      await page.click('dialog.modal button[data-action="ok"]');
      await page.waitForSelector('dialog.modal button[data-action="keep:kate"]');
      await t.areaShot(page, AREA, 'operator-rule-fix-desktop');
      const one = await t.waitPost(page, /\/api\/config$/, () => page.click('dialog.modal button[data-action="keep:kate"]'));
      assert(t.canon(JSON.parse(one.request().postData())) === t.canon({ agents: { seraphina: { role: null } } }), 'keep kate unmarks seraphina: ' + one.request().postData());
      await page.waitForFunction(() => !document.querySelector('[data-banner="problem"]'));
      // one failed read of the personas
      await scenario(t, 'normal');
      await openOperator(t, page);
      let failed = false;
      await page.route(/\/api\/personas$/, (route) => { if (!failed) { failed = true; return route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: 'busy, try again' }) }); } return route.fallback(); });
      t.allowHttp(true);
      await page.click('[data-action="mark"]');
      await page.waitForSelector('dialog.modal [data-action="personas-retry"]');
      t.allowHttp(false);
      const words = await page.textContent('dialog.modal');
      assert(/The personas could not be read: busy, try again/.test(words) && !/No personas yet/.test(words), 'the error, not "no personas": ' + words);
      await page.click('dialog.modal [data-action="personas-retry"]');
      await page.waitForSelector('dialog.modal input[data-persona="kate"]');
      await page.unroute(/\/api\/personas$/);
      await page.keyboard.press('Escape');
      await page.waitForFunction(() => !document.querySelector('dialog.modal'));
    });

    await step(AREA + ': Receipts folder: a folder that overlaps the harness’s files is refused with the server’s words, as text, in the dialog; clearing it says there is nothing to read', async () => {
      const page = d.page;
      for (const [pg, tag] of [[page, 'desktop'], [p.page, 'phone']]) {
        await openOperator(t, pg);
        await pg.click('[data-action="folder"]');
        await pg.waitForSelector('dialog.modal #op-folder');
        await pg.fill('#op-folder', '/srv/ai-work/factory/receipts');
        t.allowHttp(true);
        await pg.click('dialog.modal button[data-action="ok"]');
        await pg.waitForSelector('dialog.modal .form-error:not([hidden])');
        t.allowHttp(false);
        const err = await pg.textContent('dialog.modal .form-error');
        assert(/operator\.receipts_dir: \/srv\/ai-work\/factory\/receipts overlaps the harness's data directory/.test(err), 'the 400, verbatim: ' + err);
        if (tag === 'phone') await noOverflow(pg, AREA + ' folder dialog phone');
        await t.areaShot(pg, AREA, 'operator-folder-refused-' + tag);
        await pg.keyboard.press('Escape');
        await pg.waitForFunction(() => !document.querySelector('dialog.modal'));
      }
      await page.click('[data-action="folder"]');
      await page.waitForSelector('dialog.modal #op-folder');
      await page.fill('#op-folder', '');
      await t.waitPost(page, /\/api\/config$/, () => page.click('dialog.modal button[data-action="ok"]'));
      await page.waitForSelector('.toast-info .toast-msg >> text=No receipts folder now');
      await page.waitForSelector('#operator-root [data-banner="no-folder"]');
      assert(await page.locator('#operator-root [data-banner="scan-problem"]').count() === 0, 'clearing it is not "cannot read"');
      await scenario(t, 'normal');
    });

    await step(AREA + ': the list says when it holds only the newest receipts, and Show older loads more (focus on the first new one when the button goes)', async () => {
      const page = d.page;
      await scenario(t, 'many');
      for (const [pg, tag] of [[page, 'desktop'], [p.page, 'phone']]) {
        await openOperator(t, pg);
        await pg.waitForSelector('#operator-root .op-cut');
        assert(/The newest 200 of 246 receipts are listed, and the counts below are of these\./.test(await pg.textContent('#operator-root .op-cut')), tag + ': the cut: ' + await pg.textContent('#operator-root .op-cut'));
        assert(await pg.locator('#operator-root .op-rcpt').count() === 200, 'the first 200');
        await noOverflow(pg, AREA + ' cut ' + tag);
        await scrollTo(pg, '#operator-root .op-cut');
        await t.areaShot(pg, AREA, 'operator-receipts-cut-' + tag);
      }
      const first = await page.$$eval('#operator-root .op-rcpt', (els) => els.length);
      await page.focus('[data-action="receipts-more"]');
      await page.keyboard.press('Enter');
      await page.waitForFunction(() => document.querySelectorAll('#operator-root .op-rcpt').length === 246);
      assert(await page.locator('#operator-root .op-cut, [data-action="receipts-more"]').count() === 0, 'all listed: no cut note, no button');
      await page.waitForFunction((n) => { const a = document.activeElement; const li = a && a.closest('.op-rcpt'); return li && Array.from(document.querySelectorAll('#operator-root .op-rcpt')).indexOf(li) === n; }, first);
      await scenario(t, 'normal');
    });

    await step(AREA + ': Activity (#/activity): who, since and until pickers; one row per entry with its source and evidence; receipts expand to not verified, verified, notes and other files; custody rows amber, never red or green; drift rows never imply no drift', async () => {
      const page = d.page;
      await scenario(t, 'normal');
      const urls = [];
      page.on('request', (r) => { if (/\/api\/activity\?/.test(r.url())) urls.push(r.url()); });
      await page.goto(t.url + '/#/activity?agent=kate&since=7d&limit=10');
      await page.waitForSelector('#operator-root .op-ent');
      assert(/^Activity: kate$/.test(await page.textContent('#operator-root .view-title')), 'the title');
      assert(await page.getAttribute('.op-tab[data-tab="activity"]', 'aria-current') === 'page', 'the Activity tab is current');
      assert(/Showing 10 of 41 entries/.test(await page.textContent('.op-act-count')), 'count: ' + await page.textContent('.op-act-count'));
      assert(/kate is the operator/.test(await page.textContent('.op-act-who')), 'operator line');
      for (let n = 20; n <= 41; n = Math.min(n + 10, 41)) {
        await page.focus('[data-action="more"]');
        await page.keyboard.press('Enter');
        await page.waitForFunction((k) => document.querySelectorAll('#operator-root .op-ent').length === k, n);
        if (n === 41) break;
        await page.waitForFunction(() => document.activeElement && document.activeElement.dataset.focus === 'act-more');   // the busy button kept focus
      }
      assert(await page.locator('[data-action="more"]').count() === 0, 'no Show more after the last page');
      await page.waitForFunction(() => document.activeElement && document.activeElement.dataset.focus === 'ent-40');   // the first new entry
      await page.waitForFunction(() => /^Showing 41 of 41 entries\.$/.test(document.querySelector('#sf-announce').textContent), null, { timeout: 3000 });   // announced once
      const pages = urls.map((u) => new URL(u).searchParams);
      assert(pages.length >= 5 && pages.slice(1).every((s) => s.get('after') && s.get('since') === '7d' && s.get('limit') === '10' && s.get('agent') === 'kate'), 'Show more keeps since, until and limit and adds after: ' + urls.join(' '));
      const rows = await page.$$eval('#operator-root .op-ent', (els) => els.map((li) => ({ source: li.dataset.source, kind: li.dataset.kind, text: li.textContent,
        tone: (li.querySelector('.op-ent-head .pill') || { className: '' }).className, ev: (li.querySelector('.op-ev code') || { textContent: '' }).textContent })));
      for (const s of ['receipt', 'tracker', 'audit', 'run', 'custody', 'drift', 'snapraid']) assert(rows.some((r) => r.source === s), 'a row from ' + s);
      assert(rows.some((r) => r.source === 'snapraid' && /Guard report/.test(r.text)) && rows.some((r) => r.source === 'snapraid' && /system:S-1/.test(r.text)), 'disk protection rows with their items');
      assert(rows.every((r) => r.ev), 'every row says where it was read');
      assert(rows.some((r) => /\/srv\/ai-work\/factory\/custody\/journal-\d{4}-\d{2}\.jsonl:\d+$/.test(r.ev)), 'file:line evidence');
      const custody = rows.filter((r) => r.source === 'custody');
      for (const k of ['lease denied', 'lease outcome unknown', 'lease unreleased', 'register restarted', 'claim outcome unknown', 'lease wait', 'lease wait gave up', 'lease lost']) {
        const r = custody.find((x) => x.kind === k);
        assert(r && /tone-warn/.test(r.tone), k + ' is amber: ' + (r && r.tone));
      }
      assert(custody.every((r) => !/tone-ok|tone-err/.test(r.tone)), 'custody never red or green');
      assert(custody.some((r) => /wake seraphina W-2210 <b>check-in<\/b>/.test(r.text)), 'a purpose stays text');
      // DESIGN §37, §40: FULL in its words, a HELD wait and how it ended, a request with no answer recorded
      assert(custody.some((r) => r.kind === 'lease denied' && /\(the card cannot fit this model now\)/.test(r.text)), 'FULL in words');
      assert(custody.some((r) => r.kind === 'lease wait' && /waits up to 120s for .*: the card is held by someone else/.test(r.text)), 'a HELD wait');
      assert(custody.some((r) => r.kind === 'claim outcome unknown' && /requested with no answer recorded/.test(r.text) && /check the register/.test(r.text)), 'a request with no answer: outcome unknown');
      const receipts = rows.filter((r) => r.source === 'receipt' && r.kind === 'change');
      assert(receipts.some((r) => /The change: outcome unknown: it may or may not have happened; check before retrying/.test(r.text)), 'her reported outcome in the activity');
      const drift = rows.filter((r) => r.source === 'drift');
      assert(drift.some((r) => r.kind === 'drift check' && /system:S-1/.test(r.text) && /Checked/.test(r.text)), 'a drift check with its items');
      assert(drift.some((r) => /Cannot read/.test(r.text) && /This check says nothing about drift\./.test(r.text)), 'cannot read says nothing about drift');
      assert(drift.every((r) => !/no drift/i.test(r.text.replace(/"no drift"/g, ''))), 'no drift row implies no drift');
      assert(rows.some((r) => r.source === 'receipt' && r.kind === 'cannot read' && /nested more than 64 levels deep/.test(r.text)), 'her cannot-read files');
      assert(await page.locator('#operator-root .op-ent b, #operator-root .op-ent img, #operator-root .op-ent script').count() === 0, 'summaries stay text');
      const moved = page.locator('.op-ent[data-source="receipt"]', { hasText: 'r-20261025-010' });
      await moved.locator('.op-ent-more > summary').click();
      const more = await moved.textContent();
      assert(/Not verified \(0\)/.test(more) && /Nothing verified|Verified, as she reports \(1\)/.test(more) && /Also in: \/home\/operator\/receipts\/inbox\/r-20261025-010\.json \(was here \(gone\)\)/.test(more), 'a moved receipt: ' + more);
      assert(more.indexOf('Not verified (0)') < more.indexOf('Verified, as she reports'), 'not verified first');
      const zero = await page.evaluate(() => { const li = document.querySelector('.op-act-srcs li.is-zero'); return li ? getComputedStyle(li).opacity : '1'; });
      assert(zero === '1', 'a zero count is dimmed by its colour only (an opacity would fall below 4.5:1): ' + zero);
      await noOverflow(page, AREA + ' activity desktop');
      await scrollTo(page, '.op-ent[data-source="receipt"] .op-ent-more[open]');
      await t.areaShot(page, AREA, 'operator-activity-receipt-desktop');
      await page.evaluate(() => document.getElementById('main').scrollTo(0, 0));
      await t.areaShot(page, AREA, 'operator-activity-desktop');
      await p.page.goto(t.url + '/#/activity?agent=kate&since=7d&limit=10');
      await p.page.waitForSelector('#operator-root .op-ent');
      await noOverflow(p.page, AREA + ' activity phone');
      await t.areaShot(p.page, AREA, 'operator-activity-phone');
      await scrollTo(p.page, '.op-ent[data-source="custody"]');
      await t.areaShot(p.page, AREA, 'operator-activity-custody-phone');
    });

    await step(AREA + ': Activity pickers apply on Show only (an arrow key on a closed list runs nothing); a span the picker has no preset for (2w) is kept as its own choice; a date to start from; a bad cursor is a 400; a failed receipt scan is a muted note', async () => {
      const page = d.page;
      await openActivity(t, page, 'agent=kate&since=7d');
      const gets = countGets(page, /^\/api\/activity\?/);
      for (const id of ['#op-act-agent', '#op-act-since', '#op-act-until']) {
        await page.focus(id);
        for (let i = 0; i < 2; i++) await page.keyboard.press('ArrowDown');
        await page.waitForTimeout(250);
        assert(await page.evaluate(() => location.hash) === '#/activity?agent=kate&since=7d', id + ': an arrow key does not load anything: ' + await page.evaluate(() => location.hash));
        assert(await page.evaluate((x) => document.activeElement === document.querySelector(x), id), id + ': focus stays on the picker, not ' + await page.evaluate(() => { const a = document.activeElement; return a ? a.tagName + '#' + a.id + '.' + a.className + ' ' + (a.dataset ? JSON.stringify(a.dataset) : '') : 'none'; }));
      }
      gets.stop();
      assert(gets.n === 0, 'no query ran while arrowing: ' + gets.n);
      assert(/Not shown yet: choose Show to see it\./.test(await page.textContent('.op-act-pending')), 'it says the choice is not shown yet');
      await openActivity(t, page, 'agent=kate&since=7d');
      await page.evaluate(() => { window.__blurs = []; document.addEventListener('focusout', (e) => { window.__blurs.push((e.target.dataset && e.target.dataset.focus) + (e.target.isConnected ? '' : ' (removed)') + ' @' + Math.round(performance.now()) + ' ' + new Error().stack.split('\n').slice(2, 5).join(' < ')); }, true); });
      await page.selectOption('#op-act-agent', 'builder');
      await page.waitForTimeout(200);
      assert(/agent=kate/.test(await page.evaluate(() => location.hash)), 'choosing is not yet showing');
      await page.click('[data-action="show"]');
      await page.waitForFunction(() => /agent=builder/.test(location.hash));
      await page.waitForFunction(() => /builder is not the operator/.test((document.querySelector('.op-act-who') || {}).textContent || ''));
      assert(/^Activity: builder$/.test(await page.textContent('#operator-root .view-title')), 'the title follows the agent');
      assert(await focusKey(page) === 'act-show', 'focus stays on Show: ' + await focusKey(page) + ' blurs: ' + JSON.stringify(await page.evaluate(() => window.__blurs)));
      const builder = await page.$$eval('#operator-root .op-ent', (els) => els.map((li) => li.dataset.source));
      assert(builder.length && builder.every((s) => s !== 'drift') && builder.indexOf('receipt') < 0, 'its own entries, no drift: ' + builder);
      // a span the server reads but the picker has no preset for
      await openActivity(t, page, 'agent=kate&since=2w');
      assert(await page.inputValue('#op-act-since') === '2w' && /^Last 2 weeks$/.test(await page.$eval('#op-act-since', (s) => s.selectedOptions[0].textContent)), 'since=2w is "Last 2 weeks"');
      assert(await page.isHidden('#op-act-since-at'), 'not an empty date field');
      await t.areaShot(page, AREA, 'operator-activity-span-desktop');
      await page.selectOption('#op-act-agent', 'seraphina');
      await page.click('[data-action="show"]');
      await page.waitForFunction(() => location.hash === '#/activity?agent=seraphina&since=2w');
      await page.waitForFunction(() => /^seraphina /.test((document.querySelector('.op-act-who') || {}).textContent || ''));
      await page.selectOption('#op-act-since', 'custom');
      await page.waitForSelector('#op-act-since-at:not([hidden])');
      await page.fill('#op-act-since-at', '2026-01-02T03:04');
      // an event that refreshes the page keeps what is being chosen
      await publish(t, { type: 'operator.receipts', data: { read: 8, new: 0, items: [] } });
      await page.waitForSelector('.op-act-note >> text=New receipts were read');
      assert(await page.inputValue('#op-act-since') === 'custom' && await page.inputValue('#op-act-since-at') === '2026-01-02T03:04', 'the pickers survive a refresh');
      assert(await focusKey(page) === 'act-since-at', 'and so does the keyboard’s place in them: ' + await focusKey(page));
      await page.click('[data-action="show"]');
      await page.waitForFunction(() => /since=\d{4}-\d{2}-\d{2}T\d{2}%3A\d{2}%3A00Z/.test(location.hash) || /since=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:00Z/.test(decodeURIComponent(location.hash)));
      await page.waitForSelector('#operator-root .op-act-who');
      t.allowHttp(true);
      const bad = await page.evaluate(() => window.SF.api('/api/activity?agent=kate&after=bm90LWEtY3Vyc29y').then(() => 'ok', (e) => e.status + ' ' + e.message));
      assert(bad === '400 after must be the next_after value an activity query returned', 'a bad cursor: ' + bad);
      await page.goto(t.url + '/#/activity?agent=kate&since=not-a-time');
      await page.waitForSelector('#operator-root .op-act-err');
      t.allowHttp(false);
      assert(/'not-a-time' is not a time/.test(await page.textContent('.op-act-err')), 'a bad time is the server’s words');
      assert(/^From not-a-time$/.test(await page.$eval('#op-act-since', (s) => s.selectedOptions[0].textContent)), 'the value is shown as it is, to be changed');
      await scenario(t, 'scan-fails');
      await page.goto(t.url + '/#/activity?agent=kate&since=24h');
      await page.waitForSelector('#operator-root .op-act-note');
      assert(/The receipts are from the last good scan: ItemsBusy: the scan failed; what was read before is used/.test(await page.textContent('#operator-root .op-act-note')), 'the receipt scan error is a note');
      await scenario(t, 'normal');
    });

    await step(AREA + ': GPU custody: residency apart from the leases (held, unknown, lost: what the register view can hold), the generation, a fenced register, stale answers and unknown outcomes in amber; read again each visit; Read now asks again and says so', async () => {
      for (const [page, tag] of [[d.page, 'desktop'], [p.page, 'phone']]) {
        await pinFixtures(t, page);
        await openOperator(t, page, '#/operator/custody');
        await page.waitForSelector('#operator-root .op-reg');
        const regs = await page.$$eval('#operator-root .op-reg', (els) => els.map((e) => e.dataset.register));
        assert(regs[0] === 'https://127.0.0.1:7300' && regs[regs.length - 1] === 'http://127.0.0.1:7500', 'the register with leases first, the one no call needs last: ' + regs);
        const gpu0 = await page.textContent('.op-reg[data-register="https://127.0.0.1:7300"]');
        assert(/Generation 42 · epoch reg-2026-10-20/.test(gpu0), 'generation and epoch');
        assert(/tabby-raw follows tabby \(the same server\): its custody is edited on tabby/.test(gpu0), 'the follower: ' + gpu0.slice(0, 300));
        const res = await page.textContent('.op-reg[data-register="https://127.0.0.1:7300"] [data-part="residency"]');
        const leases = await page.textContent('.op-reg[data-register="https://127.0.0.1:7300"] [data-part="leases"]');
        assert(/qwen3\.8-flash-next/.test(res) && /embed-small/.test(res) && !/L-8c21d0/.test(res), 'residency is its own part, without leases');
        assert(/Leases this harness holds \(3\)/.test(leases) && /Outcome unknown/.test(leases) && !/Not given back/.test(leases) && /Lost: the register restarted/.test(leases), 'lease states: ' + leases.slice(0, 200));
        const tones = await page.$$eval('.op-lease .op-lease-head .pill', (els) => els.map((e) => e.className));
        assert(tones.every((c) => !/tone-ok|tone-err/.test(c)) && tones.filter((c) => /tone-warn/.test(c)).length === 2, 'never red or green: ' + tones);
        assert(await page.locator('.op-lease b').count() === 0 && /wake seraphina W-2216 <b>check-in<\/b>/.test(leases), 'a purpose stays text');
        assert(/As this harness held it at/.test(await page.textContent('#operator-root [data-part="as-of"]')), 'when the view was read');
        await page.click('.op-reg[data-register="https://127.0.0.1:7300"] [data-disclosure] > summary');
        await page.click('.op-reg[data-register="https://127.0.0.1:7300"] [data-action="hist-all"]');
        await page.waitForSelector('.op-reg[data-register="https://127.0.0.1:7300"] .op-hist-row[data-op="epoch"]');
        const hist = await page.textContent('.op-reg[data-register="https://127.0.0.1:7300"] .op-reg-hist');
        assert(/Stale answer refused/.test(hist) && /Outcome unknown: the request was not sent again/.test(hist) && /Lease denied: another lease covers it/.test(hist) && /The register restarted/.test(hist) && /Residency could not be read/.test(hist) && /Could not be given back/.test(hist), 'history words: ' + hist.slice(0, 600));
        // FULL and HELD in words (reason_codes), the one HELD wait and how it ended, and "not sent" to a restarted register
        assert(/Lease denied: the card cannot fit this model now/.test(hist) && /Waits up to 120s: the card is held by someone else/.test(hist) && /Gave up after 120s: the card is still held/.test(hist), 'FULL and HELD: ' + hist.slice(0, 600));
        assert(/Not released: the register restarted, so it no longer knows this lease/.test(hist) && /nothing was sent: the register restarted/.test(hist), 'not sent: ' + hist.slice(0, 600));
        const waitTones = await page.$$eval('.op-reg[data-register="https://127.0.0.1:7300"] .op-hist-row[data-op="wait"] .pill', (els) => els.map((e) => e.className));
        assert(waitTones.length === 2 && waitTones.every((c) => /tone-warn/.test(c)), 'a wait is amber: ' + waitTones);
        assert(/tabby needs a lease .* · a HELD answer waits up to 120s, once per run, when no other card is free; FULL never waits/.test(gpu0) && /Its token: the systemd credential custody-token/.test(gpu0), 'the held wait and the credential: ' + gpu0.slice(0, 600));
        const strata = await page.textContent('.op-reg[data-register="http://127.0.0.1:7400"]');
        assert(/The register’s generation went back from 17 to 3/.test(strata) && /its answers are refused until it passes 17 or the harness restarts/.test(strata), 'fenced: ' + strata.slice(0, 300));
        assert(await page.locator('#operator-root [role="alert"]').count() === 0, 'the fenced banner is a plain callout');
        assert(/The register has not been read \(or cannot be: the doctor’s custody strata check says why\)/.test(strata), 'residency null');
        assert(/needs no lease/.test(await page.textContent('.op-reg[data-register="http://127.0.0.1:7500"]')), 'a provider that needs none');
        assert(/Contract proposed/.test(await page.textContent('#operator-root .op-sec-head')), 'labelled proposed');
        await noOverflow(page, AREA + ' custody ' + tag);
        await t.areaShot(page, AREA, 'operator-custody-history-' + tag);
        await page.evaluate(() => document.getElementById('main').scrollTo(0, 0));
        await t.areaShot(page, AREA, 'operator-custody-' + tag);
        if (tag === 'desktop') {
          // coming back to the tab reads it again (no event says a lease changed)
          const gets = countGets(page, /^\/api\/custody$/);
          await page.click('.op-tab[data-tab="receipts"]');
          await page.waitForSelector('#operator-root [data-card="who"]');
          await page.click('.op-tab[data-tab="custody"]');
          await page.waitForSelector('#operator-root .op-reg');
          await page.waitForTimeout(200);
          gets.stop();
          assert(gets.n === 1, 'the tab reads custody again on each visit: ' + gets.n);
          // Read now: busy at once, keeps focus, one request however often it is pressed
          const body = await hook(t, 'GET', 'custody');
          let refreshes = 0;
          await page.route(/\/api\/custody\?refresh=1$/, async (route) => { refreshes += 1; await new Promise((r) => setTimeout(r, 1200)); await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(body) }); });
          await page.focus('[data-action="read-custody"]');
          await page.keyboard.press('Enter');
          await page.waitForFunction(() => { const b = document.querySelector('[data-action="read-custody"]'); return b && b.getAttribute('aria-busy') === 'true' && /Reading…/.test(b.textContent); });
          assert(await focusKey(page) === 'read-custody', 'focus stays on Read now while it reads');
          await page.keyboard.press('Enter');
          await page.waitForFunction(() => { const b = document.querySelector('[data-action="read-custody"]'); return b && b.getAttribute('aria-busy') === 'false'; }, null, { timeout: 5000 });
          assert(refreshes === 1, 'a second press while reading sends nothing: ' + refreshes);
          await page.unroute(/\/api\/custody\?refresh=1$/);
        }
      }
      await scrollTo(p.page, '.op-reg[data-register="https://127.0.0.1:7300"] [data-part="leases"]');
      await t.areaShot(p.page, AREA, 'operator-custody-leases-phone');
    });

    await step(AREA + ': Drift checks: each status in words, last check, valid until, findings with their items; an inventory-only source says nothing about drift; only "Checked" after findings with nothing present says no drift', async () => {
      for (const [page, tag] of [[d.page, 'desktop'], [p.page, 'phone']]) {
        await pinFixtures(t, page);
        await openOperator(t, page, '#/operator/drift');
        await page.waitForSelector('#operator-root .op-drift');
        const cards = await page.$$eval('#operator-root .op-drift', (els) => Object.fromEntries(els.map((e) => [e.dataset.source, e.textContent])));
        const words = { checker: 'Checked', 'edge-proxy': 'Checked', 'role-inventory': 'Checked', inventory: 'Cannot read', 'gpu-watch': 'Check in progress', 'nas-check': 'Cannot reach it', 'cert-watch': 'Observation too old', 'disk-audit': 'Error', 'backup-checker': 'Not checked yet' };
        for (const [name, word] of Object.entries(words)) assert(cards[name] && cards[name].indexOf(word) >= 0, name + ': ' + word);
        const clear = Object.entries(cards).filter(([, txt]) => /No drift reported/.test(txt)).map(([n]) => n);
        assert(t.canon(clear) === t.canon(['edge-proxy']), 'only an ok check with findings and nothing present says no drift: ' + clear);
        assert(/Inventory only: this source has sent no findings, so it says nothing about drift\./.test(cards['role-inventory']), 'inventory only: ' + cards['role-inventory'].slice(0, 200));
        assert(/2 findings reported/.test(cards.checker) && /system:S-1/.test(cards.checker) && /Valid until/.test(cards.checker) && /generated, never applied/.test(cards.checker), 'checker: ' + cards.checker.slice(0, 200));
        assert(/Cannot read: this says nothing about drift/.test(cards.inventory) && /unknown schemaVersion '3'/.test(cards.inventory), 'cannot read');
        assert(/expired/.test(cards['cert-watch']), 'valid until has passed');
        assert(await page.locator('#operator-root .op-drift b').count() === 0, 'finding keys stay text');
        assert(/^Check now, on the tracker$/.test((await page.textContent('[data-focus="drift-tracker"]')).trim()), 'the link says where it goes');
        await noOverflow(page, AREA + ' drift ' + tag);
        await t.areaShot(page, AREA, 'operator-drift-' + tag);
      }
    });

    await step(AREA + ': Errors & capture: failed calls from the event stream with status, request ids, retried or gave up, the body behind a toggle (a chevron says so); copy for the server’s log and the capture command; a new one appears live', async () => {
      await hook(t, 'POST', 'errors');
      for (const [page, tag] of [[d.page, 'desktop'], [p.page, 'phone']]) {
        await openOperator(t, page, '#/operator/errors');
        await page.waitForFunction(() => document.querySelectorAll('#operator-root .op-err').length >= 5);
        const proc = await page.textContent('#operator-root .op-sec .proc');
        assert(/Note the time of the failure\./.test(proc) && /Capture first\./.test(proc) && /Read the bundle before sharing it\./.test(proc), 'the capture-before-fix steps');
        const rows = await page.$$eval('#operator-root .op-err', (els) => els.map((e) => ({ kind: e.dataset.kind, at: e.dataset.at, text: e.textContent, chev: !!e.querySelector('.op-err-box > summary .chev') })));
        assert(rows.every((r) => r.chev), 'every row shows it opens');
        const times = rows.map((r) => Date.parse(r.at));
        assert(times.every((x, i) => i === 0 || times[i - 1] >= x), 'newest first');
        assert(rows.some((r) => /HTTP 200, error 500 in the answer/.test(r.text) && /retried/.test(r.text)), 'an error inside a 200 answer');
        assert(rows.some((r) => /HTTP 503/.test(r.text) && /gave up/.test(r.text)), 'gave up');
        assert(rows.some((r) => /No answer/.test(r.text) && /connection lost or answer cut off/.test(r.text)), 'connection lost');
        assert(rows.some((r) => r.kind === 'tool' && /Tool Bash failed/.test(r.text)), 'a tool error');
        assert(rows.some((r) => r.kind === 'persona' && /A persona turn did not finish/.test(r.text)), 'a persona turn window');
        if (tag === 'desktop') {
          const first = page.locator('.op-err', { hasText: 'HTTP 200, error 500 in the answer' }).first();
          await first.locator('.op-err-box > summary').click();
          const body = await first.textContent();
          assert(/x-request-id: tabby-7781/.test(body) && /body\.request_id: req_8f2a91/.test(body), 'request ids: ' + body.slice(0, 400));
          await first.locator('.op-err-raw > summary').click();
          assert(/CUDA out of memory <b>while<\/b> loading/.test(await first.locator('.op-err-raw pre').textContent()), 'the body as text');
          await first.locator('[data-action="copy-log"]').click();
          const copied = await page.evaluate(() => navigator.clipboard.readText().catch(() => ''));
          assert(copied === '' || (/^failed_at \d{4}-\d{2}-\d{2}T[\d:.]+Z/.test(copied) && /x-request-id: tabby-7781/.test(copied)), 'copied for the server log: ' + copied);
          const cmd = await first.locator('.op-cmd').textContent();
          assert(/^factory capture --since \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z --until now --out ~\/captures\/\d{4}-\d{2}-\d{2}-tabby$/.test(cmd), 'capture command: ' + cmd);
          const big = page.locator('.op-err', { hasText: 'HTTP 503' }).first();
          await big.locator('.op-err-box > summary').click();
          assert(/first 16384 characters/.test(await big.locator('.op-err-raw > summary').textContent()), 'a cut body says so');
          assert(await page.locator('#operator-root .op-err b, #operator-root .op-err script, #operator-root .op-err img').count() === 0, 'bodies and outputs stay text');
          const before = rows.length;
          await publish(t, { type: 'agent.error', agent: 'builder', repo: 'my-app', job: '003-fix-the-flaky-checkout-test',
            data: { kind: 'tool', name: 'Edit', id: 'call_live', output: 'no such file', output_truncated: false, started_at: new Date(Date.now() - 1000).toISOString(), failed_at: new Date(Date.now() + 60000).toISOString(), elapsed_ms: 1000 } });
          await page.waitForFunction((n) => document.querySelectorAll('#operator-root .op-err').length === n + 1, before);
          assert(/Tool Edit failed/.test(await page.textContent('#operator-root .op-err')), 'the live one is first');
        }
        await noOverflow(page, AREA + ' errors ' + tag);
        await t.areaShot(page, AREA, 'operator-errors-' + tag);
      }
    });

    await step(AREA + ': nobody marked but a persona can be: the rail entry, a neutral Home card and "Choose the operator…" lead to the first mark; a fresh install with no persona, register or drift source leaves the page out', async () => {
      await scenario(t, 'none');
      for (const [pg, tag] of [[d.page, 'desktop'], [p.page, 'phone']]) {
        await pg.goto(t.url + '/#/home');
        await pg.reload();
        await pg.waitForFunction(() => /No operator yet/.test((document.querySelector('.home-card[data-section="operator"]') || {}).textContent || ''));
        assert(await pg.getAttribute('.home-card[data-section="operator"] [data-action="open"]', 'href') === '#/operator', tag + ': Home leads to the page');
        if (tag === 'desktop') assert(await pg.isVisible('.rail-item[data-view="operator"]'), 'the rail entry shows with nobody marked');
        await scrollTo(pg, '.home-card[data-section="operator"]');
        await t.areaShot(pg, AREA, 'operator-home-none-' + tag);
        await pg.click('.home-card[data-section="operator"] [data-action="open"]');
        await pg.waitForSelector('#operator-root .op-banner[data-banner="none"]');
        assert(/No persona is the operator\./.test(await pg.textContent('.op-banner[data-banner="none"]')), 'says nobody is');
        await pg.waitForSelector('[data-action="mark"]');
        await noOverflow(pg, AREA + ' none ' + tag);
        await t.areaShot(pg, AREA, 'operator-none-' + tag);
      }
      const page = d.page;
      await page.click('[data-action="mark"]');
      await page.waitForSelector('dialog.modal input[data-persona="kate"]');
      await page.check('input[data-persona="kate"]');
      const first = await t.waitPost(page, /\/api\/config$/, () => page.click('dialog.modal button[data-action="ok"]'));
      assert(t.canon(JSON.parse(first.request().postData())) === t.canon({ agents: { kate: { role: 'operator' } } }), 'the first mark: ' + first.request().postData());
      await page.waitForSelector('.op-who-list li[data-operator="kate"]');
      await scenario(t, 'normal');
      const l = await t.newPage(AREA + '-light', { colorScheme: 'light' });
      try {
        await openOperator(t, l.page);
        await l.page.waitForSelector('#operator-root .op-rcpt');
        await t.areaShot(l.page, AREA, 'operator-receipts-light-desktop');
        await l.page.goto(t.url + '/#/activity?agent=kate&since=7d');
        await l.page.waitForSelector('#operator-root .op-ent');
        await t.areaShot(l.page, AREA, 'operator-activity-light-desktop');
      } finally {
        await l.ctx.close();
      }
      const fresh = await t.startMock(['--fresh']);
      try {
        for (const phone of [false, true]) {
          const f = await t.newPage(AREA + '-fresh' + (phone ? '-phone' : ''), { mockUrl: fresh.url, phone });
          try {
            await f.page.goto(fresh.url + '/#/home');
            await f.page.waitForSelector('#view-home:not([hidden]) .home-card[data-section="factory"]');
            await f.page.waitForTimeout(600);
            assert(await f.page.locator('.home-card[data-section="operator"]').count() === 0, 'no operator card on a fresh install');
            if (!phone) assert(await f.page.isHidden('.rail-item[data-view="operator"]'), 'no rail entry on a fresh install');
            await f.page.goto(fresh.url + '/#/operator');
            await f.page.waitForSelector('#operator-root .op-banner[data-banner="none"]');
            assert(/No persona is the operator\./.test(await f.page.textContent('.op-banner[data-banner="none"]')), 'the page itself still works');
            await f.page.waitForFunction(() => /No receipts yet/.test(document.getElementById('operator-root').textContent));   // an empty list says what comes
            await noOverflow(f.page, AREA + ' fresh');
            await t.areaShot(f.page, AREA, 'operator-fresh-' + (phone ? 'phone' : 'desktop'));
          } finally {
            await f.ctx.close();
          }
        }
      } finally {
        fresh.proc.kill();
      }
    });
  } finally {
    await scenario(t, 'normal').catch(() => {});
    await d.ctx.close();
    await p.ctx.close();
  }
}
