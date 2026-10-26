// Checks for Personas (DESIGN §33, §15 "Chats without a repository, personas and Open WebUI profiles", the capability
// map, §35 "A new persona from an Open WebUI profile", §36's operator mark): the view module swfactory/web/views/personas.js
// and its chat extensions against tests/gui_mock/personas.py, run by tests/gui/smoke.mjs (AREA_SUITES) after the core
// checks, or alone with GUI_ONLY=personas node tests/gui/smoke.mjs. It runs on a mock of its own (imports, the offline
// switch and new chats change state the other suites must not see). Screenshots of every new screen, desktop 1440x900
// and phone 390x844: t.areaShot(page, AREA, name) → $GUI_AREA_SHOTS/personas/<name>.png.
//
// run(t) gets the harness of smoke.mjs: { browser, url, startMock, step, assert, canon, waitPost, shot, cleanShot,
// noOverflow, watch, allowHttp, newPage(label, {phone, width, height, colorScheme, mockUrl}), areaShot(page, area, name) }.

const AREA = 'personas';
const KATE_V = (version, extra) => Object.assign({
  id: 'kate', name: 'Kate', base_model_id: 'qwen3.8-flash', version_id: version, user_id: 'u-owner',
  meta: { description: 'Kate <b>exported</b>', profile_image_url: 'data:image/png;base64,' + 'A'.repeat(700000), toolIds: ['server_status'], skillIds: ['house-style'] },
  params: { system: 'You are Kate. Hello {{USER_NAME}}.', temperature: 0.4, stop: ['\\nUser:'] },
}, extra || {});
const NOVA = {
  id: 'nova', name: 'Nova', base_model_id: 'strata', version_id: 'v-nova-2', background_image_data: 'B'.repeat(500000),
  meta: { description: 'Nova, a second preset', profile_image_url: 'data:image/png;base64,' + 'C'.repeat(10) },
  params: { system: 'You are Nova.', top_p: 0.8 },
};

