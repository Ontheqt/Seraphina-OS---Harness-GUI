// Checks for Your server (DESIGN §42; docs/seraphina/YOUR_SERVER.md §2, §3, §4, §7.2, §8, §11.5, §12.2 U1-U12),
// phases P1a to P1c: the view module swfactory/web/views/yourserver.js against tests/gui_mock/yourserver.py (the real
// swfactory/yourserver.py over a placeholder server), run by tests/gui/smoke.mjs (AREA_SUITES), or alone with
// GUI_ONLY=yourserver node tests/gui/smoke.mjs. Screenshots of every screen at 1440x900 and 390x844, light and dark:
// t.areaShot(page, AREA, name) → $GUI_AREA_SHOTS/yourserver/<name>.png.

import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const AREA = 'yourserver';
const VIEW_JS = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', '..', 'swfactory', 'web', 'views', 'yourserver.js');
const LOOKS = 'This area only looks.';
// Buttons of later phases (Maintain, Fix, root-access changes): never drawn in P1a
const LATER = /Install updates|Clean up|Restart (it|the server|a service)|Start it|Stop it|Remove (it from|this)|Update it|Lock (the|this) account|Remove the key/;
// state: {app id: [tone, code]} for the cards that state is about
const CARDS = {
  ready: { 'a-photos': ['ok', 'working'], 'a-beszel': ['ok', 'working'], 'a-media': ['ok', 'unchecked'], 'a-cockpit': ['ok', 'working'] },
  mixed: { 'a-ai': ['err', 'down_error'], 'a-gateway': ['err', 'gateway_down'], 'a-filebrowser': ['warn', 'errors'], 'a-notes': ['warn', 'stopped'], 'a-dockge': ['off', 'off'], 'a-photos': ['ok', 'working'] },
  stale: { 'a-photos': ['muted', 'stale'], 'a-beszel': ['muted', 'stale'] },
  docker_stopped: { 'a-photos': ['muted', 'docker_stopped'], 'a-cockpit': ['ok', 'working'] },
  outside_edit: { 'a-photos': ['ok', 'unchecked'] },
  beszel_down: { 'a-beszel': ['warn', 'not_answering'] },
};

async function setState(t, page, state) {
  const r = await page.request.post(t.url + '/__mock/yourserver/' + state, { data: {} });
  t.assert(r.status() === 200, 'mock state ' + state + ': ' + r.status());
}
async function openApps(page, url, what) {
  await page.goto(url + '/#/apps');
  await page.reload();
  await page.waitForSelector('#view-yourserver:not([hidden]) ' + (what || '.ys-strip'));
}
/** The keys of the view module's WORDS at its top level, in source order (a later key of the same name silently wins). */
function wordKeys(src) {
  let i = src.indexOf('{', src.indexOf('const WORDS = {')) + 1;
  let depth = 1;
  let expectKey = true;
  const keys = [];
  while (i < src.length && depth > 0) {
    const c = src[i];
    if (c === "'" || c === '"' || c === '`') {
      let j = i + 1;
      while (src[j] !== c) j += src[j] === '\\' ? 2 : 1;
      const text = src.slice(i + 1, j);
      i = j + 1;
      if (depth === 1 && expectKey && /^\s*:/.test(src.slice(i, i + 8))) { keys.push(text); expectKey = false; }
      continue;
    }
    if (c === '/' && src[i + 1] === '/') { i = src.indexOf('\n', i); continue; }
    if (c === '/' && src[i + 1] === '*') { i = src.indexOf('*/', i) + 2; continue; }
    const m = depth === 1 && expectKey ? /^[A-Za-z_$][\w$]*\s*:/.exec(src.slice(i, i + 80)) : null;
    if (m) { keys.push(m[0].replace(/\s*:$/, '')); expectKey = false; i += m[0].length; continue; }
    if ('{[('.includes(c)) depth++;
    else if ('}])'.includes(c)) depth--;
    else if (c === ',' && depth === 1) expectKey = true;
    i++;
  }
  return keys;
}
/** Every GET path page asks for from now on, until the returned function stops listening. */
function gets(page) {
  const seen = [];
  const on = (r) => { if (r.method() === 'GET') seen.push(new URL(r.url()).pathname + new URL(r.url()).search); };
  page.on('request', on);
  return { seen, count: (p) => seen.filter((x) => x.split('?')[0] === p).length, stop: () => page.off('request', on) };
}
/** Until page has asked for nothing for ms (at most max): a page load replays the event stream's past events, and
 *  the reads they cause must be over before a step counts reads. */
async function quiet(page, ms = 600, max = 6000) {
  let last = Date.now();
  const on = () => { last = Date.now(); };
  page.on('request', on);
  const end = Date.now() + max;
  try { while (Date.now() - last < ms && Date.now() < end) await page.waitForTimeout(100); } finally { page.off('request', on); }
}
const cardLight = (page, id) => page.$eval('.ys-card[data-app="' + id + '"] .ys-light', (e) => ({ tone: e.dataset.tone, code: e.dataset.code, reason: e.querySelector('.ys-reason').textContent }));

