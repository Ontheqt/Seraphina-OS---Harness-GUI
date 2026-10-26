// End-to-end: the GUI (swfactory/web) in headless Chromium against the REAL backend, not the mock.
//
//   FACTORY_E2E=1 node tests/gui/e2e_real.mjs
//
// Gated like the other end-to-end tests (tests/e2e/test_e2e_server.py): without FACTORY_E2E=1 it says so and exits 0.
// tests/e2e/gui_backend.py starts `factory serve` from this checkout on a free loopback port with a temporary HOME
// and data dir, the scripted model (tests/e2e/scripted_llm.py) as a local server, a mock Open WebUI, a mock ntfy on
// a Unix socket and a scratch git repository, and a small control server for what only a phone or the operator does
// (a tap on Got it, a change receipt). One real flow per area, in order, each building on the one before:
//
//   1. Home loads with live statuses (a fresh install opens Welcome first);
//   2. the setup wizard adds a provider (the scripted server as llama.cpp, with Test) and the repository;
//   3. Start work: a feature with a routing profile, its estimate; the run's placement reason and cost; the job
//      stops at needs-human (its spec marks a product decision);
//   4. the job's Items tab confirms the (product) decision, then Approve merges it;
//   5. a persona card (kate, through Open WebUI) starts a persona chat that answers;
//   6. the ntfy phone: accept the no-sandbox risk, pair (a test, Got it from the phone), the notice in the Inbox;
//   7. the operator: mark kate and her receipts folder, a synthetic receipt, Scan now, then the Activity view.
//
// Every step fails on a console error, a dialog or an unexpected HTTP error. Screenshots (1440x900 dark and
// 390x844 light) go to $GUI_E2E_SHOTS (default <tmp>/sf-gui-e2e-shots). FACTORY_E2E_KEEP=1 keeps the backend's
// folder (a failed run keeps it anyway); FACTORY_E2E_SCRATCH says where it goes. GUI_E2E_STOP_AFTER=<n> stops
// after flow n (for working on one area).
import { createRequire } from 'node:module';
import { spawn, execSync } from 'node:child_process';
import { mkdirSync, existsSync, readdirSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

if (process.env.FACTORY_E2E !== '1') {
  console.log('skipped: end-to-end GUI test against the real backend: set FACTORY_E2E=1');
  process.exit(0);
}

const require = createRequire(import.meta.url);
function loadPlaywright() {
  for (const at of [null, '/opt/node-tools/node_modules/playwright']) {
    try { return at ? require(at) : require('playwright'); } catch (e) { /* the next place */ }
  }
  return require(path.join(execSync('npm root -g').toString().trim(), 'playwright'));
}
const { chromium } = loadPlaywright();

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const SHOTS = process.env.GUI_E2E_SHOTS || path.join(os.tmpdir(), 'sf-gui-e2e-shots');
const STOP_AFTER = Number(process.env.GUI_E2E_STOP_AFTER) || 99;
const STEP_LIMIT_MS = Number(process.env.GUI_STEP_TIMEOUT_MS) || 240000;
const FEATURE = 'Add clearCompleted(items) to src/todo.js that drops done items, with a test';
const ACK = 'I understand my AI agents can read my messages and send them anywhere';
mkdirSync(SHOTS, { recursive: true });

const results = [];
const consoleErrors = [];
const httpErrors = [];
const dialogs = [];
let allowHttpErrors = false;
let failed = false;

const log = (...a) => console.log(...a);
function assert(cond, msg) { if (!cond) throw new Error(msg); }

// ---------------------------------------------------------------- the backend

/** Start tests/e2e/gui_backend.py; resolves with its READY data plus {proc, lines}. */
function startBackend() {
  return new Promise((resolve, reject) => {
    const args = ['-m', 'tests.e2e.gui_backend'];
    if (process.env.FACTORY_E2E_KEEP === '1') args.push('--keep');
    const proc = spawn(process.env.FACTORY_PYTHON || 'python3', args, { cwd: ROOT, stdio: ['pipe', 'pipe', 'pipe'] });
    const lines = [];
    let buf = '';
    let done = false;
    const timer = setTimeout(() => { if (!done) { done = true; reject(new Error('the backend did not print READY within 60 s:\n' + lines.join('\n'))); } }, 60000);
    proc.stdout.on('data', (d) => {
      buf += d.toString();
      let i;
      while ((i = buf.indexOf('\n')) >= 0) {
        const line = buf.slice(0, i);
        buf = buf.slice(i + 1);
        lines.push(line);
        if (line.startsWith('READY ') && !done) { done = true; clearTimeout(timer); resolve(Object.assign(JSON.parse(line.slice(6)), { proc, lines })); }
        else if (!line.startsWith('READY ')) log('[backend] ' + line.slice(0, 2000));
      }
    });
    proc.stderr.on('data', (d) => process.stderr.write('[backend] ' + d));
    proc.on('exit', (code) => { if (!done) { done = true; clearTimeout(timer); reject(new Error('the backend exited with ' + code + ':\n' + lines.join('\n'))); } });
  });
}
async function stopBackend(be) {
  if (!be || be.proc.exitCode !== null) return;
  const exited = new Promise((r) => be.proc.on('exit', r));
  try { await fetch(be.control + '/stop', { method: 'POST', body: '{}' }); } catch (e) { be.proc.kill('SIGTERM'); }
  await Promise.race([exited, new Promise((r) => setTimeout(r, 40000))]);
  if (be.proc.exitCode === null) be.proc.kill('SIGKILL');
}
/** The control server: what a phone or the operator does, and what the scripted model saw. */
async function ctl(be, p, body) {
  const r = await fetch(be.control + p, body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  return r.json();
}
/** The real API, with the token (checks only: the browser does the driving). */
async function api(be, p) {
  const r = await fetch(be.url + p, { headers: { Authorization: 'Bearer ' + be.token } });
  const text = await r.text();
  try { return JSON.parse(text); } catch (e) { return text; }
}
async function waitApi(be, p, pred, what, ms = 180000) {
  const t0 = Date.now();
  let last = null;
  while (Date.now() - t0 < ms) {
    last = await api(be, p);
    if (pred(last)) return last;
    await new Promise((r) => setTimeout(r, 500));
  }
  throw new Error('timed out after ' + ms / 1000 + ' s waiting for ' + what + ': ' + JSON.stringify(last).slice(0, 1500));
}

// ---------------------------------------------------------------- the browser

async function launch() {
  try { return await chromium.launch(); } catch (e) {
    const base = process.env.PLAYWRIGHT_BROWSERS_PATH || '/opt/pw-browsers';
    const cands = existsSync(base) ? readdirSync(base).filter((d) => /^chromium-\d+/.test(d)).map((d) => path.join(base, d, 'chrome-linux', 'chrome')) : [];
    for (const exe of cands) if (existsSync(exe)) return chromium.launch({ executablePath: exe });
    throw e;
  }
}
function watch(page, label) {
  page.on('console', (m) => {
    if (m.type() !== 'error') return;
    const text = m.text();
    if (allowHttpErrors && /Failed to load resource: the server responded with a status of (400|404|409)/.test(text)) return;
    consoleErrors.push(`[${label}] ${text}`);
  });
  page.on('pageerror', (e) => consoleErrors.push(`[${label}] pageerror: ${e.message}`));
  page.on('response', (r) => {
    if (r.status() >= 400 && !allowHttpErrors) httpErrors.push(`[${label}] ${r.status()} ${r.request().method()} ${new URL(r.url()).pathname}`);
  });
  page.on('dialog', async (d) => { dialogs.push(`[${label}] ${d.type()}: ${d.message()}`); await d.dismiss().catch(() => {}); });
}
async function newPage(browser, be, label, phone) {
  const ctx = await browser.newContext({ viewport: phone ? { width: 390, height: 844 } : { width: 1440, height: 900 },
    colorScheme: phone ? 'light' : 'dark', deviceScaleFactor: phone ? 2 : 1, isMobile: !!phone, hasTouch: !!phone });
  const page = await ctx.newPage();
  watch(page, label);
  return { ctx, page };
}
async function shot(page, area, name) {
  const dir = path.join(SHOTS, area);
  mkdirSync(dir, { recursive: true });
  await page.evaluate(() => document.querySelectorAll('#toasts .toast, #toasts .toast-more').forEach((t) => t.remove())).catch(() => {});
  await page.waitForTimeout(120);
  await page.screenshot({ path: path.join(dir, name + '.png') });
}
async function noOverflow(page, label) {
  const m = await page.evaluate(() => {
    const main = document.getElementById('main');
    return { doc: document.documentElement.scrollWidth, inner: window.innerWidth, main: main ? main.scrollWidth - main.clientWidth : 0 };
  });
  assert(m.doc <= m.inner && m.main <= 1, `${label}: sideways scroll (page ${m.doc} > ${m.inner}, main ${m.main})`);
}
async function waitPost(page, re, action, ms = 20000) {
  const [resp] = await Promise.all([
    page.waitForResponse((r) => r.request().method() === 'POST' && re.test(new URL(r.url()).pathname), { timeout: ms }),
    action(),
  ]);
  return resp;
}
const text = async (page, sel) => ((await page.locator(sel).count()) ? (await page.locator(sel).first().textContent()) || '' : '');
const atStep = (page, id) => page.waitForSelector('.wz-main[data-step="' + id + '"]', { timeout: 15000 });
async function next(page, id) {
  await page.click('.wz-foot [data-action="wz-next"]');
  if (id) await atStep(page, id);
}
const pick = (page, opt) => page.click('.wz-main .opt-card[data-opt="' + opt + '"]');
/** Home's live cards all show a status (not "checking…"). */
const homeLoaded = (page) => page.waitForFunction(() => {
  const cards = Array.from(document.querySelectorAll('#home-root .home-grid:not(.is-planned) .home-card'));
  return cards.length >= 3 && cards.every((c) => { const s = c.querySelector('.home-status-text'); return s && s.textContent && !/checking/.test(s.textContent); });
}, null, { timeout: 20000 });
/** The header's Doctor badge counts what GET /api/doctor finds now (every check that fails, info aside); returns the
 *  failing checks as "level name". A badge read before the last setting was saved fails this within 20 s. */
async function doctorBadgeMatches(page, be) {
  const t0 = Date.now();
  let want = null;
  let shown = null;
  while (Date.now() - t0 < 20000) {
    const checks = (await api(be, '/api/doctor')).checks || [];
    want = checks.filter((c) => !c.ok && (c.level || 'error') !== 'info').length;
    shown = await page.evaluate(() => ({ count: document.getElementById('doctor-count').hidden ? 0 : Number(document.getElementById('doctor-count').textContent),
      label: document.getElementById('doctor-label').textContent, level: document.getElementById('doctor-badge').dataset.level }));
    if (shown.count === want && !/to set up/.test(shown.label)) return checks.filter((c) => !c.ok).map((c) => c.level + ' ' + c.name);
    await page.waitForTimeout(500);
  }
  throw new Error('the Doctor badge says ' + JSON.stringify(shown) + ' while the server finds ' + want + ' problems now');
}
const homeStatus = (page) => page.$$eval('#home-root .home-grid:not(.is-planned) .home-card', (els) => Object.fromEntries(els.map((e) => [e.dataset.section, e.querySelector('.home-status-text').textContent])));

// ---------------------------------------------------------------- steps

async function step(n, name, fn, ctx) {
  if (failed) { results.push({ name, skipped: true }); log(`  skip ${name}`); return; }
  if (n > STOP_AFTER) { results.push({ name, skipped: true }); return; }
  const t0 = Date.now();
  let timer = null;
  const watchdog = new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(`step did not finish within ${STEP_LIMIT_MS / 1000} s`)), STEP_LIMIT_MS); });
  try {
    const before = consoleErrors.length + httpErrors.length + dialogs.length;
    await Promise.race([fn(), watchdog]);
    const extra = consoleErrors.concat(httpErrors, dialogs);
    assert(extra.length === before, 'console errors, HTTP errors or dialogs: ' + extra.slice(before).join(' | '));
    results.push({ name, ok: true });
    log(`  ok   ${n}. ${name} (${Date.now() - t0} ms)`);
  } catch (e) {
    failed = true;
    results.push({ name, ok: false, error: String(e && e.stack || e) });
    log(`  FAIL ${n}. ${name}: ${e && e.message ? e.message : e}`);
    for (const [label, page] of Object.entries(ctx.pages || {})) {
      try { await page.screenshot({ path: path.join(SHOTS, 'FAIL-' + n + '-' + label + '.png'), fullPage: false }); } catch (err) { /* closed */ }
    }
    try { log('control status: ' + JSON.stringify(await ctl(ctx.be, '/status')).slice(0, 3000)); } catch (err) { /* gone */ }
    try { log('server log:\n' + (await ctl(ctx.be, '/log')).log); } catch (err) { /* gone */ }
    try { await ctl(ctx.be, '/failed', {}); } catch (err) { /* keep it */ }
  } finally {
    clearTimeout(timer);
  }
}

