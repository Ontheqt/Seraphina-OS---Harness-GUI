// Every screen of the GUI as a screenshot, for a visual review: desktop 1440x900 and phone 390x844, light and dark.
//
//   node tests/gui/screens.mjs [outdir]          (default: $GUI_SHOTS/final)
//   GUI_SCREENS=home,tracker node tests/gui/screens.mjs    only the screens whose name starts with one of these
//   GUI_SCROLL=1                                  also save each long page scrolled, a screen at a time (outdir/scroll/)
//
// Runs against tests/gui_mock_server.py (seed data, no background activity, so the shots are stable) and, for the
// empty states, a second mock started with --fresh. Writes <outdir>/<screen>-<desktop|phone>-<light|dark>.png and
// <outdir>/report.json: per shot, sideways scroll, elements that stick out past the right edge at 390 px, and console
// errors. This is a review aid, not a test: it exits 0 unless the mock or the browser cannot start.
import { createRequire } from 'node:module';
import { spawn, execSync } from 'node:child_process';
import { mkdirSync, existsSync, readdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
function loadPlaywright() {
  try { return require('playwright'); } catch (e) {
    for (const root of ['/opt/node-tools/node_modules', execSync('npm root -g').toString().trim()]) {
      try { return require(path.join(root, 'playwright')); } catch (e2) { /* next */ }
    }
    throw e;
  }
}
const { chromium } = loadPlaywright();
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const OUT = process.argv[2] || path.join(process.env.GUI_SHOTS || '/tmp/gui-shots', 'final');
const ONLY = (process.env.GUI_SCREENS || '').split(',').map((x) => x.trim()).filter(Boolean);
const SCROLL = !!process.env.GUI_SCROLL;
const VARIANTS = (process.env.GUI_VARIANTS || 'desktop-light,desktop-dark,phone-light,phone-dark').split(',');

function startMock(extra) {
  return new Promise((resolve, reject) => {
    const proc = spawn('python3', [path.join(ROOT, 'tests', 'gui_mock_server.py'), '--port', '0', '--no-scenario', ...(extra || [])], { cwd: ROOT, stdio: ['ignore', 'pipe', 'pipe'] });
    let buf = '';
    const timer = setTimeout(() => reject(new Error('mock server did not print READY')), 15000);
    proc.stdout.on('data', (d) => { buf += d.toString(); const m = /READY (http:\/\/\S+)/.exec(buf); if (m) { clearTimeout(timer); resolve({ proc, url: m[1] }); } });
    proc.stderr.on('data', () => {});
    proc.on('exit', (code) => { clearTimeout(timer); reject(new Error('mock server exited with ' + code)); });
  });
}
async function launch() {
  try { return await chromium.launch(); } catch (e) {
    const base = '/opt/pw-browsers';
    const cands = existsSync(base) ? readdirSync(base).filter((d) => /^chromium-\d+/.test(d)).map((d) => path.join(base, d, 'chrome-linux', 'chrome')) : [];
    for (const exe of cands) if (existsSync(exe)) return chromium.launch({ executablePath: exe });
    throw e;
  }
}

const J = (repo, id, tab) => '#/job/' + repo + '/' + id + (tab ? '/' + tab : '');
const click = (sel) => async (page) => { await page.locator(sel).first().click(); await page.waitForTimeout(500); };
/** The screens: name, hash (or path), fresh (the --fresh mock), wait (a selector), act (after load). */
const SCREENS = [
  { name: 'login', path: '/login', noAuth: true, wait: '#token' },
  { name: 'welcome', hash: '#/welcome' },
  { name: 'welcome-fresh', hash: '#/welcome', fresh: true },
  { name: 'home', hash: '#/home', wait: '.home-card' },
  { name: 'home-fresh', hash: '#/home', fresh: true, wait: '.home-card' },
  { name: 'factory', hash: '#/factory', wait: '.board .card' },
  { name: 'factory-fresh', hash: '#/factory', fresh: true },
  { name: 'job-live', hash: J('my-app', '002-rate-limit-signup-form', 'live'), wait: '#drawer:not([hidden])' },
  { name: 'job-files', hash: J('my-app', '002-rate-limit-signup-form', 'files'), wait: '#drawer:not([hidden])' },
  { name: 'job-diff', hash: J('my-app', '002-rate-limit-signup-form', 'diff'), wait: '#drawer:not([hidden])' },
  { name: 'job-routing', hash: J('my-app', '002-rate-limit-signup-form', 'routing'), wait: '#drawer:not([hidden])' },
  { name: 'job-items', hash: J('my-app', '002-rate-limit-signup-form', 'items'), wait: '#drawer:not([hidden])' },
  { name: 'job-needs-you', hash: J('my-app', '004-render-user-notes'), wait: '#drawer:not([hidden])' },
  { name: 'job-delivered', hash: J('my-app', '009-export-the-audit-log-as-csv'), wait: '#drawer:not([hidden])' },
  { name: 'new-feature', hash: '#/factory', wait: '.board .card', act: click('#new-feature-btn') },
  { name: 'shortcuts', hash: '#/factory', wait: '.board .card', act: async (page) => { await page.keyboard.press('?'); await page.waitForTimeout(400); } },
  { name: 'plans', hash: '#/plans' },
  { name: 'plans-fresh', hash: '#/plans', fresh: true },
  { name: 'plan-detail', hash: '#/plans/docs-site/P001-move-the-docs-to-a-versioned' },
  { name: 'plan-ready', hash: '#/plans/my-app/P002-audit-log-v2' },
  { name: 'chat', hash: '#/chat' },
  { name: 'chat-fresh', hash: '#/chat', fresh: true },
  { name: 'chat-open', hash: '#/chat/c-1a2b3c4d5e' },
  { name: 'chat-persona', hash: '#/chat/c-fa17e0c001' },
  { name: 'chat-error', hash: '#/chat/c-fa17e0d001' },
  { name: 'agents', hash: '#/agents' },
  { name: 'agents-coder', hash: '#/agents/coder' },
  { name: 'settings', hash: '#/settings' },
  { name: 'settings-profiles', hash: '#/settings/profiles' },
  { name: 'settings-providers', hash: '#/settings/providers' },
  { name: 'settings-repos', hash: '#/settings/repos' },
  { name: 'doctor', hash: '#/doctor' },
  { name: 'grid', hash: '#/grid' },
  { name: 'grid-node', hash: '#/grid/models' },
  { name: 'procedures', hash: '#/procedures' },
  { name: 'setup', hash: '#/setup' },
  { name: 'setup-keys', hash: '#/setup/api-keys' },
  { name: 'setup-map', hash: '#/setup/map' },
  { name: 'start', hash: '#/start' },
  { name: 'placement', hash: '#/placement' },
  { name: 'tracker', hash: '#/housekeeping', wait: '.tr-row, .tr-item, [data-ref]' },
  { name: 'tracker-fresh', hash: '#/housekeeping', fresh: true },
  { name: 'tracker-waiting', hash: '#/housekeeping?status=waiting' },
  { name: 'tracker-system', hash: '#/housekeeping?scope=system' },
  { name: 'tracker-item', hash: '#/housekeeping/job:my-app/002-rate-limit-signup-form/I-6' },
  { name: 'tracker-decision', hash: '#/housekeeping/job:my-app/002-rate-limit-signup-form/I-3' },
  { name: 'tracker-agents', hash: '#/housekeeping/agents' },
  { name: 'personas', hash: '#/personas', wait: '.pe-card' },
  { name: 'personas-fresh', hash: '#/personas', fresh: true },
  { name: 'persona-card', hash: '#/personas/kate', wait: '[data-sec]' },
  { name: 'persona-offline', hash: '#/personas/kate-offline' },
  { name: 'persona-wake', hash: '#/personas/mira' },
  { name: 'messages', hash: '#/messages' },
  { name: 'messages-fresh', hash: '#/messages', fresh: true },
  { name: 'message', hash: '#/messages/m/M-000013' },
  { name: 'message-outcomes', hash: '#/messages/m/M-000011' },   // a failure and an unknown outcome (DESIGN §40)
  { name: 'messages-inbound', hash: '#/messages/inbound' },
  { name: 'phone-setup', hash: '#/messages/setup' },
  { name: 'phone-setup-fresh', hash: '#/messages/setup', fresh: true },
  ...[1, 2, 3, 4, 5, 6, 7].map((n) => ({ name: 'ntfy-' + n, hash: '#/messages/setup/ntfy/' + n })),
  ...[1, 2, 3, 4, 5].map((n) => ({ name: 'telegram-' + n, hash: '#/messages/setup/telegram/' + n })),
  { name: 'messages-settings', hash: '#/messages/settings' },
  { name: 'schedule', hash: '#/schedule' },
  { name: 'schedule-fresh', hash: '#/schedule', fresh: true },
  ...['overview', 'instructions', 'memory', 'journal', 'thread', 'runs'].map((tab) => ({ name: 'wake-persona-' + tab, hash: '#/schedule/persona/seraphina/' + tab })),
  { name: 'wake-persona-import', hash: '#/schedule/persona/mira' },   // Changes before import (§15, §33)
  { name: 'chat-checkins', hash: '#/schedule/chats' },
  { name: 'persona-setup', hash: '#/schedule/setup' },
  { name: 'operator', hash: '#/operator' },
  { name: 'operator-fresh', hash: '#/operator', fresh: true },
  { name: 'operator-receipt', hash: '#/operator/receipt/r-20261025-010' },
  { name: 'operator-custody', hash: '#/operator/custody' },
  { name: 'operator-drift', hash: '#/operator/drift' },
  { name: 'operator-errors', hash: '#/operator/errors' },
  { name: 'operator-activity', hash: '#/activity' },
];

/** What a page shows that a reviewer should know about: sideways scroll, things past the right edge, clipped text. */
async function inspect(page) {
  return page.evaluate(() => {
    const W = window.innerWidth;
    const out = { doc: document.documentElement.scrollWidth - W, past: [], clipped: [] };
    const scrollsX = (el) => { for (let a = el.parentElement; a; a = a.parentElement) { const s = getComputedStyle(a); if (/(auto|scroll|hidden|clip)/.test(s.overflowX) && a.scrollWidth > a.clientWidth + 1) return true; } return false; };
    const name = (el) => el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + (el.className && typeof el.className === 'string' ? '.' + el.className.trim().split(/\s+/).slice(0, 2).join('.') : '');
    for (const el of document.querySelectorAll('#app *, #drawer *, .modal *, dialog *')) {
      if (!el.getClientRects().length) continue;
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) continue;
      const cs = getComputedStyle(el);
      if (cs.visibility === 'hidden') continue;
      if (r.right > W + 1 && !scrollsX(el) && out.past.length < 15) out.past.push(name(el) + ' right=' + Math.round(r.right));
      // Visually hidden text (.sr-only and the like: clipped to a pixel on purpose) is not clipped text.
      if (r.width <= 2 || r.height <= 2 || cs.clip !== 'auto' || /sr-only/.test(String(el.className))) continue;
      const own = Array.from(el.childNodes).some((n) => n.nodeType === 3 && n.textContent.trim());
      if (own && el.scrollWidth > el.clientWidth + 2 && /(hidden|clip)/.test(cs.overflowX) && cs.textOverflow !== 'ellipsis' && out.clipped.length < 15) out.clipped.push(name(el) + ' "' + el.textContent.trim().slice(0, 40) + '"');
    }
    return out;
  });
}