export async function run(t) {
  const { step, assert, noOverflow, waitPost } = t;
  const mock = await t.startMock(['--no-scenario']);
  const M = mock.url;
  const d = await t.newPage(AREA, { mockUrl: M });
  const p = await t.newPage(AREA + '-phone', { phone: true, mockUrl: M });
  const page = d.page;
  const hook = (name, body) => page.request.post(M + '/__mock/personas/' + name, { data: body || {} });
  const persona = (name) => '#personas-root .pe-card[data-persona="' + name + '"]';
  /** Go to a route of the mock (a hash change keeps the page: a dialog a failed step left open is closed first). */
  const go = async (pg, hash) => {
    await pg.evaluate(() => document.querySelectorAll('dialog[open]').forEach((x) => x.close())).catch(() => {});
    await pg.goto(M + '/' + hash);
  };
  const attached = { state: 'attached' };
  try {
    await step(AREA + ': the list groups Open WebUI personas, offline copies and wake personas; badges, drift, the operator mark, errors; text stays text; 1440 and 390 px', async () => {
      for (const [pg, tag] of [[page, 'desktop'], [p.page, 'phone']]) {
        await go(pg, '#/personas');
        await pg.waitForSelector('#view-personas:not([hidden]) #personas-root .pe-card[data-persona="kate"]');
        if (tag === 'desktop') assert(await pg.getAttribute('.rail-item[data-view="personas"]', 'aria-current') === 'page', 'the rail marks it');
        const groups = await pg.$$eval('#personas-root .pe-group', (els) => els.map((e) => e.dataset.group + ':' + e.querySelectorAll('.pe-card').length));
        assert(t.canon(groups) === t.canon(['openwebui:4', 'snapshot:2', 'wake:2']), 'groups: ' + groups);
        await noOverflow(pg, AREA + ' list ' + tag);
        await t.areaShot(pg, AREA, 'personas-list-' + tag);
      }
      const kate = persona('kate');
      assert(await page.locator(kate + ' .pe-operator').count() === 1, 'kate carries the Operator badge');
      assert(/Acts on the server with her own tools, outside the harness/.test(await page.textContent(kate + ' .pe-operator-note')), 'with its help text, shown');
      assert(await page.locator('#personas-root .pe-operator').count() === 1, 'only the marked persona');
      assert(await page.getAttribute(kate + ' .pe-item', 'href') === '#/housekeeping/system%3AS-3', 'the drift links its tracker item');
      assert(/changed in Open WebUI/.test(await page.textContent(kate + ' .pe-drift')), 'the drift in words');
      assert(/knowledge off/.test(await page.textContent(kate + ' .pe-tags')) && /knowledge on/.test(await page.textContent(persona('sage') + ' .pe-tags')), 'knowledge chips');
      assert(await page.locator(persona('sage') + ' [data-action="chat"]').count() === 0 && /Cannot start/.test(await page.textContent(persona('sage'))), 'an errored persona shows its error, no Chat');
      assert(/offline copy not enabled/.test(await page.textContent(persona('nova-offline'))) && await page.locator(persona('nova-offline') + ' [data-action="chat"]').count() === 0, 'a disabled copy: no Chat');
      assert(await page.locator(persona('kate-offline') + ' [data-action="chat"]').count() === 1, 'an enabled copy has Chat');
      assert(await page.getAttribute(persona('mira') + ' a.btn', 'href') === '#/schedule/persona/mira', 'a wake persona links to her page in Messages & Wakes');
      assert(await page.locator(persona('mira') + ' [data-action="chat"], ' + persona('mira') + ' [data-action="card"]').count() === 0, 'and has neither Chat nor a profile card');
      assert(await page.locator('#personas-root b, #personas-root i, #personas-root script').count() === 0, 'descriptions with markup stay text');
      assert((await page.textContent(persona('mira') + ' .pe-desc')).includes('<b>backups</b>'), 'shown as written');
      assert(await page.locator('#personas-root .pe-proc ol.proc li').count() === 4, 'How personas work, as a numbered procedure');
    });

    await step(AREA + ': Check now lists each persona in words (a listing change without a new version, an unlisted model, a stale copy); a new version opens its item and persona.drift reloads the list', async () => {
      await go(page, '#/personas');
      await page.waitForSelector(persona('kate'));
      const resp = await waitPost(page, /\/api\/personas\/check$/, () => page.click('#personas-root [data-action="check"]'));
      assert(resp.status() === 200, 'check ' + resp.status());
      await page.waitForSelector('#personas-root .pe-check[data-check="all"] .pe-check-row');
      const rows = await page.$$eval('#personas-root .pe-check-row', (els) => Object.fromEntries(els.map((e) => [e.dataset.persona, e.querySelector('.pe-check-words').textContent])));
      assert(/without a new version/.test(rows['kate-browser']), 'kate-browser: ' + rows['kate-browser']);
      assert(/does not list 'nova'/.test(rows['nova-offline']), 'nova-offline: ' + rows['nova-offline']);
      assert(/offline copy is of another version/.test(rows['kate-offline']), 'kate-offline: ' + rows['kate-offline']);
      assert(rows.mira === undefined && rows.seraphina === undefined, 'wake personas are not checked here');
      await t.areaShot(page, AREA, 'personas-check-desktop');
      await go(p.page, '#/personas');
      await p.page.waitForSelector(persona('kate'));
      await waitPost(p.page, /\/api\/personas\/check$/, () => p.page.click('#personas-root [data-action="check"]'));
      await p.page.waitForSelector('#personas-root .pe-check[data-check="all"] .pe-check-row');
      await noOverflow(p.page, AREA + ' check phone');
      await t.areaShot(p.page, AREA, 'personas-check-phone');
      await hook('bump', { model: 'kate', version: 'v-333ccc', kind: 'behaviour' });
      await go(page, '#/personas/kate');
      await page.waitForSelector('#personas-root [data-sec="card"]');
      const drift = page.waitForEvent('response', (r) => /\/api\/personas$/.test(new URL(r.url()).pathname), { timeout: 10000 });
      const own = await waitPost(page, /\/api\/personas\/check$/, () => page.click('#personas-root [data-action="check"]'));
      assert(JSON.parse(own.request().postData()).persona === 'kate', 'the page checks its own persona: ' + own.request().postData());
      await drift;
      await page.waitForSelector('#personas-root .pe-check[data-check="kate"] .pe-check-row.is-warn');
      assert(/version v-222bbb → v-333ccc/.test(await page.textContent('#personas-root .pe-check[data-check="kate"]')), 'the change names both versions');
    });

    await step(AREA + ': a profile card page: the card (versions, settings as stored, knowledge as text), her warning and drift banners, the appended text word for word, and a jump list', async () => {
      await go(page, '#/personas/kate');
      await page.waitForSelector('#personas-root [data-sec="map"] .pe-surface');
      const api = await (await page.request.get(M + '/api/personas/kate/card')).json();
      assert(await page.textContent('#personas-root .pe-appended') === api.appended_text, 'the appended text is shown word for word');
      assert(/# Skill: house-style/.test(api.appended_text), 'with its skill section');
      assert(/changed in Open WebUI since this card/.test(await page.textContent('#personas-root [data-sec="card"]')), 'card version vs live version');
      assert(await page.locator('#personas-root .pe-warning').count() === api.live.warnings.length && api.live.warnings.length > 0, 'one banner per live warning');
      assert(/Kate changed in Open WebUI/.test(await page.textContent('#personas-root .pe-drift-banner')), 'the drift banner');
      const stop = await page.$eval('#personas-root [data-sec="settings"] .pe-raw', (e) => e.textContent);
      assert(stop === '\\nUser:', 'a stop entry is shown as stored (a backslash and an n): ' + JSON.stringify(stop));
      assert((await page.textContent('#personas-root [data-sec="card"]')).includes("Runbooks <script>alert('kb')</script>"), 'knowledge names are text');
      assert(await page.locator('#personas-root script, #personas-root b').count() === 0, 'no markup from Open WebUI is rendered');
      const toc = await page.$$eval('#personas-root .pe-toc button', (els) => els.map((e) => e.textContent));
      assert(toc.includes('What runs where') && toc.includes('What the harness adds after her prompt'), 'jump list: ' + toc);
      // DESIGN §36: an Activity link on each persona page, to the Operator area's activity page
      assert(await page.getAttribute('#personas-root [data-action="activity"]', 'href') === '#/activity?agent=kate&since=7d', 'the persona page links her activity');
      await page.click('#personas-root .pe-toc button[data-focus="toc:map"]');
      assert(await page.evaluate(() => document.activeElement && document.activeElement.id) === 'pe-map-t', 'the jump list moves focus to the section');
      await page.evaluate(() => { const m = document.getElementById('main'); m.scrollTop = 0; });
      await noOverflow(page, AREA + ' card desktop');
      await t.areaShot(page, AREA, 'personas-card-desktop');
      await go(p.page, '#/personas/kate');
      await p.page.waitForSelector('#personas-root [data-sec="map"] .pe-surface');
      await noOverflow(p.page, AREA + ' card phone');
      await t.areaShot(p.page, AREA, 'personas-card-phone');
    });

    await step(AREA + ': Changes before import on the profile card page (DESIGN §15, §33): the baseline first, word for word, no diff; her receipts by id as she reported them, never green; attach (a 400 in its words) and detach', async () => {
      await go(page, '#/personas/kate');
      await page.waitForSelector('#personas-root [data-sec="before-import"] .bi-row');
      const sec = '#personas-root [data-sec="before-import"]';
      const api = await (await page.request.get(M + '/api/personas/kate/card')).json();
      assert(await page.textContent(sec + ' [data-part="baseline"]') === api.import.baseline, 'the baseline sentence word for word');
      assert(await page.locator(sec + ' .mw-diff, ' + sec + ' .sf-pre').count() === 0, 'no diff, no history');
      const ids = await page.$$eval(sec + ' .bi-row', (els) => els.map((e) => e.dataset.receipt));
      assert(JSON.stringify(ids) === JSON.stringify(api.import.changes_before_import.map((r) => r.id)), 'in the order given: ' + ids);
      assert(await page.locator(sec + ' .tone-ok, ' + sec + ' .tag-ok').count() === 0, 'no row is green or ticked');
      assert(/r-20261019-000/.test(await page.textContent(sec + ' .bi-row.is-missing')) && await page.locator(sec + ' .bi-row.is-missing [data-action="detach"]').count() === 1, 'a missing row keeps its Detach');
      const toc = await page.$$eval('#personas-root .pe-toc button', (els) => els.map((e) => e.textContent));
      assert(toc.includes('Changes before import'), 'in the jump list: ' + toc);
      await page.click(sec + ' .bi-form > summary');
      await page.fill(sec + ' [data-field="before-import-ids"]', 'r-20261025-010 r-nope');
      t.allowHttp(true);
      await waitPost(page, /\/api\/personas\/kate\/before-import$/, () => page.click(sec + ' [data-action="attach"]'));
      await page.waitForFunction((s2) => /^unknown receipt id r-nope: only a receipt the harness has read can be attached \(scan her receipts first\)$/.test((document.querySelector(s2 + ' .bi-err') || {}).textContent || ''), sec);
      t.allowHttp(false);
      assert(await page.locator(sec + ' .bi-row[data-receipt="r-20261025-010"]').count() === 0, 'a 400 changes nothing');
      await page.fill(sec + ' [data-field="before-import-ids"]', 'r-20261025-010');
      const att = await waitPost(page, /\/api\/personas\/kate\/before-import$/, () => page.click(sec + ' [data-action="attach"]'));
      assert(JSON.stringify(JSON.parse(att.request().postData())) === JSON.stringify({ attach: ['r-20261025-010'], by: 'owner' }), 'attach by id, by the owner: ' + att.request().postData());
      await page.waitForSelector(sec + ' .bi-row[data-receipt="r-20261025-010"]');
      const det = await waitPost(page, /\/api\/personas\/kate\/before-import$/, () => page.click(sec + ' .bi-row[data-receipt="r-20261025-010"] [data-action="detach"]'));
      assert(JSON.stringify(JSON.parse(det.request().postData())) === JSON.stringify({ detach: ['r-20261025-010'], by: 'owner' }), 'detach: ' + det.request().postData());
      await page.waitForFunction((s2) => !document.querySelector(s2 + ' .bi-row[data-receipt="r-20261025-010"]'), sec);
      await page.$eval(sec, (e) => e.scrollIntoView({ block: 'start' }));
      await t.areaShot(page, AREA, 'personas-before-import-desktop');
      await go(p.page, '#/personas/kate');
      await p.page.waitForSelector('#personas-root [data-sec="before-import"] .bi-row');
      await noOverflow(p.page, AREA + ' before import phone');
      await p.page.$eval('#personas-root [data-sec="before-import"]', (e) => e.scrollIntoView({ block: 'start' }));
      await t.areaShot(p.page, AREA, 'personas-before-import-phone');
    });

    await step(AREA + ': the capability map: one column per surface in order, Has and Does not have, no parity, Open WebUI tools only on her own chats, a browsing persona not set up with its file and the MCP setting', async () => {
      const surfaces = await page.$$eval('#personas-root .pe-surface', (els) => els.map((e) => e.dataset.surface));
      assert(t.canon(surfaces) === t.canon(['openwebui', 'harness-chat', 'offline-copy', 'browsing', 'wake']), 'surfaces in order: ' + surfaces);
      assert(/No surface is the persona's own Open WebUI chat/.test(await page.textContent('#personas-root .pe-map-note')), 'the no-parity note, verbatim');
      const python = await page.$$eval('#personas-root .pe-surface', (els) => els.map((e) => e.dataset.surface + ':' + e.querySelectorAll('.pe-parts:not(.is-not) [data-part="Open WebUI Python tools"], .pe-parts:not(.is-not) [data-part="Open WebUI tool servers"]').length));
      assert(t.canon(python) === t.canon(['openwebui:2', 'harness-chat:0', 'offline-copy:0', 'browsing:0', 'wake:0']), 'Python tools and tool servers are available only on her own chats: ' + python);
      const lacking = await page.$$eval('#personas-root .pe-surface:not([data-surface="openwebui"])', (els) => els.every((e) => e.querySelectorAll('.pe-parts.is-not [data-part="Open WebUI Python tools"]').length === 1));
      assert(lacking, 'every other surface says it lacks them');
      const browsing = '#personas-root .pe-surface[data-surface="browsing"]';
      assert(await page.locator(browsing + '.is-off').count() === 1 && /not set up/.test(await page.textContent(browsing + ' .pe-surface-head')), 'browsing: not set up, greyed');
      assert(/kate-browser/.test(await page.textContent(browsing + ' .pe-surface-who')) && /kate-browser\.md/.test(await page.textContent(browsing + ' .pe-surface-who')), 'its persona and file named');
      assert(await page.getAttribute(browsing + ' .pe-part-note a', 'href') === '#/settings/mcp_servers', 'the browser note links the MCP servers settings');
      assert(await page.getAttribute('#personas-root .pe-surface[data-surface="wake"] .pe-surface-who a', 'href') === '#/schedule/persona/mira', 'a wake persona links her page');
      const del = await page.$$eval('#personas-root .pe-delegation li', (els) => els.map((e) => e.dataset.route));
      assert(t.canon(del) === t.canon(['act', 'code']), 'the two delegation routes: ' + del);
      assert(!/\btotal\b|parity: true|all capabilities/i.test(await page.textContent('#personas-root [data-sec="map"]')), 'no combined total, no parity');
      await page.$eval('#personas-root [data-sec="map"]', (e) => e.scrollIntoView({ block: 'start' }));
      await t.areaShot(page, AREA, 'personas-map-desktop');
      await p.page.$eval('#personas-root [data-sec="map"]', (e) => e.scrollIntoView({ block: 'start' }));
      await t.areaShot(p.page, AREA, 'personas-map-phone');
    });

    await step(AREA + ': a model id finds its persona; a persona without a card offers Import; Open WebUI not listing it or not answering is said beside the card', async () => {
      await go(page, '#/personas/nova');
      await page.waitForSelector('#personas-root .callout-info');
      assert(/Workspace model nova/.test(await page.textContent('#personas-root')) && /Nova, imported snapshot/.test(await page.textContent('#personas-root .view-title')), 'nova → nova-offline');
      // live.listed false, once the card page loaded: an offline copy still runs, and is re-imported from the Export file
      await page.waitForFunction(() => /Open WebUI no longer lists nova for this key\. This copy still runs on this machine\. To re-import it, use the Export file\./.test(document.getElementById('personas-root').textContent), null, { timeout: 10000 });
      assert(!/cannot start until it does/.test(await page.textContent('#personas-root')), 'never "its chats cannot start" for a copy that runs');
      await go(page, '#/personas/kate-browser/card');
      await page.waitForSelector('#personas-root .empty');
      assert(/No profile card yet/.test(await page.textContent('#personas-root .empty')) && await page.locator('#personas-root .empty button').count() === 1, 'no card: Import');
      assert(await page.locator('#personas-root [data-sec="map"]').count() === 0, 'no map without a card');
      assert(/MCP servers: browser/.test(await page.textContent('#personas-root .pe-appended')), 'its appended text still shows');
      await t.areaShot(page, AREA, 'personas-nocard-desktop');
      await go(p.page, '#/personas/kate-browser');
      await p.page.waitForSelector('#personas-root .empty');
      await noOverflow(p.page, AREA + ' no card phone');
      await t.areaShot(p.page, AREA, 'personas-nocard-phone');
      t.allowHttp(true);
      try {
        await hook('openwebui', { state: 'down' });
        await go(page, '#/personas/kate');
        await page.waitForSelector('#personas-root .pe-live-err');
        assert(/cannot reach/.test(await page.textContent('#personas-root .pe-live-err')) && await page.locator('#personas-root [data-sec="card"]').count() === 1, 'the error beside the saved card');
        await t.areaShot(page, AREA, 'personas-live-error-desktop');
        await go(p.page, '#/personas/kate');
        await p.page.waitForSelector('#personas-root .pe-live-err');
        await noOverflow(p.page, AREA + ' live error phone');
        await t.areaShot(p.page, AREA, 'personas-live-error-phone');
      } finally { await hook('openwebui', { state: 'ok' }); t.allowHttp(false); }
    });

    await step(AREA + ': an offline copy’s page: no warning about where her words go (the copy never asks Open WebUI), the through-the-API table titled as her Open WebUI chats’; 1440 and 390 px', async () => {
      for (const [pg, tag] of [[page, 'desktop'], [p.page, 'phone']]) {
        await go(pg, '#/personas/kate-offline');
        await pg.waitForSelector('#personas-root [data-sec="parts"]');
        const kate = await (await pg.request.get(M + '/api/personas/kate-offline/card')).json();
        assert((kate.live.warnings || []).length > 0, 'the server gives the live base-model warnings for a copy too');
        assert(await pg.locator('#personas-root .pe-warning').count() === 0, 'none shown on the copy’s page');
        assert(await pg.textContent('#personas-root #pe-parts-t') === 'What reaches Kate’s chats through Open WebUI', 'the table is about her Open WebUI chats: ' + await pg.textContent('#personas-root #pe-parts-t'));
        assert(/not this copy/.test(await pg.textContent('#personas-root [data-sec="parts"] .pe-sec-sub')), 'and says so');
        assert(/This copy still runs|Allow chats with this offline copy/.test(await pg.textContent('#personas-root')), 'the copy’s own switch');
        await pg.evaluate(() => { const m = document.getElementById('main'); if (m) m.scrollTop = 0; });
        await noOverflow(pg, AREA + ' copy page ' + tag);
        await t.areaShot(pg, AREA, 'personas-copy-page-' + tag);
      }
      for (const [pg, tag] of [[page, 'desktop'], [p.page, 'phone']]) {
        await go(pg, '#/personas/nova-offline');
        await pg.waitForFunction(() => /This copy still runs on this machine/.test(document.getElementById('personas-root').textContent), null, { timeout: 10000 });
        await noOverflow(pg, AREA + ' copy unlisted ' + tag);
        await t.areaShot(pg, AREA, 'personas-copy-unlisted-' + tag);
      }
    });

    await step(AREA + ': the offline switch turns an imported copy’s chats on and off (POST /api/config agents.<name>.offline)', async () => {
      await go(page, '#/personas');
      await page.waitForSelector(persona('nova-offline') + ' .pe-switch input');
      assert(await page.textContent(persona('nova-offline') + ' .pe-switch label') === 'Allow chats with this offline copy' && !(await page.isChecked(persona('nova-offline') + ' .pe-switch input')),
        'the label asks, the unticked box says it is not allowed');
      const on = await waitPost(page, /^\/api\/config$/, () => page.check(persona('nova-offline') + ' .pe-switch input'));
      assert(on.status() === 200 && t.canon(JSON.parse(on.request().postData())) === t.canon({ agents: { 'nova-offline': { offline: true } } }), 'body ' + on.request().postData());
      await page.waitForSelector(persona('nova-offline') + ' [data-action="chat"]');
      const off = await waitPost(page, /^\/api\/config$/, () => page.uncheck(persona('nova-offline') + ' .pe-switch input'));
      assert(JSON.parse(off.request().postData()).agents['nova-offline'].offline === false, 'off');
      await page.waitForFunction((sel) => !document.querySelector(sel), persona('nova-offline') + ' [data-action="chat"]');
    });

    await step(AREA + ': Import an Export file: pictures left out, a model picked, the offline copy asked for, the "maps to" answer offered as a choice, a taken name warned about first', async () => {
      await go(page, '#/personas');
      await page.click('#personas-root [data-action="import"]');
      await page.waitForSelector('dialog[open] #pe-imp-file');
      assert(!(await page.isChecked('dialog[open] [data-snapshot]')), 'the offline copy is never pre-checked');
      await page.setInputFiles('dialog[open] #pe-imp-file', { name: 'models-export.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify([KATE_V('v-444ddd'), NOVA])) });
      await page.waitForSelector('dialog[open] #pe-imp-file-model option[value="nova"]', attached);
      await page.selectOption('dialog[open] #pe-imp-file-model', 'nova');
      await page.check('dialog[open] [data-snapshot]');
      await page.waitForSelector('dialog[open] .pe-snap-fields:not([hidden])');
      assert(await page.inputValue('dialog[open] #pe-imp-snap-name') === 'nova-offline', 'the name is prefilled <model>-offline');
      await t.areaShot(page, AREA, 'personas-import-desktop');
      await go(p.page, '#/personas');
      await p.page.click('#personas-root [data-action="import"]');
      await p.page.waitForSelector('dialog[open] #pe-imp-file');
      await p.page.check('dialog[open] [data-snapshot]');
      const iw = await p.page.evaluate(() => { const b = document.querySelector('dialog[open] .modal-body'); return [b.scrollWidth, b.clientWidth]; });
      assert(iw[0] <= iw[1] + 1, 'Import fits the phone: ' + iw);
      await t.areaShot(p.page, AREA, 'personas-import-phone');
      await p.page.click('dialog[open] input[data-what="wake"]');
      await p.page.click('dialog[open] input[data-from="live"]');
      await p.page.waitForSelector('dialog[open] #pe-imp-wake-key:visible');
      await p.page.$eval('dialog[open] #pe-imp-wake-name', (e) => e.scrollIntoView({ block: 'center' }));
      await t.areaShot(p.page, AREA, 'personas-import-wake-phone');
      await p.page.click('dialog[open] button[data-action="cancel"]');
      t.allowHttp(true);
      try {
        await page.fill('dialog[open] #pe-imp-snap-name', 'local');
        await page.click('dialog[open] button[data-action="ok"]');
        await page.waitForSelector('dialog[open] .form-error:not([hidden])');
        assert(/models\.local already exists/.test(await page.textContent('dialog[open] .form-error')), 'warned before sending a models key');
        const taken = await waitPost(page, /\/api\/personas\/import$/, () => page.click('dialog[open] button[data-action="ok"]'));
        assert(taken.status() === 400, 'sent on the second press: ' + taken.status());
        await page.waitForFunction(() => /Choose another name/.test(document.querySelector('dialog[open] .form-error').textContent));
        await page.fill('dialog[open] #pe-imp-snap-name', 'nova-offline');
        await page.click('dialog[open] button[data-action="ok"]');
        await page.waitForFunction(() => /This replaces the offline copy nova-offline \(version v-9c1d02\) with a new copy and turns its chats on \(they are off now\)\. Press Import again to confirm/.test(document.querySelector('dialog[open] .form-error').textContent));
        assert(await page.evaluate(() => document.activeElement && document.activeElement.id) === 'pe-imp-snap-name', 'warned before replacing its own copy, the name focused');
        await t.areaShot(page, AREA, 'personas-import-replace-desktop');
        const maps = await waitPost(page, /\/api\/personas\/import$/, () => page.click('dialog[open] button[data-action="ok"]'));
        assert(maps.status() === 400, 'maps to two models: ' + maps.status());
        const sent = JSON.parse(maps.request().postData());
        assert(sent.model_id === 'nova' && sent.export.length === 2 && !sent.export[0].meta.profile_image_url && !sent.export[1].background_image_data, 'pictures left out');
        assert(maps.request().postData().length < 1024 * 1024, 'under 1 MB');
        await page.waitForFunction(() => /Which of this machine’s models/.test(document.querySelector('dialog[open] .form-error').textContent));
        const opts = await page.$$eval('dialog[open] #pe-imp-snap-base option', (os) => os.map((o) => o.value).filter(Boolean));
        assert(t.canon(opts) === t.canon(['local', 'sonnet']), 'the candidates it maps to: ' + opts);
        await t.areaShot(page, AREA, 'personas-import-maps-desktop');
        await page.selectOption('dialog[open] #pe-imp-snap-base', 'local');
        const ok = await waitPost(page, /\/api\/personas\/import$/, () => page.click('dialog[open] button[data-action="ok"]'));
        assert(ok.status() === 201, 'imported ' + ok.status());
        assert(t.canon(JSON.parse(ok.request().postData()).snapshot) === t.canon({ name: 'nova-offline', model: 'local' }), 'snapshot body');
      } finally { t.allowHttp(false); }
      await page.waitForFunction(() => location.hash === '#/personas/nova-offline');
      await page.waitForFunction(() => Array.from(document.querySelectorAll('#toasts .toast')).some((x) => /offline copy nova-offline replaced: its chats are on/.test(x.textContent)));
      await page.waitForSelector('#personas-root [data-sec="card"]');
      assert(/v-nova-2/.test(await page.textContent('#personas-root .view-title')), 'the re-imported copy');
    });

    await step(AREA + ': Import from Open WebUI: the listing, a card whose model no persona uses opens in the panel; no key, a read key and Open WebUI down are explained with where to fix them', async () => {
      await go(page, '#/personas');
      await page.click('#personas-root [data-action="import"]');
      await page.click('dialog[open] input[data-from="live"]');
      await page.waitForSelector('dialog[open] #pe-imp-live-model option[value="muse"]', attached);
      const presets = await page.$$eval('dialog[open] #pe-imp-live-model option', (os) => os.map((o) => o.value));
      assert(!presets.includes('qwen3.8-flash'), 'only workspace models: ' + presets);
      await page.selectOption('dialog[open] #pe-imp-live-model', 'muse');
      const r = await waitPost(page, /\/api\/personas\/import$/, () => page.click('dialog[open] button[data-action="ok"]'));
      assert(r.status() === 201 && t.canon(JSON.parse(r.request().postData())) === t.canon({ source: 'openwebui', model_id: 'muse' }), 'live body ' + r.request().postData());
      await page.waitForSelector('#drawer:not([hidden]) .panel [data-sec="card"]');
      await page.waitForTimeout(400);   // the panel's slide-in
      assert(/No persona is bound to Open WebUI’s muse/.test(await page.textContent('#drawer .drawer-sub')), 'the loose card opens in the panel');
      await t.areaShot(page, AREA, 'personas-loose-card-desktop');
      await page.keyboard.press('Escape');
      await page.waitForSelector('#drawer', { state: 'hidden' });
      await go(p.page, '#/personas');
      await p.page.click('#personas-root [data-action="import"]');
      await p.page.click('dialog[open] input[data-from="live"]');
      await p.page.waitForSelector('dialog[open] #pe-imp-live-model option[value="muse"]', attached);
      await p.page.selectOption('dialog[open] #pe-imp-live-model', 'muse');
      await waitPost(p.page, /\/api\/personas\/import$/, () => p.page.click('dialog[open] button[data-action="ok"]'));
      await p.page.waitForSelector('#drawer:not([hidden]) .panel [data-sec="card"]');
      await p.page.waitForTimeout(400);
      await noOverflow(p.page, AREA + ' loose card phone');
      await t.areaShot(p.page, AREA, 'personas-loose-card-phone');
      await p.page.keyboard.press('Escape');
      await p.page.waitForSelector('#drawer', { state: 'hidden' });
      t.allowHttp(true);
      try {
        await hook('openwebui', { state: 'nokey' });
        await page.click('#personas-root [data-action="import"]');
        await page.click('dialog[open] input[data-from="live"]');
        await page.waitForFunction(() => /OPENWEBUI_API_KEY is not set/.test(document.querySelector('dialog[open] [data-pane="live"]').textContent));
        assert(await page.locator('dialog[open] [data-pane="live"] a[href="#/setup/api-keys"]').count() === 1, 'a link to the key');
        await page.fill('dialog[open] #pe-imp-live-id', 'kate');
        const nokey = await waitPost(page, /\/api\/personas\/import$/, () => page.click('dialog[open] button[data-action="ok"]'));
        assert(nokey.status() === 400, 'no key: ' + nokey.status());
        await page.waitForFunction(() => /OPENWEBUI_API_KEY is not set/.test(document.querySelector('dialog[open] .form-error').textContent));
        await t.areaShot(page, AREA, 'personas-import-nokey-desktop');
        await hook('openwebui', { state: 'readkey' });
        const read = await waitPost(page, /\/api\/personas\/import$/, () => page.click('dialog[open] button[data-action="ok"]'));
        assert(read.status() === 502, 'read key: ' + read.status());
        await page.waitForSelector('dialog[open] .form-error button:has-text("Use the Export file instead")');
        await page.click('dialog[open] .form-error button');
        assert(await page.isChecked('dialog[open] input[data-from="file"]'), 'switched to the Export file');
        await page.click('dialog[open] button[data-action="cancel"]');
      } finally { await hook('openwebui', { state: 'ok' }); t.allowHttp(false); }
    });

    await step(AREA + ': Import a persona that wakes herself: the key for this call only, a sensitive or taken name refused, then her page', async () => {
      await go(page, '#/personas');
      await page.click('#personas-root [data-action="import"]');
      await page.click('dialog[open] input[data-what="wake"]');
      await page.click('dialog[open] input[data-from="live"]');
      await page.waitForSelector('dialog[open] #pe-imp-wake-key:visible');
      await page.selectOption('dialog[open] #pe-imp-live-model', 'kate');
      await page.fill('dialog[open] #pe-imp-wake-name', 'healthbot');
      await page.fill('dialog[open] #pe-imp-wake-key', 'sk-owner-key');
      await t.areaShot(page, AREA, 'personas-import-wake-desktop');
      t.allowHttp(true);
      try {
        const bad = await waitPost(page, /\/api\/personas\/import$/, () => page.click('dialog[open] button[data-action="ok"]'));
        assert(bad.status() === 400 && /sensitive words/.test((await bad.json()).error), 'a sensitive word');
        assert(await page.inputValue('dialog[open] #pe-imp-wake-key') === '', 'the key is cleared after the call');
        await page.fill('dialog[open] #pe-imp-wake-name', 'mira');
        await page.fill('dialog[open] #pe-imp-wake-key', 'sk-owner-key');
        const taken = await waitPost(page, /\/api\/personas\/import$/, () => page.click('dialog[open] button[data-action="ok"]'));
        assert(taken.status() === 409, 'she exists: ' + taken.status());
        await page.waitForFunction(() => /re-import her soul from her page/.test(document.querySelector('dialog[open] .form-error').textContent));
        assert(await page.getAttribute('dialog[open] .form-error a', 'href') === '#/schedule/persona/mira', 'a link to her page, where her re-import is');
        assert(await page.evaluate(() => document.activeElement && document.activeElement.id) === 'pe-imp-wake-name', 'the name is focused, not the page');
        await t.areaShot(page, AREA, 'personas-import-wake-taken-desktop');
      } finally { t.allowHttp(false); }
      await page.fill('dialog[open] #pe-imp-wake-name', 'wren');
      await page.fill('dialog[open] #pe-imp-wake-display', 'Wren');
      await page.fill('dialog[open] #pe-imp-wake-key', 'sk-owner-key');
      const ok = await waitPost(page, /\/api\/personas\/import$/, () => page.click('dialog[open] button[data-action="ok"]'));
      const body = JSON.parse(ok.request().postData());
      assert(ok.status() === 201 && t.canon(body) === t.canon({ name: 'wren', model_id: 'kate', source: 'openwebui', key: 'sk-owner-key', display: 'Wren' }), 'wake body ' + ok.request().postData());
      await page.waitForFunction(() => location.hash === '#/schedule/persona/wren');
      assert(!(await page.evaluate(() => location.href)).includes('sk-'), 'the key is never in the URL');
    });

    let chatId = null;
    await step(AREA + ': a persona chat: started from her card, its head (label, version, base model, what does not come through the API), a dismissible warning, no hand-off; a turn shows the Open WebUI window, and a newer version is marked', async () => {
      await go(page, '#/personas/kate');
      await page.waitForSelector('#personas-root [data-action="chat"]');
      const r = await waitPost(page, /^\/api\/chats$/, () => page.click('#personas-root [data-action="chat"]'));
      assert(r.status() === 201 && t.canon(JSON.parse(r.request().postData())) === t.canon({ persona: 'kate' }), 'POST /api/chats {persona}');
      chatId = (await r.json()).chat.id;
      await page.waitForFunction((id) => location.hash === '#/chat/' + id, chatId);
      await page.waitForSelector('.chat-head .pe-chat-label');
      const head = await page.textContent('.chat-head');
      assert(/Kate through Open WebUI/.test(head) && /version/.test(head) && /base model qwen3\.8-flash, connection external/.test(head), 'head: ' + head);
      assert(/not available here: .*tools/.test(await page.textContent('.chat-head .pe-chat-not')), 'what does not come through the API');
      assert(/no repository/.test(head) && await page.locator('.chat-head button:has-text("Hand off")').count() === 0, 'no repository, no hand-off');
      assert(await page.locator('.chat-head .pe-chat-warning').count() === 1, 'the warning banner');
      await page.focus('.chat-head .pe-chat-warning button');
      await page.keyboard.press('Enter');
      await page.waitForSelector('.chat-head .pe-chat-warning', { state: 'detached' });
      await page.waitForTimeout(200);   // a re-render or two
      assert(await page.evaluate(() => document.activeElement && document.activeElement.classList.contains('chat-title')), 'dismissed with the keyboard: focus stays in the chat (its title), not on the page');
      await page.reload();
      await page.waitForSelector('.chat-head .pe-chat-label');
      assert(await page.locator('.chat-head .pe-chat-warning').count() === 0, 'dismissed for this chat, after a reload too');
      await page.waitForSelector('.composer textarea:not([disabled])');
      assert(/Message kate/.test(await page.getAttribute('.composer textarea', 'placeholder')), 'the composer names her');
      await page.fill('.composer textarea', 'When is the <b>backup</b>?');
      await waitPost(page, /\/messages$/, () => page.press('.composer textarea', 'Enter'));
      await page.waitForSelector('.turn .pe-reply-window', { timeout: 20000 });
      assert(/^Asked kate \d\d:\d\d:\d\d\.\d{3} to \d\d:\d\d:\d\d\.\d{3} UTC/.test(await page.textContent('.turn .pe-reply-window')), 'the window: ' + await page.textContent('.turn .pe-reply-window'));
      await page.waitForSelector('.composer textarea:not([disabled])', { timeout: 20000 });
      await hook('bump', { model: 'kate', version: 'v-555eee' });
      await page.fill('.composer textarea', 'And now?');
      await waitPost(page, /\/messages$/, () => page.press('.composer textarea', 'Enter'));
      await page.waitForSelector('.turn .pe-reply-version', { timeout: 20000 });
      assert(/Answered by a newer Kate \(version v-333ccc → v-555eee\)/.test(await page.textContent('.turn .pe-reply-version')), 'the newer version: ' + await page.textContent('.turn .pe-reply-version'));
      await page.waitForSelector('.composer textarea:not([disabled])', { timeout: 20000 });
      await page.reload();
      await page.waitForSelector('.chat-thread .msg-assistant .pe-reply-version');
      assert(await page.locator('.chat-thread .msg-assistant .pe-reply-window').count() === 2, 'after a reload the history shows each window');
      assert(await page.locator('.chat-thread img, .chat-thread b').count() === 0, 'raw text stays text');
      await t.areaShot(page, AREA, 'personas-chat-desktop');
      await go(p.page, '#/chat/c-fa17e0c001');
      await p.page.waitForSelector('.chat-head .pe-chat-label');
      await p.page.waitForSelector('.chat-thread .pe-reply-version');
      await noOverflow(p.page, AREA + ' chat phone');
      await t.areaShot(p.page, AREA, 'personas-chat-phone');
    });

    await step(AREA + ': an offline copy’s chat labels every reply (history and live) and says when the live preset moved on', async () => {
      await go(page, '#/chat/c-fa17e05001');
      await page.waitForSelector('.chat-thread .msg-assistant .pe-reply-label');
      assert(/^from the imported copy: Kate, imported snapshot of 2026-10-20, version v-111aaa$/.test(await page.textContent('.chat-thread .msg-assistant .pe-reply-label')), 'the label under the reply');
      assert(await page.locator('.chat-head .tag:has-text("offline copy")').count() === 1, 'the offline copy badge');
      assert(/live preset has changed since this copy/.test(await page.textContent('.chat-head .pe-chat-stale')), 'the stale banner');
      await page.waitForSelector('.composer textarea:not([disabled])');
      await page.fill('.composer textarea', 'One more line, please.');
      await waitPost(page, /\/messages$/, () => page.press('.composer textarea', 'Enter'));
      await page.waitForSelector('.turn .pe-reply-label', { timeout: 20000 });
      assert(/from the imported copy/.test(await page.textContent('.turn .pe-reply-label')), 'a live reply is labelled too');
      await page.waitForSelector('.composer textarea:not([disabled])', { timeout: 20000 });
      await t.areaShot(page, AREA, 'personas-snapshot-chat-desktop');
      await go(p.page, '#/chat/c-fa17e05001');
      await p.page.waitForSelector('.chat-thread .msg-assistant .pe-reply-label');
      await noOverflow(p.page, AREA + ' snapshot chat phone');
      await t.areaShot(p.page, AREA, 'personas-snapshot-chat-phone');
    });

    await step(AREA + ': after a refused turn each later reply keeps its own version mark and Open WebUI window (persona.turns follows the transcript, not the log); the refused run did not run and its error sits under the last message', async () => {
      assert(chatId, 'the persona chat of the step above');
      const windows = () => page.locator('.chat-thread .pe-reply-window').count();
      await go(page, '#/chat/' + chatId);
      await page.waitForSelector('.chat-thread .msg-assistant .pe-reply-window');
      await page.waitForSelector('.composer textarea:not([disabled])');
      assert(await windows() === 2, 'two replies so far');
      await hook('away', { persona: 'kate' });
      try {
        await page.fill('.composer textarea', 'Third?');
        await waitPost(page, /\/messages$/, () => page.press('.composer textarea', 'Enter'));
        await page.waitForSelector('.chat-outro .callout-err', { timeout: 20000 });
      } finally { await hook('away', { persona: 'kate', away: false }); }
      assert(/which is not on this machine/.test(await page.textContent('.chat-outro .callout-err')), 'the refusal as the server wrote it');
      const box = await page.evaluate(() => {
        const e = document.querySelector('.chat-outro .callout-err').getBoundingClientRect();
        const sc = document.querySelector('#chat-main .chat-scroll').getBoundingClientRect();
        const last = Array.from(document.querySelectorAll('.chat-thread .msg, .chat-thread .turn')).pop().getBoundingClientRect();
        return { inView: e.top >= sc.top - 1 && e.bottom <= sc.bottom + 1, after: e.top >= last.bottom - 1 };
      });
      assert(box.inView && box.after, 'the error is under the last message, in view: ' + JSON.stringify(box));
      assert(await page.locator('.chat-thread .turn .pe-reply-window, .chat-thread .turn .pe-reply-version').count() === 0, 'nothing is said to have answered the refused turn');
      await t.areaShot(page, AREA, 'personas-refused-turn-desktop');
      await hook('bump', { model: 'kate', version: 'v-666fff' });
      let want = 2;
      for (const text of ['Fourth?', 'Fifth?']) {
        await page.waitForSelector('.composer textarea:not([disabled])');
        await page.fill('.composer textarea', text);
        await waitPost(page, /\/messages$/, () => page.press('.composer textarea', 'Enter'));
        want += 1;
        await page.waitForFunction((n) => document.querySelectorAll('.chat-thread .pe-reply-window').length === n, want, { timeout: 20000 });
      }
      await page.waitForSelector('.composer textarea:not([disabled])', { timeout: 20000 });
      await page.reload();
      await page.waitForSelector('.chat-thread .msg-assistant .pe-reply-window');
      await page.waitForTimeout(500);   // the event replay after a load
      const data = await (await page.request.get(M + '/api/chats/' + chatId)).json();
      const replies = data.messages.filter((m) => m.role === 'assistant');
      const entries = data.chat.persona.turns;
      assert(replies.map((m) => m.turn).join() === '1,2,3,4' && entries.map((e) => e.turn).join() === '1,2,3,4' && data.chat.turns === 5,
        'the transcript numbers replies 1-4; the refused turn has no record; chat.turns counts all 5: ' + JSON.stringify([replies.map((m) => m.turn), entries.map((e) => e.turn), data.chat.turns]));
      assert(await page.locator('.chat-thread .turn').count() === 0, 'after a reload no replayed run is shown again below the history');
      const shown = await page.$$eval('.chat-thread .msg-assistant', (els) => els.map((e) => [(e.querySelector('.pe-reply-window') || {}).textContent || '', (e.querySelector('.pe-reply-version') || {}).textContent || '']));
      assert(shown.length === 4, 'four replies: ' + shown.length);
      shown.forEach(([w], i) => assert(w.includes(entries[i].window.started_at.slice(11, 23)), 'reply ' + (i + 1) + ' shows its own Open WebUI window: ' + w + ' / ' + entries[i].window.started_at));
      assert(shown.map(([, v]) => (v ? 'mark' : '-')).join() === '-,mark,mark,-', 'the newer-version marks sit under replies 2 and 3: ' + shown.map(([, v]) => v).join(' | '));
      assert(/version v-555eee → v-666fff/.test(shown[2][1]), 'reply 3 was answered by v-666fff: ' + shown[2][1]);
      await t.areaShot(page, AREA, 'personas-turns-after-refusal-desktop');
      await go(p.page, '#/chat/' + chatId);
      await p.page.waitForSelector('.chat-thread .msg-assistant .pe-reply-window');
      await noOverflow(p.page, AREA + ' turns after refusal phone');
      await p.page.evaluate(() => { const s = document.querySelector('#chat-main .chat-scroll'); if (s) s.scrollTop = s.scrollHeight; });
      await t.areaShot(p.page, AREA, 'personas-turns-after-refusal-phone');
      // An offline copy refused before its model call (its chats switched off): the run its stale-copy note opened
      // did not run, and is not labelled as a reply from the copy.
      const made = await page.request.post(M + '/api/chats', { data: { persona: 'kate-offline' } });
      const copyId = (await made.json()).chat.id;
      await page.request.post(M + '/api/config', { data: { agents: { 'kate-offline': { offline: false } } } });
      try {
        await go(page, '#/chat/' + copyId);
        await page.waitForSelector('.composer textarea:not([disabled])');
        await page.fill('.composer textarea', 'Hello?');
        await waitPost(page, /\/messages$/, () => page.press('.composer textarea', 'Enter'));
        await page.waitForSelector('.chat-head .pe-chat-now', { timeout: 20000 });
        await page.waitForSelector('.chat-thread .turn .turn-foot:not([hidden])');
        const foot = await page.textContent('.chat-thread .turn .turn-foot');
        assert(/did not run/.test(foot) && !/stopped/.test(foot), 'the refused run did not run: ' + foot);
        assert(await page.locator('.chat-thread .turn .pe-reply-label').count() === 0, 'no "from the imported copy" on it');
        await t.areaShot(page, AREA, 'personas-copy-refused-desktop');
      } finally { await page.request.post(M + '/api/config', { data: { agents: { 'kate-offline': { offline: true } } } }); }
    });

    await step(AREA + ': Import again after a change: it reads her own provider, and the drift banner then says what is left (look, re-import a copy, mark the item Proven); with two Open WebUI providers the dialog asks which', async () => {
      await go(page, '#/personas/kate');
      await page.waitForSelector('#personas-root .pe-drift-banner[data-imported="0"]');
      assert(/Import her profile card again/.test(await page.textContent('#personas-root .pe-drift-banner')), 'before: import her card again');
      await page.click('#personas-root [data-action="import"]');
      await page.click('dialog[open] input[data-from="live"]');
      await page.waitForSelector('dialog[open] #pe-imp-live-model option[value="kate"]', attached);
      assert(await page.isHidden('dialog[open] [data-slot="provider"]'), 'one Open WebUI provider: no choice to make');
      const r = await waitPost(page, /\/api\/personas\/import$/, () => page.click('dialog[open] button[data-action="ok"]'));
      assert(r.status() === 201 && t.canon(JSON.parse(r.request().postData())) === t.canon({ source: 'openwebui', model_id: 'kate', provider: 'openwebui' }), 'Import again names her provider: ' + r.request().postData());
      await page.waitForSelector('#personas-root .pe-drift-banner[data-imported="1"]');
      const banner = await page.textContent('#personas-root .pe-drift-banner');
      assert(/You imported her card after this change\. Look at what changed below, re-import any offline copy of her, then mark system:S-3 Proven on the Housekeeping Tracker\./.test(banner), 'after: ' + banner);
      assert(!/Import her profile card again/.test(banner), 'no longer asks to import again');
      assert(await page.locator('#personas-root .pe-drift-banner button[data-action="check"]').count() === 1, 'with Check now beside it');
      assert(/the same as Open WebUI lists now/.test(await page.textContent('#personas-root [data-sec="card"]')), 'the card is of the live version');
      await page.evaluate(() => { const m = document.getElementById('main'); if (m) m.scrollTop = 0; });
      await t.areaShot(page, AREA, 'personas-drift-imported-desktop');
      await go(p.page, '#/personas/kate');
      await p.page.waitForSelector('#personas-root .pe-drift-banner[data-imported="1"]');
      await noOverflow(p.page, AREA + ' drift imported phone');
      await t.areaShot(p.page, AREA, 'personas-drift-imported-phone');
      // Two providers marked kind openwebui (the preset's and a second one): the dialog asks which to read.
      const add = await page.request.post(M + '/api/config', { data: { providers: {
        openwebui: { kind: 'openwebui', type: 'openai', base_url: 'http://127.0.0.1:8083/api', api_key_env: 'OPENWEBUI_API_KEY' },
        'owui-lab': { kind: 'openwebui', type: 'openai', base_url: 'http://127.0.0.1:8084/api', api_key_env: 'OPENWEBUI_LAB_KEY' } } } });
      assert(add.status() === 200, 'two Open WebUI providers: ' + add.status());
      try {
        await go(page, '#/personas/kate');
        await page.waitForSelector('#personas-root [data-action="import"]');
        await page.click('#personas-root [data-action="import"]');
        await page.click('dialog[open] input[data-from="live"]');
        await page.waitForSelector('dialog[open] [data-slot="provider"]:not([hidden])');
        assert(await page.inputValue('dialog[open] #pe-imp-provider') === 'openwebui', 'her own provider is picked first');
        const listed = page.waitForResponse((x) => new URL(x.url()).pathname === '/api/openwebui/models' && new URL(x.url()).searchParams.get('provider') === 'owui-lab');
        await page.selectOption('dialog[open] #pe-imp-provider', 'owui-lab');
        await listed;
        await page.waitForFunction(() => /provider owui-lab/.test(document.querySelector('dialog[open] [data-pane="live"]').textContent));
        await page.waitForSelector('dialog[open] #pe-imp-live-model option[value="kate"]', attached);
        await t.areaShot(page, AREA, 'personas-import-provider-desktop');
        const r2 = await waitPost(page, /\/api\/personas\/import$/, () => page.click('dialog[open] button[data-action="ok"]'));
        assert(JSON.parse(r2.request().postData()).provider === 'owui-lab', 'the body names the chosen provider: ' + r2.request().postData());
        await go(p.page, '#/personas/kate');
        await p.page.waitForSelector('#personas-root [data-action="import"]');
        await p.page.click('#personas-root [data-action="import"]');
        await p.page.click('dialog[open] input[data-from="live"]');
        await p.page.waitForSelector('dialog[open] [data-slot="provider"]:not([hidden])');
        const pw = await p.page.evaluate(() => { const b = document.querySelector('dialog[open] .modal-body'); return [b.scrollWidth, b.clientWidth]; });
        assert(pw[0] <= pw[1] + 1, 'the provider choice fits the phone: ' + pw);
        await t.areaShot(p.page, AREA, 'personas-import-provider-phone');
        await p.page.click('dialog[open] button[data-action="cancel"]');
      } finally {
        await page.request.post(M + '/api/config', { data: { providers: { openwebui: null, 'owui-lab': null } } });
      }
    });

    await step(AREA + ': a turn refused because the agent became a persona: the error, and "Start a chat with" for an Open WebUI persona; her page for one that wakes herself', async () => {
      await go(page, '#/chat/c-fa17e0d001');
      await page.waitForSelector('.chat-head .pe-chat-now');
      await page.waitForSelector('.chat-head .pe-chat-now a[href="#/schedule/persona/mira"]', { timeout: 5000 }).catch(() => {});
      assert(await page.getAttribute('.chat-head .pe-chat-now a', 'href') === '#/schedule/persona/mira', 'her page');
      // A refusal for good ends the chat: no "Start the conversation", no "try again", and the composer is closed.
      await page.waitForSelector('#chat-main .callout-err');
      const ended = await page.evaluate(() => ({ main: document.getElementById('chat-main').textContent, closed: document.querySelector('.composer textarea').disabled }));
      assert(/This chat cannot go on/.test(ended.main) && !/Start the conversation|Send another message to try again/.test(ended.main) && ended.closed, 'an ended chat says so and takes no message: ' + JSON.stringify(ended).slice(0, 300));
      await go(page, '#/chat/c-fa17e0b001');
      await page.waitForSelector('.chat-head .pe-chat-now button');
      assert(/is now a persona: its chats run only as persona chats/.test(await page.textContent('#chat-main')), 'the turn error, as the server wrote it');
      await t.areaShot(page, AREA, 'personas-refused-chat-desktop');
      await go(p.page, '#/chat/c-fa17e0b001');
      await p.page.waitForSelector('.chat-head .pe-chat-now button');
      await noOverflow(p.page, AREA + ' refused chat phone');
      await t.areaShot(p.page, AREA, 'personas-refused-chat-phone');
      const r = await waitPost(page, /^\/api\/chats$/, () => page.click('.chat-head .pe-chat-now button'));
      assert(r.status() === 201 && t.canon(JSON.parse(r.request().postData())) === t.canon({ persona: 'kate-browser' }), 'Start a chat with kate-browser');
      await page.waitForFunction((id) => location.hash === '#/chat/' + id, (await r.json()).chat.id);
    });

    await step(AREA + ': New chat has three starts: on a repository, no repository (POST {mode: "none"}) and a persona; a refusal is shown as the server wrote it', async () => {
      await go(page, '#/chat');
      await page.click('#new-chat-btn');
      await page.waitForSelector('dialog[open] .nc-starts');
      const starts = await page.$$eval('dialog[open] .nc-starts input', (els) => els.map((e) => e.value));
      assert(t.canon(starts) === t.canon(['on-repo', 'no-repo', 'persona']), 'starts: ' + starts);
      await page.click('dialog[open] input[data-start="persona"]');
      await page.waitForSelector('dialog[open] #nc-persona option[value="kate"]', attached);
      const listed = await page.$$eval('dialog[open] #nc-persona option', (os) => os.map((o) => o.value));
      assert(!listed.includes('sage') && !listed.includes('mira') && !listed.includes('seraphina'), 'only startable personas: ' + listed);
      assert(listed.includes('nova-offline'), 'the offline import above turned its copy’s chats on (agents.nova-offline.offline): ' + listed);
      await t.areaShot(page, AREA, 'personas-newchat-desktop');
      t.allowHttp(true);
      try {
        await page.selectOption('dialog[open] #nc-persona', 'scribe');
        const bad = await waitPost(page, /^\/api\/chats$/, () => page.click('dialog[open] button[data-action="ok"]'));
        assert(bad.status() === 400, 'scribe has no models entry: ' + bad.status());
        await page.waitForFunction(() => /models\.scribe, which is not configured/.test(document.querySelector('dialog[open] .form-error').textContent));
        assert(await page.locator('dialog[open] .form-error a[href="#/settings/routing"]').count() === 1, 'with where to fix it (the openwebui preset)');
        assert(/Not startable: sage \(This persona file has a problem: timezone/.test(await page.textContent('dialog[open] [data-pane="persona"]')), 'an errored persona is named with its error');
      } finally { t.allowHttp(false); }
      await page.click('dialog[open] input[data-start="no-repo"]');
      const free = await page.$$eval('dialog[open] #nc-free-agent option', (os) => os.map((o) => o.value));
      assert(free.includes('coder') && !free.some((n) => ['kate', 'kate-offline', 'kate-browser', 'nova-offline', 'sage', 'scribe', 'mira', 'seraphina'].includes(n)), 'no persona file among the plain agents: ' + free);
      assert(/Not listed here: .*kate.*\(see “With a persona”\)/.test(await page.textContent('dialog[open] [data-pane="no-repo"] .nc-claimed')), 'and the start says where they are');
      await t.areaShot(page, AREA, 'personas-newchat-norepo-desktop');
      await page.selectOption('dialog[open] #nc-free-agent', 'coder');
      const r = await waitPost(page, /^\/api\/chats$/, () => page.click('dialog[open] button[data-action="ok"]'));
      assert(r.status() === 201 && t.canon(JSON.parse(r.request().postData())) === t.canon({ mode: 'none', agent: 'coder' }), 'no-repo body ' + r.request().postData());
      await page.waitForSelector('.chat-head .meta-row');
      assert(/no repository/.test(await page.textContent('.chat-head')) && await page.locator('.chat-head button:has-text("Hand off")').count() === 0, 'no repository, no hand-off');
      assert(/scratch folder/.test(await page.textContent('#chat-main')), 'the empty chat says where it works');
      await go(p.page, '#/chat');
      await p.page.click('#new-chat-btn');
      await p.page.waitForSelector('dialog[open] .nc-starts');
      await p.page.click('dialog[open] input[data-start="persona"]');
      await p.page.waitForSelector('dialog[open] #nc-persona option[value="kate"]', attached);
      const dw = await p.page.evaluate(() => { const b = document.querySelector('dialog[open] .modal-body'); return [b.scrollWidth, b.clientWidth]; });
      assert(dw[0] <= dw[1] + 1, 'New chat fits the phone: ' + dw);
      await t.areaShot(p.page, AREA, 'personas-newchat-phone');
      await p.page.click('dialog[open] button[data-action="cancel"]');
    });

    await step(AREA + ': an Export file over 1 MB in bytes (a prompt in Japanese: 400k characters, 1.2 MB) is caught before sending; a 413 gets the same advice', async () => {
      await go(page, '#/personas');
      await page.waitForSelector(persona('kate'));
      const sent = [];
      const onReq = (r) => { if (r.method() === 'POST' && new URL(r.url()).pathname === '/api/personas/import') sent.push(r); };
      page.on('request', onReq);
      try {
        await page.click('#personas-root [data-action="import"]');
        await page.waitForSelector('dialog[open] #pe-imp-file');
        const big = KATE_V('v-777ggg', { params: { system: 'あ'.repeat(400000), temperature: 0.4 } });
        await page.setInputFiles('dialog[open] #pe-imp-file', { name: 'kate-ja.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify([big])) });
        await page.waitForFunction(() => /1 model in kate-ja\.json/.test(document.querySelector('dialog[open] [data-pane="file"]').textContent));
        await page.click('dialog[open] button[data-action="ok"]');
        await page.waitForFunction(() => /larger than 1 MB even without its pictures: export only this one model/.test((document.querySelector('dialog[open] .form-error') || {}).textContent || ''));
        assert(sent.length === 0, 'nothing was sent: ' + sent.length);
        await t.areaShot(page, AREA, 'personas-import-toobig-desktop');
        t.allowHttp(true);
        await page.route(M + '/api/personas/import', (r) => r.fulfill({ status: 413, contentType: 'application/json', body: JSON.stringify({ error: 'request body too large (limit 1 MB)' }) }));
        try {
          await page.setInputFiles('dialog[open] #pe-imp-file', { name: 'kate.json', mimeType: 'application/json', buffer: Buffer.from(JSON.stringify([KATE_V('v-777ggg')])) });
          await page.waitForFunction(() => /1 model in kate\.json/.test(document.querySelector('dialog[open] [data-pane="file"]').textContent));
          await page.click('dialog[open] button[data-action="ok"]');
          await page.waitForFunction(() => /Nothing was imported\. The file is larger than 1 MB.*request body too large \(limit 1 MB\)/.test((document.querySelector('dialog[open] .form-error') || {}).textContent || ''));
        } finally { await page.unroute(M + '/api/personas/import'); t.allowHttp(false); }
        await page.click('dialog[open] button[data-action="cancel"]');
      } finally { page.off('request', onReq); }
    });

    await step(AREA + ': when the personas cannot be read (a persona file that does not parse), New chat’s persona start says so and is off, not "Loading…" for good', async () => {
      const bad = { error: "agents/kate.md: line 4: timezone: 'Mars/Olympus' is not a time zone" };
      t.allowHttp(true);
      try {
        for (const phone of [false, true]) {
          const e = await t.newPage(AREA + (phone ? '-err-phone' : '-err'), { phone, mockUrl: M });
          try {
            await e.page.route(M + '/api/personas', (r) => r.fulfill({ status: 400, contentType: 'application/json', body: JSON.stringify(bad) }));
            await e.page.goto(M + '/#/chat');
            await e.page.click('#new-chat-btn');
            await e.page.waitForSelector('dialog[open] .nc-starts');
            await e.page.waitForFunction(() => { const x = document.querySelector('dialog[open] input[data-start="persona"]'); return x && x.disabled; }, null, { timeout: 10000 });
            assert(/Your personas could not be loaded/.test(await e.page.textContent('dialog[open] .nc-starts')), 'the start says why');
            assert(/Mars\/Olympus/.test(await e.page.textContent('dialog[open] [data-pane="persona"]')), 'with the server’s words in its pane');
            await t.areaShot(e.page, AREA, 'personas-newchat-unreadable-' + (phone ? 'phone' : 'desktop'));
          } finally { await e.ctx.close(); }
        }
      } finally { t.allowHttp(false); }
    });

    await step(AREA + ': a chat that cannot start says why, with where to fix it (Open WebUI down, a missing models entry)', async () => {
      t.allowHttp(true);
      try {
        await hook('openwebui', { state: 'down' });
        await go(page, '#/personas');
        await page.waitForSelector(persona('kate') + ' [data-action="chat"]');
        await waitPost(page, /^\/api\/chats$/, () => page.click(persona('kate') + ' [data-action="chat"]'));
        await page.waitForSelector('dialog[open] .pe-modal-err');
        assert(/persona kate: cannot reach/.test(await page.textContent('dialog[open] .pe-modal-err')), 'the refusal');
        assert(await page.locator('dialog[open] a[href="#/doctor"]').count() === 1, 'a link to the Doctor');
        assert(await page.locator('dialog[open] a[href="#/settings/providers"]').count() === 1 && await page.locator('dialog[open] a[href="#/setup/api-keys"]').count() === 0, 'Open WebUI down: the provider settings, not the key');
        await page.waitForTimeout(300);   // the dialog's fade-in
        await t.areaShot(page, AREA, 'personas-start-error-desktop');
        await page.click('dialog[open] button[data-action="cancel"]');
        await go(p.page, '#/personas');
        await p.page.waitForSelector(persona('kate') + ' [data-action="chat"]');
        await waitPost(p.page, /^\/api\/chats$/, () => p.page.click(persona('kate') + ' [data-action="chat"]'));
        await p.page.waitForSelector('dialog[open] .pe-modal-err');
        await p.page.waitForTimeout(300);
        await t.areaShot(p.page, AREA, 'personas-start-error-phone');
        await p.page.click('dialog[open] button[data-action="cancel"]');
      } finally { await hook('openwebui', { state: 'ok' }); t.allowHttp(false); }
      assert(await page.evaluate(() => location.hash) === '#/personas', 'stays on the list');
    });

    await step(AREA + ': a wake persona’s soul drift (persona.drift with changes) reloads the list; Home’s card says a persona changed in Open WebUI', async () => {
      await go(page, '#/personas');
      await page.waitForSelector(persona('mira'));
      const reload = page.waitForEvent('response', (r) => /\/api\/personas$/.test(new URL(r.url()).pathname), { timeout: 10000 });
      await hook('wake-drift', { persona: 'mira', changes: 2 });
      await reload;
      await go(page, '#/home');
      await page.waitForFunction(() => /changed in Open WebUI/.test((document.querySelector('.home-card[data-section="personas"] .home-status-text') || {}).textContent || ''));
    });
    await step(AREA + ': with no repository the Chat page offers New chat first (a persona or an agent needs none) and Add a repository second', async () => {
      const r = await page.request.post(M + '/api/config', { data: { repos: [] } });
      assert(r.status() === 200, 'repos: [] ' + r.status());
      await go(page, '#/chat');
      await page.waitForSelector('#chat-main .empty');
      // repos.updated refreshes the state, and the empty state is drawn again: wait for it rather than read the old one
      await page.waitForFunction(() => /neither needs a repository/.test((document.querySelector('#chat-main .empty') || {}).textContent || ''), null, { timeout: 10000 });
      assert(await page.textContent('#chat-main .empty-title') === 'Talk to an agent or a persona', 'its title');
      const text = await page.textContent('#chat-main .empty');
      assert(/Talk to an agent or a persona/.test(text) && /neither needs a repository/.test(text) && !/Chats run an agent on a checkout/.test(text), 'what works without a repository: ' + text);
      const acts = await page.$$eval('#chat-main .empty .btn', (els) => els.map((e) => [e.textContent.trim(), e.classList.contains('btn-primary'), e.getAttribute('href')]));
      assert(acts.length === 2 && acts[0][0] === 'New chat' && acts[0][1] && acts[1][0] === 'Add a repository' && acts[1][2] === '#/setup/repo', 'New chat first, Add a repository second: ' + JSON.stringify(acts));
      await t.areaShot(page, AREA, 'personas-chat-norepos-desktop');
      await page.click('#chat-main .empty .btn-primary');
      await page.waitForSelector('dialog[open] .nc-starts');
      assert(await page.isDisabled('dialog[open] input[data-start="on-repo"]') && await page.isChecked('dialog[open] input[data-start="no-repo"]'), 'New chat starts without a repository');
      await page.click('dialog[open] button[data-action="cancel"]');
      await go(p.page, '#/chat');
      await p.page.waitForSelector('#chat-list .chat-item');
      await noOverflow(p.page, AREA + ' chat no repos phone');
      await t.areaShot(p.page, AREA, 'personas-chat-norepos-phone');
    });
  } finally {
    await d.ctx.close();
    await p.ctx.close();
    mock.proc.kill('SIGTERM');
  }
}
