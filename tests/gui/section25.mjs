// DESIGN.md §25 checks for tests/gui/smoke.mjs: Welcome on a fresh install, the guided setup (option cards,
// Test on a temporary entry, keys in memory only, Apply in order), the flows as maps, the Grid Card, Start work
// for every kind of work, the procedures booklet, delivery modes, chat agents, toasts and agent.updated.
//
// run(t) gets the harness of smoke.mjs: { browser, url, startMock, step, assert, canon, waitPost, shot, cleanShot,
// noOverflow, watch, allowHttp }.

const FRESH_KEY = 'sk-ant-smoke-secret-0001';

/** Every POST the page sends while fn runs: [{path, body}] in order. */
async function postsDuring(page, fn) {
  const seen = [];
  const on = (r) => {
    if (r.method() !== 'POST') return;
    let body = null;
    try { body = JSON.parse(r.postData() || 'null'); } catch (e) { body = r.postData(); }
    seen.push({ path: new URL(r.url()).pathname, body });
  };
  page.on('request', on);
  try { await fn(); } finally { page.off('request', on); }
  return seen;
}
const atStep = (page, id) => page.waitForSelector('.wz-main[data-step="' + id + '"]');
/** The fresh mock's POSTs to the look for model servers so far. */
const looks = async (page) => JSON.parse(await page.evaluate(async () => (await fetch('/__mock/requests')).text())).requests.filter((r) => r.path === '/api/setup/look-around').length;
const mockLook = (page, body) => page.evaluate(async (b) => (await fetch('/__mock/look-around', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(b) })).json(), body);
const DEMO_FINDS = [{ url: 'http://127.0.0.1:5000/v1', port: 5000, status: 200, ids: ['qwen3.8-flash-next'] }, { url: 'http://127.0.0.1:5005/v1', port: 5005, status: 401, ids: [] }];
async function next(page, id) {
  await page.click('.wz-foot [data-action="wz-next"]');
  if (id) await atStep(page, id);
}
const pick = (page, opt) => page.click('.wz-main .opt-card[data-opt="' + opt + '"]');
const checked = (page, opt) => page.getAttribute('.wz-main .opt-card[data-opt="' + opt + '"]', 'aria-checked');
const storage = (page) => page.evaluate(() => { try { return JSON.stringify(Object.assign({}, window.localStorage)); } catch (e) { return ''; } });
const labels = (page, scope) => page.$$eval(scope + ' .map-label', (els) => els.map((e) => e.textContent).join(' '));