async function main() {
  const be = await startBackend();
  log('real backend at ' + be.url + ' (root ' + be.root + ')');
  const browser = await launch();
  const d = await newPage(browser, be, 'desktop', false);
  const p = await newPage(browser, be, 'phone', true);
  const page = d.page;
  const phone = p.page;
  const ctx = { be, pages: { desktop: page, phone } };
  const S = {};   // what one flow hands the next: the job, the chat
  try {
    // ------------------------------------------------------------ 1. Home
    await step(1, 'Home: sign in with the token; a fresh install opens Welcome; Home shows every section with its live status', async () => {
      await page.goto(be.url + '/');
      await page.waitForSelector('#token');
      await page.fill('#token', be.token);
      await page.click('#submit');
      // a fresh install (no repository, no model for the line) opens Welcome with its first step
      await page.waitForFunction(() => location.hash === '#/welcome', null, { timeout: 15000 });
      await page.waitForSelector('.first-step [data-action="first-step"]');
      assert(/Not set up yet/.test(await text(page, '.setup-chips')), 'Welcome says the harness is not set up: ' + await text(page, '.setup-chips'));
      await shot(page, 'e2e-real', '01-welcome-desktop');
      await page.goto(be.url + '/#/home');
      await page.waitForSelector('#view-home:not([hidden]) .home-card');
      await homeLoaded(page);
      await page.waitForSelector('#conn[data-state="open"]', { timeout: 15000 });   // the live event stream
      const status = await homeStatus(page);
      assert(/Home · Seraphina OS Harness$/.test(await page.title()), 'title: ' + await page.title());
      // the statuses are the server's own: no repository yet, the personas it finds (kate through Open WebUI, the
      // shipped seraphina), an empty inbox, nobody marked the operator
      const personas = (await api(be, '/api/personas')).personas;
      const unread = (await api(be, '/api/messages?limit=1')).unread;
      assert(/no repository yet/.test(status.factory), 'Software Factory: ' + status.factory);
      assert(status.personas === personas.length + ' personas', 'Personas: ' + status.personas + ' (the server has ' + personas.length + ')');
      assert(unread === 0 && /no unread messages/.test(status.messages), 'Messages: ' + status.messages);
      assert(/No operator yet/.test(status.operator), 'Operator: ' + status.operator);
      S.home0 = status;
      log('    Home statuses: ' + JSON.stringify(status));
      await shot(page, 'e2e-real', '01-home-desktop');
      await phone.context().addCookies([{ name: 'sf_token', value: be.token, url: be.url }]);
      await phone.goto(be.url + '/#/home');
      await homeLoaded(phone);
      await noOverflow(phone, 'Home phone');
      await shot(phone, 'e2e-real', '01-home-phone');
    }, ctx);

    // ------------------------------------------------------------ 2. Setup wizard
    await step(2, 'Setup wizard: a llama.cpp provider at the scripted server (Test passes) and the scratch repository; Apply saves both', async () => {
      await page.goto(be.url + '/#/welcome');
      await page.click('.first-step [data-action="first-step"]');
      await atStep(page, 'where');
      await pick(page, 'local');
      await page.waitForSelector('.opt-card[data-opt="local"][aria-checked="true"]');
      await next(page, 'local-servers');
      await pick(page, 'llamacpp');
      await page.waitForSelector('.opt-card[data-opt="llamacpp"][aria-checked="true"]');
      await next(page, 'local-details');
      // Nothing looks for model servers by itself: the owner presses "Look for model servers" (this run does not, so
      // the test machine's other services get nothing), and the setup status the page has read has no look in it.
      assert(/it sends no key and changes nothing/.test(await text(page, '.wz-group[data-group="look-around"]')), 'the look-around button says what it does');
      const host = (await api(be, '/api/setup')).host;
      assert(host && !('model_servers' in host) && Array.isArray(host.listening), 'the setup status never looks for model servers by itself: ' + JSON.stringify(host));
      await page.fill('#wz-local-details-llamacpp_url', be.llm_url);
      await page.fill('#wz-local-details-llamacpp_model', be.llm_model);
      await page.fill('#wz-local-details-llamacpp_context', '65536');
      const probe = await waitPost(page, /^\/api\/models\/[^/]+\/test$/, () => page.click('[data-action="wz-test"][data-test="local:llamacpp"]'), 60000);
      assert(probe.status() === 200, 'Test answered ' + probe.status());
      await page.waitForSelector('.wz-test[data-test="local:llamacpp"] .wz-test-result.ok', { timeout: 30000 });
      log('    Test: ' + (await text(page, '.wz-test[data-test="local:llamacpp"] .wz-test-result')).trim());
      await shot(page, 'e2e-real', '02-setup-local-details-desktop');
      await next(page, 'roles');
      const rolesChecked = await page.$$eval('.wz-main .opt-card[aria-checked="true"]', (els) => els.map((e) => e.dataset.opt));
      assert(JSON.stringify(rolesChecked) === '["preset:strata"]', 'a fresh install with one local server is offered the all-local preset: ' + rolesChecked);
      await next(page, 'tools');
      await next(page, 'sandbox');
      await next(page, 'protected');
      await next(page, 'repo');
      await page.fill('#wz-repo-path', be.repo);
      await next(page, 'output');
      assert(await page.getAttribute('.wz-main .opt-card[data-opt="merge"]', 'aria-checked') === 'true', 'finished work merges by default');
      await next(page, 'merge-policy');
      await next(page, 'review');
      const items = await page.$$eval('.apply-item', (els) => els.map((e) => e.dataset.item));
      log('    apply items: ' + items.join(', '));
      assert(JSON.stringify(items) === JSON.stringify(['preset', 'models', 'repo']), 'the review applies the preset, the models and the repository, in order: ' + items);
      const preset = await text(page, '.apply-item[data-item="preset"]');
      assert(new RegExp(be.llm_model + ' · llama\\.cpp takes the place of the preset’s local model').test(preset), 'the scripted server takes the preset’s local model: ' + preset.slice(0, 600));
      await shot(page, 'e2e-real', '02-setup-review-desktop');
      await page.click('.wz-foot [data-action="wz-next"]');
      await page.waitForSelector('.apply-done [data-action="go-start"]', { timeout: 60000 });
      assert(await page.locator('.apply-item.is-ok').count() === items.length, 'every item applied');
      // the Doctor runs again: its badge is this server's findings now, not the fresh install's to-do list in red
      await doctorBadgeMatches(page, be);
      await shot(page, 'e2e-real', '02-setup-applied-desktop');
      // the real config now has both
      const cfg = await api(be, '/api/config');
      const config = cfg.config || cfg;
      const repos = (config.repos || []).map((r) => r.name);
      assert(repos.includes(be.repo_name), 'the repository is registered: ' + JSON.stringify(config.repos));
      const local = (config.models || {}).local || {};
      const prov = (config.providers || {})[local.provider] || {};
      assert(local.model === be.llm_model && prov.base_url === be.llm_url, 'models.local runs on the scripted server: ' + JSON.stringify({ local, prov }));
    }, ctx);

    // ------------------------------------------------------------ 3. Start a feature with a profile
    await step(3, 'Start work: a feature with the local-quick profile, its estimate; the job runs, each run shows its placement reason and cost; it stops at needs-human', async () => {
      await page.click('.apply-done [data-action="go-start"]');
      await page.waitForFunction(() => /^#\/start/.test(location.hash));
      await atStep(page, 'what');
      await pick(page, 'feature');
      await next(page, 'repo');
      await pick(page, be.repo_name);
      await next(page, 'describe');
      await page.fill('#wz-describe-text', FEATURE);
      await next(page, 'routing');
      await page.waitForSelector('#wz-routing-profile');
      await page.selectOption('#wz-routing-profile', 'profile:' + be.profile);
      await next(page, 'output');
      await next(page, 'review');
      await page.waitForSelector('.wz-run .pl-est .pl-est-total', { timeout: 20000 });
      const est = await text(page, '.wz-run .pl-est');
      log('    estimate: ' + est.replace(/\s+/g, ' ').slice(0, 300));
      assert(new RegExp('profile ' + be.profile).test(await text(page, '.wz-run .pl-est-head')), 'the estimate is the profile’s: ' + await text(page, '.wz-run .pl-est-head'));
      const raw = await text(page, '.wz-raw pre');
      assert(raw.includes('"profile": "' + be.profile + '"'), 'the request carries the profile: ' + raw);
      await shot(page, 'e2e-real', '03-start-review-desktop');
      const resp = await waitPost(page, /^\/api\/jobs$/, () => page.click('.wz-foot [data-action="wz-next"]'));
      assert(resp.status() === 201, 'POST /api/jobs ' + resp.status());
      const job = (await resp.json()).job;
      S.job = job;
      assert(job.profile === be.profile, 'the job has the profile: ' + job.profile);
      await page.waitForFunction((h) => location.hash.startsWith(h), '#/job/' + be.repo_name + '/' + job.id);
      await page.waitForSelector('#drawer:not([hidden])');
      // live: the drawer follows the job to needs-human (the spec marks a product decision)
      await page.waitForFunction(() => /Needs human|needs you|Needs you/i.test((document.querySelector('#drawer-head .pill') || {}).textContent || ''), null, { timeout: 200000 });
      const now = await api(be, '/api/jobs/' + be.repo_name + '/' + job.id);
      assert(now.job.stage === 'needs-human', 'the job stopped for a person: ' + now.job.stage + ' ' + now.job.reason);
      log('    stopped: ' + now.job.reason);
      assert(/profile local-quick/.test(await text(page, '#drawer-head [data-meta="profile"]')), 'the drawer names the profile: ' + await text(page, '#drawer-head [data-meta="profile"]'));
      assert(/cost \$0/.test(await text(page, '#drawer-head [data-meta="cost"]')), 'the drawer shows the cost: ' + await text(page, '#drawer-head [data-meta="cost"]'));
      await page.click('#tab-runs');
      await page.waitForSelector('#panel-runs:not([hidden]) .pl-run');
      const rows = await page.$$eval('#panel-runs .pl-run', (els) => els.map((e) => ({ run: e.dataset.run, why: e.querySelector('.pl-why') ? e.querySelector('.pl-why').dataset.why : null,
        whyText: e.querySelector('.pl-why') ? e.querySelector('.pl-why').textContent : '', text: e.textContent })));
      log('    runs: ' + rows.map((r) => r.run + ':' + r.why).join(' '));
      const builder = rows.find((r) => /builder/.test(r.run));
      assert(builder && builder.why === 'cost', 'the builder’s run shows its placement reason (cost): ' + JSON.stringify(builder));
      assert(/a one-model test server: every step on the local model, free/.test(await text(page, '#panel-runs .pl-step[data-agent="builder"]')), 'and the profile’s reason in words: ' + await text(page, '#panel-runs .pl-step[data-agent="builder"]'));
      assert(rows.every((r) => /\$0/.test(r.text)), 'every run shows its cost ($0 on the local server): ' + rows.map((r) => r.text.slice(0, 80)).join(' | '));
      assert(rows.some((r) => r.why === 'stage-fit'), 'a judge’s run shows its own reason (stage-fit)');
      await shot(page, 'e2e-real', '03-job-runs-desktop');
      await phone.goto(be.url + '/#/job/' + be.repo_name + '/' + job.id + '/runs');
      await phone.waitForSelector('#panel-runs:not([hidden]) .pl-run');
      await noOverflow(phone, 'Runs tab phone');
      await shot(phone, 'e2e-real', '03-job-runs-phone');
    }, ctx);

    // ------------------------------------------------------------ 4. Items: confirm the product decision, approve
    await step(4, 'Items tab: the (product) decision is confirmed by a person; Approve then merges the job', async () => {
      const job = S.job;
      await page.goto(be.url + '/#/job/' + be.repo_name + '/' + job.id + '/items');
      await page.waitForSelector('#panel-items .tr-decisions .tr-decision');
      const lines = await page.$$eval('#panel-items .tr-decision', (els) => els.map((e) => ({ done: e.classList.contains('is-done'), text: e.querySelector('.tr-line').textContent })));
      assert(lines.length === 1 && !lines[0].done && lines[0].text.includes(be.product_line), 'one product decision waits: ' + JSON.stringify(lines));
      assert(await page.locator('#panel-items .tr-decision input:checked').count() === 0, 'never pre-checked');
      await shot(page, 'e2e-real', '04-items-decision-desktop');
      await page.check('#panel-items .tr-decision input[data-line="0"]');
      const c = await waitPost(page, /\/confirm$/, () => page.click('[data-action="confirm-selected"]'));
      assert(c.status() === 200, 'confirm ' + c.status());
      await page.waitForSelector('#panel-items .tr-decision.is-done');
      const asm = await api(be, '/api/jobs/' + be.repo_name + '/' + job.id + '/assumptions');
      assert(asm.unconfirmed.length === 0 && asm.confirmed.length === 1, 'the server has it confirmed: ' + JSON.stringify(asm));
      await shot(page, 'e2e-real', '04-items-confirmed-desktop');
      await phone.goto(be.url + '/#/job/' + be.repo_name + '/' + job.id + '/items');
      await phone.waitForSelector('#panel-items .tr-decision.is-done');
      await noOverflow(phone, 'Items tab phone');
      await shot(phone, 'e2e-real', '04-items-confirmed-phone');
      await page.waitForSelector('#drawer-head [data-action="approve"]');
      await page.click('#drawer-head [data-action="approve"]');
      await page.waitForSelector('dialog[open] button[data-action="ok"]');
      // the decision was the job's one must-do item: nothing is left to move to the follow-ups
      assert(!(await page.locator('dialog[open] input[data-carry-ok]').isVisible()), 'no must-do item is left to carry');
      await shot(page, 'e2e-real', '04-approve-dialog-desktop');
      const a = await waitPost(page, /\/approve$/, () => page.click('dialog[open] button[data-action="ok"]'));
      assert(a.status() === 200, 'approve ' + a.status());
      await waitApi(be, '/api/jobs/' + be.repo_name + '/' + job.id, (r) => r.job && r.job.stage === 'merged', 'the job to merge', 60000);
      await page.waitForFunction(() => /Merged/i.test((document.querySelector('#drawer-head .pill') || {}).textContent || ''), null, { timeout: 20000 });
      await shot(page, 'e2e-real', '04-job-merged-desktop');
    }, ctx);

    // ------------------------------------------------------------ 5. Persona chat
    await step(5, 'Personas: kate’s profile card (read from Open WebUI) starts a persona chat that answers through Open WebUI', async () => {
      await page.goto(be.url + '/#/personas');
      const card = '#personas-root .pe-card[data-persona="kate"]';
      await page.waitForSelector(card + ' [data-action="card"]', { timeout: 20000 });
      assert(await page.getAttribute(card, 'data-kind') === 'openwebui', 'kate is bound to Open WebUI');
      await shot(page, 'e2e-real', '05-personas-desktop');
      await page.click(card + ' [data-action="card"]');
      await page.waitForFunction(() => location.hash === '#/personas/kate');
      // no card yet on a fresh install: import it from Open WebUI (read only there: a GET of the export)
      await page.waitForSelector('#personas-root .empty button', { timeout: 20000 });
      assert(/No profile card yet/.test(await text(page, '#personas-root .empty')), 'a fresh install has no card: ' + await text(page, '#personas-root .empty'));
      await page.click('#personas-root .empty button');
      await page.waitForSelector('dialog[open] input[data-from="live"]');
      await page.click('dialog[open] input[data-from="live"]');
      await page.waitForSelector('dialog[open] #pe-imp-live-model option[value="kate"]', { state: 'attached', timeout: 20000 });
      await page.selectOption('dialog[open] #pe-imp-live-model', 'kate');
      await shot(page, 'e2e-real', '05-persona-import-desktop');
      const imp = await waitPost(page, /\/api\/personas\/import$/, () => page.click('dialog[open] button[data-action="ok"]'));
      assert(imp.status() === 201, 'import ' + imp.status() + ' ' + await imp.text());
      log('    import body: ' + imp.request().postData());
      await page.waitForSelector('dialog[open]', { state: 'detached' });
      if (!/^#\/personas\/kate/.test(await page.evaluate(() => location.hash))) await page.goto(be.url + '/#/personas/kate');
      await page.waitForSelector('#personas-root [data-sec="map"] .pe-surface', { timeout: 20000 });
      // the card is read from Open WebUI: its base model and settings as stored there
      const cardText = await text(page, '#personas-root');
      assert(/qwen3\.8-flash/.test(cardText) && /temperature/.test(cardText), 'the profile card from Open WebUI: ' + cardText.slice(0, 600));
      await shot(page, 'e2e-real', '05-persona-card-desktop');
      const r = await waitPost(page, /^\/api\/chats$/, () => page.click('#personas-root [data-action="chat"]'));
      assert(r.status() === 201, 'POST /api/chats ' + r.status());
      const chat = (await r.json()).chat;
      S.chat = chat;
      await page.waitForFunction((id) => location.hash === '#/chat/' + id, chat.id);
      await page.waitForSelector('.composer textarea:not([disabled])');
      await page.fill('.composer textarea', 'Hello Kate, what did you check today?');
      await waitPost(page, /\/messages$/, () => page.press('.composer textarea', 'Enter'));
      await page.waitForFunction(() => /ECHO Hello Kate/.test(Array.from(document.querySelectorAll('.chat-thread .msg-assistant, .chat-thread .turn')).map((e) => e.textContent).join(' ')), null, { timeout: 60000 });
      await page.waitForSelector('.composer textarea:not([disabled])', { timeout: 30000 });
      await shot(page, 'e2e-real', '05-persona-chat-desktop');
      await phone.goto(be.url + '/#/chat/' + chat.id);
      await phone.waitForSelector('.chat-thread .msg-user');
      await noOverflow(phone, 'persona chat phone');
      await shot(phone, 'e2e-real', '05-persona-chat-phone');
    }, ctx);

    // ------------------------------------------------------------ 6. ntfy: pair the phone, a notice in the Inbox
    await step(6, 'Messages: accept the no-sandbox risk, pair the ntfy phone (a test, Got it from the phone), the notice in the Inbox', async () => {
      await page.goto(be.url + '/#/messages');
      await page.waitForSelector('.mw-banner[data-banner="precondition"] [data-action="acknowledge"]', { timeout: 20000 });
      await page.click('.mw-banner[data-banner="precondition"] [data-action="acknowledge"]');
      await page.waitForSelector('dialog[open] textarea[data-field="ack"]');
      await page.fill('dialog[open] textarea[data-field="ack"]', ACK);
      const ack = await waitPost(page, /\/api\/wakes\/acknowledge$/, () => page.click('dialog[open] button[data-action="ok"]'));
      assert(ack.status() === 200, 'acknowledge ' + ack.status());
      await page.waitForSelector('.mw-banner[data-banner="acknowledged"]');
      // Phone setup: the channel the config names, now able to send; its setup steps, then the phone's step
      await page.click('#messages-root a[href="#/messages/setup"]');
      const card = '#messages-root .mw-channel[data-channel="phone"]';
      await page.waitForSelector(card, { timeout: 20000 });
      await page.waitForFunction((sel) => /Can send now/.test((document.querySelector(sel) || {}).textContent || ''), card, { timeout: 20000 });
      await shot(page, 'e2e-real', '06-phone-setup-desktop');
      await page.click(card + ' a[href^="#/messages/setup/ntfy/1"]');
      await page.waitForSelector('.mw-wizard[data-step="1"]');
      await page.click('.mw-wizard .mw-steps a[href^="#/messages/setup/ntfy/6"]');
      await page.waitForSelector('.mw-wizard[data-step="6"] .mw-pairbox', { timeout: 20000 });
      assert((await text(page, '.mw-pairbox')).includes('https://box.tail0123.ts.net:8445'), 'the server address to add on the phone: ' + await text(page, '.mw-pairbox'));
      await shot(page, 'e2e-real', '06-ntfy-pair-desktop');
      await page.click('[data-action="next"]');
      await page.waitForSelector('.mw-wizard[data-step="7"]');
      const test = await waitPost(page, /\/api\/notify\/test$/, () => page.click('[data-action="test"]'));
      assert(test.status() === 202, 'test ' + test.status());
      const tap = await ctl(be, '/phone/tap', { title: 'Test', timeout: 20 });
      assert(tap.tapped, 'the phone got the test and tapped Got it: ' + JSON.stringify(tap));
      await page.waitForSelector('.mw-pair-state[data-state="paired"]', { timeout: 30000 });
      await shot(page, 'e2e-real', '06-ntfy-paired-desktop');
      const msgs = (await api(be, '/api/messages')).messages;
      const testMsg = msgs.find((m) => m.kind === 'test');
      assert(testMsg && testMsg.deliveries.phone === 'sent' && !testMsg.unread, 'the server has the test notice sent and read: ' + JSON.stringify(msgs).slice(0, 800));
      await page.goto(be.url + '/#/messages');
      const row = '#messages-root .mw-msg[data-message="' + testMsg.id + '"]';
      await page.waitForSelector(row, { timeout: 20000 });
      assert(!/is-unread/.test(await page.getAttribute(row, 'class')), 'Got it on the phone marked it read');
      assert(await page.locator(row + ' .mw-dchip[data-state="sent"]').count() === 1, 'its phone delivery is Sent');
      assert(await text(page, row + ' .mw-row-title') === 'Test', 'its title: ' + await text(page, row + ' .mw-row-title'));
      const needsHuman = msgs.find((m) => m.kind === 'job.needs_human');
      assert(needsHuman && await page.locator('#messages-root .mw-msg[data-message="' + needsHuman.id + '"]').count() === 1, 'the job’s needs-human notice is listed too');
      await shot(page, 'e2e-real', '06-inbox-desktop');
      await page.click(row + ' a.mw-row-main');
      await page.waitForFunction((id) => location.hash === '#/messages/m/' + id, testMsg.id);
      await page.waitForFunction(() => /What each phone got/.test(document.body.textContent), null, { timeout: 15000 });
      // DESIGN §40 on the real Notifier: a confirmed delivery reads Succeeded, and it reached every account (partial false)
      const detail = await api(be, '/api/messages/' + testMsg.id);
      const served = detail.deliveries.find((d) => d.channel === 'phone');
      assert(served && served.state === 'sent' && served.partial === false, 'the server’s phone delivery: ' + JSON.stringify(served));
      const outcome = await text(page, '#drawer .mw-delivery[data-channel="phone"] .mw-outcome');
      assert(/^Succeeded the phone service confirmed it\.$/.test(outcome.trim()), 'its outcome in words: ' + outcome);
      await shot(page, 'e2e-real', '06-message-desktop');
      await phone.goto(be.url + '/#/messages');
      await phone.waitForSelector(row, { timeout: 20000 });
      await noOverflow(phone, 'Inbox phone');
      await shot(phone, 'e2e-real', '06-inbox-phone');
    }, ctx);

    // ------------------------------------------------------------ 7. Operator: a receipt, then the activity view
    await step(7, 'Operator: mark kate and her receipts folder, a synthetic receipt, Scan now; the receipt in the list and in kate’s Activity', async () => {
      await page.goto(be.url + '/#/operator');
      await page.waitForSelector('#operator-root [data-action="mark"]', { timeout: 20000 });
      await page.click('[data-action="mark"]');
      await page.waitForSelector('dialog.modal .op-mark-list');
      await page.check('input[data-persona="kate"]');
      const mark = await waitPost(page, /^\/api\/config$/, () => page.click('dialog.modal button[data-action="ok"]'));
      assert(mark.status() === 200, 'mark ' + mark.status());
      log('    mark body: ' + mark.request().postData());
      await page.waitForFunction(() => !document.querySelector('dialog.modal'));
      const op = await api(be, '/api/operator');
      assert(JSON.stringify(op.operators) === '["kate"]', 'kate is the operator on the server: ' + JSON.stringify(op));
      await page.click('[data-action="folder"]');
      await page.waitForSelector('dialog.modal #op-folder');
      await page.fill('#op-folder', be.receipts_dir);
      const folder = await waitPost(page, /^\/api\/config$/, () => page.click('dialog.modal button[data-action="ok"]'));
      assert(folder.status() === 200, 'folder ' + folder.status());
      await page.waitForFunction(() => !document.querySelector('dialog.modal'));
      const rec = await ctl(be, '/receipt', { intent: 'Keep two weeks of the web server’s access logs' });
      S.receipt = rec;
      const gets = [];
      const onReq = (r) => { if (/\/api\/operator(\/receipts)?(\?|$)/.test(new URL(r.url()).pathname + new URL(r.url()).search)) gets.push(Date.now() + ' ' + r.method() + ' ' + new URL(r.url()).pathname + new URL(r.url()).search); };
      page.on('request', onReq);
      const scan = await waitPost(page, /\/api\/operator\/receipts\/scan$/, () => page.click('[data-action="scan"]'));
      assert(scan.status() === 200, 'scan ' + scan.status());
      await page.waitForFunction((id) => document.querySelector('#operator-root').textContent.includes(id), rec.id, { timeout: 20000 });
      try {
        await page.waitForFunction(() => /1 receipt read/.test((document.querySelector('.op-who') || {}).textContent || ''), null, { timeout: 8000 });
      } catch (e) {
        throw new Error('Who and where still says ' + JSON.stringify(await text(page, '.op-who')) + ' after Scan now; the server: ' + JSON.stringify((await api(be, '/api/operator')).scan) + '; requests: ' + gets.join(' | '));
      }
      page.off('request', onReq);
      const list = await text(page, '#operator-root');
      assert(/Reported by the operator/i.test(list), 'labelled as reported: ' + list.slice(0, 400));
      await shot(page, 'e2e-real', '07-operator-receipts-desktop');
      await page.goto(be.url + '/#/activity?agent=kate&since=24h');
      await page.waitForSelector('#operator-root .op-ent[data-source="receipt"]', { timeout: 20000 });
      const ent = await text(page, '#operator-root .op-ent[data-source="receipt"]');
      assert(ent.includes(rec.id) || ent.includes(rec.intent), 'the receipt is in kate’s activity: ' + ent);
      await shot(page, 'e2e-real', '07-activity-desktop');
      await phone.goto(be.url + '/#/activity?agent=kate&since=24h');
      await phone.waitForSelector('#operator-root .op-ent[data-source="receipt"]', { timeout: 20000 });
      await noOverflow(phone, 'Activity phone');
      await shot(phone, 'e2e-real', '07-activity-phone');
    }, ctx);

    await step(8, 'The scripted model saw no protocol violation and no failed tool call; Open WebUI was never written to; Home’s statuses and the Doctor badge follow the work', async () => {
      const st = await ctl(be, '/status');
      assert(st.violations.length === 0, 'protocol violations: ' + JSON.stringify(st.violations));
      assert(st.tool_errors.length === 0, 'failed tool calls: ' + JSON.stringify(st.tool_errors).slice(0, 2000));
      assert(st.openwebui_writes.length === 0, 'the harness wrote to Open WebUI: ' + JSON.stringify(st.openwebui_writes));
      assert(st.server_alive, 'the server is still up');
      await page.goto(be.url + '/#/home');
      await homeLoaded(page);
      try {
        await page.waitForFunction(() => /kate is the operator/.test((document.querySelector('#home-root .home-card[data-section="operator"] .home-status-text') || {}).textContent || ''), null, { timeout: 8000 });
      } catch (e) {
        throw new Error('Home’s Operator card still says ' + JSON.stringify((await homeStatus(page)).operator) + ' after kate was marked: ' + JSON.stringify(await api(be, '/api/operator')));
      }
      const status = await homeStatus(page);
      log('    Home statuses now: ' + JSON.stringify(status));
      assert(JSON.stringify(status) !== JSON.stringify(S.home0), 'the statuses follow the work: ' + JSON.stringify(status));
      assert(/idle, ready for work/.test(status.factory) && /kate is the operator/.test(status.operator), 'Software Factory and Operator: ' + JSON.stringify(status));
      await shot(page, 'e2e-real', '08-home-after-desktop');
      // The badge counts what the server finds now; what that is on this test server (no sandbox, no Tailscale,
      // a receipts folder the harness's own user can write) is said, not judged.
      // GET /api/doctor runs in the server (run_checks(factory=…)): never a shell-only "<check> credential" warning
      const rows = await api(be, '/api/doctor');
      const names = (Array.isArray(rows) ? rows : rows.checks || []).map((r) => r.name);
      assert(names.length && !names.some((n) => / credential$/.test(n)), 'no shell-only credential row: ' + names.join(', '));
      const open = await doctorBadgeMatches(page, be);
      log('    Doctor: ' + (open.join('; ') || 'all checks pass') + ' | badge: ' + (await text(page, '#doctor-label')).trim());
    }, ctx);
  } finally {
    await browser.close().catch(() => {});
    await stopBackend(be);
  }
  // A process the server started (an agent's shell, a dev server) must not outlive it: the backend kills and names any.
  const strays = be.lines.filter((l) => /^STRAYS /.test(l));
  if (strays.length) results.push({ name: 'no process outlives the server', ok: false, error: strays.join('\n') });
  const ok = results.filter((r) => r.ok).length;
  const bad = results.filter((r) => r.ok === false).length;
  const skipped = results.filter((r) => r.skipped).length;
  log(`\n${ok} passed, ${bad} failed, ${skipped} skipped; screenshots in ${SHOTS}`);
  for (const r of results.filter((x) => x.ok === false)) log('\nFAILED: ' + r.name + '\n' + r.error);
  for (const line of be.lines.filter((l) => /^(KEPT|STRAYS|FAILED|SERVER EXITED)/.test(l))) log(line);
  process.exit(bad ? 1 : 0);
}

main().catch((e) => { console.error(e); process.exit(1); });