async function settle(page, wait) {
  if (wait) await page.waitForSelector(wait, { timeout: 8000 }).catch(() => {});
  await page.waitForFunction(() => !Array.from(document.querySelectorAll('.skel')).some((e) => e.getClientRects().length), null, { timeout: 6000 }).catch(() => {});
  await page.waitForTimeout(500);
}
async function noToasts(page) {
  await page.evaluate(() => document.querySelectorAll('#toasts .toast, #toasts .toast-more').forEach((t) => t.remove()));
  await page.waitForTimeout(60);
}
/** The element that scrolls the page's content: an open drawer or modal first, then the tallest scroller in #main. */
async function scroller(page) {
  return page.evaluate(() => {
    const cands = Array.from(document.querySelectorAll('#drawer:not([hidden]) *, .modal *, #main, #main *')).filter((el) => {
      if (!el.getClientRects().length) return false;
      const s = getComputedStyle(el);
      return /(auto|scroll)/.test(s.overflowY) && el.scrollHeight > el.clientHeight + 40;
    });
    const open = cands.filter((el) => el.closest('#drawer:not([hidden]), .modal'));
    const list = open.length ? open : cands;
    list.sort((a, b) => (b.clientHeight * (b.scrollHeight - b.clientHeight)) - (a.clientHeight * (a.scrollHeight - a.clientHeight)));
    const el = list[0];
    if (!el) return null;
    el.setAttribute('data-shot-scroller', '1');
    return { h: el.scrollHeight, c: el.clientHeight };
  });
}

