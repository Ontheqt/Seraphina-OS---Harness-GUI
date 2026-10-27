// GUI smoke test: drives swfactory/web against tests/gui_mock_server.py in headless Chromium.
//
//   cd <the checkout> && NODE_PATH=$(npm root -g) PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers node tests/gui/smoke.mjs
//
// Exits non-zero when any check fails. Screenshots go to $GUI_SHOTS (default below); the Seraphina OS Harness areas
// save theirs under $GUI_AREA_SHOTS/<area>/ (default: $GUI_SHOTS/areas/<area>/).
//
// Each area of the GUI has its own file of checks, imported below and run with the same harness `t` (see AREA_SUITES):
// home.mjs (Home, branding, the view registry) and routing, tracker, personas, messages, operator, backups,
// yourserver and browser .mjs, one per view module (swfactory/web/views/<area>.js). GUI_ONLY=home,tracker runs only those suites
// (after login).
// For a visual review, tests/gui/screens.mjs (not a test) saves every screen at 1440 and 390 px, light and dark.
import { createRequire } from 'node:module';
import { spawn, execSync } from 'node:child_process';
import { mkdirSync, existsSync, readdirSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { run as section25 } from './section25.mjs';
import { run as homeSuite } from './home.mjs';
import { run as routingSuite } from './routing.mjs';
import { run as trackerSuite } from './tracker.mjs';
import { run as personasSuite } from './personas.mjs';
import { run as messagesSuite } from './messages.mjs';
import { run as operatorSuite } from './operator.mjs';
import { run as backupsSuite } from './backups.mjs';
import { run as yourserverSuite } from './yourserver.mjs';
import { run as browserSuite } from './browser.mjs';
import { run as codexSuite } from './codex.mjs';

/** The Seraphina OS Harness suites, in order. Each exports run(t); t is built in main() (see `harness`). */
const AREA_SUITES = [['home', homeSuite], ['routing', routingSuite], ['tracker', trackerSuite], ['personas', personasSuite],
  ['messages', messagesSuite], ['operator', operatorSuite], ['backups', backupsSuite], ['yourserver', yourserverSuite],
  ['browser', browserSuite], ['codex', codexSuite]];
const ONLY = (process.env.GUI_ONLY || '').split(',').map((x) => x.trim()).filter(Boolean);

const require = createRequire(import.meta.url);
function loadPlaywright() {
  try { return require('playwright'); } catch (e) {
    const root = execSync('npm root -g').toString().trim();
    return require(path.join(root, 'playwright'));
  }
}
const { chromium } = loadPlaywright();

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const SHOTS = process.env.GUI_SHOTS || '/tmp/claude-0/-home-user/03a51815-8655-5582-9e2d-e5c582ed1208/scratchpad/gui-shots';
const SPEED = process.env.GUI_SPEED || '3';
const AREA_SHOTS = process.env.GUI_AREA_SHOTS || path.join(SHOTS, 'areas');
mkdirSync(SHOTS, { recursive: true });

const results = [];
let failures = 0;
const consoleErrors = [];
const dialogs = [];
const httpErrors = [];
let allowHttpErrors = false;

function log(...a) { console.log(...a); }
// A watchdog per step: page.evaluate and keyboard calls have no timeout of their own, so a hung page fails its step.
const STEP_LIMIT_MS = Number(process.env.GUI_STEP_TIMEOUT_MS) || 120000;
async function step(name, fn) {
  const t0 = Date.now();
  let timer = null;
  const watchdog = new Promise((resolve, reject) => {
    timer = setTimeout(() => reject(new Error(`step did not finish within ${STEP_LIMIT_MS / 1000} s`)), STEP_LIMIT_MS);
  });
  try {
    await Promise.race([fn(), watchdog]);
    results.push({ name, ok: true });
    log(`  ok   ${name} (${Date.now() - t0} ms)`);
  } catch (e) {
    failures++;
    results.push({ name, ok: false, error: String(e && e.stack || e) });
    log(`  FAIL ${name}: ${e && e.message ? e.message : e}`);
  } finally {
    clearTimeout(timer);
  }
}
function assert(cond, msg) { if (!cond) throw new Error(msg); }
/** JSON with sorted object keys, to compare request bodies whatever their key order. */
const canon = (v) => JSON.stringify(v, (k, x) => (x && typeof x === 'object' && !Array.isArray(x) ? Object.keys(x).sort().reduce((o, key) => { o[key] = x[key]; return o; }, {}) : x));

function startMock(extra) {
  return new Promise((resolve, reject) => {
    const proc = spawn('python3', [path.join(ROOT, 'tests', 'gui_mock_server.py'), '--port', '0', '--scenario-speed', SPEED, '--ping-interval', '2', ...(extra || [])], { cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe'] });
    let buf = '';
    const timer = setTimeout(() => reject(new Error('mock server did not print READY')), 15000);
    proc.stdout.on('data', (d) => {
      buf += d.toString();
      const m = /READY (http:\/\/\S+)/.exec(buf);
      if (m) { clearTimeout(timer); resolve({ proc, url: m[1] }); }
    });
    proc.stderr.on('data', (d) => process.stderr.write('[mock] ' + d));
    proc.on('exit', (code) => { clearTimeout(timer); reject(new Error('mock server exited with ' + code)); });
  });
}

async function launch() {
  try { return await chromium.launch(); } catch (e) {
    const base = '/opt/pw-browsers';
    const cands = existsSync(base) ? readdirSync(base).filter((d) => /^chromium-\d+/.test(d)).map((d) => path.join(base, d, 'chrome-linux', 'chrome')) : [];
    cands.push('/opt/pw-browsers/chromium-1194/chrome-linux/chrome');
    for (const exe of cands) if (existsSync(exe)) return chromium.launch({ executablePath: exe });
    throw e;
  }
}

function watch(page, label) {
  page.on('console', (m) => {
    if (m.type() !== 'error') return;
    const text = m.text();
    if (allowHttpErrors && /Failed to load resource: (the server responded with a status of (400|401|404|409|413|429|5\d\d)|net::ERR_FAILED)/.test(text)) return;
    consoleErrors.push(`[${label}] ${text}`);
  });
  page.on('pageerror', (e) => consoleErrors.push(`[${label}] pageerror: ${e.message}`));
  // Which request a "Failed to load resource" line was about (Chromium's console text names no URL).
  page.on('response', (r) => {
    if (r.status() >= 400 && !allowHttpErrors) httpErrors.push(`[${label}] ${r.status()} ${r.request().method()} ${new URL(r.url()).pathname}`);
  });
  page.on('dialog', async (d) => { dialogs.push(`[${label}] ${d.type()}: ${d.message()}`); await d.dismiss().catch(() => {}); });
}

const shot = (page, name) => page.screenshot({ path: path.join(SHOTS, name + '.png') });
/** A screenshot without the toasts the steps before it left (they cover the view). */
async function cleanShot(page, name) {
  await page.evaluate(() => document.querySelectorAll('#toasts .toast, #toasts .toast-more').forEach((t) => t.remove()));
  await page.waitForTimeout(80);
  await shot(page, name);
}
const visibleCount = (page, sel) => page.locator(sel).filter({ visible: true }).count();
async function waitPost(page, re, action) {
  const [resp] = await Promise.all([
    page.waitForResponse((r) => r.request().method() === 'POST' && re.test(new URL(r.url()).pathname), { timeout: 15000 }),
    action(),
  ]);
  return resp;
}
async function noOverflow(page, label) {
  const m = await page.evaluate(() => {
    const main = document.getElementById('main');
    const panel = document.querySelector('#drawer:not([hidden]) .panel:not([hidden])');
    return { doc: document.documentElement.scrollWidth, inner: window.innerWidth, main: main ? main.scrollWidth - main.clientWidth : 0,
      panel: panel ? panel.scrollWidth - panel.clientWidth : 0 };
  });
  assert(m.panel <= 1, `${label}: drawer panel scrolls horizontally by ${m.panel}px`);
  assert(m.doc <= m.inner, `${label}: page scrollWidth ${m.doc} > innerWidth ${m.inner}`);
  assert(m.main <= 1, `${label}: main view scrolls horizontally by ${m.main}px`);
}

/** A signed-in page of its own: label names it in console errors; opts {width, height, colorScheme, phone, mockUrl}. */
async function newPage(browser, url, label, opts) {
  opts = opts || {};
  const phone = !!opts.phone;
  const ctx = await browser.newContext({ viewport: { width: opts.width || (phone ? 390 : 1440), height: opts.height || (phone ? 844 : 900) },
    colorScheme: opts.colorScheme || (phone ? 'light' : 'dark'), deviceScaleFactor: phone ? 2 : 1, isMobile: phone, hasTouch: phone });
  const at = opts.mockUrl || url;
  await ctx.addCookies([{ name: 'sf_token', value: 'test-token', url: at }]);
  const page = await ctx.newPage();
  watch(page, label);
  return { ctx, page, url: at };
}
/** A screenshot of an area's screen: $GUI_AREA_SHOTS/<area>/<name>.png, without leftover toasts. */
async function areaShot(page, area, name) {
  const dir = path.join(AREA_SHOTS, area);
  mkdirSync(dir, { recursive: true });
  await page.evaluate(() => document.querySelectorAll('#toasts .toast, #toasts .toast-more').forEach((t) => t.remove()));
  await page.waitForTimeout(80);
  await page.screenshot({ path: path.join(dir, name + '.png') });
}

async function main() {
  const { proc, url } = await startMock();
  log('mock server at ' + url);
  const browser = await launch();
  /** What every suite file gets: the browser, the main mock's url and the helpers of this file. */
  const harness = { browser, url, startMock, step, assert, canon, waitPost, shot, cleanShot, noOverflow, watch,
    allowHttp: (v) => { allowHttpErrors = v; }, newPage: (label, opts) => newPage(browser, url, label, opts), areaShot };
  try {
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: 'dark', deviceScaleFactor: 1 });
    const page = await ctx.newPage();
    watch(page, 'desktop');

    await step('login page explains the token and rejects a bad one', async () => {
      await page.goto(url + '/');
      await page.waitForSelector('#token');
      const help = await page.textContent('body');
      assert(/factory url/.test(help), 'login page must mention "factory url"');
      allowHttpErrors = true;
      await page.fill('#token', 'wrong-token');
      await page.click('#submit');
      await page.waitForSelector('#error:not([hidden])');
      allowHttpErrors = false;
      await page.fill('#token', 'test-token');
      await Promise.all([page.waitForURL(url + '/'), page.click('#submit')]);
      await page.waitForSelector('#app');
      // Home is the default route of a harness in use (Welcome is for a fresh install); the board is a click away.
      await page.waitForSelector('#view-home:not([hidden]) .home-card[data-section="factory"]');
      assert(/Home · Seraphina OS Harness$/.test(await page.title()), 'title: ' + await page.title());
      await page.goto(url + '/#/factory');
      await page.waitForSelector('#view-factory:not([hidden])');
    });

    if (ONLY.length) {
      for (const [name, suite] of AREA_SUITES) if (ONLY.includes(name)) await suite(harness);
      await step('zero console errors and no dialogs', async () => {
        assert(consoleErrors.length === 0, 'console errors:\n' + consoleErrors.join('\n') + (httpErrors.length ? '\nHTTP errors:\n' + httpErrors.join('\n') : ''));
        assert(dialogs.length === 0, 'dialogs: ' + dialogs.join(' | '));
      });
      return;
    }

    await step('factory line strip renders every stage with models and counts', async () => {
      await page.waitForSelector('.node[data-node="spec"]');
      const nodes = await page.$$eval('.strip-canvas .node', (els) => els.map((e) => e.dataset.node));
      for (const n of ['feature', 'spec', 'builder', 'rev:security', 'rev:ux', 'rev:ui', 'rev:code', 'wait', 'approver', 'merge', 'human']) assert(nodes.includes(n), 'missing node ' + n);
      await page.waitForFunction(() => Number(document.querySelector('.node[data-node="human"] .node-count').textContent) >= 2);
      const model = await page.textContent('.node[data-node="spec"] .node-model');
      assert(/deepseek-v4-pro/.test(model), 'spec-writer box should show its resolved model, got ' + model);
      const uiModel = await page.textContent('.node[data-node="rev:code"] .node-model');
      assert(/strata/.test(uiModel), 'reviewer-code override should resolve to strata, got ' + uiModel);
      assert(await page.locator('.wire-label.loop').count() === 1, 'loop-back label missing');
      assert(/max 2/.test(await page.textContent('.wire-label.loop')), 'loop label should show max resumes');
      const fit = await page.evaluate(() => { const s = document.getElementById('strip-scroll'); return [s.scrollWidth, s.clientWidth]; });
      assert(fit[0] <= fit[1], 'the whole factory line should fit at 1440px: ' + fit);
      const sub = await page.textContent('.node[data-node="human"] .node-sub');
      assert(/approve · rework · reject/.test(sub) && !/failed/.test(sub), 'needs-human box copy: ' + sub);
    });

    await step('board columns and cards render and update live', async () => {
      const cols = await page.$$eval('.board .col', (els) => els.map((e) => e.dataset.col));
      assert(JSON.stringify(cols) === JSON.stringify(['queued', 'spec', 'build', 'review', 'approve', 'needs-human', 'merged', 'closed']), 'columns: ' + cols);
      assert(await page.locator('.col-needs-human .card').count() >= 2, 'needs-human column should hold the seeded jobs');
      const merged0 = await page.$$eval('.col-merged .card', (els) => els.map((e) => e.dataset.job));
      await page.waitForSelector('.chip.v-running', { timeout: 30000 });
      await page.waitForSelector('.col-review .card .chip.v-CHANGES, .col-build .card .chip.v-CHANGES, .col-merged .card .chip.v-CHANGES', { timeout: 30000 });
      await page.waitForFunction((before) => Array.from(document.querySelectorAll('.col-merged .card')).some((e) => !before.includes(e.dataset.job)), merged0, { timeout: 45000 });
      const rows = await page.$$eval('.card[data-job="my-app/002-rate-limit-signup-form"] .round', (els) => els.length);
      assert(rows === 3, 'job 002 should show 3 review rounds, got ' + rows);
      const roundText = await page.textContent('.card[data-job="my-app/002-rate-limit-signup-form"] .card-round');
      assert(/r3 · resumes 2\/2/.test(roundText), 'round line: ' + roundText);
      const idw = await page.$eval('.card[data-job="my-app/002-rate-limit-signup-form"] .card-id', (e) => [e.scrollWidth, e.clientWidth, e.closest('.card').clientWidth]);
      assert(idw[0] <= idw[1] || idw[1] >= idw[2] * 0.6, 'job id should get most of the card header: ' + idw);
      const roles = await page.$$eval('.board .card', (els) => els.map((e) => (e.getAttribute('role') || 'button') + '/' + e.parentElement.getAttribute('role')));
      assert(roles.every((r) => r === 'button/listitem'), 'cards are buttons inside list items: ' + roles.slice(0, 3));
      const nh = await page.$eval('.col-needs-human', (e) => { const r = e.getBoundingClientRect(); return r.right <= window.innerWidth + 1 && r.left >= 0; });
      assert(nh, 'Needs human column should be on screen at 1440px');
    });
    await shot(page, 'desktop-dark');

    await step('job drawer opens from a card with all tabs', async () => {
      await page.click('.card[data-job="my-app/002-rate-limit-signup-form"]');
      await page.waitForSelector('#drawer:not([hidden]) #drawer-title');
      assert(/#\/job\/my-app\/002-rate-limit-signup-form/.test(page.url()), 'hash: ' + page.url());
      // Job 002 has 16 run logs; wait for all of them (the open, latest run changes while they load).
      await page.waitForFunction(() => document.querySelectorAll('#panel-live .run').length >= 16, null, { timeout: 15000 });
      const heads = await page.$$eval('#panel-live .run-agent', (els) => els.map((e) => e.textContent));
      assert(heads.includes('spec-writer') && heads.includes('reviewer-security'), 'run headers: ' + heads.join(','));
      const durs = await page.$$eval('#panel-live .run-dur', (els) => els.map((e) => e.textContent));
      assert(durs.filter((d) => d && d !== '0s' && d !== '<1s').length >= 5, 'finished runs should show real durations: ' + durs.join(','));
      assert(await page.evaluate(() => document.getElementById('app').hasAttribute('inert') && document.getElementById('drawer').getAttribute('role') === 'dialog'), 'the drawer is modal: app inert, role=dialog');
      await page.locator('#panel-live .run[open] .t-tool > summary').first().click();
      await page.waitForSelector('#panel-live .t-tool[open] .t-pre');
      await page.click('#tab-files');
      await page.waitForFunction(() => document.querySelectorAll('#panel-files .file-btn').length >= 10);
      assert(await page.locator('#panel-files .file-btn .verdict').count() >= 5, 'verdict badges missing');
      await page.click('#panel-files .file-btn[data-file="round-3/review-security.md"]');
      await page.waitForSelector('#panel-files .file-view .md h2');
      await page.click('#tab-diff');
      await page.waitForSelector('#panel-diff .diff-file .dl.add');
      assert(await page.locator('#panel-diff .stat-row').count() >= 2, 'diff stat rows missing');
      await page.click('#tab-raw');
      const raw = await page.textContent('#panel-raw .json-view');
      assert(raw.includes('"id": "002-rate-limit-signup-form"'), 'raw tab should show job.json');
      await page.click('#tab-live');
    });
    await shot(page, 'drawer');

    await step('Escape closes the drawer; a hash deep link reopens it on a tab', async () => {
      await page.keyboard.press('Escape');
      await page.waitForSelector('#drawer[hidden]', { state: 'attached' });
      assert(/#\/factory$/.test(page.url()), 'hash after close: ' + page.url());
      await page.goto(url + '/#/job/my-app/004-render-user-notes/files');
      await page.waitForSelector('#drawer:not([hidden])');
      assert(await page.getAttribute('#tab-files', 'aria-selected') === 'true', 'files tab should be selected from the hash');
      await page.waitForSelector('#panel-files .file-view .md');
    });

    await step('hostile content renders as text and never executes', async () => {
      const md = await page.textContent('#panel-files .file-view .md');
      assert(md.includes("<img src=x onerror=alert('decision')>"), 'hostile HTML should be visible as text');
      const bad = await page.evaluate(() => ({
        img: document.querySelectorAll('img').length,
        iframe: document.querySelectorAll('iframe').length,
        handlers: document.querySelectorAll('[onerror],[onload],[onclick]').length,
        js: Array.from(document.querySelectorAll('a[href]')).filter((a) => !/^(https?:|#|\/)/i.test(a.getAttribute('href'))).length,
        scripts: document.querySelectorAll('body script').length,
        safe: Array.from(document.querySelectorAll('.md a[href^="https://example.com"]')).every((a) => a.rel === 'noopener noreferrer' && a.target === '_blank'),
        safeCount: document.querySelectorAll('.md a[href^="https://example.com"]').length,
        tasks: document.querySelectorAll('.md li.task input[type=checkbox][disabled]').length,
        table: document.querySelectorAll('.md table th').length,
      }));
      assert(bad.img === 0 && bad.iframe === 0 && bad.handlers === 0 && bad.scripts === 0, 'injected elements: ' + JSON.stringify(bad));
      assert(bad.js === 0, 'non-http link rendered: ' + JSON.stringify(bad));
      assert(bad.safeCount >= 1 && bad.safe, 'https link should render with rel/target');
      assert(bad.tasks === 2 && bad.table === 2, 'checkboxes/table: ' + JSON.stringify(bad));
      await page.click('#tab-diff');
      await page.waitForSelector('#panel-diff .callout-warn');
      const diffText = await page.textContent('#panel-diff');
      assert(diffText.includes("<img src=x onerror=alert('diff')>"), 'diff should show hostile line as text');
      // Nested emphasis and brackets once hung the renderer: render them and time it.
      await page.click('#tab-files');
      const t0 = Date.now();
      await page.click('#panel-files .file-btn[data-file="round-1/review-code.md"]', { timeout: 5000 });
      await page.waitForSelector('#panel-files .file-view .md a[href="https://example.com/rfc6749"]', { timeout: 5000 });
      assert(Date.now() - t0 < 3000, 'nested markdown took ' + (Date.now() - t0) + ' ms');
      const nested = await page.textContent('#panel-files .file-view .md a[href="https://example.com/rfc6749"]');
      assert(nested === 'RFC 6749 [section 4.1]', 'nested link label: ' + nested);
      assert(await page.locator('#panel-files .file-view .md a[href^="javascript"]').count() === 0, 'nested javascript: link must not render');
      await page.click('#tab-live');
      await page.waitForSelector('#panel-live .run');
      const card = await page.textContent('.card[data-job="my-app/004-render-user-notes"] .card-feature');
      assert(card.includes('<b>markdown</b>'), 'card feature should be literal text');
      assert(await page.locator('img').count() === 0, 'no img elements anywhere');
      assert(dialogs.length === 0, 'dialogs fired: ' + dialogs.join(' | '));
    });

    await step('diff tab shows a git error from the server as an error, not as "no changes"', async () => {
      await page.goto(url + '/#/job/docs-site/002-migrate-the-docs-to-astro/diff');
      await page.waitForSelector('#panel-diff .error-state');
      const t = await page.textContent('#panel-diff .error-state');
      assert(/unknown revision/.test(t), 'error text: ' + t);
      assert(await page.locator('#panel-diff .error-state button:has-text("Retry")').count() === 1, 'retry button');
      assert(!/No changes on the branch yet/.test(await page.textContent('#panel-diff')), 'must not claim no changes');
    });

    await step('failed job: the run that failed is marked failed', async () => {
      await page.goto(url + '/#/job/my-app/003-fix-the-flaky-checkout-test');
      await page.waitForSelector('#panel-live .run.is-failed');
      const st = await page.textContent('#panel-live .run.is-failed .run-status');
      assert(/failed/.test(st), 'status: ' + st);
      assert(await page.locator('#panel-live .run.is-failed[open]').count() === 1, 'failed run expanded');
    });

    await step('a push after a merge says its outcome in words (DESIGN §40): unknown is amber, says what the remote has and links its tracker item; succeeded and refused on the cards', async () => {
      await page.goto(url + '/#/job/my-app/001-add-dark-mode-toggle');
      await page.waitForSelector('#drawer-head .push-box[data-push="unknown"]');
      const box = await page.textContent('#drawer-head .push-box');
      assert(/The push of main to origin: outcome unknown/.test(box) && /Outcome unknown: it may or may not have happened/.test(box), 'the outcome in words: ' + box);
      assert(/Look at the remote before you push again: nothing is pushed again by itself\./.test(box), 'what to do');
      assert(/the remote has main at 0f9e8d7c6b5a \(not this commit\)/.test(box) && /128/.test(box), 'what a look at the remote found, and git’s exit: ' + box);
      assert(/<b>\(raw\)<\/b>/.test(box) && await page.locator('#drawer-head .push-box b').count() === 0, 'git’s words stay text');
      assert(await page.getAttribute('#drawer-head .push-box .item-link', 'href') === '#/housekeeping/system%3AS-10', 'its tracker item');
      assert(/tone-warn/.test(await page.getAttribute('#drawer-head .push-box', 'class')), 'amber');
      await shot(page, 'job-push-unknown');
      const st = await (await page.request.get(url + '/api/state')).json();
      const pushed = st.jobs.filter((j) => j.push).map((j) => [j.repo + '/' + j.id, j.push.outcome]);
      assert(pushed.length === 3 && new Set(pushed.map((x) => x[1])).size === 3, 'one push of each outcome: ' + JSON.stringify(pushed));
      await page.keyboard.press('Escape');
      await page.waitForSelector('#drawer[hidden]', { state: 'attached' });
      await page.goto(url + '/#/factory');
      for (const [key, outcome] of pushed) {
        await page.waitForSelector('.card[data-job="' + key + '"] .tag-push');
        const chip = await page.$eval('.card[data-job="' + key + '"] .tag-push', (e) => [e.dataset.push, e.textContent, e.className]);
        assert(chip[0] === outcome && chip[1] === { succeeded: 'pushed', failed: 'push failed', unknown: 'push: outcome unknown' }[outcome], 'the card says it: ' + JSON.stringify(chip));
        assert(!/tag-ok/.test(chip[2]), 'never a bare green');
      }
      const refused = pushed.find((x) => x[1] === 'failed')[0];
      await page.goto(url + '/#/job/' + refused);
      await page.waitForSelector('#drawer-head .push-box[data-push="failed"]');
      assert(/\[rejected\] \(fetch first\)/.test(await page.textContent('#drawer-head .push-box')) && /tone-err/.test(await page.getAttribute('#drawer-head .push-box', 'class')), 'a refusal in git’s words, red');
      await page.keyboard.press('Escape');
    });

    await step('approve flow posts to the API', async () => {
      await page.goto(url + '/#/job/my-app/002-rate-limit-signup-form');
      await page.waitForSelector('#drawer-head [data-action="approve"]');
      await page.click('#drawer-head [data-action="approve"]');
      await page.waitForSelector('dialog[open]');
      await page.check('dialog[open] input[data-carry-ok]');   // its must-do items move to follow-ups (tracker.mjs checks the rest)
      const resp = await waitPost(page, /\/api\/jobs\/my-app\/002-rate-limit-signup-form\/approve$/, () => page.click('dialog[open] button[data-action="ok"]'));
      assert(resp.status() === 200, 'approve status ' + resp.status());
      await page.waitForSelector('dialog[open]', { state: 'detached' });
      await page.waitForSelector('.toast-ok');
    });

    await step('rework flow validates the note and posts it', async () => {
      await page.goto(url + '/#/job/my-app/004-render-user-notes');
      await page.waitForSelector('#drawer-head [data-action="rework"]');
      await page.click('#drawer-head [data-action="rework"]');
      await page.waitForSelector('dialog[open] textarea');
      await page.click('dialog[open] button[data-action="ok"]');
      await page.waitForSelector('dialog[open] .form-error:not([hidden])');
      await page.fill('dialog[open] textarea', 'Escape HTML in note titles too, and add a test.');
      await page.keyboard.press('Escape');
      await page.waitForTimeout(150);
      assert(await page.locator('dialog[open] textarea').count() === 1, 'Esc with typed text keeps the dialog open');
      assert(/Esc again/.test(await page.textContent('dialog[open] .form-error')), 'Esc warns before discarding');
      const resp = await waitPost(page, /\/rework$/, () => page.click('dialog[open] button[data-action="ok"]'));
      assert(resp.status() === 200, 'rework status ' + resp.status());
      assert(JSON.parse(resp.request().postData()).note.startsWith('Escape HTML'), 'rework body');
    });

    await step('reject flow confirms and posts a reason', async () => {
      await page.goto(url + '/#/job/my-app/003-fix-the-flaky-checkout-test');
      await page.waitForSelector('#drawer-head [data-action="reject"]');
      assert(await page.locator('#drawer-head [data-action="retry"]').count() === 1, 'failed job should offer Retry');
      await page.click('#drawer-head [data-action="reject"]');
      await page.waitForSelector('dialog[open] input');
      await page.fill('dialog[open] input', 'not worth it');
      const resp = await waitPost(page, /\/reject$/, () => page.click('dialog[open] button[data-action="ok"]'));
      assert(resp.status() === 200, 'reject status ' + resp.status());
      assert(JSON.parse(resp.request().postData()).reason === 'not worth it', 'reject body');
      await page.waitForFunction(() => document.querySelector('#drawer-head .pill') && /Rejected/.test(document.querySelector('#drawer-head .pill').textContent));
      await page.keyboard.press('Escape');
    });

    await step('keyboard: / focuses the filter, n opens New feature, Ctrl+Enter runs it', async () => {
      await page.goto(url + '/#/factory');
      await page.waitForSelector('.board .card');
      await page.locator('body').click({ position: { x: 700, y: 880 } }).catch(() => {});
      await page.keyboard.press('/');
      assert(await page.evaluate(() => document.activeElement.id) === 'board-search', '/ should focus the board filter');
      await page.keyboard.type('rate limit');
      await page.waitForFunction(() => document.querySelectorAll('.board .card').length === 1);
      await page.keyboard.press('Escape');
      await page.waitForFunction(() => document.querySelectorAll('.board .card').length > 3);
      await page.evaluate(() => document.activeElement.blur());
      await page.keyboard.press('n');
      await page.waitForSelector('dialog[open] #nf-feature');
      assert(await page.evaluate(() => document.activeElement.id) === 'nf-feature', 'feature textarea should be focused');
      await page.keyboard.type('Add a CSV export button to the audit log');
      const resp = await waitPost(page, /^\/api\/jobs$/, () => page.keyboard.press('Control+Enter'));
      assert(resp.status() === 201, 'create job status ' + resp.status());
      await page.waitForSelector('dialog[open]', { state: 'detached' });
      await page.waitForSelector('.card-feature:text("Add a CSV export button to the audit log")');
    });

    await step('backlog panel: add an item and run next', async () => {
      await page.click('#backlog-toggle');
      await page.waitForSelector('#backlog:not([hidden]) .backlog-item');
      await page.fill('#backlog .backlog-add input', 'Add SSO login');
      const resp = await waitPost(page, /\/api\/repos\/my-app\/backlog$/, () => page.click('#backlog .backlog-add button'));
      assert(resp.status() === 200, 'backlog add status ' + resp.status());
      await page.waitForSelector('#backlog .bl-text:text("Add SSO login")');
      const next = await waitPost(page, /\/api\/repos\/my-app\/next$/, () => page.click('#backlog button:has-text("Run next")'));
      assert(next.status() === 200, 'run next status ' + next.status());
      await page.waitForSelector('#backlog .backlog-item.done .bl-job');
      await page.click('#backlog-toggle');
    });

    await step('theme toggle cycles auto → light → dark → auto', async () => {
      await page.click('#theme-toggle');
      assert(await page.getAttribute('html', 'data-theme') === 'light', 'light');
      const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor);
      assert(bg === 'rgb(244, 245, 247)', 'light body background: ' + bg);
      await page.waitForTimeout(250);
      await shot(page, 'desktop-light');
      await page.click('#theme-toggle');
      assert(await page.getAttribute('html', 'data-theme') === 'dark', 'dark');
      await page.click('#theme-toggle');
      assert(await page.getAttribute('html', 'data-theme') === null, 'auto removes data-theme');
      assert(await page.evaluate(() => localStorage.getItem('sf.theme')) === null, 'auto is not stored');
    });

    let chatId = null;
    await step('chat: create, send, stream, hand off', async () => {
      await page.goto(url + '/#/chat');
      await page.waitForSelector('#chat-list .chat-item');
      await page.click('#new-chat-btn');
      await page.waitForSelector('dialog[open] #nc-repo');
      await page.selectOption('dialog[open] #nc-repo', 'my-app');
      await page.click('dialog[open] input[value="repo"]');
      await page.waitForSelector('dialog[open] .callout-warn');
      await page.click('dialog[open] input[value="worktree"]');
      const resp = await waitPost(page, /^\/api\/chats$/, () => page.click('dialog[open] button[data-action="ok"]'));
      assert(resp.status() === 201, 'create chat ' + resp.status());
      chatId = (await resp.json()).chat.id;
      await page.waitForFunction((id) => location.hash === '#/chat/' + id, chatId);
      await page.waitForSelector('.composer textarea:not([disabled])');
      await page.fill('.composer textarea', 'Explain the <b>auth</b> flow');
      const send = await waitPost(page, /\/messages$/, () => page.press('.composer textarea', 'Enter'));
      assert(send.status() === 202, 'send status ' + send.status());
      await page.waitForSelector('.msg-user .bubble:text("Explain the <b>auth</b> flow")');
      await page.waitForSelector('.chat-running:not([hidden])');
      assert(await page.locator('.composer textarea').isDisabled(), 'composer disabled while running');
      await page.waitForSelector('.turn .t-tool', { timeout: 15000 });
      await page.waitForFunction(() => /Here is what I found/.test((document.querySelector('.turn .t-text:last-child') || {}).textContent || ''), null, { timeout: 20000 });
      await page.waitForSelector('.composer textarea:not([disabled])', { timeout: 20000 });
      await page.waitForSelector('.turn .t-text .md li');
      await page.waitForTimeout(200);
      await shot(page, 'chat');
      await page.click('.chat-head button:has-text("Hand off to factory")');
      await page.waitForSelector('dialog[open] input');
      const ho = await waitPost(page, /\/handoff$/, () => page.click('dialog[open] button[data-action="ok"]'));
      assert(ho.status() === 200, 'handoff status ' + ho.status());
      await page.waitForSelector('.toast-ok :text("Handed off")');
      await page.waitForSelector('.chat-head a.tag-accent');
      // A reload replays the turn's events: the turn must still show once (history + replay).
      await page.reload();
      await page.waitForSelector('.chat-thread .msg-user');
      await page.waitForTimeout(700);
      const counts = await page.evaluate(() => ({ users: document.querySelectorAll('.chat-thread .msg-user').length, assistant: document.querySelectorAll('.chat-thread .msg-assistant').length }));
      assert(counts.users === 1 && counts.assistant === 1, 'turn shown once after reload: ' + JSON.stringify(counts));
    });

    await step('chat: chat.removed from elsewhere drops it from the list', async () => {
      const r = await page.request.post(url + '/api/chats', { data: { repo: 'docs-site', mode: 'repo' } });
      const id = (await r.json()).chat.id;
      await page.waitForSelector('#chat-list [data-chat="' + id + '"]');
      await page.request.post(url + '/api/chats/' + id + '/delete', { data: {} });
      await page.waitForSelector('#chat-list [data-chat="' + id + '"]', { state: 'detached', timeout: 5000 });
    });

    await step('chat: cancel a running turn', async () => {
      await page.fill('.composer textarea', 'Now refactor it');
      await waitPost(page, /\/messages$/, () => page.press('.composer textarea', 'Enter'));
      await page.waitForSelector('.chat-running:not([hidden]) button');
      const resp = await waitPost(page, /\/cancel$/, () => page.click('.chat-running button'));
      assert(resp.status() === 200, 'cancel status ' + resp.status());
      await page.waitForSelector('.composer textarea:not([disabled])', { timeout: 15000 });
    });

    await step('settings: load, edit, save only changed sections, test models, server errors inline', async () => {
      await page.goto(url + '/#/settings');
      await page.waitForSelector('#set-providers .prov-row:not(.cfg-head)');
      assert(await page.locator('#set-providers .prov-row:not(.cfg-head)').count() >= 8, 'providers rows');
      const provFit = await page.evaluate(() => {
        const del = document.querySelector('#set-providers .prov-row:not(.cfg-head) .pc-del button');
        const max = document.querySelector('#set-providers input[aria-label="strata max concurrency"]');
        const card = document.getElementById('set-providers').getBoundingClientRect();
        return [del, max].every((e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.right <= card.right && r.left >= card.left; });
      });
      assert(provFit, 'every provider field and Remove button is visible without scrolling at 1440px');
      assert(await page.locator('#set-providers .tag-ok').count() >= 1 && await page.locator('#set-providers .tag-warn').count() >= 1, 'key badges');
      const html = await page.content();
      assert(!html.includes('real-secret-value'), 'secrets must not be displayed');
      const maxRes = page.locator('#set-pipeline input[type=number]').first();
      await maxRes.fill('3');
      await page.waitForSelector('.save-bar:not([hidden])');
      const save = await waitPost(page, /^\/api\/config$/, () => page.click('.save-bar button:has-text("Save changes")'));
      assert(save.status() === 200, 'save status ' + save.status());
      const body = JSON.parse(save.request().postData());
      assert(JSON.stringify(Object.keys(body)) === '["pipeline"]' && body.pipeline.max_resumes === 3, 'patch body: ' + JSON.stringify(body));
      await page.waitForSelector('.toast-ok :text("Settings saved")');
      // client-side JSON validation
      const extra = page.locator('#set-models textarea[aria-label="haiku extra_body JSON"]');
      await extra.fill('{bad json');
      await page.waitForSelector('#set-models .field-error:not([hidden])');
      assert(await page.locator('.save-bar button:has-text("Save changes")').isDisabled(), 'save disabled while JSON is invalid');
      await extra.fill('{"thinking": {"type": "disabled"}}');
      await page.waitForFunction(() => !document.querySelector('.save-bar') || document.querySelector('.save-bar').hidden);
      // server-side validation error shown inline
      await page.fill('#set-providers input[aria-label="vllm base URL"]', '');
      allowHttpErrors = true;
      const bad = await waitPost(page, /^\/api\/config$/, () => page.click('.save-bar button:has-text("Save changes")'));
      assert(bad.status() === 400, 'expected 400, got ' + bad.status());
      await page.waitForSelector('#set-providers .form-error:has-text("base_url")');
      await page.waitForSelector('.save-bar .form-error');
      allowHttpErrors = false;
      await page.click('.save-bar button:has-text("Discard")');
      await page.waitForFunction(() => !document.querySelector('.save-bar') || document.querySelector('.save-bar').hidden);
      // model tests
      const t1 = await waitPost(page, /\/api\/models\/opus\/test$/, () => page.click('#set-models .mdl-row[data-model="opus"] button:has-text("Test")'));
      assert(t1.status() === 200, 'test status');
      await page.waitForSelector('#set-models .test-result.err');
      await waitPost(page, /\/api\/models\/local\/test$/, () => page.click('#set-models .mdl-row[data-model="local"] button:has-text("Test")'));
      await page.waitForSelector('#set-models .test-result.ok');
      // repos: add + scout
      await page.fill('#set-repos input[placeholder="/home/you/code/my-app"]', '/home/me/code/new-thing');
      const add = await waitPost(page, /^\/api\/repos$/, () => page.click('#set-repos button:has-text("Add repo")'));
      assert(add.status() === 200, 'add repo status ' + add.status());
      await page.waitForSelector('#set-repos .repo-name:text("new-thing")');
      const sc = await waitPost(page, /\/api\/repos\/my-app\/scout$/, () => page.locator('#set-repos .repo-row', { hasText: 'my-app' }).locator('button:has-text("Scout")').click());
      assert(sc.status() === 200, 'scout status');
      await page.locator('#set-repos .repo-row', { hasText: 'my-app' }).locator('button:has-text("View draft")').waitFor({ timeout: 15000 });
      await page.goto(url + '/#/settings/models');
      await page.waitForSelector('#set-models');
      await page.waitForTimeout(300);
      await shot(page, 'settings');
    });

    await step('skip link focuses the content without changing the view', async () => {
      await page.goto(url + '/#/settings');
      await page.waitForSelector('#set-providers');
      await page.reload();
      await page.waitForSelector('#set-providers');
      await page.keyboard.press('Tab');
      assert(await page.evaluate(() => document.activeElement.classList.contains('skip-link')), 'first Tab reaches the skip link');
      await page.keyboard.press('Enter');
      await page.waitForTimeout(150);
      assert(/#\/settings$/.test(page.url()), 'hash kept: ' + page.url());
      assert(await page.isVisible('#view-settings'), 'settings still visible');
      assert(await page.evaluate(() => document.activeElement.id) === 'main', 'focus moved to main');
    });

    await step('doctor renders grouped checks and re-runs', async () => {
      await page.goto(url + '/#/doctor');
      await page.waitForSelector('.check-row');
      assert(await page.locator('.check-row').count() >= 8, 'check rows');
      assert(await page.getAttribute('#doctor-badge', 'data-level') === 'error', 'doctor badge should be red');
      assert(await page.locator('.check-group').count() >= 3, 'groups');
      const [req] = await Promise.all([page.waitForRequest((r) => r.url().endsWith('/api/doctor')), page.click('button:has-text("Re-run checks")')]);
      assert(req, 'doctor re-run request');
    });

    await step('agents view lists agents and renders a prompt', async () => {
      await page.goto(url + '/#/agents');
      await page.waitForSelector('.agent-card');
      assert(await page.locator('.agent-card').count() >= 8, 'agent cards');
      await page.click('.agent-card:has-text("reviewer-ui")');
      await page.waitForFunction(() => location.hash === '#/agents/reviewer-ui');
      await page.waitForSelector('.agent-viewer-body .md p');
      assert(await page.locator('img').count() === 0, 'hostile agent description must not inject');
    });

    // ---------------------------------------------------------------- routing (DESIGN §23)
    await step('routing view: every step of the line, LOCAL/API, tier fallback; saves only changed routes, null removes one', async () => {
      await page.goto(url + '/#/factory');
      await page.waitForSelector('#strip-routing');
      await page.click('#strip-routing');
      await page.waitForFunction(() => location.hash === '#/settings/routing');
      await page.waitForSelector('#set-routing .rt-box[data-agent="approver"]');
      const boxes = await page.$$eval('#set-routing .rt-box', (els) => els.map((e) => e.dataset.agent));
      for (const a of ['spec-writer', 'builder', 'reviewer-security', 'reviewer-ux', 'reviewer-ui', 'reviewer-code', 'approver', 'lead', 'coder', 'repo-scout', '*']) assert(boxes.includes(a), 'routing box missing: ' + a);
      const line = await page.$$eval('#set-routing .rt-line .rt-box', (els) => els.map((e) => e.dataset.agent));
      assert(line.join(',') === 'spec-writer,builder,reviewer-security,reviewer-ux,reviewer-ui,reviewer-code,approver', 'the line in order: ' + line);
      assert(/tier opus/.test(await page.textContent('#set-routing .rt-box[data-agent="spec-writer"] .rt-own')), 'a step without a route shows the tier it falls back to');
      assert(/^Inherit: tier opus$/.test(await page.$eval('#set-routing select[aria-label="spec-writer model"]', (s) => s.options[s.selectedIndex].textContent)), 'the inherit option names the tier');
      const groups = await page.$$eval('#set-routing select[aria-label="spec-writer model"] optgroup', (gs) => gs.map((g) => g.label + ':' + g.children.length));
      assert(groups.length === 2 && /^Inherit:1$/.test(groups[0]) && /^Pin a model:/.test(groups[1]), 'inherit and pins are separate groups: ' + groups);
      assert(await page.$eval('#set-routing select[aria-label="spec-writer driver"]', (s) => s.options[s.selectedIndex].textContent) === 'Inherit: builtin', 'the driver inherit option says so');
      assert(/Advanced/.test(await page.textContent('#set-routing .rt-box[data-agent="builder"] .rt-adv-btn')), 'Advanced is a labelled button');
      assert(await page.locator('#set-routing .rt-top .rt-box[data-agent="*"]').count() === 1, 'Default (*) leads the view');
      const y0 = await page.$eval('#set-routing .rt-box[data-agent="approver"]', (e) => e.getBoundingClientRect().top);
      assert(await page.locator('#set-routing .rt-box[data-agent="reviewer-code"] .tag-local').count() === 1, 'reviewer-code (strata, loopback) is LOCAL');
      assert(await page.locator('#set-routing .rt-box[data-agent="spec-writer"] .tag-api').count() === 1, 'spec-writer (deepseek) is API');
      await page.selectOption('#set-routing select[aria-label="builder model"]', 'local');
      await page.selectOption('#set-routing select[aria-label="builder driver"]', 'qwen');
      await page.click('#set-routing .rt-box[data-agent="approver"] .rt-adv-btn');
      const adv = page.locator('#set-routing textarea[aria-label="approver extra_body override (JSON)"]');
      assert(Math.abs(await page.$eval('#set-routing .rt-box[data-agent="approver"]', (e) => e.getBoundingClientRect().top) - y0) < 1, 'opening Advanced does not move the box');
      await adv.fill('{"output_config": ');
      await page.waitForSelector('#set-routing [data-err-for="agents.approver.extra_body"]:not([hidden])');
      assert(await page.locator('.save-bar button:has-text("Save changes")').isDisabled(), 'save is disabled while extra_body is invalid');
      // A re-render (another box's change) keeps the invalid text and its mark.
      await page.selectOption('#set-routing select[aria-label="Default model"]', 'sonnet');
      await page.selectOption('#set-routing select[aria-label="Default model"]', '');
      assert(await adv.inputValue() === '{"output_config": ' && await adv.getAttribute('aria-invalid') === 'true', 'the invalid draft survives a re-render');
      await adv.fill('{"output_config": {"effort": "xhigh"}}');
      await page.waitForFunction(() => { const b = Array.from(document.querySelectorAll('.save-bar button')).find((x) => /Save changes/.test(x.textContent)); return b && !b.disabled; });
      await page.locator('#set-routing').scrollIntoViewIfNeeded();
      await page.evaluate(() => document.getElementById('main').scrollTo(0, 0));
      await page.waitForTimeout(150);
      await cleanShot(page, 'routing');
      const save = await waitPost(page, /^\/api\/config$/, () => page.click('.save-bar button:has-text("Save changes")'));
      assert(save.status() === 200, 'save status ' + save.status());
      const body = JSON.parse(save.request().postData());
      assert(canon(body) === canon({ agents: { builder: { model: 'local', driver: 'qwen' }, approver: { extra_body: { output_config: { effort: 'xhigh' } } } } }), 'only the changed routes: ' + JSON.stringify(body));
      await page.waitForSelector('.toast-ok :text("Settings saved")');
      await page.waitForFunction(() => /own route/.test(document.querySelector('#set-routing .rt-box[data-agent="builder"] .rt-own').textContent));
      await page.selectOption('#set-routing select[aria-label="builder model"]', '');
      await page.selectOption('#set-routing select[aria-label="builder driver"]', '');
      const save2 = await waitPost(page, /^\/api\/config$/, () => page.click('.save-bar button:has-text("Save changes")'));
      assert(canon(JSON.parse(save2.request().postData())) === canon({ agents: { builder: null } }), 'null removes the override: ' + save2.request().postData());
      await page.waitForFunction(() => /tier opus/.test(document.querySelector('#set-routing .rt-box[data-agent="builder"] .rt-own').textContent));
    });

    await step('routing presets: a confirm explains what is replaced, then POST /api/config/preset', async () => {
      await page.selectOption('#set-routing select[aria-label="Apply a preset"]', 'deepseek');
      await page.waitForSelector('dialog[open] .preset-explain');
      const txt = await page.textContent('dialog[open]');
      assert(/Replaced/.test(txt) && /Kept/.test(txt) && /repos/.test(txt) && /snapshot/.test(txt), 'the confirm explains the merge: ' + txt.slice(0, 300));
      const pr = await waitPost(page, /^\/api\/config\/preset$/, () => page.click('dialog[open] button[data-action="ok"]'));
      assert(pr.status() === 200 && canon(JSON.parse(pr.request().postData())) === canon({ name: 'deepseek' }), 'preset body: ' + pr.request().postData());
      await page.waitForSelector('.toast-ok :text("preset")');
      await page.waitForFunction(() => document.querySelector('#set-routing select[aria-label="reviewer-code model"]').value === 'sonnet');
      assert(await page.inputValue('#set-routing select[aria-label="reviewer-ui driver"]') === '', 'the preset replaced the reviewer-ui route whole (its claude driver is gone)');
      assert(await page.inputValue('#set-routing select[aria-label="Apply a preset"]') === '', 'the menu resets after applying');
    });

    await step('models: add from template pre-fills the form (still editable); Test shows reply, tool call and tokens/s', async () => {
      await page.goto(url + '/#/settings/models');
      await page.waitForSelector('#set-models [data-action="model-template"]');
      await page.click('#set-models [data-action="model-template"]');
      await page.waitForSelector('dialog[open] #mt-template');
      await page.selectOption('dialog[open] #mt-kind', 'llamacpp');
      await page.selectOption('dialog[open] #mt-template', 'gpt-oss-120b');
      assert(JSON.parse(await page.inputValue('dialog[open] #mt-extra')).chat_template_kwargs.reasoning_effort === 'high', 'gpt-oss on llama.cpp: chat_template_kwargs');
      assert(await page.inputValue('dialog[open] #mt-ctx') === '131072', 'gpt-oss context 131072');
      assert(/-c 131072/.test(await page.textContent('dialog[open] .mt-notes')), 'the note says to match llama-server -c');
      await page.selectOption('dialog[open] #mt-kind', 'vllm');
      await page.selectOption('dialog[open] #mt-template', 'gpt-oss-120b');
      assert(JSON.parse(await page.inputValue('dialog[open] #mt-extra')).reasoning_effort === 'high', 'gpt-oss on vLLM: top-level reasoning_effort');
      await page.selectOption('dialog[open] #mt-kind', 'llamacpp');
      assert(JSON.parse(await page.inputValue('dialog[open] #mt-extra')).chat_template_kwargs, 'switching the server re-shapes extra_body (llama.cpp)');
      await page.selectOption('dialog[open] #mt-kind', 'tabby');
      const tabbyExtra = JSON.parse(await page.inputValue('dialog[open] #mt-extra'));
      assert(tabbyExtra.reasoning_effort === 'high' && !tabbyExtra.chat_template_kwargs, 'and back (TabbyAPI): ' + JSON.stringify(tabbyExtra));
      const tabbyNotes = await page.textContent('dialog[open] .mt-notes');
      assert(/max_seq_len: 131072/.test(tabbyNotes) && !/extra_body shown is for llama\.cpp/.test(tabbyNotes), 'notes follow the server: ' + tabbyNotes);
      await page.selectOption('dialog[open] #mt-kind', 'ollama');
      await page.selectOption('dialog[open] #mt-template', 'qwen3-coder');
      assert(await page.inputValue('dialog[open] #mt-model') === 'qwen3-coder:30b', 'Ollama gets the tag-style model id');
      assert(await page.inputValue('dialog[open] #mt-ctx') === '262144' && await page.inputValue('dialog[open] #mt-max') === '16384', 'context and max tokens pre-filled');
      assert(/OLLAMA_CONTEXT_LENGTH/.test(await page.textContent('dialog[open] .mt-notes')), 'the Ollama context note');
      await page.fill('dialog[open] #mt-key', 'coder-local');
      await page.waitForTimeout(150);
      await cleanShot(page, 'models-template');
      await page.click('dialog[open] button[data-action="ok"]');
      await page.waitForSelector('dialog[open]', { state: 'detached' });
      await page.waitForSelector('#set-models .mdl-row[data-model="coder-local"]');
      const sv = await waitPost(page, /^\/api\/config$/, () => page.click('.save-bar button:has-text("Save changes")'));
      const mb = JSON.parse(sv.request().postData());
      const m = mb.models && mb.models['coder-local'];
      assert(sv.status() === 200 && m && m.provider === 'ollama' && m.model === 'qwen3-coder:30b' && m.context_tokens === 262144 && m.max_tokens === 16384, 'template model saved: ' + JSON.stringify(mb));
      assert(Object.keys(mb.models).length === 1 && !mb.providers, 'only the new model is sent: ' + JSON.stringify(Object.keys(mb)));
      await page.waitForSelector('.toast-ok :text("Settings saved")');
      await waitPost(page, /\/api\/models\/coder-local\/test$/, () => page.click('#set-models .mdl-row[data-model="coder-local"] button:has-text("Test")'));
      await page.waitForSelector('#set-models .mdl-row[data-model="coder-local"] .test-result.ok');
      const res = await page.textContent('#set-models .mdl-row[data-model="coder-local"] .test-result');
      assert(/reply 88 ms/.test(res) && /“OK”/.test(res) && /tool call/.test(res) && !/no tool call/.test(res) && /91\.6 tok\/s/.test(res), 'both probes and tokens/s: ' + res);
      await waitPost(page, /\/api\/models\/opus\/test$/, () => page.click('#set-models .mdl-row[data-model="opus"] button:has-text("Test")'));
      await page.waitForSelector('#set-models .mdl-row[data-model="opus"] .test-result.err');
      assert(/DEEPSEEK_API_KEY is not set/.test(await page.textContent('#set-models .mdl-row[data-model="opus"] .test-result')), 'a failed probe says why');
      await page.goto(url + '/#/settings/routing');
      await page.waitForSelector('#set-routing select[aria-label="builder model"]');
      const failChip = page.locator('#set-routing .rt-box[data-agent="spec-writer"] .rt-test');
      assert(/key missing/.test(await failChip.textContent()) && /DEEPSEEK_API_KEY is not set/.test(await failChip.getAttribute('aria-label')), 'a failed Test names its reason on the step');
      assert(/^\s*Test\s*$/.test(await page.textContent('#set-routing .rt-box[data-agent="reviewer-ux"] .rt-test')), 'an unmeasured model offers Test, not an empty tok/s');
      await page.selectOption('#set-routing select[aria-label="builder model"]', 'coder-local');
      await page.waitForFunction(() => /91\.6 tok\/s/.test(document.querySelector('#set-routing .rt-box[data-agent="builder"] .rt-meta').textContent));
      assert(await page.locator('#set-routing .rt-box[data-agent="builder"] .tag-local').count() === 1, 'the Ollama model is LOCAL');
      await page.click('.save-bar button:has-text("Discard")');
      // "Suggested for builder, coder" routes those steps to the new model when asked (unsaved, with the model).
      await page.goto(url + '/#/settings/models');
      await page.click('#set-models [data-action="model-template"]');
      await page.waitForSelector('dialog[open] #mt-template');
      await page.selectOption('dialog[open] #mt-kind', 'ollama');
      await page.selectOption('dialog[open] #mt-template', 'qwen3-coder');
      await page.fill('dialog[open] #mt-key', 'coder-two');
      await page.check('dialog[open] #mt-route');
      await page.click('dialog[open] button[data-action="ok"]');
      await page.waitForSelector('dialog[open]', { state: 'detached' });
      await page.goto(url + '/#/settings/routing');
      await page.waitForSelector('#set-routing select[aria-label="builder model"]');
      assert(await page.inputValue('#set-routing select[aria-label="builder model"]') === 'coder-two' && await page.inputValue('#set-routing select[aria-label="coder model"]') === 'coder-two', 'the suggested steps are routed to the new model');
      await page.click('.save-bar button:has-text("Discard")');
    });

    let waitJob = null;
    await step('per-run routing: New feature posts a preset name, or only the steps Custom changed', async () => {
      await page.goto(url + '/#/factory');
      await page.waitForSelector('.board .card');
      await page.click('#new-feature-btn');
      await page.waitForSelector('dialog[open] #nf-feature');
      await page.fill('dialog[open] #nf-feature', 'Show the build number in the footer');
      await page.waitForSelector('dialog[open] select[name="routing"] option[value="preset:hybrid"]', { state: 'attached' });
      assert(await page.inputValue('dialog[open] select[name="routing"]') === '', 'Routing: Default first');
      assert(/hybrid — /.test(await page.textContent('dialog[open] select[name="routing"] option[value="preset:hybrid"]')), 'preset options say what they do');
      await page.selectOption('dialog[open] select[name="routing"]', 'preset:hybrid');
      assert(await page.locator('dialog[open] button[data-action="backlog"]').isDisabled(), 'the backlog runs with the saved routing only');
      assert(/Add to backlog needs Default/.test(await page.textContent('dialog[open] .rt-hint')), 'a visible note says why Add to backlog is off');
      const r1 = await waitPost(page, /^\/api\/jobs$/, () => page.click('dialog[open] button[data-action="run"]'));
      const b1 = JSON.parse(r1.request().postData());
      assert(r1.status() === 201 && b1.routing === 'hybrid', 'preset routing body: ' + JSON.stringify(b1));
      const job1 = (await r1.json()).job;
      assert(job1.routing.builder.provider === 'tabby' && job1.routing.approver.model === 'claude-opus', 'the snapshot follows the preset');
      await page.waitForSelector('dialog[open]', { state: 'detached' });
      // Hold the strata slot so the custom-routed spec-writer queues on it.
      await page.request.post(url + '/__mock/hold', { data: { provider: 'strata', seconds: 9 } });
      await page.click('#new-feature-btn');
      await page.waitForSelector('dialog[open] #nf-feature');
      await page.fill('dialog[open] #nf-feature', 'Add a health check endpoint');
      await page.selectOption('dialog[open] select[name="routing"]', ':custom');
      await page.waitForSelector('dialog[open] .rt-row[data-agent="spec-writer"]');
      const rows = await page.$$eval('dialog[open] .rt-row', (els) => els.map((e) => e.dataset.agent));
      assert(rows.join(',') === 'spec-writer,builder,reviewer-security,reviewer-ux,reviewer-ui,reviewer-code,approver', 'custom rows follow the line: ' + rows);
      assert(await page.inputValue('dialog[open] select[aria-label="reviewer-code model"]') === 'sonnet', 'custom rows are prefilled from the saved routing');
      await page.click('dialog[open] button[data-action="run"]');
      await page.waitForSelector('dialog[open] .form-error:not([hidden])');
      assert(await page.locator('dialog[open] button[data-action="backlog"]').isDisabled(), 'a refused submit leaves Add to backlog disabled under a custom routing');
      await page.selectOption('dialog[open] select[aria-label="spec-writer model"]', 'local');
      await page.waitForSelector('dialog[open] .rt-row.changed[data-agent="spec-writer"]');
      assert(await page.locator('dialog[open] .modal-body > .form-error').isHidden(), 'the error clears once a step changes');
      await page.waitForTimeout(120);
      await cleanShot(page, 'new-feature-routing');
      const r2 = await waitPost(page, /^\/api\/jobs$/, () => page.click('dialog[open] button[data-action="run"]'));
      assert(r2.status() === 201, 'create status ' + r2.status());
      assert(canon(JSON.parse(r2.request().postData()).routing) === canon({ 'spec-writer': { model: 'local' } }), 'custom routing sends the changed step only: ' + r2.request().postData());
      waitJob = (await r2.json()).job;
      assert(waitJob.routing['spec-writer'].provider === 'strata' && waitJob.routing.builder.provider === 'deepseek', 'snapshot: the custom step plus the saved routes');
    });

    await step('provider wait: card, strip queue count and Live run header say "waiting for strata…" until the next event', async () => {
      const key = waitJob.repo + '/' + waitJob.id;
      await page.waitForSelector(`.card[data-job="${key}"] .card-wait`, { timeout: 15000 });
      assert(/waiting for strata…/.test(await page.textContent(`.card[data-job="${key}"] .card-wait`)), 'card wait text');
      await page.waitForSelector('#strip-summary .queue-pill[data-provider="strata"]');
      assert(/strata/.test(await page.textContent('#strip-summary .queue-pill[data-provider="strata"]')), 'strip queue pill');
      await page.click(`.card[data-job="${key}"]`);
      await page.waitForSelector('#panel-live .run-status.wait');
      assert(/waiting for strata…/.test(await page.textContent('#panel-live .run-status.wait')), 'the run header waits');
      assert(await page.locator('#drawer-head .wait-row').count() === 1, 'the drawer header says the step is queued');
      assert(await page.locator('#drawer-head .route-table').count() === 0, 'the routing table is not in the header');
      await page.click('#drawer-head [data-action="show-routing"]');
      await page.waitForSelector('#panel-routing:not([hidden]) .route-table');
      assert(/\/routing$/.test(page.url()), 'the Routing tab has its own URL: ' + page.url());
      const spec = await page.textContent('#drawer .route-table tr[data-agent="spec-writer"]');
      assert(/strata/.test(spec) && /LOCAL/.test(spec) && /builtin/.test(spec), 'drawer routing row: ' + spec);
      const builder = await page.textContent('#drawer .route-table tr[data-agent="builder"]');
      assert(/deepseek-v4-pro/.test(builder) && /API/.test(builder), 'builder row: ' + builder);
      await page.click('#tab-live');
      await page.waitForSelector('#panel-live .run-status.wait', { state: 'detached', timeout: 20000 });
      await page.keyboard.press('Escape');
      await page.waitForSelector(`.card[data-job="${key}"] .card-wait`, { state: 'detached', timeout: 5000 });
      await page.waitForSelector('#strip-summary .queue-pill', { state: 'detached', timeout: 5000 });
    });

    await step('drawer: Change routing keeps a step’s snapshot model (and its extra_body) when only the driver changes', async () => {
      let stopped = null;
      for (let i = 0; i < 60 && !stopped; i++) {
        const st = await (await page.request.get(url + '/api/state')).json();
        stopped = st.jobs.find((j) => (j.stage === 'needs-human' || j.stage === 'failed') && j.routing && j.routing.builder && j.routing.builder.provider === 'deepseek' && j.routing.builder.driver === 'builtin');
        if (!stopped) await page.waitForTimeout(500);
      }
      assert(stopped, 'a needs-human or failed job with a routing snapshot');
      await page.goto(url + '/#/job/' + stopped.repo + '/' + stopped.id);
      await page.waitForSelector('#drawer-head [data-action="routing"]');
      const acts = await page.$$eval('#drawer-head .actions button', (bs) => bs.map((b) => b.dataset.action));
      assert(acts[acts.length - 1] === 'routing' && (acts[0] === 'approve' || acts[0] === 'retry'), 'the decision comes first, Change routing last: ' + acts);
      await page.click('#drawer-head [data-action="routing"]');
      await page.waitForSelector('dialog[open] .rt-row[data-agent="builder"]');
      assert(await page.inputValue('dialog[open] select[aria-label="builder model"]') === ':keep', 'each step starts on Keep (its snapshot)');
      assert((await page.$eval('dialog[open] select[aria-label="builder model"]', (s) => s.options[s.selectedIndex].textContent)).includes(stopped.routing.builder.model_id), 'Keep names the snapshot model id');
      assert(await page.locator(`dialog[open] button[data-action="${stopped.stage === 'failed' ? 'retry' : 'rework'}"]`).count() === 1, 'the dialog offers the follow-up action');
      await page.selectOption('dialog[open] select[aria-label="builder driver"]', 'qwen');
      const rr = await waitPost(page, /\/routing$/, () => page.click('dialog[open] button[data-action="ok"]'));
      assert(rr.status() === 200, 'routing status ' + rr.status());
      const rb = JSON.parse(rr.request().postData());
      const want = { model: stopped.routing.builder.model, driver: 'qwen' };
      if (Object.keys(stopped.routing.builder.extra_body || {}).length) want.extra_body = stopped.routing.builder.extra_body;
      // ... and what the snapshot placed there (views/routing.js keepPlacements): an explicit model drops agents.<name>'s
      const b0 = stopped.routing.builder;
      if (b0.reason && b0.reason.kind) want.reason = { kind: b0.reason.kind, text: b0.reason.text || '' };
      if (Array.isArray(b0.escalate) && b0.escalate.length) want.escalate = b0.escalate.map((r) => r.model);
      assert(Object.keys(rb.routing).join() === 'builder' && canon(rb.routing.builder) === canon(want), 'driver-only change keeps the model, its extra_body and its placement: ' + JSON.stringify(rb));
      const after = (await rr.json()).job.routing.builder;
      assert(after.model_id === stopped.routing.builder.model_id && after.driver === 'qwen' && canon(after.extra_body) === canon(stopped.routing.builder.extra_body), 'the snapshot keeps its model: ' + JSON.stringify(after));
      assert(canon((after.escalate || []).map((r) => r.model)) === canon(want.escalate || []) && canon(after.reason || null) === canon(want.reason || null), 'and its ladder and reason: ' + JSON.stringify(after.escalate));
      await page.click('#tab-routing');
      await page.waitForFunction(() => { const r = document.querySelector('#drawer .route-table tr[data-agent="builder"]'); return r && /qwen/.test(r.textContent); });
      await page.keyboard.press('Escape');
    });

    await step('drawer: a preset job whose model left the settings cannot silently move provider; Change & retry', async () => {
      const made = await page.request.post(url + '/__mock/stopped-job', { data: { repo: 'my-app', feature: 'Tune the CSV export batch size', routing: 'anthropic', stage: 'failed' } });
      const pj = (await made.json()).job;
      assert(pj.routing.builder.provider === 'anthropic', 'the preset job runs on anthropic: ' + JSON.stringify(pj.routing.builder));
      await page.goto(url + '/#/job/my-app/' + pj.id + '/routing');
      await page.waitForSelector('#panel-routing:not([hidden]) .route-table tr[data-agent="builder"]');
      assert(/anthropic/.test(await page.textContent('#drawer .route-table tr[data-agent="builder"]')), 'the Routing tab shows the snapshot');
      await page.click('#panel-routing [data-action="routing"]');
      await page.waitForSelector('dialog[open] .rt-row[data-agent="builder"]');
      assert(/anthropic/.test(await page.$eval('dialog[open] select[aria-label="builder model"]', (s) => s.options[s.selectedIndex].textContent)), 'Keep shows the snapshot provider, not the live one');
      await page.selectOption('dialog[open] select[aria-label="builder driver"]', 'qwen');
      await page.waitForSelector('dialog[open] .rt-row.invalid[data-agent="builder"] .rt-row-warn:not([hidden])');
      let posted = false;
      const onReq = (r) => { if (r.method() === 'POST' && /\/routing$/.test(new URL(r.url()).pathname)) posted = true; };
      page.on('request', onReq);
      await page.click('dialog[open] button[data-action="ok"]');
      await page.waitForSelector('dialog[open] .modal-body > .form-error:not([hidden])');
      page.off('request', onReq);
      assert(!posted && /no longer one of your saved models/.test(await page.textContent('dialog[open] .modal-body > .form-error')), 'refused before posting');
      await page.selectOption('dialog[open] select[aria-label="builder model"]', 'opus');
      const [rr, rt] = await Promise.all([
        page.waitForResponse((r) => r.request().method() === 'POST' && /\/routing$/.test(new URL(r.url()).pathname)),
        page.waitForResponse((r) => r.request().method() === 'POST' && /\/retry$/.test(new URL(r.url()).pathname)),
        page.click('dialog[open] button[data-action="retry"]'),
      ]);
      assert(canon(JSON.parse(rr.request().postData()).routing) === canon({ builder: { model: 'opus', driver: 'qwen' } }), 'an explicit pick: ' + rr.request().postData());
      assert(rt.status() === 200, 'Change & retry retries: ' + rt.status());
      await page.waitForSelector('dialog[open]', { state: 'detached' });
      await page.keyboard.press('Escape');
    });

    // ---------------------------------------------------------------- plans (DESIGN §24)
    await step('plans: list with progress, the lead’s questions, a layered job graph, click-through to a job', async () => {
      await page.goto(url + '/#/plans');
      await page.waitForSelector('.plan-item');
      const items = await page.$$eval('.plan-item', (els) => els.map((e) => e.dataset.plan));
      for (const k of ['my-app/P001-csv-export-for-reports', 'my-app/P002-audit-log-v2', 'docs-site/P001-move-the-docs-to-a-versioned']) assert(items.includes(k), 'plan listed: ' + k);
      const done = await page.textContent('.plan-item[data-plan="my-app/P001-csv-export-for-reports"]');
      assert(/Done/.test(done) && /4\/4 merged/.test(done) && /PASS/.test(done), 'plan row: ' + done);
      await page.click('.plan-item[data-plan="docs-site/P001-move-the-docs-to-a-versioned"]');
      await page.waitForSelector('.plan-questions li');
      assert(await page.locator('.plan-questions li').count() === 3, 'the three questions');
      assert(await page.locator('.plan-detail [data-action="retry"]').count() === 1 && await page.locator('.plan-detail [data-action="edit"]').count() === 1, 'Retry and Edit offered');
      assert((await page.textContent('.plan-questions')).includes("<script>alert('q')</script>") && await page.locator('img').count() === 0, 'hostile question text stays text');
      await page.click('.plan-item[data-plan="my-app/P002-audit-log-v2"]');
      await page.waitForSelector('.pg-node[data-n="5"]');
      const g = await page.evaluate(() => ({
        x: Object.fromEntries(Array.from(document.querySelectorAll('.pg-node')).map((n) => [n.dataset.n, Number(/translate\(([\d.]+)/.exec(n.getAttribute('transform'))[1])])),
        edges: Array.from(document.querySelectorAll('.pg-edge')).map((e) => e.dataset.from + '>' + e.dataset.to).sort(),
      }));
      assert(Object.keys(g.x).length === 5 && g.edges.join() === '1>2,1>3,2>4,3>5,4>5', 'graph nodes and arrows: ' + JSON.stringify(g));
      assert(g.x[1] < g.x[2] && g.x[2] === g.x[3] && g.x[2] < g.x[4] && g.x[4] < g.x[5], 'dependency layers left→right: ' + JSON.stringify(g.x));
      assert(await page.locator('.plan-detail [data-action="run"]').count() === 1, 'a ready plan offers Run');
      await page.waitForTimeout(150);
      await cleanShot(page, 'plan-detail');
      await page.click('.plan-item[data-plan="my-app/P001-csv-export-for-reports"]');
      await page.waitForSelector('.pg-node.tone-ok[data-n="2"]');
      await page.waitForSelector('.plan-review .verdict');
      assert(/PASS/.test(await page.textContent('.plan-review .verdict')), 'final review with its verdict badge');
      await page.waitForSelector('.plan-review .md');
      const rvText = await page.textContent('.plan-review .md');
      assert(!/VERDICT/.test(rvText) && !/Final review 1/.test(rvText), 'the review body drops the verdict line and title the card shows: ' + rvText.slice(0, 80));
      assert(await page.locator('.plan-detail .tag:has-text("PLAN: READY")').count() === 0, 'a done plan shows no PLAN: READY badge');
      assert(!/PLAN: READY/.test(await page.textContent('.plan-detail')), 'nor the status line as body text');
      await page.click('.pg-node[data-n="2"]');
      await page.waitForSelector('#drawer:not([hidden]) #drawer-title');
      assert(/#\/job\/my-app\/006-/.test(page.url()), 'the node opens its job: ' + page.url());
      assert(await page.isVisible('#view-plans'), 'the plan stays behind the drawer');
      assert(await page.locator('#drawer-head a.meta-link').count() === 1, 'the drawer links back to the plan');
      await page.keyboard.press('Escape');
      await page.waitForFunction(() => location.hash === '#/plans/my-app/P001-csv-export-for-reports');
      await page.waitForSelector('#drawer[hidden]', { state: 'attached' });
    });

    await step('plans: an unsaved plan.md edit survives switching plans; Cancel asks before discarding', async () => {
      await page.goto(url + '/#/plans/my-app/P002-audit-log-v2');
      await page.waitForSelector('.plan-detail [data-action="edit"]');
      assert(await page.locator('.plan-detail button:has-text("Edit")').count() === 1, 'one Edit entry point');
      await page.click('.plan-detail [data-action="edit"]');
      await page.waitForSelector('.plan-editor');
      const typed = (await page.inputValue('.plan-editor')).trimEnd() + '\n\n## Notes\nKeep the old table until the backfill ends.\n';
      await page.fill('.plan-editor', typed);
      await page.click('.plan-item[data-plan="my-app/P001-csv-export-for-reports"]');
      await page.waitForSelector('.plan-editor', { state: 'detached' });
      await page.waitForSelector('.toast :text("kept as a draft")');
      await page.waitForSelector('.plan-item[data-plan="my-app/P002-audit-log-v2"] .plan-draft');
      await page.click('.plan-item[data-plan="my-app/P002-audit-log-v2"]');
      await page.waitForSelector('.plan-editor');
      assert(await page.inputValue('.plan-editor') === typed, 'the draft comes back as typed');
      assert(/Draft restored/.test(await page.textContent('.plan-sec.is-editing')), 'and says so');
      await page.click('[data-action="cancel-edit"]');
      await page.waitForSelector('dialog[open]');
      assert(/Discard your plan\.md edits/.test(await page.textContent('dialog[open]')), 'Cancel asks first');
      await page.click('dialog[open] button[data-action="ok"]');
      await page.waitForSelector('.plan-editor', { state: 'detached' });
      assert(await page.locator('.plan-item[data-plan="my-app/P002-audit-log-v2"] .plan-draft').count() === 0, 'no draft left');
    });

    await step('plans: create with per-plan routing, edit (a bad plan is refused inline), run to done with live updates', async () => {
      await page.click('[data-action="new-plan"]');
      await page.waitForSelector('dialog[open] #np-request');
      await page.selectOption('dialog[open] #np-repo', 'my-app');
      await page.fill('dialog[open] #np-request', 'Add team workspaces with invites');
      await page.selectOption('dialog[open] select[name="routing"]', ':custom');
      await page.waitForSelector('dialog[open] .rt-row[data-agent="lead"]');
      await page.selectOption('dialog[open] select[aria-label="lead model"]', 'sonnet');
      const pr = await waitPost(page, /^\/api\/plans$/, () => page.click('dialog[open] button[data-action="ok"]'));
      assert(pr.status() === 201, 'create plan status ' + pr.status());
      const pb = JSON.parse(pr.request().postData());
      assert(pb.repo === 'my-app' && pb.request === 'Add team workspaces with invites' && canon(pb.routing) === canon({ lead: { model: 'sonnet' } }), 'new plan body: ' + JSON.stringify(pb));
      const plan = (await pr.json()).plan;
      await page.waitForFunction((id) => location.hash === '#/plans/my-app/' + id, plan.id);
      await page.waitForSelector('.plan-detail [data-action="edit"]', { timeout: 20000 });
      await page.waitForSelector('.pg-node[data-n="3"]');
      await page.waitForTimeout(150);
      await cleanShot(page, 'plans');
      await page.click('.plan-detail [data-action="edit"]');
      await page.waitForSelector('.plan-editor');
      assert((await page.inputValue('.plan-editor')).startsWith('PLAN: READY'), 'the editor holds plan.md');
      allowHttpErrors = true;
      await page.fill('.plan-editor', 'PLAN: READY\n\n## Jobs\n1. Add the workspace model\n2. Invite flow (after: 3)\n3. Accept invites (after: 2)\n');
      const bad = await waitPost(page, /\/plan$/, () => page.click('[data-action="save-plan"]'));
      assert(bad.status() === 400, 'a cycle is refused: ' + bad.status());
      await page.waitForSelector('.plan-detail .form-error:has-text("cycle")');
      allowHttpErrors = false;
      assert(await page.locator('.plan-editor').count() === 1, 'the editor stays open with the text');
      await page.fill('.plan-editor', 'PLAN: READY\n\n## Jobs\n1. Add the workspace model with a migration\n2. Invite teammates by email (after: 1)\n');
      const ok = await waitPost(page, /\/plan$/, () => page.click('[data-action="save-plan"]'));
      assert(ok.status() === 200 && JSON.parse(ok.request().postData()).text.startsWith('PLAN: READY'), 'plan saved');
      await page.waitForSelector('.plan-editor', { state: 'detached' });
      await page.waitForFunction(() => document.querySelectorAll('.pg-node').length === 2 && document.querySelectorAll('.pg-edge').length === 1);
      const run = await waitPost(page, /\/run$/, () => page.click('.plan-detail [data-action="run"]'));
      assert(run.status() === 200, 'run status ' + run.status());
      await page.waitForSelector('.pg-node[data-n="1"][data-job]', { timeout: 10000 });
      await page.waitForSelector('.plan-detail .plan-review .verdict', { timeout: 60000 });
      await page.waitForFunction(() => /Done/.test(document.querySelector('.plan-detail .pill').textContent), null, { timeout: 15000 });
      assert(await page.locator('.pg-node.tone-ok').count() === 2, 'both jobs merged in the graph');
      assert(/2\/2 merged/.test(await page.textContent(`.plan-item[data-plan="my-app/${plan.id}"]`)), 'the list row shows the progress');
    });

    await step('plans: the lead’s questions are answered inline; the answers go into plan.md and Retry re-plans', async () => {
      const cr = await page.request.post(url + '/api/plans', { data: { repo: 'docs-site', request: 'Split the docs search into its own service? Keep the old URL.' } });
      const qp = (await cr.json()).plan;
      await page.goto(url + '/#/plans/docs-site/' + qp.id);
      await page.waitForSelector('.plan-q-answer[data-q="1"]', { timeout: 20000 });
      assert(await page.locator('.plan-detail .callout-human').count() === 0, 'the questions card replaces the generic "Needs you" banner');
      assert(await page.locator('.plan-detail .plan-raw').count() === 1, 'plan.md as the lead wrote it is folded away');
      await page.fill('.plan-q-answer[data-q="0"]', 'Yes, behind a flag for two weeks.');
      await page.fill('.plan-q-answer[data-q="1"]', 'Only this one.');
      const [pp, rt] = await Promise.all([
        page.waitForResponse((r) => r.request().method() === 'POST' && /\/plan$/.test(new URL(r.url()).pathname)),
        page.waitForResponse((r) => r.request().method() === 'POST' && /\/retry$/.test(new URL(r.url()).pathname)),
        page.click('[data-action="send-answers"]'),
      ]);
      const text = JSON.parse(pp.request().postData()).text;
      assert(pp.status() === 200 && rt.status() === 200, 'answers saved, then Retry: ' + pp.status() + ' ' + rt.status());
      assert(/^PLAN: QUESTIONS\n/.test(text) && /\n1\. Should[^\n]*\n {3}\*\*Answer:\*\* Yes, behind a flag for two weeks\.\n2\. /.test(text) && /\n {3}\*\*Answer:\*\* Only this one\.\n$/.test(text), 'each answer under its question: ' + JSON.stringify(text));
      await page.waitForSelector('.plan-detail [data-action="run"]', { timeout: 20000 });
      assert(await page.locator('.plan-questions').count() === 0, 'the re-plan is READY');
    });

    await step('an open job drawer and plan detail re-read only on live events (no polling loop)', async () => {
      const hits = [];
      const onReq = (r) => { const u = new URL(r.url()).pathname; if (/^\/api\/(jobs|plans)\//.test(u)) hits.push(u); };
      await page.goto(url + '/#/plans/my-app/P001-csv-export-for-reports');
      await page.waitForSelector('.pg-node');
      await page.waitForTimeout(800);
      page.on('request', onReq);
      await page.waitForTimeout(2500);
      const planHits = hits.length;
      await page.goto(url + '/#/job/my-app/005-add-export-csv-with-tests');
      await page.waitForSelector('#drawer:not([hidden]) #drawer-title');
      await page.waitForTimeout(1500);
      hits.length = 0;
      await page.waitForTimeout(2500);
      page.off('request', onReq);
      assert(planHits === 0, 'a quiet plan detail made ' + planHits + ' requests');
      assert(hits.length === 0, 'a quiet job drawer made requests: ' + hits.join(', '));
      await page.keyboard.press('Escape');
    });

    // ---------------------------------------------------------------- agents (DESIGN §22)
    await step('agents: edit (an invalid file is refused inline), new reviewer, add to pipeline, a 5-reviewer line everywhere, reset', async () => {
      await page.goto(url + '/#/agents/reviewer-ux');
      await page.waitForSelector('.agent-viewer [data-action="edit-agent"]:not([disabled])');
      assert(await page.locator('.agent-viewer .src-tag[data-source="builtin"]').count() >= 1, 'source badge: builtin');
      // DESIGN §36: an Activity link on each agent page, to the Operator area's activity page
      assert(await page.getAttribute('.agent-viewer [data-action="agent-activity"]', 'href') === '#/activity?agent=reviewer-ux&since=7d', 'the agent page links its activity');
      await page.click('[data-action="edit-agent"]');
      await page.waitForSelector('dialog[open] textarea.agent-editor');
      await page.waitForTimeout(100);
      const caret = await page.$eval('dialog[open] textarea.agent-editor', (t) => [t.selectionStart, t.scrollTop, document.activeElement === t]);
      assert(caret[0] === 0 && caret[1] === 0 && caret[2], 'the editor opens at the frontmatter: ' + caret);
      const text = await page.inputValue('dialog[open] textarea.agent-editor');
      assert(text.startsWith('---') && /\nname: reviewer-ux\n/.test(text) && /\nmodel: sonnet\n/.test(text), 'the whole file, frontmatter included');
      allowHttpErrors = true;
      await page.fill('dialog[open] textarea.agent-editor', text.replace('name: reviewer-ux', 'name: reviewer-uxx'));
      const bad = await waitPost(page, /\/api\/agents\/reviewer-ux$/, () => page.click('dialog[open] button[data-action="ok"]'));
      assert(bad.status() === 400, 'invalid frontmatter: ' + bad.status());
      await page.waitForSelector('dialog[open] .form-error:has-text("frontmatter name")');
      allowHttpErrors = false;
      await page.waitForTimeout(120);
      await cleanShot(page, 'agents-edit');
      await page.fill('dialog[open] textarea.agent-editor', text.trimEnd() + '\n\nAlso check the focus order of every new dialog.\n');
      const good = await waitPost(page, /\/api\/agents\/reviewer-ux$/, () => page.click('dialog[open] button[data-action="ok"]'));
      assert(good.status() === 200 && JSON.parse(good.request().postData()).text.includes('focus order'), 'save posts the whole text');
      await page.waitForSelector('dialog[open]', { state: 'detached' });
      await page.waitForSelector('.agent-viewer .src-tag[data-source="user"]');
      await page.waitForSelector('.agent-viewer [data-action="reset-agent"]');
      await page.waitForSelector('.agent-viewer-body .md :text("focus order")');
      await page.click('[data-action="new-agent"]');
      await page.waitForSelector('dialog[open] #na-name');
      await page.fill('dialog[open] #na-name', 'reviewer-perf');
      await page.selectOption('dialog[open] #na-from', 'reviewer-code');
      const na = await waitPost(page, /^\/api\/agents$/, () => page.click('dialog[open] button[data-action="ok"]'));
      assert(na.status() === 201 && canon(JSON.parse(na.request().postData())) === canon({ name: 'reviewer-perf', from: 'reviewer-code' }), 'new agent body: ' + na.request().postData());
      await page.waitForFunction(() => location.hash === '#/agents/reviewer-perf');
      await page.waitForSelector('.agent-viewer [data-action="add-pipeline"]');
      const ap = await waitPost(page, /^\/api\/config$/, () => page.click('.agent-viewer [data-action="add-pipeline"]'));
      assert(canon(JSON.parse(ap.request().postData())) === canon({ pipeline: { reviewers: ['security', 'ux', 'ui', 'code', 'perf'] } }), 'add to pipeline: ' + ap.request().postData());
      await page.waitForSelector('.agent-viewer [data-action="add-pipeline"]', { state: 'detached' });
      // Five reviewers: the strip, a new job's chips and the Routing view all follow pipeline.reviewers.
      await page.goto(url + '/#/factory');
      await page.waitForSelector('.node[data-node="rev:perf"]');
      assert(await page.locator('.strip-canvas .node.reviewer').count() === 5, 'five reviewer boxes on the strip');
      const fit = await page.evaluate(() => { const s = document.getElementById('strip-scroll'); return [s.scrollWidth, s.clientWidth]; });
      assert(fit[0] <= fit[1], 'the 5-reviewer line still fits: ' + fit);
      const nj = await page.request.post(url + '/api/jobs', { data: { repo: 'my-app', feature: 'Profile the slow dashboard query' } });
      const njob = (await nj.json()).job;
      assert(njob.reviewers.length === 5 && njob.routing['reviewer-perf'], 'new jobs run five reviewers');
      const ck = 'my-app/' + njob.id;
      await page.waitForFunction((k) => document.querySelectorAll(`.card[data-job="${k}"] .round .chip`).length === 5, ck, { timeout: 30000 });
      const labels = await page.$$eval(`.card[data-job="${ck}"] .round:first-child .chip`, (els) => els.map((e) => e.textContent));
      assert(labels.join(',') === 'S,U,Ui,C,P', 'chip labels: ' + labels);
      await cleanShot(page, 'five-reviewers');
      await page.goto(url + '/#/settings/routing');
      await page.waitForSelector('#set-routing .rt-revs .rt-box[data-agent="reviewer-perf"]');
      assert(await page.locator('#set-routing .rt-revs .rt-box').count() === 5, 'the Routing line shows five reviewers');
      // Reset: an override of a shipped agent brings the shipped file back ...
      await page.goto(url + '/#/agents/reviewer-ux');
      await page.waitForSelector('.agent-viewer [data-action="reset-agent"]');
      await page.click('.agent-viewer [data-action="reset-agent"]');
      await page.waitForSelector('dialog[open]');
      const rs = await waitPost(page, /\/api\/agents\/reviewer-ux\/reset$/, () => page.click('dialog[open] button[data-action="ok"]'));
      assert(rs.status() === 200 && (await rs.json()).agent.source === 'builtin', 'reset answers the built-in agent');
      await page.waitForSelector('.agent-viewer .src-tag[data-source="builtin"]');
      await page.waitForSelector('.agent-viewer [data-action="reset-agent"]', { state: 'detached' });
      // ... and an agent that exists only as the user's file is removed ({agent: null}), and leaves the pipeline.
      await page.goto(url + '/#/agents/reviewer-perf');
      await page.waitForSelector('.agent-viewer [data-action="reset-agent"]');
      await page.click('.agent-viewer [data-action="reset-agent"]');
      await page.waitForSelector('dialog[open] input[type=checkbox]:checked');
      const [rp, cp] = await Promise.all([
        page.waitForResponse((r) => r.request().method() === 'POST' && /\/api\/agents\/reviewer-perf\/reset$/.test(new URL(r.url()).pathname)),
        page.waitForResponse((r) => r.request().method() === 'POST' && /^\/api\/config$/.test(new URL(r.url()).pathname)),
        page.click('dialog[open] button[data-action="ok"]'),
      ]);
      assert((await rp.json()).agent === null, 'reset of a user-only agent answers {agent: null}');
      assert(canon(JSON.parse(cp.request().postData())) === canon({ pipeline: { reviewers: ['security', 'ux', 'ui', 'code'] } }), 'removed from the pipeline: ' + cp.request().postData());
      await page.waitForFunction(() => location.hash === '#/agents');
      await page.waitForSelector('.agent-card[data-agent="reviewer-perf"]', { state: 'detached' });
    });

    await step('live connection reconnects and refreshes state', async () => {
      await page.goto(url + '/#/factory');
      await page.waitForSelector('#conn[data-state="open"]');
      const stateReq = page.waitForRequest((r) => r.url().endsWith('/api/state'), { timeout: 20000 });
      const r = await page.request.post(url + '/__mock/drop-sse', { data: {} });
      assert(r.status() === 200, 'drop-sse');
      await page.waitForSelector('#conn:not([data-state="open"])', { timeout: 5000 });
      await page.waitForSelector('#conn[data-state="open"]', { timeout: 20000 });
      await stateReq;
    });

    await step('a lasting disconnect shows the offline banner; Retry reconnects', async () => {
      const right = await page.$eval('#new-feature-btn', (e) => window.innerWidth - e.getBoundingClientRect().right);
      assert(right < 24, 'top bar actions stay right-aligned: ' + right);
      assert(!(await page.isVisible('#offline-banner')), 'no banner while live');
      allowHttpErrors = true;  // the aborted event-stream requests below log net::ERR_FAILED
      await page.route('**/api/events*', (r) => r.abort());
      await page.request.post(url + '/__mock/drop-sse', { data: {} });
      await page.waitForSelector('#offline-banner:not([hidden])', { timeout: 9000 });
      assert(await page.evaluate(() => document.getElementById('app').classList.contains('is-offline')), 'live animations paused');
      await page.unroute('**/api/events*');
      await page.click('#offline-retry');
      await page.waitForSelector('#conn[data-state="open"]', { timeout: 10000 });
      await page.waitForSelector('#offline-banner', { state: 'hidden' });
      await page.waitForTimeout(300);
      allowHttpErrors = false;
    });

    await step('desktop: no horizontal page scroll', async () => { await noOverflow(page, 'desktop factory'); });

    await step('New feature: settings that fail to load say so (Default still works) and Retry brings the presets', async () => {
      const fctx = await browser.newContext({ viewport: { width: 1280, height: 800 }, colorScheme: 'dark' });
      await fctx.addCookies([{ name: 'sf_token', value: 'test-token', url }]);
      const f = await fctx.newPage();
      // The failing GET is the point of this page, so only page errors count here.
      f.on('pageerror', (e) => consoleErrors.push('[config-fail] pageerror: ' + e.message));
      let failing = true;
      await f.route('**/api/config', (r) => (failing ? r.fulfill({ status: 500, contentType: 'application/json', body: '{"error":"config.json is not readable"}' }) : r.continue()));
      try {
        await f.goto(url + '/#/factory');
        await f.waitForSelector('.board .card');
        await f.click('#new-feature-btn');
        await f.waitForSelector('dialog[open] [data-action="retry-presets"]');
        assert(/did not load \(config\.json is not readable\)/.test(await f.textContent('dialog[open] .rt-hint')), 'the reason is shown');
        assert(!(await f.$$eval('dialog[open] select[name="routing"] option', (os) => os.map((o) => o.textContent).join('|'))).includes('Loading'), 'no endless "Loading presets…"');
        failing = false;
        await f.click('dialog[open] [data-action="retry-presets"]');
        await f.waitForSelector('dialog[open] select[name="routing"] option[value=":custom"]', { state: 'attached' });
      } finally { await fctx.close(); }
    });

    // ---------------------------------------------------------------- laptop (1024px)
    const tctx = await browser.newContext({ viewport: { width: 1024, height: 768 }, colorScheme: 'dark' });
    await tctx.addCookies([{ name: 'sf_token', value: 'test-token', url }]);
    const t = await tctx.newPage();
    watch(t, 'laptop');
    await step('1024px: the whole factory line fits and Needs human stays on screen', async () => {
      await t.goto(url + '/#/factory');
      await t.waitForSelector('.board .card');
      await t.waitForSelector('.node[data-node="human"]');
      await t.waitForTimeout(300);
      const m = await t.evaluate(() => {
        const s = document.getElementById('strip-scroll');
        const h = document.querySelector('.col-needs-human').getBoundingClientRect();
        return { strip: [s.scrollWidth, s.clientWidth], human: [Math.round(h.left), Math.round(h.right)], vw: window.innerWidth };
      });
      assert(m.strip[0] <= m.strip[1], 'strip overflows at 1024: ' + m.strip);
      assert(m.human[0] >= 0 && m.human[1] <= m.vw, 'Needs human off screen at 1024: ' + JSON.stringify(m));
      await noOverflow(t, 'laptop factory');
      await shot(t, 'laptop-1024');
      await t.click('#backlog-toggle');
      await t.waitForSelector('#backlog:not([hidden]) .backlog-item');
      await t.waitForTimeout(300);
      const h2 = await t.$eval('.col-needs-human', (e) => { const r = e.getBoundingClientRect(); const b = document.getElementById('backlog').getBoundingClientRect(); return r.left >= 0 && r.right <= b.left + 1; });
      assert(h2, 'Needs human stays visible next to the open backlog');
      await shot(t, 'laptop-1024-backlog');
      await t.click('#backlog-toggle');
      await t.goto(url + '/#/settings/providers');
      await t.waitForSelector('#set-providers .prov-row:not(.cfg-head)');
      await t.waitForTimeout(300);
      await noOverflow(t, 'laptop settings');
      const provFit = await t.evaluate(() => {
        const card = document.getElementById('set-providers').getBoundingClientRect();
        const m = document.getElementById('set-models').getBoundingClientRect();
        const inCard = (sel, c) => Array.from(document.querySelectorAll(sel)).every((e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.right <= c.right && r.left >= c.left; });
        return inCard('#set-providers .prov-row .pc-del button, #set-providers .pc-max input', card) && inCard('#set-models .mdl-row .pc-test button, #set-models .mdl-row .pc-del button', m);
      });
      assert(provFit, 'provider and model fields (Test, Remove) fit at 1024px');
      await shot(t, 'laptop-1024-settings');
    });
    await tctx.close();

    // ---------------------------------------------------------------- mobile
    const mctx = await browser.newContext({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, hasTouch: true, isMobile: true, colorScheme: 'light' });
    await mctx.addCookies([{ name: 'sf_token', value: 'test-token', url }]);
    const m = await mctx.newPage();
    watch(m, 'mobile');
    await step('390px layout: bottom nav, stacked board, no horizontal scroll', async () => {
      await m.goto(url + '/#/factory');
      await m.waitForSelector('.board .card');
      const rail = await m.locator('.rail').boundingBox();
      assert(rail && rail.y > 700 && rail.width >= 380, 'nav should be a bottom bar: ' + JSON.stringify(rail));
      const order = await m.$$eval('.board .col', (els) => els.map((e) => [e.dataset.col, e.getBoundingClientRect().top]).sort((a, b) => a[1] - b[1]).map((x) => x[0]));
      assert(order[0] === 'needs-human', 'needs-human first on mobile: ' + order.join(','));
      await noOverflow(m, 'mobile factory');
      await m.waitForTimeout(200);
      await shot(m, 'mobile');
      await m.goto(url + '/#/job/my-app/004-render-user-notes');
      await m.waitForSelector('#drawer:not([hidden]) #drawer-title');
      await m.waitForTimeout(350);
      const box = await m.locator('#drawer').boundingBox();
      assert(Math.round(box.width) === 390 && box.x === 0, 'drawer full-screen: ' + JSON.stringify(box));
      await m.waitForSelector('#panel-live .run');
      await noOverflow(m, 'mobile drawer');
      const tabsFit = await m.evaluate(() => { const t = document.querySelector('#drawer .tabs'); return [t.scrollWidth, t.clientWidth]; });
      assert(tabsFit[0] <= tabsFit[1], 'all the drawer tabs (the view modules’ too) fit the phone: ' + tabsFit);
      await shot(m, 'mobile-drawer');
      await m.click('#tab-routing');
      await m.waitForSelector('#panel-routing .route-table tr[data-agent="builder"]');
      const rowBox = await m.$eval('#panel-routing .route-table tr[data-agent="builder"]', (r) => { const b = r.getBoundingClientRect(); return [b.right, r.scrollWidth - r.clientWidth]; });
      assert(rowBox[0] <= 390 && rowBox[1] <= 1, 'routing rows stack inside the phone: ' + rowBox);
      await noOverflow(m, 'mobile drawer routing');
      await m.click('#tab-files');
      await m.waitForSelector('#panel-files .file-view .md');
      assert(await m.isVisible('#panel-files .files-picker') && !(await m.isVisible('#panel-files .files-list')), 'phones pick files from a select, not a nested list');
      await m.selectOption('#panel-files .files-picker', 'round-1/review-security.md');
      await m.waitForFunction(() => { const md = document.querySelector('#panel-files .file-view .md'); return document.querySelector('#panel-files .files-picker').value === 'round-1/review-security.md' && md && /^VERDICT: PASS/.test(md.textContent); });
      await noOverflow(m, 'mobile files');
      await shot(m, 'mobile-files');
      await m.click('#tab-diff');
      await m.waitForSelector('#panel-diff .diff-file');
      await noOverflow(m, 'mobile diff');
      await m.keyboard.press('Escape');
      for (const route of ['#/chat', '#/chat/c-1a2b3c4d5e', '#/agents', '#/agents/reviewer-code', '#/settings', '#/settings/routing', '#/plans', '#/plans/my-app/P002-audit-log-v2', '#/doctor']) {
        await m.goto(url + '/' + route);
        await m.waitForTimeout(400);
        await noOverflow(m, 'mobile ' + route);
      }
      await m.goto(url + '/#/plans/my-app/P002-audit-log-v2');
      await m.waitForSelector('.plan-sec-graph.is-narrow .pg-list li');
      assert(await m.isVisible('.plan-sec-graph .pg-list') && await m.locator('.pg-graph-wrap[open]').count() === 0, 'a phone lists the plan’s jobs; the graph is one tap away');
      await m.click('.pg-graph-wrap > summary');
      await m.waitForSelector('.pg-graph-wrap[open] .pg-node[data-n="5"]');
      await noOverflow(m, 'mobile plan graph');
      await m.goto(url + '/#/plans/docs-site/P001-move-the-docs-to-a-versioned');
      await m.waitForSelector('.plan-questions li');
      await m.waitForTimeout(250);
      await noOverflow(m, 'mobile plan questions');
      await shot(m, 'mobile-plan');
      await m.goto(url + '/#/settings/routing');
      await m.waitForSelector('#set-routing .rt-box');
      await m.waitForTimeout(250);
      await shot(m, 'mobile-routing');
      await m.goto(url + '/#/settings/repos');
      await m.waitForSelector('#set-repos');
      await m.waitForTimeout(500);
      const pos = await m.evaluate(() => {
        const nav = document.querySelector('.settings-nav').getBoundingClientRect();
        const title = document.getElementById('set-repos-t').getBoundingClientRect();
        const cur = document.querySelector('.settings-nav a[aria-current="true"]').getBoundingClientRect();
        return { navBottom: nav.bottom, titleTop: title.top, cur: [cur.left, cur.right], vw: window.innerWidth };
      });
      assert(pos.titleTop >= pos.navBottom, 'section title must not hide under the sticky nav: ' + JSON.stringify(pos));
      assert(pos.cur[0] >= 0 && pos.cur[1] <= pos.vw, 'current section link scrolled into view: ' + JSON.stringify(pos));
      await shot(m, 'mobile-settings');
      await m.goto(url + '/#/factory');
      await m.waitForSelector('.board .card');
      await m.click('#new-feature-btn');
      await m.waitForSelector('dialog[open] #nf-feature');
      await m.waitForTimeout(300);
      assert(!(await m.isVisible('dialog[open] .foot-hint')), 'no keyboard hint on touch screens');
      await m.selectOption('dialog[open] select[name="routing"]', ':custom');
      await m.waitForSelector('dialog[open] .rt-row');
      const dw = await m.evaluate(() => { const b = document.querySelector('dialog[open] .modal-body'); return [b.scrollWidth, b.clientWidth]; });
      assert(dw[0] <= dw[1] + 1, 'the custom routing rows fit the phone dialog: ' + dw);
      await shot(m, 'mobile-newfeature');
      await m.click('dialog[open] button[data-action="cancel"]');
      await m.goto(url + '/#/chat/c-1a2b3c4d5e');
      await m.waitForSelector('.chat-thread .msg-user');
      await shot(m, 'mobile-chat');
    });

    // ---------------------------------------------------------------- DESIGN §25: Welcome, setup, maps, Grid Card, Start, procedures, delivery
    await section25(harness);

    // ---------------------------------------------------------------- Seraphina OS Harness: Home and the view modules
    for (const [, suite] of AREA_SUITES) await suite(harness);

    await step('zero console errors and no dialogs', async () => {
      assert(consoleErrors.length === 0, 'console errors:\n' + consoleErrors.join('\n') + (httpErrors.length ? '\nHTTP errors:\n' + httpErrors.join('\n') : ''));
      assert(dialogs.length === 0, 'dialogs: ' + dialogs.join(' | '));
    });
    await mctx.close();
    await ctx.close();
  } finally {
    await browser.close().catch(() => {});
    proc.kill('SIGTERM');
    log(`\n${results.length - failures}/${results.length} checks passed. Screenshots: ${SHOTS}`);
    if (failures) {
      for (const r of results.filter((x) => !x.ok)) log('\n--- ' + r.name + '\n' + r.error);
      process.exit(1);
    }
  }
}

main().catch((e) => { console.error(e); process.exit(2); });
