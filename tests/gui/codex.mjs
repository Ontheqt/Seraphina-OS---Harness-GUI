// Checks for the Codex worker (DESIGN §44; docs/seraphina/BROWSER_AND_CODEX.md §3.3, §3.6, §3.7, §3.12, §4.4), phase
// BC1b: the view module swfactory/web/views/codex.js against tests/gui_mock/codex.py (real swfactory/codex receipts
// over a temporary data directory), run by tests/gui/smoke.mjs (AREA_SUITES), or alone with GUI_ONLY=codex node
// tests/gui/smoke.mjs. Screenshots at 1440x900 and 390x844, light and dark: t.areaShot(page, AREA, name).

const AREA = 'codex';
const FIXED = 'my-app/051-fix-the-greeting';
const TIMED = 'my-app/052-rename-the-greeting';

async function setState(t, page, state) {
  const r = await page.request.post(t.url + '/__mock/codex/' + state, { data: {} });
  t.assert(r.status() === 200, 'mock state ' + state + ': ' + r.status());
}
async function openReceipts(page, url, job) {
  await page.goto(url + '/#/job/' + job + '/receipts');
  await page.reload();
  await page.waitForSelector('#panel-receipts:not([hidden]) .cx-receipt');
}
async function openJob(page, url, job) {
  await page.goto(url + '/#/job/' + job);
  await page.reload();
  await page.waitForSelector('.drawer .cx-chip');
}

