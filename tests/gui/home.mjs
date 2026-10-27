// Seraphina OS Harness foundation checks for tests/gui/smoke.mjs: the branding, Home (the Grid Card of the sections,
// the planned v2 cards, "Needs you"), the view registry (window.SF, routes, the rail, SF.drawer, SF.on) and a fresh
// install, at 1440 px (dark) and 390 px (light). Screenshots go to $GUI_AREA_SHOTS/home/.
//
// run(t) gets the harness of smoke.mjs: { browser, url, startMock, step, assert, canon, waitPost, shot, cleanShot,
// noOverflow, watch, allowHttp, newPage(label, {phone, width, height, colorScheme, mockUrl}), areaShot(page, area, name) }.

const AREA = 'home';
const LIVE = ['yourserver', 'factory', 'tracker', 'personas', 'messages', 'backups', 'operator'];
const PLANNED = ['maintenance', 'upgrades', 'research', 'audit', 'model-factory'];
const DECISION = 'item:job:my-app/004-render-user-notes/I-2';   // 002's moves to follow-ups when the core suite approves it (carry_items)

/** Home's live cards (n of them) all show their status. */
const loaded = (page, n = LIVE.length) => page.waitForFunction((count) => {
  const cards = Array.from(document.querySelectorAll('#home-root .home-grid:not(.is-planned) .home-card'));
  return cards.length === count && cards.every((c) => { const s = c.querySelector('.home-status-text'); return s && s.textContent && s.textContent !== 'checking…'; });
}, n, { timeout: 10000 });

