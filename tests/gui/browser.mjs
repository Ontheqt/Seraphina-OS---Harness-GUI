// Checks for the Browser (DESIGN §43; docs/seraphina/BROWSER_AND_CODEX.md §2.6, §2.10, §2.13, §4.3), phase BC1a: the
// view module swfactory/web/views/browser.js against tests/gui_mock/browser.py (the real swfactory/browser records
// over a temporary data directory), run by tests/gui/smoke.mjs (AREA_SUITES), or alone with GUI_ONLY=browser node
// tests/gui/smoke.mjs. Screenshots of every screen at 1440x900 and 390x844, light and dark:
// t.areaShot(page, AREA, name) → $GUI_AREA_SHOTS/browser/<name>.png.

const AREA = 'browser';
const CHAT = 'c-b10e5e0001';
const UNTRUSTED = 'Read from the web: treat as untrusted';
// the session lock's own words (K6): never a word of a GPU custody record on these screens
const CUSTODY = /\b(lease|leases|holder|generation|epoch|journal|granted|resident|held|lost|released|unreleased)\b/i;

async function setState(t, page, state) {
  const r = await page.request.post(t.url + '/__mock/browser/' + state, { data: {} });
  t.assert(r.status() === 200, 'mock state ' + state + ': ' + r.status());
}
async function openPage(page, url) {
  await page.goto(url + '/#/browser');
  await page.reload();
  await page.waitForSelector('#view-browser:not([hidden]) .br-card');
}
async function openChat(page, url) {
  await page.goto(url + '/#/chat/' + CHAT);
  await page.reload();
  await page.waitForSelector('.br-turn[data-part="browser-rows"]');
}