export async function run(t) {
  const { browser, url, step, assert, canon, waitPost, shot, cleanShot, noOverflow, watch, allowHttp } = t;

  /** The server-details step's "Look for model servers": nothing looked before the press (page loads, the Grid, the
   *  Doctor badge), one look per press with a busy state, the results and what was sent, the hints that use them, a
   *  second press too soon refused by the server, and an empty answer that says what to do. */
  async function lookAround(f) {
    const box = '.wz-group[data-group="look-around"]';
    const vllm = () => f.textContent('.wz-group[data-group="vllm"]');
    assert(await looks(f) === 0, 'nothing looked for model servers before the button was pressed');
    assert(/Nothing listens on 127\.0\.0\.1:8000 now\. Ports in use here: 5000, 5005, 18090\. Press “Look for model servers” above to see which of them is a model server\./.test(await vllm()), 'before a look the hint names the ports and the button: ' + await vllm());
    const intro = await f.textContent(box);
    assert(/Asks each program listening on this machine whether it is a model server; it sends no key and changes nothing\./.test(intro) && !(await f.isVisible(box + ' .wz-look-list')), 'one plain sentence says what pressing it does, and nothing is shown yet: ' + intro);
    assert(await f.$eval('.wz-body .wz-group', (el) => el.dataset.group) === 'look-around', 'the button comes before the servers');
    await f.click('[data-action="wz-look-around"]');
    await f.waitForSelector('[data-action="wz-look-around"][aria-busy="true"]', { timeout: 3000 });
    assert(/Asking each program listening here/.test(await f.textContent(box)), 'busy: it says what it is doing');
    await f.waitForSelector(box + ' .wz-look-list li[data-url="http://127.0.0.1:5005/v1"]');
    const found = await f.textContent(box);
    assert(/http:\/\/127\.0\.0\.1:5000\/v1 lists qwen3\.8-flash-next/.test(found) && /http:\/\/127\.0\.0\.1:5005\/v1 wants a key/.test(found), 'the model servers it found: ' + found);
    assert(/Sent one GET \/v1\/models, without a key, to each program listening on this machine/.test(found) && /Last look:\s*just now/.test(found), 'what it sent, and when: ' + found);
    assert(await looks(f) === 1, 'one press, one look');
    assert(await f.evaluate(() => document.activeElement && document.activeElement.dataset.action) === 'wz-look-around', 'the button keeps the focus');
    assert(/Nothing listens on 127\.0\.0\.1:8000 now\. Model servers answer here: http:\/\/127\.0\.0\.1:5000\/v1 \(lists qwen3\.8-flash-next\); http:\/\/127\.0\.0\.1:5005\/v1 \(wants a key\)\. If one of them is vLLM, use its address\./.test(await vllm()), 'the address hint offers what the look found (B.5.4)');
    await cleanShot(f, 'setup-look-around');
    allowHttp(true);   // a second press at once: the server refuses it (429), the answer stays
    try {
      await f.click('[data-action="wz-look-around"]');
      await f.waitForSelector(box + ' .test-result.warn');
    } finally { allowHttp(false); }
    const refused = await f.textContent(box);
    assert(/You looked just now\. You can look again in \d+ seconds\./.test(refused) && await f.locator(box + ' .wz-look-list li').count() === 2, 'refused too soon, and the last answer stays: ' + refused);
    // the refusal goes once its Retry-After has passed (a 3 s gap here), without a press and without a look
    await mockLook(f, { finds: DEMO_FINDS, gap: 3 });
    await f.click('[data-action="wz-look-around"]');
    await f.waitForSelector('[data-action="wz-look-around"][aria-busy="true"]', { timeout: 3000 });
    await f.waitForSelector('[data-action="wz-look-around"]:not([aria-busy="true"])');
    allowHttp(true);
    try {
      await f.click('[data-action="wz-look-around"]');
      await f.waitForSelector(box + ' .test-result.warn');
    } finally { allowHttp(false); }
    const before = await looks(f);
    await f.waitForSelector(box + ' .test-result.warn', { state: 'detached', timeout: 6000 });
    assert(await looks(f) === before && await f.locator(box + ' .wz-look-list li').count() === 2, 'the refusal went by itself, and nothing looked again');
    // a look that broke: only its error and a next step, not "nothing answered", not what was sent; a reload keeps it
    await mockLook(f, { error: "PermissionError: [Errno 13] Permission denied: '/proc/net/tcp'" });
    await f.click('[data-action="wz-look-around"]');
    await f.waitForSelector(box + ' .wz-look-broke');
    const broke = async () => [await f.textContent(box), await vllm()];
    let [said, hint] = await broke();
    assert(/The look did not work: PermissionError: \[Errno 13\] Permission denied: '\/proc\/net\/tcp'\. Look again in a moment, or type each server’s address below\./.test(said)
      && !/No program listening|Sent one GET/.test(said) && !(await f.isVisible(box + ' .wz-look-list')), 'a broken look says so, and only that: ' + said);
    assert(/The last look for model servers did not work: press “Look for model servers” above to try again\./.test(hint) && !/None of them answered/.test(hint), 'and the hint does not say nothing answered: ' + hint);
    await f.reload();
    await atStep(f, 'local-details');
    await f.waitForSelector(box + ' .wz-look-broke');
    [said, hint] = await broke();
    assert(/The look did not work: PermissionError/.test(said) && !/No program listening/.test(said) && /did not work/.test(hint), 'after a reload too: ' + said);
    await mockLook(f, { finds: [] });   // a fresh budget, and nothing answers this time
    await f.click('[data-action="wz-look-around"]');
    await f.waitForSelector(box + ' .wz-look-empty');
    const none = await f.textContent(box);
    assert(/No program listening on this machine answered like a model server\. Start your model server and wait until it has loaded its model, then look again\. If it runs on another machine, type its address below\./.test(none) && !(await f.isVisible(box + ' .test-result.warn')), 'an empty answer says what to do: ' + none);
    assert(/None of them answered like a model server when you looked\./.test(await vllm()), 'and the hint says so: ' + await vllm());
    await f.reload();
    await atStep(f, 'local-details');
    assert(/Last look:/.test(await f.textContent(box)) && await f.isVisible(box + ' .wz-look-empty'), 'GET /api/setup brings the last look back after a reload, with when it was taken');
    assert(await looks(f) === 6, 'a reload does not look again: ' + await looks(f));
    await mockLook(f, { finds: DEMO_FINDS, forget: true });
  }

  // ================================================================ a fresh install (its own mock)
  const fresh = await t.startMock(['--fresh']);
  const fctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: 'light', deviceScaleFactor: 1 });
  await fctx.addCookies([{ name: 'sf_token', value: 'test-token', url: fresh.url }]);
  const f = await fctx.newPage();
  watch(f, 'fresh');
  const furl = fresh.url;

  try {
    await step('fresh install: "/" opens Welcome with its first step; empty views route to the wizard', async () => {
      await f.goto(furl + '/');
      await f.waitForFunction(() => location.hash === '#/welcome', null, { timeout: 8000 });
      await f.waitForSelector('.first-step [data-action="first-step"]');
      assert(/Not set up yet/.test(await f.textContent('.setup-chips')), 'Welcome says the harness is not set up');
      const launch = await f.$$eval('.opt-launch .opt-card', (els) => els.map((e) => e.dataset.opt));
      assert(launch.length >= 4, 'Welcome offers what to do next: ' + launch);
      assert(!launch.includes('setup'), 'the first-step banner is the way into setup; no second "Set up" card: ' + launch);
      await f.waitForFunction(() => document.getElementById('doctor-badge').dataset.level !== 'unknown');
      assert(await f.getAttribute('#doctor-badge', 'data-level') !== 'error', 'a fresh install is a to-do list, not a red alarm');
      assert(await f.locator('.wz-proc-card ol.proc li').count() >= 3, 'Welcome explains the harness as a numbered procedure');
      const step1 = await f.$eval('.wz-proc-card ol.proc li', (li) => [li.querySelector('strong') && li.querySelector('strong').textContent, li.textContent]);
      assert(step1[0] && /\.$/.test(step1[0]) && step1[1].length > step1[0].length + 10, 'owner style: a bold lead phrase, then a plain sentence: ' + step1);
      await cleanShot(f, 'welcome');
      await f.goto(furl + '/#/factory');
      await f.waitForSelector('#view-factory a[href="#/setup/repo"]');
      await f.goto(furl + '/#/chat');
      await f.waitForSelector('#view-chat a[href="#/setup/repo"]');
      await f.goto(furl + '/#/start/repo');
      await f.waitForSelector('.wz-main[data-step="repo"] a[href="#/setup/repo"]');
    });

    await step('setup wizard: cards are a radio group (arrows, Enter); Test runs on a temporary entry; a typed key never reaches storage', async () => {
      await f.goto(furl + '/#/welcome');
      await f.click('.first-step [data-action="first-step"]');
      await atStep(f, 'where');
      assert(await f.getAttribute('.wz-main .opt-grid', 'role') === 'radiogroup', 'single choice cards form a radiogroup');
      assert(await checked(f, 'hybrid') === 'true', 'the recommended card (Hybrid) is preselected');
      assert(/Recommended/.test(await f.textContent('.opt-card[data-opt="hybrid"] .opt-badges')), 'Hybrid carries the Recommended badge');
      const dts = await f.$$eval('.opt-card[data-opt="local"] .opt-dt', (els) => els.map((e) => e.textContent));
      assert(canon(dts) === canon(['What it does', 'Choose this when']), 'every card says what it does and when to choose it: ' + dts);
      assert(await f.locator('.wz-main ol.proc li').count() >= 3, 'the step has its numbered procedure');
      await f.focus('.opt-card[data-opt="hybrid"]');
      await f.keyboard.press('ArrowLeft');
      await f.waitForSelector('.opt-card[data-opt="api"][aria-checked="true"]');
      assert(await f.evaluate(() => document.activeElement && document.activeElement.dataset.opt) === 'api', 'focus follows the selection');
      await f.keyboard.press('ArrowRight');
      await f.waitForSelector('.opt-card[data-opt="hybrid"][aria-checked="true"]');
      await cleanShot(f, 'setup-step');
      await f.keyboard.press('Enter');
      await atStep(f, 'local-servers');
      assert(await f.getAttribute('.wz-main .opt-grid', 'role') === 'group', 'pick-several cards are a group of checkboxes');
      await next(f);
      await f.waitForSelector('.wz-error:not([hidden])');
      await pick(f, 'llamacpp');
      await f.waitForSelector('.opt-card[data-opt="llamacpp"][aria-checked="true"]');
      await next(f, 'local-details');
      await f.fill('#wz-local-details-llamacpp_model', 'qwen3.8-27b');
      const testPosts = await postsDuring(f, async () => {
        const probe = await waitPost(f, /^\/api\/models\/sf-setup-test-llamacpp-[a-z0-9]+\/test$/, () => f.click('[data-action="wz-test"][data-test="local:llamacpp"]'));
        assert(probe.status() === 200, 'probe status ' + probe.status());
        await f.waitForSelector('.wz-test[data-test="local:llamacpp"] .wz-test-result.ok');
        await f.waitForTimeout(300);
      });
      const cfgPosts = testPosts.filter((p) => p.path === '/api/config');
      assert(cfgPosts.length === 2, 'Test adds a temporary entry and removes it: ' + JSON.stringify(testPosts));
      const add = cfgPosts[0].body;
      const tk = Object.keys(add.models)[0];
      assert(/^sf-setup-test-llamacpp-/.test(tk) && add.providers[tk].base_url === 'http://127.0.0.1:8080/v1' && add.models[tk].model === 'qwen3.8-27b' && add.models[tk].provider === tk, 'temporary entry: ' + JSON.stringify(add));
      assert(canon(cfgPosts[1].body) === canon({ models: { [tk]: null }, providers: { [tk]: null } }), 'removed afterwards: ' + JSON.stringify(cfgPosts[1].body));
      const cfgNow = await f.evaluate(async () => (await fetch('/api/config')).text());
      assert(!/sf-setup-test/.test(cfgNow), 'nothing of the test stays in the config');
      assert(/tok\/s/.test(await f.textContent('.wz-test[data-test="local:llamacpp"] .wz-test-result')), 'Test reports tokens per second');
      await next(f, 'api-providers');
      await pick(f, 'anthropic');
      await next(f, 'api-keys');
      await next(f);
      await f.waitForSelector('[data-err-of="wz-api-keys-anthropic_key"]:not([hidden])');
      await f.fill('#wz-api-keys-anthropic_key', FRESH_KEY);
      assert(!(await storage(f)).includes(FRESH_KEY), 'a typed key is kept in memory only');
      await next(f, 'roles');
      assert(await checked(f, 'preset:hybrid') === 'true', 'roles recommend the hybrid preset for a hybrid setup');
      await next(f, 'tools');
      await f.click('#wz-tools-rea input[value="preset:rea"]');
      await f.waitForSelector('#wz-tools-rea_agents input[value="analyst"]:checked');
      await f.check('#wz-tools-rea_agents input[value="coder"]');
      await next(f, 'sandbox');
      assert(await checked(f, 'none') === 'true', 'the sandbox step starts from the saved setting');
      assert(/Current/.test(await f.textContent('.opt-card[data-opt="none"] .opt-badges')), 'the saved setting is marked Current');
      assert(/Your current setting is No sandbox; Bubblewrap sandbox is recommended/.test(await f.textContent('.wz-current')), 'and said in words next to the Recommended one');
      await pick(f, 'bwrap');
      await f.waitForSelector('#wz-sandbox-network');
      await next(f, 'protected');
      await next(f, 'repo');
      assert(await f.locator('.wz-main .opt-card').count() === 0, 'no repository yet: the step is the form, not a lone card');
      await next(f);
      await f.waitForSelector('[data-err-of="wz-repo-path"]:not([hidden])');
      await f.fill('#wz-repo-path', '/home/me/code/shop');
      await f.fill('#wz-repo-base', 'main');
      await next(f, 'output');
      await pick(f, 'folder');
      await f.waitForSelector('.wz-main .wz-tip:has-text("merge policy step is skipped")');
      await f.fill('#wz-output-output_dir', '/srv/factory-output');
      await next(f, 'review');
      const items = await f.$$eval('.apply-item', (els) => els.map((e) => e.dataset.item));
      assert(canon(items) === canon(['keys', 'preset', 'models', 'tools', 'agents', 'safety', 'repo', 'output']), 'apply order: ' + items);
      const review = await f.textContent('.wz-review');
      assert(!review.includes(FRESH_KEY), 'the review never shows a key');
      assert(/sandbox/.test(review) && /bwrap/.test(review) && /\/srv\/factory-output/.test(review), 'the review shows each setting before and after');
      assert(await f.locator('.wz-path .wz-chip').count() >= 10, 'the path bar lists every answered step');
      assert(await f.locator('.wz-side .mm-answer').count() >= 10, 'beside the step, the path shows each answer');
      assert(!(await f.isVisible('.wz-path')), 'wide screens show the path once (beside the step), not twice');
      const pre = await f.textContent('.apply-item[data-item="preset"]');
      assert(/spec-writer/.test(pre) && /qwen3\.8-27b · llama\.cpp/.test(pre) && /takes the place of the preset’s local model/.test(pre), 'the preset says, per step, what each runs on afterwards: ' + pre.slice(0, 400));
      assert(!/TABBY_API_KEY/.test(pre), 'no key the choices already cover (llama.cpp replaces TabbyAPI): ' + pre.slice(0, 600));
    });

    await step('setup map: Wizard ⇄ Map, the chosen path highlighted, diamonds where steps branch; a step’s card goes back to it', async () => {
      await f.click('.wz-head .seg-btn[data-mode="map"]');
      await f.waitForFunction(() => location.hash === '#/setup/map');
      await f.waitForSelector('.flow-map svg .map-node[data-node="where"]');
      assert(await f.locator('.flow-map .map-node.shape-diamond[data-node="where"]').count() === 1, 'a branching step is a diamond');
      assert(await f.locator('.flow-map .map-node.shape-box[data-node="local-details"]').count() === 1, 'a form is a box: nothing is chosen there');
      assert(await f.locator('.flow-map .map-node.shape-diamond.is-cond[data-node="if:local-details"]').count() === 1, 'the earlier answer it follows is a separate check');
      assert(await f.locator('.flow-map .map-node.shape-diamond[data-node="output"]').count() === 1, 'Output decides whether the merge policy is asked');
      assert(await f.locator('.flow-map .map-node.is-current[data-node="review"]').count() === 1, 'you are here: review');
      assert(await f.locator('.flow-map .map-node.is-path').count() >= 12, 'the chosen path is highlighted');
      assert(await f.locator('.flow-map .map-node.is-path[data-node="roles-custom"]').count() === 0, 'a step off the path is not highlighted');
      assert(await f.locator('.flow-map .map-edge.is-hot').count() >= 11, 'the chosen arrows are highlighted');
      const lab = await labels(f, '.flow-map');
      assert(/Hybrid/.test(lab) && /Custom per step/.test(lab), 'arrows say which answer leads where: ' + lab.slice(0, 300));
      await cleanShot(f, 'setup-map');
      await f.focus('.flow-map .map-node[data-node="sandbox"]');
      await f.keyboard.press('Enter');
      await f.waitForFunction(() => location.hash === '#/setup/map/sandbox');
      await f.waitForSelector('.map-card:has-text("How are shell commands contained?") ol.proc li');
      await f.waitForFunction(() => document.activeElement && document.activeElement.classList.contains('map-card-title'));
      await f.keyboard.press('Escape');
      await f.waitForFunction(() => location.hash === '#/setup/map');
      await f.waitForFunction(() => document.activeElement && document.activeElement.dataset.node === 'sandbox');
      await f.click('.flow-map .map-node[data-node="sandbox"]');
      await f.waitForSelector('.map-card:has-text("How are shell commands contained?") ol.proc li');
      await f.click('.map-card [data-action="go-step"]');
      await atStep(f, 'sandbox');
      assert(await checked(f, 'bwrap') === 'true', 'the answer is kept');
      await f.click('.wz-head .seg-btn[data-mode="map"]');
      await f.waitForSelector('.flow-map svg');
      await f.click('.wz-head .seg-btn[data-mode="wizard"]');
      await atStep(f, 'sandbox');
      await f.goto(furl + '/#/setup/review');
      await atStep(f, 'review');
    });

    await step('setup apply: keys, preset, models, MCP, an agent’s mcp: line, safety, repository and output, in order; the key is never logged', async () => {
      let doctorRuns = 0;
      const onDoctor = (r) => { if (r.method() === 'GET' && new URL(r.url()).pathname === '/api/doctor') doctorRuns++; };
      f.on('request', onDoctor);
      const posts = await postsDuring(f, async () => {
        await f.click('.wz-foot [data-action="wz-next"]');
        await f.waitForSelector('.apply-done [data-action="go-start"]', { timeout: 20000 });
      });
      // Saved settings change what Doctor finds: it runs again once the saves settle, so the header's badge never
      // keeps the fresh install's to-do list (shown as that many red issues once the install is no longer fresh).
      const t0 = Date.now();
      while (!doctorRuns && Date.now() - t0 < 5000) await f.waitForTimeout(100);
      f.off('request', onDoctor);
      assert(doctorRuns >= 1, 'Doctor runs again after Apply');
      const seq = posts.map((p) => p.path);
      // The shipped analyst already names rea (mcp: rea); the coder gets it added.
      const want = ['/api/secrets', '/api/config/preset', '/api/config', '/api/config/preset', '/api/agents/coder', '/api/config', '/api/repos', '/api/config', '/api/config'];
      assert(canon(seq) === canon(want), 'apply requests: ' + seq.join(' '));
      assert(canon(posts[0].body) === canon({ name: 'ANTHROPIC_API_KEY', value: FRESH_KEY }), 'the key goes to POST /api/secrets');
      assert(canon(posts[1].body) === canon({ name: 'hybrid' }), 'preset body ' + JSON.stringify(posts[1].body));
      const models = posts[2].body;
      assert(models.providers.llamacpp.base_url === 'http://127.0.0.1:8080/v1' && models.models.local.provider === 'llamacpp' && models.models.local.model === 'qwen3.8-27b', 'models body ' + JSON.stringify(models));
      assert(canon(posts[3].body) === canon({ name: 'rea' }), 'REA from the example: ' + JSON.stringify(posts[3].body));
      assert(/\nmcp: rea\n/.test(posts[4].body.text) && /\nname: coder\n/.test(posts[4].body.text), 'the coder’s file gets mcp: rea');
      assert(canon(posts[5].body) === canon({ harness: { sandbox: 'bwrap', sandbox_network: true } }), 'safety sends only what changed: ' + JSON.stringify(posts[5].body));
      assert(canon(posts[6].body) === canon({ path: '/home/me/code/shop' }), 'repo body ' + JSON.stringify(posts[6].body));
      assert(posts[7].body.repos.some((r) => r.name === 'shop' && r.base === 'main'), 'the base branch: ' + JSON.stringify(posts[7].body));
      assert(canon(posts[8].body) === canon({ merge: { delivery: 'folder', output_dir: '/srv/factory-output' } }), 'output body ' + JSON.stringify(posts[8].body));
      assert(await f.locator('.apply-item.is-ok').count() === 8, 'every item reports done');
      const log = await f.evaluate(async () => (await fetch('/__mock/requests')).text());
      assert(!log.includes(FRESH_KEY), 'the request log never holds the key');
      const st = await storage(f);
      assert(!st.includes(FRESH_KEY) && !/sf\.flow\.setup/.test(st), 'the applied draft is cleared and no key was stored');
      const secrets = await f.evaluate(async () => (await fetch('/api/secrets')).json());
      assert(secrets.secrets.some((x) => x.name === 'ANTHROPIC_API_KEY' && x.set === true), 'the key is set on the server');
      await f.click('.apply-done [data-action="go-start"]');
      await f.waitForFunction(() => location.hash === '#/start');
      await f.waitForSelector('.wz-main[data-step]');
    });

    await step('setup again: a second Apply sends the new answers; a partial failure retries only what failed, as listed; new answers make a new review', async () => {
      const side = (id) => f.click('.wz-side a.mm-step[href="#/setup/' + id + '"]');
      await f.goto(furl + '/#/setup/output');
      await atStep(f, 'output');
      assert(await checked(f, 'folder') === 'true', 'the saved output is preselected');
      await pick(f, 'branch');
      await next(f, 'review');
      assert(/folder/.test(await f.textContent('.apply-item[data-item="output"]')) && /branch/.test(await f.textContent('.apply-item[data-item="output"]')), 'the review shows the new change');
      assert((await f.textContent('.wz-foot [data-action="wz-next"]')).trim() === 'Apply' && !(await f.isDisabled('.wz-foot [data-action="wz-next"]')), 'Apply is offered again');
      let posts = await postsDuring(f, async () => {
        await f.click('.wz-foot [data-action="wz-next"]');
        await f.waitForSelector('.apply-done');
      });
      assert(canon(posts.map((p) => [p.path, p.body])) === canon([['/api/config', { merge: { delivery: 'branch' } }]]), 'second apply: ' + JSON.stringify(posts));
      // A partial failure: the repository is not a git checkout; the output and merge policy apply.
      await f.goto(furl + '/#/setup/repo');
      await atStep(f, 'repo');
      await pick(f, 'new');
      await f.fill('#wz-repo-path', '/home/me/code/notgit-app');
      await next(f, 'output');
      await pick(f, 'merge');
      await next(f, 'merge-policy');
      assert(/Merge by itself/.test(await f.textContent('.wz-main')), 'merging asks who decides it');
      await pick(f, 'ask');
      await next(f, 'review');
      allowHttp(true);
      try {
        await f.click('.wz-foot [data-action="wz-next"]');
        await f.waitForSelector('.apply-item.is-failed[data-item="repo"]', { timeout: 15000 });
        await f.waitForSelector('.apply-item.is-ok[data-item="output"]');
        await f.waitForFunction(() => /Retry failed items/.test(document.querySelector('.wz-foot [data-action="wz-next"]').textContent));
        posts = await postsDuring(f, async () => {
          await f.click('.wz-foot [data-action="wz-next"]');
          await f.waitForFunction(() => /Retry failed items/.test(document.querySelector('.wz-foot [data-action="wz-next"]').textContent) && !document.querySelector('.apply-item.is-running'));
          await f.waitForTimeout(300);
        });
      } finally { allowHttp(false); }
      assert(canon(posts.map((p) => [p.path, p.body])) === canon([['/api/repos', { path: '/home/me/code/notgit-app' }]]), 'Retry sends the failed item only, as listed: ' + JSON.stringify(posts));
      // A new answer: the old plan is dropped and the new list is shown, labelled Apply, before anything is sent.
      await side('repo');
      await atStep(f, 'repo');
      await f.fill('#wz-repo-path', '/home/me/code/second-app');
      await side('review');
      await atStep(f, 'review');
      assert((await f.textContent('.wz-foot [data-action="wz-next"]')).trim() === 'Apply', 'new answers: Apply, not Retry');
      const items = await f.$$eval('.apply-item', (els) => els.map((e) => e.dataset.item + ':' + e.className));
      assert(canon(items.map((x) => x.split(':')[0])) === canon(['repo']) && /second-app/.test(await f.textContent('.apply-item[data-item="repo"]')) && !items.some((x) => /is-ok|is-failed/.test(x)), 'the review is rebuilt: ' + items);
      posts = await postsDuring(f, async () => {
        await f.click('.wz-foot [data-action="wz-next"]');
        await f.waitForSelector('.apply-done');
      });
      assert(canon(posts.map((p) => [p.path, p.body])) === canon([['/api/repos', { path: '/home/me/code/second-app' }]]), 'what was shown is what is sent: ' + JSON.stringify(posts));
    });

    await step('setup: a step left invalid behind the review blocks Apply and opens that step; arrow keys over the cards lose no answers', async () => {
      const side = (id) => f.click('.wz-side a.mm-step[href="#/setup/' + id + '"]');
      await f.goto(furl + '/#/setup/protected');
      await atStep(f, 'protected');
      await pick(f, 'custom');
      await f.waitForSelector('#wz-protected-globs');
      for (const id of ['repo', 'output', 'merge-policy', 'review']) await next(f, id);
      await side('protected');
      await atStep(f, 'protected');
      await f.fill('#wz-protected-globs', '');
      await side('review');
      await atStep(f, 'review');
      const posts = await postsDuring(f, async () => {
        await f.click('.wz-foot [data-action="wz-next"]');
        await atStep(f, 'protected');
        await f.waitForSelector('.wz-error:not([hidden]):has-text("Apply needs this step first")');
        await f.waitForTimeout(300);
      });
      assert(!posts.length, 'nothing is sent while a step on the path is invalid: ' + JSON.stringify(posts));
      await f.click('[data-action="start-over"]');
      await f.click('dialog[open] button[data-action="ok"]');
      await atStep(f, 'where');
      await pick(f, 'local');
      await next(f, 'local-servers');
      await pick(f, 'tabby');
      await pick(f, 'vllm');
      // GET /api/setup's host: which usual ports listen on this machine (doctor.setup_host)
      assert(/nothing listens there now/.test(await f.textContent('.opt-card[data-opt="vllm"]')) && /something listens there/.test(await f.textContent('.opt-card[data-opt="tabby"]')), 'the cards say which usual ports listen here');
      await next(f, 'local-details');
      await lookAround(f);
      await f.fill('#wz-local-details-vllm_model', 'my-awq-model');
      await f.fill('#wz-local-details-tabby_url', 'http://127.0.0.1:5001/v1');
      // Two Tests at once run one after the other, each on its own temporary entry, each result under its own server.
      const testPosts = await postsDuring(f, async () => {
        // Both pressed before the page redraws (the moment two probes used to share one entry).
        await f.evaluate(() => { document.querySelector('[data-action="wz-test"][data-test="local:tabby"]').click(); document.querySelector('[data-action="wz-test"][data-test="local:vllm"]').click(); });
        await f.waitForSelector('.wz-test[data-test="local:tabby"] .wz-test-result');
        await f.waitForSelector('.wz-test[data-test="local:vllm"] .wz-test-result');
      });
      const seq = testPosts.map((p) => (p.path === '/api/config' ? (Object.values(p.body.models)[0] === null ? 'del ' : 'add ') + Object.keys(p.body.models)[0] : 'probe ' + p.path.split('/')[3]));
      const keys = Array.from(new Set(seq.map((x) => x.split(' ')[1])));
      assert(keys.length === 2 && seq.join('|') === ['add ' + keys[0], 'probe ' + keys[0], 'del ' + keys[0], 'add ' + keys[1], 'probe ' + keys[1], 'del ' + keys[1]].join('|'), 'one test at a time, each with its own entry: ' + seq.join(' | '));
      assert(/:8000/.test(await f.textContent('.wz-test[data-test="local:vllm"] .wz-test-result')) && !/:8000/.test(await f.textContent('.wz-test[data-test="local:tabby"] .wz-test-result')), 'each result is its own server’s');
      await side('where');
      await atStep(f, 'where');
      await f.focus('.opt-card[data-opt="local"]');
      await f.keyboard.press('ArrowRight');
      await f.waitForSelector('.opt-card[data-opt="api"][aria-checked="true"]');
      await f.waitForSelector('.wz-note:has-text("cleared when you continue") [data-action="wz-undo"]');
      await f.keyboard.press('ArrowLeft');
      await f.waitForSelector('.opt-card[data-opt="local"][aria-checked="true"]');
      const kept = JSON.parse(JSON.parse(await storage(f))['sf.flow.setup']);
      assert(canon(kept.choices['local-servers']) === canon(['tabby', 'vllm']) && kept.values['local-details']['vllm.model'] === 'my-awq-model', 'looking across the cards clears nothing: ' + JSON.stringify(kept));
      assert(await f.locator('.wz-note').count() === 0, 'back on the old answer, nothing is pending');
      await pick(f, 'kit');
      await f.click('[data-action="wz-undo"]');
      await f.waitForSelector('.opt-card[data-opt="local"][aria-checked="true"]');
      await f.click('[data-action="start-over"]');
      await f.click('dialog[open] button[data-action="ok"]');
      await atStep(f, 'where');
    });

    await step('390px: Welcome, a setup step and the setup map fit a phone', async () => {
      const m = await browser.newContext({ viewport: { width: 390, height: 844 }, colorScheme: 'light', deviceScaleFactor: 2, isMobile: true, hasTouch: true });
      await m.addCookies([{ name: 'sf_token', value: 'test-token', url: furl }]);
      const p = await m.newPage();
      watch(p, 'fresh-mobile');
      try {
        await p.goto(furl + '/#/welcome');
        await p.waitForSelector('.opt-launch .opt-card');
        await noOverflow(p, 'welcome 390');
        await cleanShot(p, 'welcome-390');
        await p.goto(furl + '/#/setup/where');
        await atStep(p, 'where');
        await noOverflow(p, 'setup step 390');
        const foot = await p.$eval('.wz-foot', (el) => { const r = el.getBoundingClientRect(); return [r.bottom, window.innerHeight]; });
        assert(foot[0] <= foot[1] + 1, 'Back and Next stay on screen: ' + foot);
        await cleanShot(p, 'setup-step-390');
        await pick(p, 'local');
        await next(p, 'local-servers');
        await pick(p, 'llamacpp');
        await next(p, 'local-details');
        await mockLook(p, { finds: DEMO_FINDS.concat([{ url: 'http://127.0.0.1:18090/v1', port: 18090, status: 200, ids: ['a'.repeat(120)] }]) });
        await p.click('[data-action="wz-look-around"]');
        await p.waitForSelector('.wz-look-list li');
        await noOverflow(p, 'server details with the model servers found, a 120-character model id among them, 390');
        assert(/lists a{59}…/.test(await p.textContent('.wz-look-list')), 'a long model id is cut');
        await mockLook(p, { finds: DEMO_FINDS, forget: true });
        assert(await p.isVisible('.wz-path') && !(await p.getAttribute('.wz-path', 'open')), 'on a phone the answers so far are one closed line');
        assert(/2 answers/.test(await p.textContent('.wz-path summary')), 'it counts them');
        const top = await p.$eval('#wz-title', (el) => el.getBoundingClientRect().top);
        assert(top < 360, 'the step title stays near the top: ' + top);
        await p.goto(furl + '/#/setup/map');
        await p.waitForSelector('.flow-map svg');
        await noOverflow(p, 'setup map 390');
        await cleanShot(p, 'setup-map-390');
      } finally { await m.close(); }
    });
  } finally {
    await fctx.close();
    fresh.proc.kill('SIGTERM');
  }

  // ================================================================ the seeded factory (the main mock), light theme
  const gctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, colorScheme: 'light', deviceScaleFactor: 1 });
  await gctx.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: url });
  await gctx.addCookies([{ name: 'sf_token', value: 'test-token', url }]);
  const g = await gctx.newPage();
  watch(g, 'section25');
  let chatForHandoff = null;

  try {
    await step('Grid Card: every module with live status, labelled routes, and a side card with its procedure, Configure and Open', async () => {
      await g.goto(url + '/#/grid');
      await g.waitForSelector('.grid-map svg .map-node[data-node="builder"]');
      await g.waitForSelector('.grid-map .map-node[data-node="providers"].has-warn', { timeout: 8000 });
      const ids = await g.$$eval('.grid-map .map-node', (els) => els.map((e) => e.dataset.node));
      for (const id of ['welcome', 'providers', 'models', 'routing', 'tools', 'safety', 'repos', 'output', 'start', 'queue', 'spec', 'builder', 'wait', 'approver', 'human', 'gate',
        'out-merge', 'out-branch', 'out-folder', 'lead', 'plan-ready', 'chat-agent', 'handoff']) assert(ids.includes(id), 'Grid misses ' + id);
      const st = await g.evaluate(async () => (await fetch('/api/state')).json());
      const revs = ids.filter((x) => x.indexOf('rev:') === 0).map((x) => x.slice(4));
      assert(canon(revs) === canon(st.pipeline.reviewers), 'one box per reviewer of the line: ' + revs);
      const lab = await labels(g, '.grid-map');
      for (const l of ['any CHANGES → resume builder (max ' + st.pipeline.max_resumes + ')', 'ESCALATE', 'APPROVE', 'merge', 'branch', 'folder', 'all PASS'])
        assert(lab.includes(l), 'route label "' + l + '" missing: ' + lab);
      assert(await g.locator('.grid-map .map-node[data-node="human"].has-human').count() === 1, 'Needs human shows the jobs waiting');
      assert(await g.locator('.grid-map .map-node[data-node="repos"].has-ok').count() === 1, 'repos are ready');
      assert(await g.locator('.grid-map .map-node.shape-diamond[data-node="gate"]').count() === 1, 'the gate is a decision');
      const fit = await g.evaluate(() => { const r = document.querySelector('.grid-map svg').getBoundingClientRect(); return [r.bottom, window.innerHeight]; });
      assert(fit[0] <= fit[1], 'the whole map fits 1440x900, outputs included: ' + fit);
      const labelCls = await g.$$eval('.grid-map .map-label', (els) => Object.fromEntries(els.map((e) => [e.textContent, e.getAttribute('class')])));
      assert(/is-human/.test(labelCls.ESCALATE) && /is-human/.test(labelCls['protected · too big']), 'a label has its route’s colour: ' + JSON.stringify(labelCls));
      const legend = await g.textContent('.grid-legend');
      assert(/needs attention/.test(legend) && /waiting for you/.test(legend) && /where finished work goes/.test(legend), 'every colour on the map is in the legend: ' + legend);
      await g.click('.grid-map .map-node[data-node="providers"]');
      await g.waitForFunction(() => location.hash === '#/grid/providers');
      await g.waitForSelector('.grid-card[data-node="providers"] ol.proc li');
      assert(/DEEPSEEK_API_KEY/.test(await g.textContent('.grid-card .grid-card-status')), 'the card says what needs attention');
      const cfgHref = await g.getAttribute('.grid-card [data-action="configure"]', 'href');
      assert(/^#\/setup\//.test(cfgHref), 'Configure opens the wizard: ' + cfgHref);
      await g.click('.grid-card [data-action="configure"]');
      await atStep(g, cfgHref.split('/')[2]);
      // Grid › Providers › Configure, to add one key: passing through Who does what keeps the routing.
      await pick(g, 'deepseek');
      await next(g, 'api-keys');
      await g.fill('#wz-api-keys-deepseek_key', 'sk-smoke-not-saved');
      await next(g, 'roles');
      assert(await checked(g, 'keep') === 'true', 'a factory in use keeps its routing unless a preset is picked');
      await g.click('[data-action="start-over"]');
      await g.click('dialog[open] button[data-action="ok"]');
      await g.goto(url + '/#/grid');
      await g.waitForSelector('.grid-map .map-node[data-node="builder"]');
      await g.focus('.grid-map .map-node[data-node="builder"]');
      await g.keyboard.press('Enter');
      await g.waitForFunction(() => location.hash === '#/grid/builder');
      await g.waitForFunction(() => document.activeElement && document.activeElement.classList.contains('map-card-title'));
      assert(/Open the agent file/.test(await g.textContent('.grid-card [data-action="open"]')), 'the button says where it goes');
      assert(/builder runs on deepseek-v4-pro/.test(await g.textContent('.grid-card')) && !/opus →/.test(await g.textContent('.grid-card')), 'the model, not the tier alias');
      const scale = await g.$eval('.grid-map svg', (svg) => Number(svg.getAttribute('width')) / Number(svg.getAttribute('viewBox').split(' ')[2]));
      assert(scale >= 0.849, 'beside its card the map keeps readable text: scale ' + scale);
      await g.keyboard.press('Escape');
      await g.waitForFunction(() => location.hash === '#/grid' && document.activeElement && document.activeElement.dataset.node === 'builder');
      await g.goto(url + '/#/grid/builder');
      await g.waitForSelector('.grid-card[data-node="builder"] [data-action="open"][href="#/agents/builder"]');
      assert(await g.locator('.grid-card[data-node="builder"] ol.proc li').count() >= 3, 'the builder card has its working procedure');
      await g.waitForSelector('.grid-card [data-action="procedure"]');
      await noOverflow(g, 'grid');
      await cleanShot(g, 'grid');
      await g.click('.grid-card [data-action="procedure"]');
      await g.waitForFunction(() => location.hash === '#/procedures/builder');
      await g.waitForSelector('#proc-builder');
    });

    await step('Welcome on a factory in use: "needs attention", not "not set up"; its link opens the step that fixes it; the Grid card opens over the map below 1400px', async () => {
      await g.goto(url + '/#/welcome');
      await g.waitForSelector('.wz-attn [data-action="attention"]');
      assert(/Setup needs attention/.test(await g.textContent('.setup-chips-sum')), 'chips: ' + await g.textContent('.setup-chips-sum'));
      const gap = await g.evaluate(() => document.querySelector('.wz-proc-card').getBoundingClientRect().top - document.querySelector('.wz-attn').getBoundingClientRect().bottom);
      assert(gap >= 12, 'the banner does not touch the card under it: ' + gap);
      await g.click('.wz-attn [data-action="attention"]');
      await atStep(g, 'api-keys');
      assert(await g.locator('#wz-api-keys-deepseek_key').count() === 1, 'the Keys step, with the provider whose key is missing');
      await g.click('[data-action="start-over"]');
      await g.click('dialog[open] button[data-action="ok"]');
      await g.setViewportSize({ width: 1024, height: 768 });
      try {
        await g.goto(url + '/#/grid/approver');
        await g.waitForSelector('.grid-layout.card-over .grid-card[data-node="approver"]');
        assert(await g.isVisible('.grid-layout .map-scrim'), 'a scrim dims the map behind the card');
        const box = await g.$eval('.grid-map', (el) => [el.scrollWidth - el.clientWidth, Number(el.querySelector('svg').getAttribute('width')) / Number(el.querySelector('svg').getAttribute('viewBox').split(' ')[2])]);
        assert(box[1] >= 0.849, 'the map keeps readable text and scrolls instead: ' + box);
        await g.click('.grid-layout .map-scrim', { position: { x: 20, y: 300 } });
        await g.waitForFunction(() => location.hash === '#/grid');
      } finally { await g.setViewportSize({ width: 1440, height: 900 }); }
    });

    await step('Procedures: the server’s booklet with numbered steps and process maps; Download as HTML is the server’s self-contained page', async () => {
      await g.goto(url + '/#/procedures');
      await g.waitForSelector('#proc-factory-line .book-fig svg .map-node');
      const secs = await g.$$eval('.book-sec', (els) => els.map((e) => e.id));
      for (const id of ['proc-factory-line', 'proc-spec-writer', 'proc-builder', 'proc-approver', 'proc-plans', 'proc-lead', 'proc-analyst'])
        assert(secs.includes(id), 'booklet misses ' + id + ': ' + secs);
      assert(secs.indexOf('proc-factory-line') === 0 && secs.indexOf('proc-plans') < secs.indexOf('proc-lead'), 'the flows lead their parts: ' + secs);
      assert(await g.locator('#proc-approver ol.proc li').count() >= 3, 'the approver’s numbered steps from its file');
      assert(await g.locator('#proc-approver .book-h3').count() >= 3, 'every section of the agent file');
      assert(await g.locator('#proc-builder details.book-file:not([open])').count() === 1, 'the whole agent file is one click away, closed');
      assert(await g.locator('#proc-builder > ol.proc li strong').count() >= 3, 'each module leads with its short numbered procedure');
      assert(/Runs on deepseek-v4-pro/.test(await g.textContent('#proc-builder .book-meta')), 'the booklet names the model: ' + await g.textContent('#proc-builder .book-meta'));
      assert(await g.locator('#proc-factory-line .map-node.shape-diamond').count() >= 3, 'the factory map has its decisions');
      assert(await g.locator('#proc-plans .book-fig svg').count() === 1, 'the plan flow map sits with Plans');
      const lead = await g.$eval('#proc-factory-line ol.proc li strong', (e) => e.textContent);
      assert(/\.$/.test(lead), 'owner-style lead phrase: ' + lead);
      const href = await g.getAttribute('[data-action="download"]', 'href');
      assert(href === '/api/docs/procedures.html?download=1', 'download link ' + href);
      const dl = await g.evaluate(async (h) => { const r = await fetch(h); return { status: r.status, type: r.headers.get('content-type'), disp: r.headers.get('content-disposition'), text: await r.text() }; }, href);
      assert(dl.status === 200 && /^text\/html/.test(dl.type) && /attachment/.test(dl.disp || ''), 'the booklet downloads: ' + JSON.stringify([dl.status, dl.type, dl.disp]));
      assert(/<svg/.test(dl.text) && !/<(script|link|img|iframe|object|embed)\b/i.test(dl.text) && !/\b(src|href)\s*=\s*["']?(https?:)?\/\//i.test(dl.text), 'one self-contained page: no scripts, nothing loaded from elsewhere');
      await g.click('.book-nav a[data-sec="reviewer-security"]');
      await g.waitForFunction(() => location.hash === '#/procedures/reviewer-security');
      await g.waitForFunction(() => { const r = document.getElementById('proc-reviewer-security').getBoundingClientRect(); return r.top >= 0 && r.top < 300; });
      await noOverflow(g, 'procedures');
      await g.goto(url + '/#/procedures');
      await g.waitForSelector('#proc-factory-line .book-fig svg');
      await g.evaluate(() => { document.getElementById('main').scrollTop = 0; });
      await cleanShot(g, 'procedures');
    });

    await step('Start work: a feature through the wizard; the review shows the exact body; the path bar goes back and clears what no longer applies; the draft survives a reload; Start over', async () => {
      await g.goto(url + '/#/start/review');
      await atStep(g, 'review');
      const early = await postsDuring(g, async () => {
        await g.click('.wz-foot [data-action="wz-next"]');
        await atStep(g, 'describe');
        await g.waitForSelector('.wz-error:not([hidden]):has-text("Start needs this step first")');
      });
      assert(!early.length, 'an empty request is never sent: ' + JSON.stringify(early));
      await g.goto(url + '/#/start/what');
      await atStep(g, 'what');
      assert(await checked(g, 'feature') === 'true', 'a feature is the recommended start');
      await next(g, 'repo');
      assert(await g.locator('.opt-card.is-compact[data-opt="docs-site"] .opt-dt').count() === 0 && /\/home\/me\/code\/docs-site/.test(await g.textContent('.opt-card[data-opt="docs-site"] .opt-sub')), 'a repository card is its path and base, not "What it does"');
      assert(await g.getAttribute('.wz-main .opt-add', 'href') === '#/setup/repo', 'Add a repository… is one click away');
      await pick(g, 'docs-site');
      await next(g, 'describe');
      await next(g);
      await g.waitForSelector('[data-err-of="wz-describe-text"]:not([hidden])');
      await g.fill('#wz-describe-text', 'Add a print stylesheet for the docs');
      await next(g, 'routing');
      await pick(g, 'preset:hybrid');
      await g.evaluate(() => { document.getElementById('main').scrollTop = 0; });
      await cleanShot(g, 'start-step');
      await next(g, 'output');
      assert(/Default/.test(await g.textContent('.opt-card[data-opt="merge"] .opt-badges')), 'the configured output is marked Default');
      await pick(g, 'branch');
      await next(g, 'review');
      const raw = await g.textContent('.wz-raw pre');
      assert(raw.includes('POST /api/jobs') && raw.includes('"routing": "hybrid"') && raw.includes('"delivery": "branch"'), 'the review shows exactly what is sent: ' + raw);
      await g.click('.wz-side a.mm-step[href="#/start/what"]');
      await atStep(g, 'what');
      await pick(g, 'chat');
      await g.waitForSelector('.wz-note:has-text("Which models run it?")');
      assert(/Where does finished work go\?/.test(await g.textContent('.wz-note')), 'every dropped answer is named');
      await g.reload();
      await atStep(g, 'what');
      assert(await checked(g, 'chat') === 'true', 'the draft survives a reload');
      await g.click('[data-action="start-over"]');
      await g.click('dialog[open] button[data-action="ok"]');
      await atStep(g, 'what');
      // The step re-renders a frame after the hash changes: wait for it rather than read the old cards.
      await g.waitForSelector('.wz-main .opt-card[data-opt="feature"][aria-checked="true"]', { timeout: 5000 }).catch(() => {});
      assert(await checked(g, 'feature') === 'true', 'Start over clears the draft');
      const kept = JSON.parse(JSON.parse(await storage(g))['sf.flow.start'] || '{}');
      assert(!kept.choices || (kept.choices.what !== 'chat' && !kept.choices.repo), 'and its stored copy: ' + JSON.stringify(kept));
      await next(g, 'repo');
      await pick(g, 'docs-site');
      await next(g, 'describe');
      await g.fill('#wz-describe-text', 'Add a print stylesheet for the docs');
      await next(g, 'routing');
      await pick(g, 'preset:hybrid');
      await next(g, 'output');
      await pick(g, 'branch');
      await next(g, 'review');
      const resp = await waitPost(g, /^\/api\/jobs$/, () => g.click('.wz-foot [data-action="wz-next"]'));
      const body = JSON.parse(resp.request().postData());
      assert(resp.status() === 201 && canon(body) === canon({ repo: 'docs-site', feature: 'Add a print stylesheet for the docs', routing: 'hybrid', delivery: 'branch' }), 'job body ' + JSON.stringify(body));
      const job = (await resp.json()).job;
      await g.waitForFunction((h) => location.hash === h, '#/job/docs-site/' + job.id);
      await g.waitForSelector('#drawer:not([hidden])');
      await g.waitForSelector('.toast-ok:has-text("queued")');
    });

    await step('Start work: a plan from the Plans empty path, and an analysis from Welcome, land on their views with the exact requests', async () => {
      await g.goto(url + '/#/start');
      await atStep(g, 'what');
      await pick(g, 'plan');
      await next(g, 'repo');
      await pick(g, 'my-app');
      await next(g, 'describe');
      await g.fill('#wz-describe-text', 'Audit log v3: retention settings and an export page');
      await next(g, 'routing');
      await next(g, 'output');
      await pick(g, 'folder');
      await next(g, 'review');
      const pr = await waitPost(g, /^\/api\/plans$/, () => g.click('.wz-foot [data-action="wz-next"]'));
      assert(pr.status() === 201 && canon(JSON.parse(pr.request().postData())) === canon({ repo: 'my-app', request: 'Audit log v3: retention settings and an export page', delivery: 'folder' }), 'plan body ' + pr.request().postData());
      const plan = (await pr.json()).plan;
      await g.waitForFunction((h) => location.hash === h, '#/plans/my-app/' + plan.id);
      await g.goto(url + '/#/welcome');
      await g.waitForSelector('.opt-launch .opt-card[data-opt="analyse"]');
      await g.click('.opt-launch .opt-card[data-opt="analyse"]');
      await atStep(g, 'repo');
      await pick(g, 'my-app');
      await next(g, 'describe');
      await g.fill('#wz-describe-binary', 'bin/tool');
      await g.fill('#wz-describe-text', 'Which functions parse the config file?');
      await next(g, 'review');
      const posts = await postsDuring(g, async () => {
        await g.click('.wz-foot [data-action="wz-next"]');
        await g.waitForFunction(() => /^#\/chat\/c-/.test(location.hash));
        await g.waitForSelector('.chat-thread .msg-user');
      });
      const chat = posts.find((p) => p.path === '/api/chats');
      const msg = posts.find((p) => /\/messages$/.test(p.path));
      assert(chat && canon(chat.body) === canon({ repo: 'my-app', mode: 'worktree', agent: 'analyst' }), 'analysis chat body ' + JSON.stringify(chat));
      assert(msg && msg.body.text === 'Analyse bin/tool.\n\nWhich functions parse the config file?', 'first message ' + JSON.stringify(msg));
      chatForHandoff = g.url().split('#/chat/')[1];
      await g.waitForSelector('.composer textarea:not([disabled])', { timeout: 20000 });
    });

    await step('delivery: delivered jobs sit in Done with a badge; the drawer shows where the work is (copy) and Merge it now merges it', async () => {
      await g.goto(url + '/#/factory');
      await g.waitForSelector('.col-merged .card[data-job="my-app/009-export-the-audit-log-as-csv"]');
      assert(/Done/.test(await g.textContent('.col-merged .col-title')), 'the column is Done');
      assert(/in folder/.test(await g.textContent('.card[data-job="my-app/009-export-the-audit-log-as-csv"] .tag-deliver')), 'folder badge');
      assert(/on branch/.test(await g.textContent('.card[data-job="docs-site/003-add-a-changelog-page"] .tag-deliver')), 'branch badge');
      await g.goto(url + '/#/job/my-app/009-export-the-audit-log-as-csv');
      await g.waitForSelector('#drawer .delivered-box[data-mode="folder"]');
      assert(/\/my-app\/009-export-the-audit-log-as-csv$/.test((await g.textContent('#drawer .delivered-path')).trim()), 'the output folder path');
      await g.click('#drawer [data-action="copy-output"]');
      await g.waitForSelector('.toast-ok:has-text("Folder path copied")');
      const copied = await g.evaluate(() => navigator.clipboard.readText().catch(() => ''));
      assert(copied === '' || /\/my-app\/009-export-the-audit-log-as-csv$/.test(copied), 'the path is on the clipboard: ' + copied);
      const merge = g.locator('#drawer .actions [data-action="approve"]');
      assert(/Merge it now/.test(await merge.textContent()), 'Merge it now');
      await merge.click();
      await g.waitForSelector('dialog[open] .modal-title:has-text("now?")');
      const ap = await waitPost(g, /\/api\/jobs\/my-app\/009-export-the-audit-log-as-csv\/approve$/, () => g.click('dialog[open] button[data-action="ok"]'));
      assert(ap.status() === 200, 'approve a delivered job: ' + ap.status());
      await g.waitForSelector('.card[data-job="my-app/009-export-the-audit-log-as-csv"] .tag-merged', { timeout: 20000 });
      // A folder job that stopped before its delivery: approving delivers it (nothing merges), and the dialog says so.
      const made = await (await g.request.post(url + '/__mock/stopped-job', { data: { repo: 'docs-site', feature: 'Export the glossary for translators', stage: 'needs-human', delivery: 'folder', reason: 'approver escalated' } })).json();
      await g.goto(url + '/#/job/docs-site/' + made.job.id);
      const act = g.locator('#drawer .actions [data-action="approve"]');
      await act.waitFor();
      assert(/Approve & deliver/.test(await act.textContent()), 'the button: ' + await act.textContent());
      await act.click();
      await g.waitForSelector('dialog[open] .modal-title:has-text("Approve and deliver")');
      assert(/does not move/.test(await g.textContent('dialog[open]')) && !/merges into/.test(await g.textContent('dialog[open] .modal-title')), 'nothing merges');
      const dv = await waitPost(g, new RegExp('/api/jobs/docs-site/' + made.job.id + '/approve$'), () => g.click('dialog[open] button[data-action="ok"]'));
      assert(dv.status() === 200, 'approve ' + dv.status());
      await g.waitForSelector('.toast-ok:has-text("delivering as a folder")');
      await g.waitForSelector('#drawer .delivered-box[data-mode="folder"]', { timeout: 20000 });
    });

    await step('delivery: New feature, New plan and the hand-off send the chosen output; New chat picks its agent', async () => {
      await g.keyboard.press('Escape');
      await g.goto(url + '/#/factory');
      await g.waitForSelector('.board .col');
      await g.keyboard.press('n');
      await g.waitForSelector('dialog[open] #nf-feature');
      await g.waitForSelector('dialog[open] .dl-seg[data-mode="merge"].is-on');
      assert(/default/.test(await g.textContent('dialog[open] .dl-seg[data-mode="merge"]')), 'the default output is marked');
      await g.click('dialog[open] .dl-seg[data-mode="folder"]');
      await g.waitForSelector('dialog[open] .dl-note:not([hidden])');
      assert(await g.locator('dialog[open] button[data-action="backlog"]').isDisabled(), 'the backlog uses the default output');
      assert(/REPORT\.md/.test(await g.textContent('dialog[open] .dl-line')), 'one line says what folder means');
      await g.fill('dialog[open] #nf-feature', 'Export the glossary as a PDF');
      const nf = await waitPost(g, /^\/api\/jobs$/, () => g.click('dialog[open] button[data-action="run"]'));
      const nfb = JSON.parse(nf.request().postData());
      assert(nf.status() === 201 && nfb.delivery === 'folder' && nfb.feature === 'Export the glossary as a PDF', 'New feature body ' + JSON.stringify(nfb));
      await g.waitForSelector('dialog[open]', { state: 'detached' });
      await g.keyboard.press('Escape');
      await g.goto(url + '/#/plans');
      await g.click('[data-action="new-plan"]');
      await g.waitForSelector('dialog[open] #np-request');
      await g.fill('dialog[open] #np-request', 'Rework the navigation into sections');
      await g.click('dialog[open] .dl-seg[data-mode="branch"]');
      const np = await waitPost(g, /^\/api\/plans$/, () => g.click('dialog[open] button[data-action="ok"]'));
      assert(np.status() === 201 && JSON.parse(np.request().postData()).delivery === 'branch', 'New plan body ' + np.request().postData());
      await g.waitForFunction((pid) => location.hash === '#/plans/my-app/' + pid, (await np.json()).plan.id);
      await g.goto(url + '/#/chat/' + chatForHandoff);
      await g.waitForSelector('.composer textarea:not([disabled])', { timeout: 20000 });
      await g.click('.chat-head button:has-text("Hand off to factory")');
      await g.waitForSelector('dialog[open] .dl-seg');
      await g.click('dialog[open] .dl-seg[data-mode="folder"]');
      const ho = await waitPost(g, /\/handoff$/, () => g.click('dialog[open] button[data-action="ok"]'));
      const hob = JSON.parse(ho.request().postData());
      assert(ho.status() === 200 && hob.delivery === 'folder' && hob.feature, 'hand-off body ' + JSON.stringify(hob));
      await g.goto(url + '/#/chat');
      await g.click('#new-chat-btn');
      await g.waitForSelector('dialog[open] #nc-agent');
      const agents = await g.$$eval('dialog[open] #nc-agent option', (os) => os.map((o) => o.value));
      assert(agents.includes('coder') && agents.includes('analyst'), 'chat agents: ' + agents);
      await g.selectOption('dialog[open] #nc-agent', 'analyst');
      await g.selectOption('dialog[open] #nc-repo', 'my-app');
      const nc = await waitPost(g, /^\/api\/chats$/, () => g.click('dialog[open] button[data-action="ok"]'));
      assert(nc.status() === 201 && JSON.parse(nc.request().postData()).agent === 'analyst', 'New chat body ' + nc.request().postData());
      await g.waitForSelector('.composer textarea:not([disabled])');
      assert(/analyst/.test(await g.getAttribute('.composer textarea', 'placeholder') || ''), 'the composer names the agent');
    });

    await step('toasts: three at most (older fold into "+N more"), errors stay, info waits while hovered, and the stack clears the chat composer', async () => {
      allowHttp(true);
      await g.route('**/api/doctor*', (r) => r.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ error: 'doctor exploded' }) }));
      try {
        await g.goto(url + '/#/doctor');
        await g.waitForSelector('button:has-text("Re-run checks")');
        await g.evaluate(() => document.querySelectorAll('#toasts .toast, #toasts .toast-more').forEach((t) => t.remove()));
        for (let i = 1; i <= 5; i++) {
          await g.click('button:has-text("Re-run checks")');
          await g.waitForFunction((n) => document.querySelectorAll('#toasts .toast-error').length >= n, i);
        }
      } finally { await g.unroute('**/api/doctor*'); allowHttp(false); }
      assert(await g.locator('#toasts .toast-error:visible').count() === 3, 'three toasts show');
      assert((await g.textContent('#toasts .toast-more')).trim() === '+2 more', 'the rest fold into +2 more');
      await g.waitForTimeout(5600);
      assert(await g.locator('#toasts .toast-error').count() === 5, 'errors stay until dismissed');
      await g.click('#toasts .toast-more');
      assert(await g.locator('#toasts .toast-error:visible').count() === 5, '+N more shows them all');
      await g.goto(url + '/#/chat/' + chatForHandoff);
      await g.waitForSelector('.composer textarea');
      await g.waitForTimeout(200);
      const gap = await g.evaluate(() => {
        const toasts = Array.from(document.querySelectorAll('#toasts .toast:not([hidden])'));
        const c = document.querySelector('.composer-wrap').getBoundingClientRect();
        return { lowest: Math.max.apply(null, toasts.map((t) => t.getBoundingClientRect().bottom)), composer: c.top };
      });
      assert(gap.lowest <= gap.composer, 'toasts never cover the composer: ' + JSON.stringify(gap));
      while (await g.locator('#toasts .toast-x').count()) await g.locator('#toasts .toast-x').first().click();
      await g.goto(url + '/#/job/docs-site/003-add-a-changelog-page');
      await g.waitForSelector('#drawer .delivered-box[data-mode="branch"]');
      await g.click('#drawer [data-action="copy-output"]');
      const t0 = Date.now();
      await g.hover('#toasts .toast-ok');
      await g.waitForTimeout(2800);
      assert(await g.locator('#toasts .toast-ok').count() === 1, 'a hovered toast waits');
      await g.mouse.move(5, 5);
      await g.waitForSelector('#toasts .toast-ok', { state: 'detached', timeout: 6000 });
      assert(Date.now() - t0 > 3500, 'it leaves after the pointer does');
    });

    await step('agent.updated: an agent file saved elsewhere refreshes the open agent', async () => {
      await g.goto(url + '/#/agents/reviewer-code');
      await g.waitForSelector('.agent-viewer-body .md');
      const det = await (await g.request.get(url + '/api/agents/reviewer-code')).json();
      const r = await g.request.post(url + '/api/agents/reviewer-code', { data: { text: det.text.replace(/\s*$/, '') + '\n\nSmoke marker: saved in another tab.\n' } });
      assert(r.status() === 200, 'save from elsewhere ' + r.status());
      await g.waitForSelector('.agent-viewer-body:has-text("Smoke marker: saved in another tab.")', { timeout: 8000 });
      await g.request.post(url + '/api/agents/reviewer-code/reset', { data: {} });
      await g.waitForFunction(() => !/Smoke marker/.test(document.querySelector('.agent-viewer-body').textContent), null, { timeout: 8000 });
    });

    await step('390px: the Grid Card as a list, a Start step and the booklet fit a phone', async () => {
      const m = await browser.newContext({ viewport: { width: 390, height: 844 }, colorScheme: 'light', deviceScaleFactor: 2, isMobile: true, hasTouch: true });
      await m.addCookies([{ name: 'sf_token', value: 'test-token', url }]);
      const p = await m.newPage();
      watch(p, 'section25-mobile');
      try {
        await p.goto(url + '/#/grid');
        await p.waitForSelector('.grid-list .grid-row[data-node="builder"]');
        await noOverflow(p, 'grid 390');
        const order = await p.$$eval('.grid-list-sec:nth-child(2) .grid-row', (els) => els.map((e) => e.dataset.node));
        const at = (id) => order.indexOf(id);
        assert(at('builder') < at('rev:security') && at('rev:code') < at('wait') && at('gate') < at('human'), 'the list follows the line: ' + order);
        assert(/any CHANGES → builder/.test(await p.textContent('.grid-list')) && /ESCALATE → Needs human/.test(await p.textContent('.grid-list')), 'the labelled routes are in the list');
        assert(await p.locator('.grid-legend .ml-sw').count() === 0, 'a dot-only legend for the list');
        await cleanShot(p, 'grid-390');
        await p.click('.grid-list .grid-row[data-node="approver"]');
        await p.waitForSelector('.grid-card[data-node="approver"]');
        await noOverflow(p, 'grid card 390');
        await p.goto(url + '/#/start/routing');
        await p.waitForSelector('.wz-main .opt-card');
        await noOverflow(p, 'start step 390');
        await cleanShot(p, 'start-step-390');
        await p.goto(url + '/#/procedures');
        await p.waitForSelector('#proc-factory-line .book-fig svg');
        await noOverflow(p, 'procedures 390');
        await p.waitForFunction(() => { const b = document.querySelector('#proc-factory-line .book-map'); return b && (b.scrollWidth <= b.clientWidth + 1 || b.scrollLeft > 0); });
        const m = await p.$eval('#proc-factory-line .book-map', (b) => ({ over: b.scrollWidth > b.clientWidth + 1, left: b.scrollLeft, cue: !b.parentNode.querySelector('.map-cue').hidden }));
        assert(!m.over || (m.left > 0 && m.cue), 'a wide map opens on its spine, with a cue that it scrolls: ' + JSON.stringify(m));
        await cleanShot(p, 'procedures-390');
      } finally { await m.close(); }
    });
  } finally {
    await gctx.close();
  }
}