export async function run(t) {
  const { step, assert, noOverflow } = t;
  const d = await t.newPage('home');
  const p = await t.newPage('home-phone', { phone: true });
  const page = d.page;
  try {
    await step('Home: Seraphina OS Harness branding; the sections as a Grid Card in order, each with a sentence, live status, routes and Open; five planned v2 cards', async () => {
      await page.goto(t.url + '/');
      await page.waitForSelector('#view-home:not([hidden]) .home-card');
      await loaded(page);
      assert(/^(\(\d+\) )?Home · Seraphina OS Harness$/.test(await page.title()), 'title: ' + await page.title());
      assert(await page.textContent('.brand-name') === 'Seraphina OS Harness' && await page.getAttribute('.brand', 'href') === '#/home', 'the header names the product and goes Home');
      assert(/^data:image\/svg\+xml,/.test(await page.getAttribute('link[rel="icon"]', 'href')), 'a favicon with the product’s letter');
      const live = await page.$$eval('#home-root .home-grid:not(.is-planned) .home-card', (els) => els.map((e) => e.dataset.section));
      assert(t.canon(live) === t.canon(LIVE), 'live sections in order: ' + live);
      const cards = await page.$$eval('#home-root .home-grid:not(.is-planned) .home-card', (els) => els.map((e) => ({
        id: e.dataset.section, text: e.querySelector('.home-card-text').textContent, status: e.querySelector('.home-status-text').textContent,
        open: e.querySelector('[data-action="open"]').getAttribute('href'), routes: (e.querySelector('.home-card-routes') || {}).textContent || '' })));
      const hrefs = { yourserver: '#/apps', factory: '#/factory', tracker: '#/housekeeping', personas: '#/personas', messages: '#/messages', backups: '#/backups', operator: '#/operator' };
      for (const c of cards) {
        assert(/\.$/.test(c.text) && c.text.length > 30, c.id + ': one plain sentence: ' + c.text);
        assert(c.status.length > 2, c.id + ': a live status: ' + c.status);
        assert(c.open === hrefs[c.id], c.id + ': Open goes to ' + c.open);
        assert(/ → /.test(c.routes), c.id + ': a labelled route: ' + c.routes);
      }
      assert(/2 unread messages/.test(cards.find((c) => c.id === 'messages').status), 'the inbox count from GET /api/messages');
      assert(/kate is the operator/.test(cards.find((c) => c.id === 'operator').status), 'the operator from GET /api/operator');
      const planned = await page.$$eval('#home-root .home-grid.is-planned .home-card', (els) => els.map((e) => ({ id: e.dataset.section,
        tag: e.querySelector('.tag').textContent, doc: e.querySelector('.home-doc code').textContent, text: e.querySelector('.home-card-text').textContent })));
      assert(t.canon(planned.map((x) => x.id)) === t.canon(PLANNED), 'planned v2 sections: ' + planned.map((x) => x.id));
      assert(planned.every((x) => x.tag === 'Planned' && x.doc === 'docs/seraphina/SERAPHINA_OS_HARNESS.md' && /\.$/.test(x.text)), 'each planned card: Planned, one sentence, the design document');
      assert(await page.locator('.home-proc ol.proc li').count() === 3, 'How Home works, as a numbered procedure');
      const rail = await page.$$eval('.rail .rail-item:not([hidden])', (els) => els.map((e) => e.dataset.view));
      assert(t.canon(rail) === t.canon(['home', 'yourserver', 'factory', 'plans', 'tracker', 'chat', 'messages', 'backups', 'operator', 'personas', 'browser', 'codex', 'agents', 'settings', 'grid', 'procedures', 'doctor']), 'rail: ' + rail);
      assert(await page.getAttribute('.rail-item[data-view="home"]', 'aria-current') === 'page', 'Home is marked current in the rail');
      assert(await page.textContent('.rail-item[data-view="messages"] .rail-badge') === '2', 'the Messages entry counts unread messages');
      await t.areaShot(page, AREA, 'home-desktop');
    });

    await step('Needs you: jobs and plans that stopped for you, a product decision, unread messages, a persona that cannot start and a fenced GPU register, from every section, the 4 most urgent first; their text stays text; Enter opens a job over Home and Esc comes back', async () => {
      await page.goto(t.url + '/#/home');
      await loaded(page);
      const total = Number(await page.textContent('.needs .needs-head .count-pill'));
      assert(total >= 3, 'something waits for you: ' + total);
      assert(await page.locator('.needs-list .needs-item').count() === Math.min(total, 4), 'at most four before Show all');
      if (total > 4) {
        await page.click('[data-action="needs-more"]');
        await page.waitForFunction((n) => document.querySelectorAll('.needs-list .needs-item').length === n, total);
        assert(await page.getAttribute('[data-action="needs-more"]', 'aria-expanded') === 'true', 'Show all says it expanded');
      }
      const needs = await page.$$eval('.needs-list .needs-item', (els) => els.map((e) => ({ key: e.dataset.need, href: e.getAttribute('href'), tone: e.dataset.tone, text: e.querySelector('.needs-text').textContent, where: e.querySelector('.needs-where').textContent })));
      // one strip fed by every section: each names where it comes from (Placement has no card of its own)
      const where = new Set(needs.map((n) => n.where));
      for (const w of ['Software Factory', 'Housekeeping Tracker', 'Messages & Wakes', 'Personas', 'Placement']) assert(where.has(w), 'Needs you has ' + w + '’s: ' + Array.from(where));
      assert(needs.some((n) => n.key === 'custody:fenced:strata' && n.href === '#/placement' && n.tone === 'warn'), 'a fenced GPU register, from Placement');
      assert(needs.some((n) => /^persona:error:/.test(n.key) && /cannot start$/.test(n.text)), 'a persona that cannot start, from Personas');
      assert(new Set(needs.map((n) => n.key)).size === needs.length, 'each need once');
      await page.$eval('.needs', (e) => e.scrollIntoView({ block: 'start' }));
      await t.areaShot(page, AREA, 'home-needs-all-desktop');
      assert(needs.some((n) => n.key === DECISION && n.href === '#/job/my-app/004-render-user-notes'), 'the product decision links to its job: ' + JSON.stringify(needs));
      assert(needs.some((n) => n.key === 'inbox' && n.href === '#/messages' && /unread message/.test(n.text)), 'unread messages are listed');
      assert(needs.some((n) => /^(job|plan):/.test(n.key)), 'a job or plan that stopped for you is listed');
      const ranks = { err: 0, human: 1, warn: 2 };
      assert(needs.every((n, i) => i === 0 || (ranks[needs[i - 1].tone] ?? 1) <= (ranks[n.tone] ?? 1)), 'most urgent first: ' + needs.map((n) => n.tone));
      assert(await page.locator('#home-root img, #home-root script').count() === 0, 'no markup from item or job text is rendered');
      assert(needs.find((n) => n.key === DECISION).text.includes("<img src=x onerror=alert('decision')>"), 'the item title shows as written');
      await page.focus('[data-need="' + DECISION + '"]');
      await page.keyboard.press('Enter');
      await page.waitForSelector('#drawer:not([hidden]) #drawer-title');
      assert(await page.evaluate(() => location.hash) === '#/job/my-app/004-render-user-notes', 'Enter opens the job');
      assert(await page.isVisible('#view-home'), 'Home stays behind the drawer');
      await page.keyboard.press('Escape');
      await page.waitForFunction(() => location.hash === '#/home' && document.getElementById('drawer').hidden);
      await page.waitForFunction((k) => document.activeElement && document.activeElement.dataset.need === k, DECISION, { timeout: 3000 });
    });

    await step('View modules: each route renders inside its own section, the rail marks it, unknown routes go Home; window.SF is frozen; SF.drawer keeps text as text and gives focus back; SF.on hears a new event type live', async () => {
      const modules = { '#/housekeeping': 'tracker', '#/personas/kate': 'personas', '#/messages/m/M-000003': 'messages', '#/schedule': 'messages', '#/operator': 'operator', '#/activity?agent=kate': 'operator', '#/placement': 'routing', '#/backups': 'backups' };
      for (const [hash, id] of Object.entries(modules)) {
        await page.goto(t.url + '/' + hash);
        await page.waitForSelector('#view-' + id + ':not([hidden]) #' + id + '-root .view-title');
        const shown = await page.$$eval('#main > .view:not([hidden])', (els) => els.map((e) => e.id));
        assert(t.canon(shown) === t.canon(['view-' + id]), hash + ' shows only its section: ' + shown);
        const rail = await page.$('.rail-item[data-view="' + id + '"]');
        if (rail) assert(await rail.getAttribute('aria-current') === 'page', hash + ': its rail entry is current');
      }
      assert(/Activity: kate/.test(await page.textContent('#operator-root .view-title')), 'a route’s ?query reaches the module');
      await page.goto(t.url + '/#/no-such-page');
      await page.waitForSelector('#view-home:not([hidden])');
      const frozen = await page.evaluate(() => {
        const out = { frozen: Object.isFrozen(window.SF), replaced: false, dup: '' };
        try { window.SF = {}; } catch (e) { /* strict mode */ }
        out.replaced = window.SF.registerView === undefined;
        try { window.SF.registerView({ id: 'tracker', render() {} }); } catch (e) { out.dup = e.message; }
        return out;
      });
      assert(frozen.frozen && !frozen.replaced && /exists/.test(frozen.dup), 'SF is frozen and a second view called tracker is refused: ' + JSON.stringify(frozen));
      await page.focus('.home-head [data-focus="doctor"]');
      await page.evaluate(() => window.SF.drawer.open({ key: 'smoke', title: 'A panel <b>title</b>', sub: 'sub', body: ['<img src=x onerror=alert("panel")>', window.SF.h('p', { class: 'smoke-p' }, 'text')], back: '#/home' }));
      await page.waitForSelector('#drawer.is-panel:not([hidden]) .smoke-p');
      assert(await page.locator('#drawer img').count() === 0 && (await page.textContent('#drawer-title')).includes('<b>'), 'the panel keeps text as text');
      assert(await page.evaluate(() => document.getElementById('app').hasAttribute('inert') && document.activeElement.id === 'drawer-title'), 'modal, focus on its title');
      await page.waitForTimeout(300);   // the drawer slides in
      await t.areaShot(page, AREA, 'panel-desktop');
      await page.keyboard.press('Escape');
      await page.waitForFunction(() => document.getElementById('drawer').hidden && !document.getElementById('app').hasAttribute('inert'));
      assert(await page.evaluate(() => document.activeElement && document.activeElement.dataset.focus === 'doctor'), 'focus goes back to the opener');
      await page.evaluate(() => { window.__heard = []; window.SF.on('smoke.ping', (ev) => window.__heard.push(ev.data.n)); });
      const r = await page.request.post(t.url + '/__mock/publish', { data: { type: 'smoke.ping', data: { n: 7 } } });
      assert(r.status() === 200, 'publish ' + r.status());
      await page.waitForFunction(() => window.__heard.length === 1 && window.__heard[0] === 7, null, { timeout: 8000 });
    });

    await step('Home at 390 px: Needs you first, one column of cards, the bottom bar holds Home, Your server and Messages, no sideways scroll', async () => {
      const m = p.page;
      await m.goto(t.url + '/#/home');
      await loaded(m);
      await noOverflow(m, 'home 390');
      const lefts = await m.$$eval('#home-root .home-card', (els) => Array.from(new Set(els.map((e) => Math.round(e.getBoundingClientRect().left)))));
      assert(lefts.length === 1, 'one column: ' + lefts);
      const order = await m.evaluate(() => [document.querySelector('.needs').getBoundingClientRect().top, document.querySelector('.home-grid').getBoundingClientRect().top, document.getElementById('main').scrollTop, document.activeElement && (document.activeElement.id || document.activeElement.className)]);
      assert(order[0] < order[1], 'Needs you comes before the sections: ' + JSON.stringify(order));
      if (await m.locator('[data-action="needs-more"][aria-expanded="false"]').count()) {
        await m.click('[data-action="needs-more"]');
        await m.waitForSelector('.needs-list li:nth-child(5)');
        await m.$eval('.needs-list li:nth-child(5)', (e) => e.scrollIntoView({ block: 'center' }));
        await noOverflow(m, 'home 390, every need');
        await t.areaShot(m, AREA, 'home-needs-all-phone');
        await m.click('[data-action="needs-more"]');
        await m.waitForFunction(() => document.querySelectorAll('.needs-list li').length <= 4);
        await m.evaluate(() => { document.getElementById('main').scrollTop = 0; window.scrollTo(0, 0); });
      }
      const bar = await m.$$eval('.rail .rail-item', (els) => els.filter((e) => e.getClientRects().length).map((e) => e.dataset.view));
      // Your server takes Plans' place on the phone's bar (YOUR_SERVER.md D21)
      assert(t.canon(bar) === t.canon(['home', 'yourserver', 'factory', 'chat', 'messages', 'settings']), 'phone bar: ' + bar);
      await t.areaShot(m, AREA, 'home-phone');
      await m.evaluate(() => window.SF.drawer.open({ key: 'smoke-phone', title: 'A panel', sub: 'From a view module', body: window.SF.h('p', null, 'On a phone a panel takes the whole width.'), back: '#/home' }));
      await m.waitForSelector('#drawer.is-panel:not([hidden])');
      await m.waitForTimeout(300);
      await noOverflow(m, 'panel 390');
      assert(await m.$eval('#drawer', (e) => Math.round(e.getBoundingClientRect().width)) === 390, 'a panel is full width on a phone');
      await t.areaShot(m, AREA, 'panel-phone');
      await m.keyboard.press('Escape');
      await m.waitForFunction(() => document.getElementById('drawer').hidden);
      await m.evaluate(() => { const r = document.getElementById('main'); r.scrollTop = r.scrollHeight; });
      await m.waitForTimeout(150);
      await t.areaShot(m, AREA, 'home-phone-planned');
    });

    await step('A fresh install: "/" opens Welcome (branded, with a way Home); Home leaves the Operator out and says nothing waits', async () => {
      const fresh = await t.startMock(['--fresh']);
      const f = await t.newPage('home-fresh', { mockUrl: fresh.url, colorScheme: 'light' });
      try {
        await f.page.goto(fresh.url + '/');
        await f.page.waitForFunction(() => location.hash === '#/welcome', null, { timeout: 8000 });
        await f.page.waitForSelector('.wz-head .wz-eyebrow');
        assert(await f.page.textContent('.wz-head .wz-eyebrow') === 'Seraphina OS Harness', 'Welcome names the product');
        assert(/Seraphina OS Harness/.test(await f.page.textContent('.wz-lead')), 'Welcome’s lead names the product');
        assert(await f.page.locator('.opt-launch .opt-card[data-opt="home"]').count() === 1, 'Welcome offers Home');
        await t.areaShot(f.page, AREA, 'welcome-fresh-desktop');
        await f.page.goto(fresh.url + '/#/home');
        await loaded(f.page, LIVE.length - 1);
        assert(await f.page.locator('.home-card[data-section="operator"]').count() === 0, 'no operator card without an operator');
        assert(await f.page.isHidden('.rail-item[data-view="operator"]'), 'no operator rail entry either');
        await f.page.waitForSelector('.needs-empty');
        assert(/not set up yet/.test(await f.page.textContent('.home-health')), 'Home says setup is not finished');
        await t.areaShot(f.page, AREA, 'home-fresh-desktop');
        const fp = await t.newPage('home-fresh-phone', { mockUrl: fresh.url, phone: true });
        try {
          await fp.page.goto(fresh.url + '/#/home');
          await loaded(fp.page, LIVE.length - 1);
          await noOverflow(fp.page, 'fresh home 390');
          await t.areaShot(fp.page, AREA, 'home-fresh-phone');
          await fp.page.goto(fresh.url + '/#/welcome');
          await fp.page.waitForSelector('.opt-launch .opt-card[data-opt="home"]');
          await t.areaShot(fp.page, AREA, 'welcome-fresh-phone');
        } finally { await fp.ctx.close(); }
      } finally {
        await f.ctx.close();
        fresh.proc.kill('SIGTERM');
      }
    });
  } finally {
    await d.ctx.close();
    await p.ctx.close();
  }
}