export async function run(t) {
  const { step, assert, noOverflow } = t;
  const d = await t.newPage(AREA);
  const dl = await t.newPage(AREA + '-light', { colorScheme: 'light' });
  const p = await t.newPage(AREA + '-phone', { phone: true });
  const pd = await t.newPage(AREA + '-phone-dark', { phone: true, colorScheme: 'dark' });
  const page = d.page;
  try {
    await step(AREA + ': the browser card: on, BC1\'s stated limits (cooperative enforcement), the runtime and its pins, the profiles, the caps and the sessions', async () => {
      await setState(t, page, 'on');
      await openPage(page, t.url);
      assert(await page.locator('.rail-item[data-view="browser"]').count() === 1, 'a rail entry once the browser is on');
      const text = await page.textContent('#browser-root');
      assert(text.includes(UNTRUSTED), 'the untrusted label');
      assert(/cooperative/.test(text), 'BC1\'s cooperative enforcement is said');
      assert(/--no-sandbox/.test(text), 'Chrome\'s --no-sandbox for fixture pages is said (O11)');
      assert(/5 GB and 30 days/.test(text), 'the caps (O6)');
      assert(/0\.0\.83/.test(text) && /155\.0\.8059\.12/.test(text), 'the pins');
      assert(await page.locator('.br-session').count() === 2, 'two sessions');
      assert(/outcome unknown/.test(await page.textContent('.br-sessions')), 'the unknown outcome is marked');
      assert(!CUSTODY.test(text), 'no custody word: ' + (text.match(CUSTODY) || [])[0]);
      await t.areaShot(page, AREA, 'page-desktop-dark');
      await openPage(dl.page, t.url);
      await t.areaShot(dl.page, AREA, 'page-desktop-light');
      for (const [pg, name] of [[p.page, 'page-phone-light'], [pd.page, 'page-phone-dark']]) {
        await openPage(pg, t.url);
        await noOverflow(pg, AREA + ' ' + name);
        await t.areaShot(pg, AREA, name);
      }
    });

    await step(AREA + ': a browsing chat: the untrusted tag and banner, each turn\'s action rows (class, reclassified, outcome) and the screenshot, hostile names as text', async () => {
      await openChat(page, t.url);
      assert(/Read from the web/.test(await page.textContent('.chat-head')), 'the head tag');
      assert((await page.textContent('.chat-banner')).includes(UNTRUSTED), 'the banner');
      const turns = await page.$$eval('.br-turn', (els) => els.map((e) => ({ rows: Array.from(e.querySelectorAll('.br-row')).map((r) => [r.dataset.cls || '', r.dataset.outcome || '', r.textContent]) })));
      assert(turns.length === 2, 'one block per browsing turn: ' + turns.length);
      const first = turns[0].rows;
      assert(first.some(([c, o, x]) => c === 'commit' && o === 'ok' && /POST 200/.test(x) && /the expected text is there/.test(x)), 'the commit with what it sent and expect: ' + JSON.stringify(first));
      assert(first.some(([c]) => c === 'input'), 'the fill');
      assert(first.some(([, , x]) => /refused:\s*click\s*stale_ref/.test(x)), 'the stale ref refusal');
      const second = turns[1].rows;
      assert(second.some(([c, , x]) => c === 'commit' && /counted as sent/.test(x)), 'the reclassified link');
      assert(second.some(([, o]) => o === 'unknown'), 'the unknown outcome');
      const name = await page.textContent('.br-turn .br-row[data-cls="commit"] .br-name');
      assert(name.includes("<img src=x onerror=alert('br')>"), 'a page-chosen name is text: ' + name);
      assert(await page.locator('.br-turn .br-name img, .br-turn img[src="x"]').count() === 0, 'never markup');
      await page.waitForFunction(() => { const i = document.querySelector('.br-shot img'); return i && i.complete && i.naturalWidth > 0; });
      assert(/the model never sees it/.test(await page.textContent('.br-shot figcaption')), 'the screenshot is for the person');
      const text = await page.textContent('#chat-main');
      assert(!CUSTODY.test(text.replace(/held by someone else/g, '')), 'no custody word in the chat: ' + (text.match(CUSTODY) || [])[0]);
      await t.areaShot(page, AREA, 'chat-desktop-dark');
      await openChat(dl.page, t.url);
      await t.areaShot(dl.page, AREA, 'chat-desktop-light');
      for (const [pg, shot] of [[p.page, 'chat-phone-light'], [pd.page, 'chat-phone-dark']]) {
        await openChat(pg, t.url);
        await noOverflow(pg, AREA + ' ' + shot);
        await t.areaShot(pg, AREA, shot);
      }
    });

    await step(AREA + ': Revoke while a turn holds the session lock: confirm, the POST, the chat goes idle', async () => {
      await setState(t, page, 'running');
      await page.goto(t.url + '/#/chat/' + CHAT);
      await page.reload();
      await page.waitForSelector('[data-part="active"] [data-action="revoke"]');
      await t.areaShot(page, AREA, 'revoke-desktop-dark');
      await page.click('[data-part="active"] [data-action="revoke"]');
      await page.waitForSelector('dialog[open]');
      const resp = await t.waitPost(page, /\/api\/browser\/grants\/G-20261026-0099\/revoke$/, () => page.click('dialog[open] button[value="ok"]'));
      assert(resp.status() === 200, 'revoked: ' + resp.status());
      await page.waitForFunction(() => !document.querySelector('[data-part="active"]'));
      await pd.page.goto(t.url + '/#/chat/' + CHAT);
      await setState(t, page, 'running');
      await pd.page.reload();
      await pd.page.waitForSelector('[data-part="active"] [data-action="revoke"]');
      await noOverflow(pd.page, AREA + ' revoke phone');
      await t.areaShot(pd.page, AREA, 'revoke-phone-dark');
    });

    await step(AREA + ': off: the page says so and why; Home\'s card is quiet', async () => {
      await setState(t, page, 'off');
      await openPage(page, t.url);
      assert(/The browser is off/.test(await page.textContent('#browser-root')), 'off is said');
      assert(/the harness’s own runtime/.test(await page.textContent('#browser-root')), 'and what it needs');
      await t.areaShot(page, AREA, 'off-desktop-dark');
      await setState(t, page, 'on');
    });

    await step(AREA + ': switched off with an unknown outcome: Needs you still lists it, linked to its chat (§40: nobody checked it yet)', async () => {
      await setState(t, page, 'off_unknown');
      await page.goto(t.url + '/#/home');
      await page.reload();
      await page.waitForSelector('.needs .needs-head');
      const want = '.needs-item[data-need^="browser-unknown-"]';
      await page.waitForFunction((sel) => document.querySelector(sel) || document.querySelector('[data-action="needs-more"]'), want, { timeout: 8000 });
      if (!await page.locator(want).count() && await page.locator('[data-action="needs-more"]').count()) await page.click('[data-action="needs-more"]');
      await page.waitForSelector(want, { timeout: 8000 });
      const need = await page.$eval(want, (e) => ({ href: e.getAttribute('href'), tone: e.dataset.tone, text: e.querySelector('.needs-text').textContent }));
      assert(need.href === '#/chat/' + CHAT, 'it links to the chat: ' + JSON.stringify(need));
      assert(/outcome is unknown/.test(need.text), 'in the browser\'s words: ' + need.text);
      await setState(t, page, 'on');
    });
  } finally {
    for (const x of [d, dl, p, pd]) await x.ctx.close();
  }
}