async function main() {
  mkdirSync(OUT, { recursive: true });
  if (SCROLL) mkdirSync(path.join(OUT, 'scroll'), { recursive: true });
  const seeded = await startMock();
  const fresh = await startMock(['--fresh']);
  const browser = await launch();
  const report = [];
  try {
    for (const variant of VARIANTS) {
      const [form, scheme] = variant.split('-');
      const phone = form === 'phone';
      const ctxs = {};
      const ctxFor = async (mock, auth) => {
        const key = mock.url + (auth ? '' : '#noauth');
        if (ctxs[key]) return ctxs[key];
        const ctx = await browser.newContext({ viewport: { width: phone ? 390 : 1440, height: phone ? 844 : 900 }, colorScheme: scheme,
          deviceScaleFactor: phone ? 2 : 1, isMobile: phone, hasTouch: phone });
        if (auth) await ctx.addCookies([{ name: 'sf_token', value: 'test-token', url: mock.url }]);
        const page = await ctx.newPage();
        const errors = [];
        page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
        page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
        page.on('dialog', (d) => d.dismiss().catch(() => {}));
        ctxs[key] = { ctx, page, errors };
        return ctxs[key];
      };
      for (const s of SCREENS) {
        if (ONLY.length && !ONLY.some((o) => s.name.startsWith(o))) continue;
        const mock = s.fresh ? fresh : seeded;
        const { page, errors } = await ctxFor(mock, !s.noAuth);
        errors.length = 0;
        const file = s.name + '-' + form + '-' + scheme;
        try {
          await page.goto('about:blank');
          await page.goto(mock.url + (s.path || '/') + (s.hash || ''), { waitUntil: 'load' });
          await settle(page, s.wait);
          if (s.act) { await s.act(page); await settle(page); }
          await noToasts(page);
          await page.screenshot({ path: path.join(OUT, file + '.png') });
          const info = await inspect(page);
          let segments = 0;
          if (SCROLL) {
            const sc = await scroller(page);
            if (sc) {
              const stepPx = Math.max(200, Math.floor(sc.c * 0.85));
              for (let y = stepPx, i = 2; y < sc.h - 40 && i <= 8; y += stepPx, i++) {
                await page.evaluate((top) => { const el = document.querySelector('[data-shot-scroller]'); if (el) el.scrollTop = top; }, y);
                await page.waitForTimeout(150);
                await noToasts(page);
                await page.screenshot({ path: path.join(OUT, 'scroll', file + '-' + i + '.png') });
                segments = i;
              }
              await page.evaluate(() => { const el = document.querySelector('[data-shot-scroller]'); if (el) { el.scrollTop = 0; el.removeAttribute('data-shot-scroller'); } });
            }
          }
          report.push({ file, title: await page.title(), sideways: info.doc, past: info.past, clipped: info.clipped, errors: errors.slice(), segments });
          const flags = [info.doc > 0 ? 'SIDEWAYS ' + info.doc : '', info.past.length ? 'PAST ' + info.past.length : '', info.clipped.length ? 'CLIPPED ' + info.clipped.length : '', errors.length ? 'ERRORS ' + errors.length : ''].filter(Boolean).join(' ');
          console.log((flags ? '! ' : '  ') + file + (segments ? ' (+' + (segments - 1) + ' scrolled)' : '') + (flags ? '  ' + flags : ''));
        } catch (e) {
          report.push({ file, failed: String(e && e.message || e) });
          console.log('X ' + file + ': ' + (e && e.message || e));
        }
      }
      for (const k of Object.keys(ctxs)) await ctxs[k].ctx.close();
    }
  } finally {
    writeFileSync(path.join(OUT, 'report.json'), JSON.stringify(report, null, 1));
    await browser.close();
    seeded.proc.kill();
    fresh.proc.kill();
  }
  console.log('Screens: ' + OUT);
}
main().catch((e) => { console.error(e); process.exit(1); });