export async function run(t) {
  const { step, assert, noOverflow } = t;
  const d = await t.newPage(AREA);
  const dl = await t.newPage(AREA + '-light', { colorScheme: 'light' });
  const p = await t.newPage(AREA + '-phone', { phone: true });
  const pd = await t.newPage(AREA + '-phone-dark', { phone: true, colorScheme: 'dark' });
  const page = d.page;
  try {
    await step(AREA + ': the rail entry, the looks-only line on every page, the host strip without the system access, and the summary card with no container id (U1, U12)', async () => {
      await setState(t, page, 'absent');
      await openApps(page, t.url, '.ys-summary');
      assert(await page.locator('.rail-item[data-view="yourserver"]').count() === 1, 'a rail entry');
      assert((await page.textContent('[data-part="looks-only"]')).includes(LOOKS), 'the page says it only looks');
      const chips = await page.$$eval('.ys-strip .ys-chip', (els) => els.map((e) => e.textContent));
      assert(chips.some((c) => /GB of .* free/.test(c)) && chips.some((c) => /^Up /.test(c)), 'free memory and uptime without the system access: ' + chips);
      assert(chips.includes('Restart needed for updates'), 'restart needed from /run/reboot-required');
      const summary = await page.textContent('.ys-summary');
      assert(/containers are running \(.+\), using .+ of memory/.test(summary), 'the summary sentence: ' + summary);
      assert(!/[0-9a-f]{12,}/.test(summary), 'no container id on the summary card');
      assert(await page.locator('.ys-summary [data-action="set-up"]').count() === 1, 'Set it up');
      await t.areaShot(page, AREA, 'absent-desktop-dark');
      await setState(t, page, 'off');
      await openApps(page, t.url, '[data-part="off"]');
      assert(await page.locator('[data-action="turn-on"]').count() === 1, 'off: Turn it on');
      await setState(t, page, 'unsafe');
      await openApps(page, t.url, '.ys-summary');
      assert(/wrong owner/.test(await page.textContent('.ys-summary')), 'unsafe: the file at fault in words');
    });

    await step(AREA + ': each state draws its cards with the module\'s tones and reasons; green never on stale facts; no button of a later phase (U1)', async () => {
      for (const [state, cards] of Object.entries(CARDS)) {
        await setState(t, page, state);
        await openApps(page, t.url, '.ys-card');
        for (const [id, [tone, code]] of Object.entries(cards)) {
          const l = await cardLight(page, id);
          assert(l.tone === tone && l.code === code, state + ' ' + id + ': ' + JSON.stringify(l) + ' (want ' + tone + ' ' + code + ')');
          assert(/\.$|\?$/.test(l.reason.trim()), state + ' ' + id + ': a reason sentence: ' + l.reason);
        }
        if (state === 'stale') assert(await page.locator('.ys-card .ys-light[data-tone="ok"]').count() === 0, 'stale: never green');
        if (state === 'docker_stopped') assert(/Docker is stopped/.test(await page.textContent('#yourserver-root')), 'docker stopped: the sentence');
        const text = await page.textContent('#yourserver-root');
        assert(!LATER.test(text), state + ': a button of a later phase: ' + (text.match(LATER) || [])[0]);
        if (['ready', 'mixed'].includes(state)) await t.areaShot(page, AREA, 'apps-' + state + '-desktop-dark');
      }
      await setState(t, page, 'mixed');
      await openApps(page, t.url, '.ys-card');
      assert(/Retired|old cop/.test(await page.textContent('[data-part="not-shown"]')), 'the Not shown line counts old copies');
    });

    await step(AREA + ': the first-run review list: ticked and unticked rows, edit in place, an old copy, These are right (U12)', async () => {
      await setState(t, page, 'review');
      await openApps(page, t.url, '[data-part="review"]');
      await page.click('[data-action="review"]');
      await page.waitForSelector('.ys-review .ys-row');
      const ticks = await page.$$eval('.ys-review .ys-row', (rows) => rows.map((r) => [r.dataset.app, r.querySelector('input[type=checkbox]').checked]));
      assert(ticks.some(([, c]) => c) && ticks.some(([, c]) => !c), 'some rows ticked in advance, some not: ' + JSON.stringify(ticks));
      assert(ticks.find(([a]) => a === 'a-photos')[1] === true, 'photos: its Tailscale name matches its own: ticked');
      assert(ticks.find(([a]) => a === 'a-beszel')[1] === false, 'a route through the login gateway is never ticked in advance');
      await t.areaShot(page, AREA, 'review-desktop-dark');
      await page.fill('.ys-row[data-app="a-photos"] input[data-field="name"]', 'Family photos');
      await page.selectOption('.ys-row[data-app="a-mediaold"] select[data-field="old_copy"]', 'a-media');
      const resp = await t.waitPost(page, /\/api\/yourserver\/review$/, () => page.click('[data-action="review-done"]'));
      assert(resp.status() === 200, 'review saved: ' + resp.status());
      const body = JSON.parse(resp.request().postData());
      const photos = body.rows.find((r) => r.app === 'a-photos');
      assert(photos && photos.name === 'Family photos' && /^https:\/\/photos\.example\.ts\.net/.test(photos.url) && photos.expect === 'run', 'photos row: ' + JSON.stringify(photos));
      assert(body.rows.find((r) => r.app === 'a-mediaold').old_copy === 'a-media', 'the old copy row');
      assert(!body.rows.some((r) => r.app === 'a-beszel'), 'an unticked row is not confirmed');
      await page.waitForSelector('.ys-card[data-app="a-photos"] .ys-address[data-state="confirmed"]');
      assert(/Family photos/.test(await page.textContent('.ys-card[data-app="a-photos"] .ys-name')), 'the new name on the card');
      assert(await page.locator('.ys-card[data-app="a-mediaold"]').count() === 0, 'the old copy has no card of its own');
    });

    await step(AREA + ': confirming an address on its card, the outside-edit wait, and the refusal of an address outside the tailnet (U2)', async () => {
      await setState(t, page, 'review');
      await openApps(page, t.url, '.ys-card');
      const resp = await t.waitPost(page, /\/api\/yourserver\/apps\/a-beszel\/address$/, () => page.click('.ys-card[data-app="a-beszel"] [data-action="confirm-address"]'));
      assert(resp.status() === 200 && JSON.parse(resp.request().postData()).action === 'confirm', 'confirm sent');
      await page.waitForSelector('.ys-card[data-app="a-beszel"] .ys-address[data-state="confirmed"]');
      await setState(t, page, 'outside_edit');
      await openApps(page, t.url, '.ys-card[data-app="a-photos"]');
      const text = await page.textContent('.ys-card[data-app="a-photos"]');
      assert(text.includes('This address was changed outside Seraphina. Is it still right?'), 'the outside edit waits: ' + text);
      assert(await page.locator('.ys-card[data-app="a-photos"] a[data-action="open"]').count() === 0, 'no Open button while it waits');
      await t.areaShot(page, AREA, 'outside-edit-desktop-dark');
      await setState(t, page, 'hostile');
      await openApps(page, t.url, '.ys-card');
      const dockge = await page.textContent('.ys-card[data-app="a-dockge"]');
      assert(/can't be opened from here/.test(dockge) && await page.locator('.ys-card[data-app="a-dockge"] a[data-action="open"]').count() === 0, 'a javascript: address never becomes a link: ' + dockge);
      assert(/not on your Tailscale network/.test(await page.textContent('.ys-card[data-app="a-ai"]')), 'an address on another tailnet is refused');
      await page.goto(t.url + '/#/apps/a-cockpit');
      await page.waitForSelector('[data-part="addresses"] input[data-focus="typed"]');
      await page.fill('[data-part="addresses"] input[data-focus="typed"]', 'https://cockpit.other.ts.net');
      t.allowHttp(true);
      try {
        const bad = await t.waitPost(page, /\/address$/, () => page.click('[data-action="set-address"]'));
        assert(bad.status() === 400, 'an address on another tailnet is refused: ' + bad.status());
        await page.waitForSelector('#toasts .toast');
      } finally { t.allowHttp(false); }
    });

    await step(AREA + ': Open opens a new tab with noopener noreferrer and never at Seraphina\'s own address; a card that cannot be checked still opens (U3, U4)', async () => {
      await setState(t, page, 'ready');
      await openApps(page, t.url, '.ys-card');
      const links = await page.$$eval('#yourserver-root a[target="_blank"]', (els) => els.map((a) => ({ href: a.getAttribute('href'), rel: a.rel, host: new URL(a.href).host })));
      assert(links.length >= 5, 'open links: ' + links.length);
      for (const l of links) {
        assert(/^https?:\/\//.test(l.href) && l.rel === 'noopener noreferrer', 'a safe link: ' + JSON.stringify(l));
        assert(l.host !== new URL(t.url).host, 'never at the harness: ' + l.href);
      }
      const media = await cardLight(page, 'a-media');
      assert(media.code === 'unchecked', 'media cannot be checked from the server: ' + media.code);
      assert(await page.locator('.ys-card[data-app="a-media"] a[data-action="open"]').count() === 1, 'and still opens (N8)');
      await t.areaShot(page, AREA, 'apps-ready-desktop-dark-links');
    });

    await step(AREA + ': the QR code is drawn from its rows; the Add to Home Screen steps follow the phone picked (U5)', async () => {
      await page.goto(t.url + '/#/apps');
      await page.waitForSelector('.ys-card[data-app="a-photos"] [data-action="qr"]');
      await page.click('.ys-card[data-app="a-photos"] [data-action="qr"]');
      await page.waitForSelector('.ys-card[data-app="a-photos"] .ys-qr');
      const code = await (await page.request.get(t.url + '/api/yourserver/apps/a-photos/qr')).json();
      const drawn = await page.$eval('.ys-card[data-app="a-photos"] .ys-qr', (e) => ({ cells: e.children.length, dark: e.querySelectorAll('.ys-qr-d').length }));
      const dark = code.rows.join('').split('').filter((c) => c === '1').length;
      assert(drawn.cells === (code.size + 8) ** 2 && drawn.dark === dark, 'the grid is the rows with a four-square border: ' + JSON.stringify(drawn) + ' size ' + code.size);
      await t.areaShot(page, AREA, 'qr-desktop-dark');
      await page.goto(t.url + '/#/apps/a-photos');
      await page.waitForSelector('[data-part="home-steps"]');
      assert(/Safari/.test(await page.textContent('[data-part="home-steps"]')), 'iPhone steps first');
      await page.selectOption('[data-part="home-steps"] select', 'android');
      await page.waitForFunction(() => /Chrome/.test(document.querySelector('[data-part="home-steps"]').textContent));
      await t.areaShot(page, AREA, 'app-photos-desktop-dark');
    });

    await step(AREA + ': one app: should it run, maintenance, Ask about this app with the facts first and the answer labelled (U12)', async () => {
      await page.goto(t.url + '/#/apps/a-photos');
      await page.waitForSelector('[data-part="ask"] textarea');
      const facts = await page.textContent('[data-part="ask"]');
      assert(/What is this\?/.test(facts) && /Is it healthy\?/.test(facts) && /What changed\?/.test(facts), 'the three questions first');
      await page.fill('[data-part="ask"] textarea', 'Is it healthy?');
      const asked = await t.waitPost(page, /\/ask$/, () => page.click('[data-action="ask"]'));
      assert(asked.status() === 200, 'asked');
      await page.waitForSelector('[data-part="answer"] .ys-answer-text');
      assert(await page.textContent('[data-part="answer"] .tag') === 'AI, from the facts above', 'the answer is labelled');
      assert(await page.locator('[data-part="answer"] b').count() === 0, 'the answer is text, not markup');
      const mode = await t.waitPost(page, /\/api\/yourserver\/apps\/a-photos$/, () => page.selectOption('select[data-field="maintenance"]', 'pinned'));
      assert(JSON.parse(mode.request().postData()).maintenance.mode === 'pinned', 'the maintenance toggle');
      const off = await t.waitPost(page, /\/api\/yourserver\/apps\/a-photos$/, () => page.click('[data-action="expect-off"]'));
      assert(JSON.parse(off.request().postData()).expect === 'off', 'Keep it off');
      await page.waitForSelector('.ys-app .ys-light[data-code="off"]');
      await t.areaShot(page, AREA, 'app-photos-off-desktop-dark');
      await setState(t, page, 'mixed');
      await page.goto(t.url + '/#/apps/a-media');
      await page.reload();
      await page.waitForSelector('[data-part="copies"] .ys-boots');
      assert(/docker update --restart=no jellyfin-2023/.test(await page.textContent('[data-part="copies"]')), 'a retired copy that starts at boot: the terminal line');
    });

    await step(AREA + ': Home\'s card and Needs you: an app down is err, the review list human, Beszel warn (U9)', async () => {
      const home = async (state) => {
        await setState(t, page, state);
        await page.goto(t.url + '/#/home');
        await page.reload();
        await page.waitForSelector('.home-card[data-section="yourserver"]');
        await page.waitForFunction(() => !/checking/.test(document.querySelector('.home-card[data-section="yourserver"]').textContent), null, { timeout: 8000 });
        if (await page.locator('[data-action="needs-more"][aria-expanded="false"]').count()) {
          await page.click('[data-action="needs-more"]');
          await page.waitForSelector('.needs-list li:nth-child(5)');
        }
        return page.$$eval('.needs-list li', (els) => els.map((e) => ({ text: e.textContent, tone: e.dataset.tone || (e.className.match(/tone-(\w+)/) || [])[1] || '' })));
      };
      let needs = await home('mixed');
      assert(/down/.test(await page.textContent('.home-card[data-section="yourserver"]')), 'the card says apps are down');
      assert(needs.some((n) => /Down: it stopped with an error/.test(n.text)), 'an app down in Needs you: ' + JSON.stringify(needs));
      await t.areaShot(page, AREA, 'home-mixed-desktop-dark');
      needs = await home('review');
      assert(needs.some((n) => /Check 10 app names and addresses/.test(n.text)), 'the review list in Needs you: ' + JSON.stringify(needs));
      needs = await home('beszel_down');
      assert(needs.some((n) => /Beszel is down/.test(n.text)), 'Beszel down in Needs you: ' + JSON.stringify(needs));
      await setState(t, page, 'beszel_down');
      await openApps(page, t.url, '[data-part="beszel"]');
      assert(await page.$eval('[data-part="beszel"] .ys-light', (e) => e.dataset.tone) === 'err', 'the Beszel card is red');
    });

    await step(AREA + ': hostile names are text; every page fits 390 px with no sideways scroll, light and dark (U10)', async () => {
      await setState(t, page, 'hostile');
      for (const [view, label] of [[p, 'phone-light'], [pd, 'phone-dark']]) {
        const m = view.page;
        for (const [hash, sel, name] of [['#/apps', '.ys-card', 'apps'], ['#/apps/review', '.ys-review', 'review'], ['#/apps/a-photos', '[data-part="ask"]', 'app'],
          ['#/apps/a-mediaold', '.ys-app', 'app-hostile'], ['#/server', '[data-part="access"]', 'server'], ['#/server/setup', '.ys-blocks', 'setup']]) {
          await m.goto(t.url + '/' + hash);
          await m.reload();
          await m.waitForSelector('#view-yourserver:not([hidden]) ' + sel);
          await m.waitForTimeout(150);
          await noOverflow(m, AREA + ' ' + hash + ' ' + label);
          await t.areaShot(m, AREA, name + '-' + label);
        }
        const injected = await m.evaluate(() => ({ img: document.querySelectorAll('#yourserver-root img').length, js: Array.from(document.querySelectorAll('#yourserver-root a[href]')).filter((a) => /^javascript:/i.test(a.getAttribute('href'))).length }));
        assert(injected.img === 0 && injected.js === 0, 'nothing injected: ' + JSON.stringify(injected));
      }
      await openApps(page, t.url, '.ys-card');
      const names = await page.$$eval('.ys-card .ys-name', (els) => els.map((e) => e.textContent));
      assert(names.includes("<img src=x onerror=alert('app')>") && names.some((n) => n.includes('‮')) && names.some((n) => n.length >= 600), 'hostile names shown as text: ' + names.map((n) => n.slice(0, 30)));
      await t.areaShot(page, AREA, 'apps-hostile-desktop-dark');
      for (const [hash, sel, name] of [['#/apps', '.ys-card', 'apps'], ['#/apps/a-photos', '[data-part="ask"]', 'app'], ['#/server', '[data-part="access"]', 'server'], ['#/server/setup', '.ys-blocks', 'setup']]) {
        await setState(t, dl.page, 'ready');
        await dl.page.goto(t.url + '/' + hash);
        await dl.page.reload();
        await dl.page.waitForSelector('#view-yourserver:not([hidden]) ' + sel);
        await dl.page.waitForTimeout(150);
        await noOverflow(dl.page, AREA + ' ' + hash + ' desktop light');
        await t.areaShot(dl.page, AREA, name + '-desktop-light');
      }
    });

    await step(AREA + ': a read-only Beszel login: the password made once, then Check (§4.4)', async () => {
      await setState(t, page, 'beszel_no_login');
      await openApps(page, t.url, '[data-part="beszel-login"]');
      await page.click('[data-part="beszel-login"] summary');
      await page.waitForSelector('[data-action="beszel-password"]', { state: 'visible' });
      await t.waitPost(page, /\/api\/yourserver\/beszel$/, () => page.click('[data-action="beszel-password"]'));
      await page.waitForSelector('[data-part="password"]');
      const password = await page.textContent('[data-part="password"]');
      assert(password.length >= 30, 'the password, shown once');
      // A Check whose login Beszel refuses (the password pasted wrongly) keeps the password on the page (#81)
      await page.request.post(t.url + '/__mock/yourserver/beszel_accepts', { data: { accepts: false } });
      await page.fill('[data-focus="beszel-email"]', 'seraphina@localhost');
      const refused = await t.waitPost(page, /\/api\/yourserver\/beszel$/, () => page.click('[data-action="beszel-check"]'));
      assert(refused.status() === 200 && /did not accept/.test((await refused.json()).words), 'the login refused: ' + JSON.stringify(await refused.json()));
      await page.waitForSelector('#toasts .toast-error');
      assert(/did not accept/.test(await page.textContent('#toasts .toast-error')), 'the refusal as an error');
      await page.waitForSelector('[data-part="beszel"] p.muted');
      assert(await page.textContent('[data-part="password"]') === password, 'a failed Check keeps the password to paste again');
      await page.evaluate(() => document.querySelectorAll('#toasts .toast').forEach((x) => x.remove()));
      await page.request.post(t.url + '/__mock/yourserver/beszel_accepts', { data: { accepts: true } });
      await page.fill('[data-focus="beszel-email"]', 'seraphina@localhost');
      const r = await t.waitPost(page, /\/api\/yourserver\/beszel$/, () => page.click('[data-action="beszel-check"]'));
      assert(JSON.parse(r.request().postData()).email === 'seraphina@localhost', 'the email sent');
      await page.waitForFunction(() => document.querySelector('[data-part="beszel"] .ys-light').dataset.code === 'ok' && !document.querySelector('[data-part="password"]'));
      assert(await page.locator('[data-part="password"]').count() === 0, 'not shown again once the login works');
      await t.areaShot(page, AREA, 'beszel-login-desktop-dark');
    });

    await step(AREA + ': setup shows the one paste, the steps one at a time and how to switch it off; stage and check (§6.11)', async () => {
      await setState(t, page, 'absent');
      await page.goto(t.url + '/#/server/setup');
      await page.reload();
      await page.waitForSelector('.ys-blocks .ys-block');
      await t.waitPost(page, /\/setup\/stage$/, () => page.click('[data-action="stage"]'));
      await page.waitForFunction(() => /setup system-helper\.json|system-helper\.json/.test(document.querySelector('.ys-blocks').textContent));
      const paste = await page.textContent('.ys-blocks .ys-paste');
      assert(/^sudo install -D -m 0755 -o root -g root .+\/sf-system \/usr\/local\/libexec\/seraphina\/sf-system\.new && sha256sum .+ && sudo \/usr\/local\/libexec\/seraphina\/sf-system\.new setup .+\/system-helper\.json$/.test(paste.trim()), 'the one paste: ' + paste);
      assert(/sudo rm \/etc\/sudoers\.d\/seraphina-system/.test(await page.textContent('[data-part="removal"]')), 'one file switches it off');
      assert((await page.textContent('[data-action="check"]')).trim() === 'Check it', 'the setup page\'s Check it: ' + await page.textContent('[data-action="check"]'));
      await t.areaShot(page, AREA, 'setup-desktop-dark');
    });

    await step(AREA + ': the Check buttons: each result in words, rows, the families done at home, the biggest folders; checks only look (U6)', async () => {
      await setState(t, page, 'checks');
      await page.goto(t.url + '/#/server');
      await page.reload();
      await page.waitForSelector('[data-part="checks"] .ys-check[data-check="updates"][data-state="done"]');
      const ids = await page.$$eval('[data-part="checks"] .ys-check', (els) => els.map((e) => e.dataset.check));
      assert(JSON.stringify(ids) === JSON.stringify(['updates', 'restart', 'disk', 'failed', 'health', 'listening', 'tailscale']), 'the seven checks: ' + ids);
      const words = await page.$$eval('[data-part="checks"] .ys-check', (els) => Object.fromEntries(els.map((e) => [e.dataset.check, e.querySelector('[data-part="check-words"]').textContent])));
      assert(/14 updates, 6 of them security fixes\./.test(words.updates) && /must be done at home/.test(words.updates), 'Check 1: ' + words.updates);
      assert(/a new kernel is installed but not running yet/.test(words.restart), 'Check 2: ' + words.restart);
      assert(/Docker uses .*leftover build files/.test(words.disk), 'Check 3: ' + words.disk);
      assert(/2 things failed/.test(words.failed), 'Check 4: ' + words.failed);
      assert(/1 of 4 disks need a look/.test(words.health), 'Check 5: ' + words.health);
      assert(/can be reached from your home network without Tailscale/.test(words.listening), 'Check 6: ' + words.listening);
      assert(/Funnel is off everywhere/.test(words.tailscale), 'Check 7: ' + words.tailscale);
      assert(await page.locator('[data-check="updates"] [data-action="refresh-list"]').count() === 1, 'Refresh the list, when allowed');
      assert(/Checks only look|only looks/.test(await page.textContent('[data-part="checks"]')), 'the checks say they only look');
      await t.areaShot(page, AREA, 'checks-desktop-dark');
      await page.click('[data-check="updates"] [data-part="family-blocks"] summary');
      assert(/sudo apt-get install --only-upgrade .*=/.test(await page.textContent('[data-check="updates"] [data-part="family-blocks"]')), 'exact versions to run at home');
      await page.selectOption('[data-focus="du-mount"]', { index: 0 });
      const du = await t.waitPost(page, /\/api\/yourserver\/checks\/disk$/, () => page.click('[data-action="du"]'));
      assert(JSON.parse(du.request().postData()).mount, 'the disk measured');
      await page.waitForSelector('[data-part="folders"] .ys-mono');
      const run = await t.waitPost(page, /\/api\/yourserver\/checks\/health$/, () => page.click('[data-check="health"] [data-action="run-check"]'));
      assert(run.status() === 200, 'one check run now');
      assert(/Kept|kept/.test(await page.textContent('[data-part="keep"]')), 'the previous versions section');
      assert(/may refresh Ubuntu/.test(await page.textContent('[data-part="looks-only"]')), 'the page names the two things it may change');
      await t.areaShot(page, AREA, 'checks-folders-desktop-dark');
      const text = await page.textContent('#yourserver-root');
      assert(!LATER.test(text), 'no button of a later phase: ' + (text.match(LATER) || [])[0]);
    });

    await step(AREA + ': update kinds and Docker health on the cards; an app\'s latest log lines as text; How to go back (U6, U12)', async () => {
      await setState(t, page, 'checks');
      await openApps(page, t.url, '.ys-card');
      assert(/Changed in its folder/.test(await page.textContent('.ys-card[data-app="a-photos"] [data-part="updates"]')), 'waiting in its folder');
      assert(/A newer copy is downloaded/.test(await page.textContent('.ys-card[data-app="a-media"] [data-part="updates"]')), 'downloaded, not in use');
      assert(/Docker: unhealthy/.test(await page.textContent('.ys-card[data-app="a-notes"]')), 'Docker\'s own health');
      await page.goto(t.url + '/#/apps/a-photos');
      await page.waitForSelector('[data-part="logs"] [data-action="logs"]');
      await page.click('[data-part="logs"] [data-action="logs"]');
      await page.waitForSelector('[data-part="log-lines"] li');
      const lines = await page.$$eval('[data-part="log-lines"] .ys-log-text', (els) => els.map((e) => e.textContent));
      assert(lines.includes("<img src=x onerror=alert('log')>"), 'a hostile log line shown as text: ' + lines);
      assert(!lines.join('\n').includes('PLANTED') && !lines.join('\n').includes('hunter2'), 'secrets hidden');
      assert(await page.locator('[data-part="log-lines"] img').count() === 0, 'nothing injected');
      const back = await page.textContent('[data-part="how-back"]');
      assert(/In Dockge, open the stack photos and press Edit\./.test(back) && /seraphina-keep\/photos-photos:/.test(back), 'How to go back: ' + back);
      assert(/not its data/.test(back), 'what it does not do');
      await t.areaShot(page, AREA, 'app-logs-desktop-dark');
      await setState(t, page, 'root_before_move');
      await page.goto(t.url + '/#/apps/a-photos');
      await page.reload();
      await page.waitForSelector('[data-part="logs"] [data-action="logs"]');
      await page.click('[data-part="logs"] [data-action="logs"]');
      await page.waitForSelector('[data-part="logs"] .callout');
      assert(/Open its logs in Dockge|Cockpit/.test(await page.textContent('[data-part="logs"]')) || /own account/.test(await page.textContent('[data-part="logs"]')), 'until P1c\'s preconditions hold: the reasons and the links');
      assert(await page.locator('[data-part="log-lines"]').count() === 0, 'no lines read');
    });

    await step(AREA + ': who can take full control: groups, why, since, marks, the can\'t-check list; changes disabled with their reason (U7)', async () => {
      await setState(t, page, 'root');
      await page.goto(t.url + '/#/server/root');
      await page.reload();
      await page.waitForSelector('[data-part="headline"]');
      const headline = await page.textContent('[data-part="headline"]');
      assert(/^Seraphina found \d+ ways\. 2 new since Seraphina last looked\. Some kinds can't be checked from here\.$/.test(headline), 'the headline: ' + headline);
      for (const g of ['people', 'programs', 'doors', 'files', 'escape']) assert(await page.locator('[data-part="group-' + g + '"]').count() === 1, 'group ' + g);
      assert(await page.locator('.ys-root-row[data-new="1"]').count() === 2, 'two new rows');
      assert(await page.locator('.ys-root-row[data-known="1"]').count() === 3, 'three marked known');
      assert(/sudo-rs/.test(await page.textContent('[data-part="cant-see"]')), 'sudo-rs\'s can\'t-list line');
      assert(/home folder/.test(await page.textContent('[data-part="group-files"]')), 'a root service running a script from a home folder');
      assert(/Two sudo programs are installed/.test(await page.textContent('[data-part="group-doors"]')), 'two sudo programs');
      const row = page.locator('.ys-root-row[data-new="1"]').first();
      await row.locator('summary').click();
      const p3 = row.locator('[data-part="p3"] button');
      assert(await p3.count() >= 1 && await p3.first().isDisabled(), 'changes shown, disabled');
      assert(/come later/.test(await page.textContent('[data-part="p3-why"]')), 'and why');
      await t.areaShot(page, AREA, 'root-desktop-dark');
      await row.locator('input[data-field="known"]').check();
      const id = await row.getAttribute('data-row');
      const marked = await t.waitPost(page, /\/api\/yourserver\/root\/[0-9a-f]{16}\/mark$/, () => row.locator('[data-action="mark"]').click());
      assert(JSON.parse(marked.request().postData()).known === true, 'marked');
      await page.waitForSelector('.ys-root-row[data-row="' + id + '"][data-known="1"]');
      const again = await t.waitPost(page, /\/api\/yourserver\/root\/check$/, () => page.click('[data-action="look-again"]'));
      assert(again.status() === 200, 'look again');
      await setState(t, page, 'root_before_move');
      await page.goto(t.url + '/#/server/root');
      await page.reload();
      await page.waitForSelector('[data-part="reasons"]');
      assert(/a person's account/.test(await page.textContent('[data-part="reasons"]')), 'waits, with its reasons');
      assert(/sf-system explain access/.test(await page.textContent('[data-part="explain"]')), 'and the paste for the owner\'s own terminal');
      await t.areaShot(page, AREA, 'root-before-move-desktop-dark');
    });

    await step(AREA + ': records of changes; setup offers the extra classes; every new page fits 390 px, light and dark (U10)', async () => {
      await setState(t, page, 'keep');
      await page.goto(t.url + '/#/server/receipts');
      await page.reload();
      await page.waitForSelector('.ys-receipts li');
      assert(/Done by Seraphina's system access/.test(await page.textContent('.ys-receipts')), 'labelled by where it came from');
      assert(/How to undo it:/.test(await page.textContent('.ys-receipts')), 'with its way back');
      await t.areaShot(page, AREA, 'receipts-desktop-dark');
      await page.goto(t.url + '/#/server/setup');
      await page.reload();
      await page.waitForSelector('[data-part="classes"] input[data-cls="keep"]');
      const staged = await t.waitPost(page, /\/setup\/stage$/, () => page.click('[data-action="stage"]'));
      assert(Array.isArray(JSON.parse(staged.request().postData()).classes), 'the ticked classes staged');
      for (const [state, hash, sel, name] of [['checks', '#/server', '[data-part="checks"] .ys-check', 'checks'], ['root', '#/server/root', '[data-part="headline"]', 'root'],
        ['keep', '#/server/receipts', '.ys-receipts', 'receipts'], ['checks', '#/apps/a-photos', '[data-part="logs"]', 'app-logs'], ['root_before_move', '#/server/root', '[data-part="reasons"]', 'root-wait']]) {
        for (const [view, label] of [[p, 'phone-light'], [pd, 'phone-dark'], [dl, 'desktop-light']]) {
          await setState(t, view.page, state);
          await view.page.goto(t.url + '/' + hash);
          await view.page.reload();
          await view.page.waitForSelector('#view-yourserver:not([hidden]) ' + sel);
          // the reads a page load causes move the button (the server's answer adds a line above): click once they are over
          if (name === 'app-logs') { await quiet(view.page); await view.page.click('[data-part="logs"] [data-action="logs"]'); await view.page.waitForSelector('[data-part="log-lines"] li'); }
          await view.page.waitForTimeout(150);
          await noOverflow(view.page, AREA + ' ' + hash + ' ' + state + ' ' + label);
          await t.areaShot(view.page, AREA, name + '-' + label);
        }
      }
    });

    await step(AREA + ': WORDS has each key once, so "Should it run?" offers No, keep it off (#47)', async () => {
      const keys = wordKeys(readFileSync(VIEW_JS, 'utf8'));
      assert(keys.length > 100, 'the keys read: ' + keys.length);
      const twice = keys.filter((k, i) => keys.indexOf(k) !== i);
      assert(twice.length === 0, 'a key of WORDS twice (the later one wins): ' + twice);
    });

    await step(AREA + ': a page that cannot load asks once, not at every frame; Try again asks again (#13)', async () => {
      await setState(t, page, 'ready');
      const down = (u) => u.pathname === '/api/yourserver';
      let seen = null;
      t.allowHttp(true);
      try {
        // An app that is gone: its page says so and asks no more (it asked at every frame, 160 times in 3 s)
        await page.goto(t.url + '/#/apps/a-gone');
        await page.reload();
        await page.waitForFunction(() => /not on your server any more/.test(document.querySelector('#yourserver-root').textContent));
        await quiet(page);
        seen = gets(page);
        await page.waitForTimeout(1500);
        assert(seen.count('/api/yourserver/apps/a-gone') === 0, 'an app that is gone is not asked for again at every frame: ' + seen.count('/api/yourserver/apps/a-gone'));
        seen.stop();
        // The server page while the harness answers 503
        await page.route(down, (route) => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: 'The harness is restarting.' }) }));
        await page.goto(t.url + '/#/server');
        await page.reload();
        await page.waitForSelector('#view-yourserver:not([hidden]) [data-action="retry"]');
        await quiet(page);
        seen = gets(page);
        await page.waitForTimeout(1500);
        assert(seen.count('/api/yourserver') === 0, 'a server page that cannot load is not asked for again at every frame: ' + seen.count('/api/yourserver'));
        await page.unroute(down);
        await page.click('[data-action="retry"]');
        await page.waitForSelector('#view-yourserver:not([hidden]) [data-part="access"]');
      } finally { if (seen) seen.stop(); await page.unroute(down).catch(() => {}); t.allowHttp(false); }
    });

    await step(AREA + ': the review list: Should it run? offers No, keep it off; That\'s mine keeps the owner\'s edits (#47, #82)', async () => {
      await setState(t, page, 'review_tailnet');
      await page.goto(t.url + '/#/apps/review');
      await page.reload();
      const row = '.ys-row[data-app="a-notes"]';
      await page.waitForSelector(row + ' select[data-field="expect"]');
      await quiet(page);
      const options = await page.$$eval(row + ' select[data-field="expect"] option', (els) => els.map((o) => [o.value, o.textContent]));
      assert(JSON.stringify(options) === JSON.stringify([['run', 'Yes, it should run'], ['off', 'No, keep it off'], ['', 'Not sure yet']]), 'Should it run?: ' + JSON.stringify(options));
      await page.fill(row + ' input[data-field="name"]', 'Shared notes');
      await page.selectOption(row + ' select[data-field="expect"]', 'off');
      await Promise.all([
        page.waitForResponse((r) => r.request().method() === 'POST' && new URL(r.url()).pathname === '/api/config'),
        page.waitForResponse((r) => r.request().method() === 'GET' && new URL(r.url()).pathname === '/api/yourserver/review'),
        page.click('[data-part="tailnet"] [data-action="tailnet"]'),
      ]);
      await page.waitForSelector('[data-part="tailnet"]', { state: 'detached' });
      assert(await page.inputValue(row + ' input[data-field="name"]') === 'Shared notes', 'That\'s mine keeps the name typed');
      assert(await page.inputValue(row + ' select[data-field="expect"]') === 'off', 'and the choice made');
      const saved = await t.waitPost(page, /\/api\/yourserver\/review$/, () => page.click('[data-action="review-done"]'));
      const kept = JSON.parse(saved.request().postData()).rows.find((r) => r.app === 'a-notes');
      assert(saved.status() === 200 && kept && kept.name === 'Shared notes' && kept.expect === 'off', 'and These are right sends them: ' + JSON.stringify(kept));
    });

    await step(AREA + ': the review list: an address typed and then left for a found one, or for none, is never sent (#45)', async () => {
      await setState(t, page, 'review');
      await page.goto(t.url + '/#/apps/review');
      await page.reload();
      const row = '.ys-row[data-app="a-notes"]';
      await page.waitForSelector(row + ' select[data-field="url"]');
      await quiet(page);
      const found = (await page.$$eval(row + ' select[data-field="url"] option', (els) => els.map((o) => o.value))).find((v) => v && v !== '__typed');
      await page.selectOption(row + ' select[data-field="url"]', '__typed');
      await page.fill(row + ' input[data-field="typed"]', 'http://192.168.1.50:9999');
      await page.selectOption(row + ' select[data-field="url"]', found);
      assert(await page.isHidden(row + ' input[data-field="typed"]'), 'the typed box hides');
      const other = '.ys-row[data-app="a-cockpit"]';
      await page.selectOption(other + ' select[data-field="url"]', '__typed');
      await page.fill(other + ' input[data-field="typed"]', 'http://192.168.1.51:9999');
      await page.selectOption(other + ' select[data-field="url"]', '');
      const resp = await t.waitPost(page, /\/api\/yourserver\/review$/, () => page.click('[data-action="review-done"]'));
      const rows = JSON.parse(resp.request().postData()).rows;
      const notes = rows.find((r) => r.app === 'a-notes');
      assert(notes && notes.url === found, 'the found address shown is sent, not the one typed before: ' + JSON.stringify(notes));
      const cockpit = rows.find((r) => r.app === 'a-cockpit');
      assert(cockpit && !('url' in cockpit), 'No address sends none: ' + JSON.stringify(cockpit));
      assert(resp.status() === 200, 'saved: ' + resp.status());
    });

    await step(AREA + ': Yes on an address changed outside Seraphina saves the address shown (#46)', async () => {
      await setState(t, page, 'outside_edit');
      await openApps(page, t.url, '.ys-card[data-app="a-photos"] [data-action="confirm-address"]');
      const shown = (await page.textContent('.ys-card[data-app="a-photos"] .ys-ask-address .ys-url')).trim();
      const yes = await t.waitPost(page, /\/api\/yourserver\/apps\/a-photos\/address$/, () => page.click('.ys-card[data-app="a-photos"] [data-action="confirm-address"]'));
      const sent = JSON.parse(yes.request().postData());
      assert(yes.status() === 200 && sent.url === shown, 'Yes names the address shown: ' + JSON.stringify(sent) + ' shown ' + shown);
      await page.waitForSelector('.ys-card[data-app="a-photos"] .ys-address[data-state="confirmed"]');
      assert((await page.textContent('.ys-card[data-app="a-photos"] .ys-address .ys-url')).trim() === shown, 'the address saved is the one the owner said yes to');
    });

    await step(AREA + ': No on an address changed outside Seraphina removes only that address; the found one is proposed again (#108)', async () => {
      await setState(t, page, 'outside_edit');
      await openApps(page, t.url, '.ys-card[data-app="a-photos"] .ys-ask-address[data-state="saved"] [data-action="reject-address"]');
      const no = await t.waitPost(page, /\/api\/yourserver\/apps\/a-photos\/address$/, () => page.click('.ys-card[data-app="a-photos"] [data-action="reject-address"]'));
      const sent = JSON.parse(no.request().postData());
      assert(no.status() === 200 && sent.action === 'reject' && !('candidate' in sent), 'No names no found address: ' + JSON.stringify(sent));
      await page.waitForSelector('.ys-card[data-app="a-photos"] .ys-ask-address[data-state="proposed"]');
      const proposed = (await page.textContent('.ys-card[data-app="a-photos"] .ys-ask-address .ys-url')).trim();
      assert(proposed === 'https://photos.example.ts.net', 'the found address is proposed again, never put aside: ' + proposed);
    });

    await step(AREA + ': the part chosen for one app\'s logs is not sent for another (#48)', async () => {
      await setState(t, page, 'checks');
      const logs = (id) => page.waitForResponse((r) => new URL(r.url()).pathname === '/api/yourserver/apps/' + id + '/logs');
      const pick = '[data-part="logs"] select[data-focus="logs-part"]';
      await page.goto(t.url + '/#/apps/a-notes');
      await page.reload();
      await page.waitForSelector(pick);
      const second = (await page.$$eval(pick + ' option', (els) => els.map((o) => o.value))).filter(Boolean)[1];
      await page.selectOption(pick, second);
      const [notes] = await Promise.all([logs('a-notes'), page.click('[data-part="logs"] [data-action="logs"]')]);
      assert(notes.status() === 200 && new URL(notes.url()).searchParams.get('member') === second, 'the part chosen: ' + notes.url());
      await page.goto(t.url + '/#/apps/a-ai');
      await page.waitForSelector('#view-yourserver:not([hidden]) .ys-app[data-app="a-ai"]');
      const [ai] = await Promise.all([logs('a-ai'), page.click('[data-part="logs"] [data-action="logs"]')]);
      assert(ai.status() === 200 && !new URL(ai.url()).searchParams.has('member'), 'another app\'s logs without that part: ' + ai.url() + ' ' + ai.status());
      await page.goto(t.url + '/#/apps/a-notes');
      await page.waitForSelector('#view-yourserver:not([hidden]) .ys-app[data-app="a-notes"]');
      await page.waitForSelector(pick);
      const back = await page.$eval(pick, (e) => ({ value: e.value, texts: Array.from(e.options).map((o) => o.textContent) }));
      assert(back.value === '' && !back.texts.some((x) => /not configured/.test(x)), 'back on the first app: its main part, nothing unknown: ' + JSON.stringify(back));
    });

    await step(AREA + ': the open app page reads its app again when its light changes, and only for its own app (#49)', async () => {
      await setState(t, page, 'ready');
      await page.goto(t.url + '/#/apps/a-photos');
      await page.reload();
      await page.waitForSelector('.ys-app .ys-light[data-tone="ok"]');
      await page.waitForSelector('#conn[data-state="open"]');
      await quiet(page);
      const seen = gets(page);
      try {
        await Promise.all([page.waitForResponse((r) => new URL(r.url()).pathname === '/api/yourserver/apps'),
          page.request.post(t.url + '/__mock/yourserver/app_event', { data: { app: 'a-beszel' } })]);
        await page.waitForTimeout(300);
        assert(seen.count('/api/yourserver/apps/a-photos') === 0, 'an event about another app does not read this one again: ' + seen.seen);
        await page.request.post(t.url + '/__mock/yourserver/app_event', { data: { app: 'a-photos', stop: 'photos' } });
        await page.waitForSelector('.ys-app .ys-light[data-tone="err"]', { timeout: 8000 });
        assert(seen.count('/api/yourserver/apps/a-photos') >= 1, 'read again on its own event');
      } finally { seen.stop(); }
    });

    await step(AREA + ': each record of a change says its outcome in words; a failed keep pass never reads as versions kept (#50)', async () => {
      const clearToasts = () => page.evaluate(() => document.querySelectorAll('#toasts .toast').forEach((x) => x.remove()));
      /** Press Keep the current versions now; the words of the toast it leaves. */
      const keepNow = async () => {
        await clearToasts();
        await Promise.all([page.waitForResponse((r) => new URL(r.url()).pathname === '/api/yourserver/keep' && r.request().method() === 'POST'),
          page.click('[data-action="keep-now"]')]);
        await page.waitForSelector('#toasts .toast');
        return { text: await page.textContent('#toasts .toast'), error: await page.locator('#toasts .toast-error').count() === 1 };
      };
      const row = () => page.$eval('.ys-receipts li', (e) => ({ outcome: e.dataset.outcome, pill: e.querySelector('[data-part="outcome"]').textContent,
        what: (e.querySelector('[data-part="what"]') || e.querySelector('.check-name')).textContent, text: e.textContent }));
      await setState(t, page, 'keep_failed');
      await page.goto(t.url + '/#/server/receipts');
      await page.reload();
      await page.waitForSelector('.ys-receipts li');
      const failed = await row();
      assert(failed.outcome === 'failed' && failed.pill === 'Failed', 'a failed record says so: ' + JSON.stringify(failed));
      assert(!/^Kept/.test(failed.what) && failed.what === 'Keeping previous versions', 'a failed record names the change, never "Kept N versions": ' + failed.what);
      assert(/none were kept and there is nothing to undo/.test(failed.text), 'it says nothing was kept: ' + failed.text);
      assert(!/How to undo it:|Checked:/.test(failed.text), 'no way back or checks about names never added: ' + failed.text);
      await t.areaShot(page, AREA, 'receipts-failed-desktop-dark');
      await page.goto(t.url + '/#/server');
      await page.reload();
      await page.waitForSelector('[data-part="keep"] [data-part="keep-last"]');
      const keep = await page.textContent('[data-part="keep"]');
      assert(!/Last kept:/.test(keep) && /Nothing was kept/.test(keep), 'a failed pass is not "Last kept": ' + keep);
      const pressed = await keepNow();
      assert(/Nothing was kept/.test(pressed.text) && !/already kept/.test(pressed.text) && pressed.error, 'Keep now refused by Docker says nothing was kept, as an error: ' + JSON.stringify(pressed));
      await clearToasts();

      await setState(t, page, 'keep');
      await page.goto(t.url + '/#/server/receipts');
      await page.reload();
      await page.waitForSelector('.ys-receipts li [data-part="outcome"]');
      const done = await row();
      assert(done.pill === 'Done' && /^Kept \d+ versions/.test(done.what) && /How to undo it:/.test(done.text), 'a pass that kept says Done, what it kept and how to undo it: ' + JSON.stringify(done));
      await page.goto(t.url + '/#/server');
      await page.reload();
      await page.waitForSelector('[data-part="keep"] [data-part="keep-last"]');
      assert(/Last kept:/.test(await page.textContent('[data-part="keep-last"]')), 'and its time');
      const again = await keepNow();
      assert(/already kept/.test(again.text) && !again.error, 'Keep now with every version kept: already kept: ' + JSON.stringify(again));
      await page.waitForSelector('[data-part="keep-last"][data-outcome="nothing"]');
      assert(/Last looked:/.test(await page.textContent('[data-part="keep-last"]')), 'a pass with nothing to name is not "Last kept"');
      await clearToasts();

      await setState(t, page, 'keep_partly');
      await page.goto(t.url + '/#/server');
      await page.reload();
      await page.waitForSelector('[data-action="keep-now"]');
      const some = await keepNow();
      assert(/Only some were kept/.test(some.text) && !/already kept/.test(some.text) && !some.error, 'some names refused: only some were kept: ' + JSON.stringify(some));
      await page.waitForSelector('[data-part="keep-last"][data-outcome="partly"]');
      assert(/Last kept:.*Only some were kept/.test(await page.textContent('[data-part="keep-last"]')), 'Last kept, only some');
      await clearToasts();
      await page.goto(t.url + '/#/server/receipts');
      await page.waitForSelector('.ys-receipts li [data-part="outcome"]');
      const partly = await row();
      assert(partly.pill === 'Partly done' && /^Kept \d+ (of \d+ )?versions/.test(partly.what) && /How to undo it:/.test(partly.text), 'a partly record: ' + JSON.stringify(partly));
    });
  } finally {
    for (const v of [d, dl, p, pd]) await v.ctx.close();
  }
}