export async function run(t) {
  const { step, assert, noOverflow } = t;
  const d = await t.newPage(AREA);
  const dl = await t.newPage(AREA + '-light', { colorScheme: 'light' });
  const p = await t.newPage(AREA + '-phone', { phone: true });
  const pd = await t.newPage(AREA + '-phone-dark', { phone: true, colorScheme: 'dark' });
  const page = d.page;
  try {
    await step(AREA + ': the Receipts tab: each Codex run\'s outcome, the harness commit, the checks, the paths as text, the chain', async () => {
      await setState(t, page, 'on');
      await openReceipts(page, t.url, FIXED);
      const rows = await page.$$eval('.cx-receipt', (els) => els.map((e) => ({ run: e.dataset.run, outcome: e.dataset.outcome, text: e.textContent })));
      assert(rows.length === 2, 'two receipts: ' + rows.length);
      assert(/✗ verify · exit 1/.test(rows[0].text), 'round 1: verify failed: ' + rows[0].text);
      assert(/✓ verify · exit 0/.test(rows[1].text) && /harness commit b2c3d4e5f6/.test(rows[1].text), 'round 2: committed, verify ok');
      assert(/✓ write policy/.test(rows[1].text) && /✓ secret scan/.test(rows[1].text), 'the write-policy check and the secret scan');
      assert(/Codex 0\.163\.0 · exec · factory_worker · never/.test(rows[1].text), 'the version, surface, profile and policy');
      assert(rows.every((r) => /home checked/.test(r.text)), 'the home was checked before each spawn');
      assert(rows[1].text.includes("templates/<img src=x onerror=alert('cx')>.html"), 'a path is text');
      assert(await page.locator('#panel-receipts img').count() === 0, 'never markup');
      assert(/hash chain holds/.test(await page.textContent('#panel-receipts')), 'the chain is checked');
      await page.click('.cx-receipt:nth-child(2) .cx-raw summary');
      await page.waitForFunction(() => /swf\.receipt\.run\/1/.test((document.querySelector('.cx-receipt:nth-child(2) .cx-json') || {}).textContent || ''));
      await t.areaShot(page, AREA, 'receipts-desktop-dark');
      await openReceipts(dl.page, t.url, FIXED);
      await t.areaShot(dl.page, AREA, 'receipts-desktop-light');
      for (const [pg, name] of [[p.page, 'receipts-phone-light'], [pd.page, 'receipts-phone-dark']]) {
        await openReceipts(pg, t.url, FIXED);
        await noOverflow(pg, AREA + ' ' + name);
        await t.areaShot(pg, AREA, name);
      }
    });

    await step(AREA + ': the run cards say Codex; Verify in the browser opens a browsing chat on the job\'s preview', async () => {
      await openJob(page, t.url, FIXED);
      assert(await page.locator('.drawer .cx-chip').count() >= 2, 'a Codex chip on each builder run');
      const verify = page.locator('[data-part="verify"] [data-action="verify-in-browser"]');
      await verify.waitFor();
      assert(/Never a merge gate/.test(await page.textContent('[data-part="verify"]')), 'it is never a merge gate');
      await t.areaShot(page, AREA, 'verify-desktop-dark');
      await openJob(dl.page, t.url, FIXED);
      await dl.page.waitForSelector('[data-part="verify"]');
      await t.areaShot(dl.page, AREA, 'verify-desktop-light');
      for (const [pg, name] of [[p.page, 'verify-phone-light'], [pd.page, 'verify-phone-dark']]) {
        await openJob(pg, t.url, FIXED);
        await pg.waitForSelector('[data-part="verify"]');
        await noOverflow(pg, AREA + ' ' + name);
        await t.areaShot(pg, AREA, name);
      }
      const resp = await t.waitPost(page, /\/api\/jobs\/my-app\/051-fix-the-greeting\/verify-in-browser$/, () => verify.click());
      assert(resp.status() === 201, 'the chat is made: ' + resp.status());
      await page.waitForFunction(() => location.hash === '#/chat/c-c0de0e0001');
    });

    await step(AREA + ': a refused Verify in the browser says the server\'s reason, word for word', async () => {
      const why = 'Verify in the browser is not offered: the job\'s worktree is gone';
      const route = /\/api\/jobs\/my-app\/051-fix-the-greeting\/verify-in-browser$/;
      await page.route(route, (r) => r.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ error: why }) }));
      t.allowHttp(true);   // the 409 is this check's own
      try {
        await openJob(page, t.url, FIXED);
        await page.locator('[data-part="verify"] [data-action="verify-in-browser"]').click();
        await page.waitForSelector('.toast-error .toast-msg');
        const text = (await page.textContent('.toast-error .toast-msg')).trim();
        assert(text === why, 'the toast is the reason itself: ' + JSON.stringify(text));
        await page.click('.toast-error .toast-x');
      } finally {
        await page.unroute(route);
        t.allowHttp(false);
      }
    });

    await step(AREA + ': a run that hit the wall clock: timed out, reconciled, nothing committed, Retry starts from what it left', async () => {
      await openJob(page, t.url, TIMED);
      await page.waitForSelector('[data-part="reconcile"]');
      const text = await page.textContent('[data-part="reconcile"]');
      assert(/The Codex run ended timed out/.test(text) && /left changes in app\.py, notes\.txt/.test(text), 'the reconcile: ' + text);
      assert(/Nothing was committed and nothing is sent again/.test(text), 'never replayed');
      await page.waitForSelector('.drawer .cx-outcome');
      const outcomes = await page.$$eval('.drawer .cx-outcome', (els) => els.map((e) => e.textContent));
      assert(outcomes.length >= 1 && outcomes.every((x) => x === 'timed out'), 'the outcome on the run: ' + JSON.stringify(outcomes));
      assert(await page.locator('[data-part="verify"]').count() === 0, 'no verify without a preview offer');
      await t.areaShot(page, AREA, 'reconcile-desktop-dark');
      await openJob(pd.page, t.url, TIMED);
      await pd.page.waitForSelector('[data-part="reconcile"]');
      await noOverflow(pd.page, AREA + ' reconcile phone');
      await t.areaShot(pd.page, AREA, 'reconcile-phone-dark');
    });

    await step(AREA + ': the Codex page: the pin, the user, the Responses checks and the owner\'s setup blocks (nothing applied)', async () => {
      await page.goto(t.url + '/#/codex');
      await page.reload();
      await page.waitForSelector('#codex-root .cx-block');
      const text = await page.textContent('#codex-root');
      assert(/0\.163\.0/.test(text), 'the pin');
      assert(/Nothing here is applied by the harness/.test(text), 'nothing is applied');
      assert(await page.locator('.cx-block').count() === 9, 'nine blocks, staging first');
      assert(/requirements\.toml is not installed \(O9\)/.test(text), 'the requirements file is off');
      assert(/Accepted by the owner/.test(text), 'the residual risk');
      await t.areaShot(page, AREA, 'page-desktop-dark');
      await dl.page.goto(t.url + '/#/codex');
      await dl.page.waitForSelector('#codex-root .cx-block');
      await t.areaShot(dl.page, AREA, 'page-desktop-light');
      for (const [pg, name] of [[p.page, 'page-phone-light'], [pd.page, 'page-phone-dark']]) {
        await pg.goto(t.url + '/#/codex');
        await pg.waitForSelector('#codex-root .cx-block');
        await noOverflow(pg, AREA + ' ' + name);
        await t.areaShot(pg, AREA, name);
      }
    });
  } finally {
    for (const x of [d, dl, p, pd]) await x.ctx.close();
  }
}
