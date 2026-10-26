// Checks for Placement (DESIGN §23, §15 "Placement"; GPU custody §37): the view module swfactory/web/views/routing.js
// against tests/gui_mock/routing.py, run by tests/gui/smoke.mjs (AREA_SUITES) after the core checks, or alone with
// GUI_ONLY=routing node tests/gui/smoke.mjs. Screenshots of every new screen, desktop 1440x900 and phone 390x844:
// t.areaShot(page, AREA, name) → $GUI_AREA_SHOTS/routing/<name>.png.
//
// run(t) gets the harness of smoke.mjs: { browser, url, startMock, step, assert, canon, waitPost, shot, cleanShot,
// noOverflow, watch, allowHttp, newPage(label, {phone, width, height, colorScheme, mockUrl}), areaShot(page, area, name) }.
// t.newPage gives a signed-in page of its own ({ctx, page, url}); close its ctx when done.
//
// The suite runs on a mock of its own (no background scenario), so the seeded placement records are what it reads.

const AREA = 'routing';
const SHOWCASE = 'my-app/011-cache-the-pricing-page';

/** The text of an element, or '' when it is not there. */
const text = async (page, sel) => ((await page.locator(sel).count()) ? (await page.locator(sel).first().textContent()) || '' : '');
/** The URL of the next GET whose path matches re, while action runs. */
async function waitGet(page, re, action) {
  const [req] = await Promise.all([page.waitForRequest((r) => r.method() === 'GET' && re.test(new URL(r.url()).pathname + new URL(r.url()).search), { timeout: 15000 }), action()]);
  return new URL(req.url());
}

export async function run(t) {
  const { step, assert, canon, noOverflow, waitPost } = t;
  const mock = await t.startMock(['--no-scenario']);
  const d = await t.newPage(AREA, { mockUrl: mock.url });
  const p = await t.newPage(AREA + '-phone', { phone: true, mockUrl: mock.url });
  const url = mock.url;
  const page = d.page;
  const phone = p.page;
  try {
    await step(AREA + ': a job card and its drawer say what the independence guard did (reduced, never green), the profile and the cost; the text stays text', async () => {
      await page.goto(url + '/#/factory');
      await page.waitForSelector(`.card[data-job="${SHOWCASE}"] .pl-ind`);
      const badge = page.locator(`.card[data-job="${SHOWCASE}"] .pl-ind`);
      assert((await badge.textContent()).includes('Independence reduced') && await badge.getAttribute('data-independence') === 'reduced', 'the card carries the reduced badge');
      assert(/reviewer-ui ran on gpt-sol \(openai\/gpt-6\.1-sol\), the openai family the builder ran on in round 2 \(gpt-sol\)/.test(await badge.getAttribute('title')), 'the tooltip names the shared model: ' + await badge.getAttribute('title'));
      assert(/^\$\d+\.\d\d \+ 1 unpriced$/.test((await text(page, `.card[data-job="${SHOWCASE}"] .pl-cost`)).trim()), 'the card shows the cost so far and, in words, the unpriced runs: ' + await text(page, `.card[data-job="${SHOWCASE}"] .pl-cost`));
      assert(/Shared family/.test(await text(page, '.card[data-job="my-app/002-rate-limit-signup-form"] .pl-ind')), 'an accepted shared family is a quiet chip');
      await page.click(`.card[data-job="${SHOWCASE}"]`);
      await page.waitForSelector('#drawer-head .pl-ind-block');
      const block = await page.textContent('#drawer-head .pl-ind-block');
      assert(/Independence reduced/.test(block) && /reviewer-ui ran on gpt-sol/.test(block) && /reviewer-security ran on local \(strata\), the qwen family the builder ran on in round 1 \(local\) \(accepted\)/.test(block),
        'the drawer header lists the shared models in the open: ' + block);
      assert(await page.locator('#drawer-head .pl-ind-block .pl-ind-reduced').count() === 1 && await page.locator('#drawer-head .tag-ok.pl-ind').count() === 0, 'amber, never green');
      assert(/profile local-first/.test(await text(page, '#drawer-head [data-meta="profile"]')), 'the job’s profile');
      assert(/cost \$\d+\.\d\d \+ 1 unpriced run/.test(await text(page, '#drawer-head [data-meta="cost"]')), 'the job’s cost names the unpriced run');
      await page.waitForTimeout(250);
      await t.areaShot(page, AREA, 'drawer-head-desktop');
    });

    await step(AREA + ': the Runs tab: a box per step and a row per run with its reason, build, swap, lease, escalation, overflow, independence and cost; cost per agent; where each step can run', async () => {
      await page.click('#tab-runs');
      await page.waitForSelector('#panel-runs:not([hidden]) .pl-run');
      assert(/\/runs$/.test(page.url()), 'the tab has its own URL: ' + page.url());
      const steps = await page.$$eval('#panel-runs .pl-step', (els) => els.map((e) => e.dataset.agent));
      assert(canon(steps) === canon(['spec-writer', 'builder', 'reviewer-security', 'reviewer-ux', 'reviewer-ui', 'reviewer-code', 'approver']), 'a box per step, in line order: ' + steps);
      assert(await page.getAttribute('#panel-runs .pl-step[data-agent="reviewer-ui"]', 'data-independence') === 'reduced', 'the reduced judge’s step box is marked');
      assert(/Independence reduced/.test(await text(page, '#panel-runs .pl-step[data-agent="reviewer-ui"]')), 'and says so on its box');
      const rows = await page.$$eval('#panel-runs .pl-run', (els) => els.map((e) => ({ run: e.dataset.run, text: e.textContent, why: (e.querySelector('.pl-why') || {}).dataset ? e.querySelector('.pl-why').dataset.why : null,
        marks: Array.from(e.querySelectorAll('[data-mark]')).map((m) => m.dataset.mark), ind: e.dataset.independence || null })));
      assert(rows.length === 12, 'one row per finished run: ' + rows.length);
      const row = (n) => rows.find((r) => r.run === n) || {};
      assert(canon(row('02-builder').marks) === canon(['build', 'swap', 'custody', 'no-lease']) && /build 2 of 2/.test(row('02-builder').text) && row('02-builder').why === 'cost', 'a second build with a swap and a lease: ' + JSON.stringify(row('02-builder')));
      // custody.Claims.info's custody_refused: the build the register gave no lease for, in its words, beside the build chip (DESIGN §37)
      assert(/no lease: strata\/strata/.test(row('02-builder').text) && /GPU custody gave no lease for strata\/strata: the card cannot fit this model now\./.test(row('02-builder').text), 'the refused build in words: ' + row('02-builder').text);
      const lease = await page.getAttribute('#panel-runs .pl-run[data-run="02-builder"] [data-mark="custody"]', 'title');
      assert(/GPU custody lease L-7f3a9c for qwen3\.8-flash-next on tabby/.test(lease) && /resident before: gpu0\/qwen3\.8-27b-exl3/.test(lease), 'the lease, with what was resident before (the register’s pairs): ' + lease);
      // the same facts in the open, for touch, keyboard and screen readers (DESIGN §39: lease id, model, until, resident_before)
      const facts = await page.$$eval('#panel-runs .pl-run[data-run="02-builder"] .pl-run-facts li', (els) => els.map((e) => [e.dataset.fact, e.textContent]));
      const fact = (k) => (facts.find((f) => f[0] === k) || [k, ''])[1];
      assert(/L-7f3a9c for qwen3\.8-flash-next on tabby until .+, generation \d+; resident before: gpu0\/qwen3\.8-27b-exl3/.test(fact('custody')) && /build 2 of 2: tabby\/qwen3\.8-flash-next/.test(fact('build')) && /loaded/.test(fact('swap')),
        'the run row says the lease, the build and the swap in words: ' + JSON.stringify(facts));
      assert(await page.locator('#panel-runs .pl-run[data-run="02-builder"] .pl-run-facts').isVisible(), 'visible, not only a tooltip');
      assert(canon(row('07-builder').marks) === canon(['escalation', 'handover']) && row('07-builder').why === 'capability' && /escalation rung 1 of 1 after 1 send-back/.test(row('07-builder').text), 'an escalation rung, a fresh session: ' + JSON.stringify(row('07-builder')));
      assert(row('10-reviewer-ui').ind === 'reduced' && canon(row('10-reviewer-ui').marks) === canon(['overflow']) && row('10-reviewer-ui').why === 'time', 'an overflow that reduced a judge: ' + JSON.stringify(row('10-reviewer-ui')));
      assert(row('11-reviewer-code').ind === 'alternate' && /Independent alternate \(left gpt-sol\)/.test(row('11-reviewer-code').text) && row('11-reviewer-code').why === 'stage-fit', 'an independent alternate: ' + JSON.stringify(row('11-reviewer-code')));
      assert(row('03-reviewer-security').ind === 'accepted' && /Shared family \(accepted\)/.test(row('03-reviewer-security').text), 'an accepted shared family');
      assert(/unpriced/.test(row('05-reviewer-ui').text) && /\$0(?!\.)/.test(row('02-builder').text), 'an unpriced model says so; a local run costs $0');
      assert(await page.locator('#panel-runs b').count() === 0 && (await text(page, '#panel-runs .pl-step[data-agent="approver"]')).includes('<b>strong</b>'), 'a reason’s markup stays text');
      const agents = await page.$$eval('#panel-runs .pl-agents tbody tr', (els) => els.map((e) => e.dataset.agent));
      assert(agents.includes('builder') && agents.includes('reviewer-ui') && /unpriced run/.test(await text(page, '#panel-runs .pl-agents tr[data-agent="reviewer-ui"]')), 'cost per agent: ' + agents);
      const builder = await text(page, '#panel-runs .pl-route[data-agent="builder"]');
      assert(/builds/.test(builder) && /strata\/strata → tabby\/qwen3\.8-flash-next/.test(builder) && /overflow/.test(builder) && /escalates to/.test(builder) && /gpt-sol \(openai\)/.test(builder), 'where the builder can run: ' + builder);
      assert(/none: the routing accepts the builder’s family/.test(await text(page, '#panel-runs .pl-route[data-agent="reviewer-security"]')), 'an accepted alternate in the snapshot');
      await page.evaluate(() => { document.querySelector('#panel-runs').scrollTop = 0; });
      await t.areaShot(page, AREA, 'runs-tab-desktop');
      await page.evaluate(() => { const el = document.querySelector('#panel-runs .pl-runs'); if (el) el.scrollIntoView(); });
      await t.areaShot(page, AREA, 'runs-tab-rows-desktop');
    });

    await step(AREA + ': Live run headers carry the reason badge, the placement chips, the independence chip and the run’s cost; a new run’s cost arrives live', async () => {
      await page.click('#tab-live');
      await page.waitForSelector('#panel-live .run[data-run="10-reviewer-ui"] .run-ext .pl-ind-reduced');
      const head = await text(page, '#panel-live .run[data-run="02-builder"] .run-head');
      assert(/Cost/.test(head) && /build 2 of 2/.test(head) && /model swap/.test(head) && /lease L-7f3a9c/.test(head) && /\$0/.test(head), 'the builder’s run header: ' + head);
      assert(/Independent alternate/.test(await text(page, '#panel-live .run[data-run="11-reviewer-code"] .run-head')), 'the alternate on its run header');
      const before = await text(page, '#drawer-head [data-meta="cost"]');
      const r = await page.request.post(url + '/__mock/routing/run', { data: { repo: 'my-app', job: '011-cache-the-pricing-page', agent: 'reviewer-code', round: 2, model: 'claude', input_tokens: 400000, output_tokens: 9000 } });
      assert(r.status() === 200, 'mock run ' + r.status());
      const run = (await r.json()).run;
      await page.waitForSelector(`#panel-live .run[data-run="${run}"] .run-ext .pl-cost`, { timeout: 15000 });
      assert(/^\$\d+\.\d\d$/.test((await text(page, `#panel-live .run[data-run="${run}"] .run-ext .pl-cost`)).trim()), 'agent.end’s cost on the header: ' + await text(page, `#panel-live .run[data-run="${run}"] .run-ext .pl-cost`));
      await page.waitForFunction((b) => { const el = document.querySelector('#drawer-head [data-meta="cost"]'); return el && el.textContent !== b; }, before, { timeout: 15000 });
      await page.click('#tab-runs');
      await page.waitForSelector(`#panel-runs .pl-run[data-run="${run}"]`);
      await page.click('#tab-live');
      // a run whose model call GPU custody refused (DESIGN §39): named on its header, the error in full in its tooltip
      const refusal = 'model call failed: GPU custody: LEASED (gpu0 is lent to <b>another</b> holder until 09:30)';
      const f = await page.request.post(url + '/__mock/routing/run', { data: { repo: 'my-app', job: '011-cache-the-pricing-page', agent: 'reviewer-security', round: 2, error: refusal } });
      assert(f.status() === 200, 'mock failed run ' + f.status());
      const frun = (await f.json()).run;
      await page.waitForSelector(`#panel-live .run[data-run="${frun}"] .run-ext [data-mark="custody-refused"]`, { timeout: 15000 });
      const ref = page.locator(`#panel-live .run[data-run="${frun}"] .run-ext [data-mark="custody-refused"]`);
      assert(/GPU custody refused/.test(await ref.textContent()) && await ref.getAttribute('title') === refusal && await page.locator('#panel-live b').count() === 0, 'the refusal on the run header, as text');
      // the two other custody failures (custody.lease_problem, harness/providers.py) are named apart from a refusal
      for (const [error, label] of [['model call failed: GPU custody: the lease taken for tabby/qwen3.8-flash-next at placement is no longer valid: lost (the register restarted)', 'lease no longer valid'],
        ['model call failed: connection reset; GPU custody: the outcome is unknown, so the request was not replayed (residency read again before the next call)', 'cut off under a lease']]) {
        const c = await page.request.post(url + '/__mock/routing/run', { data: { repo: 'my-app', job: '011-cache-the-pricing-page', agent: 'reviewer-security', round: 2, error } });
        const crun = (await c.json()).run;
        await page.waitForSelector(`#panel-live .run[data-run="${crun}"] .run-ext [data-mark="custody-refused"]`, { timeout: 15000 });
        const chip = (await text(page, `#panel-live .run[data-run="${crun}"] .run-ext [data-mark="custody-refused"]`)).trim();
        assert(chip === label, 'the chip names what happened: ' + chip + ' (expected ' + label + ')');
        const heard = await page.$$eval(`#panel-live .run[data-run="${crun}"] .run-ext .sr-only`, (els) => els.map((e) => e.textContent).join(' | '));
        assert(heard.includes(label + ': ' + error), 'and a screen reader hears the error: ' + heard);
      }
      // a HELD answer used up the run's one wait: the refusal reads as it is (DESIGN §37)
      const noWait = 'model call failed: GPU custody: the card is held by someone else; no wait left (the run waited out a HELD answer for up to 900s already: one wait per placement)';
      const nw = await page.request.post(url + '/__mock/routing/run', { data: { repo: 'my-app', job: '011-cache-the-pricing-page', agent: 'reviewer-security', round: 2, error: noWait } });
      const nwrun = (await nw.json()).run;
      await page.waitForSelector(`#panel-live .run[data-run="${nwrun}"] .run-ext [data-mark="custody-refused"]`, { timeout: 15000 });
      assert((await text(page, `#panel-live .run[data-run="${nwrun}"] .run-ext [data-mark="custody-refused"]`)).trim() === 'GPU custody refused: no wait left' &&
        await page.getAttribute(`#panel-live .run[data-run="${nwrun}"] .run-ext [data-mark="custody-refused"]`, 'title') === noWait, 'no wait left, as it is');
      // agent.waiting {custody: "HELD"}: waiting for the GPU custody register, not for a slot
      const held = await page.request.post(url + '/__mock/routing/held', { data: { repo: 'my-app', job: '011-cache-the-pricing-page', agent: 'reviewer-code', round: 2 } });
      const hrun = (await held.json()).run;
      await page.waitForFunction((r) => { const el = document.querySelector('#panel-live .run[data-run="' + r + '"] .run-status'); return el && /waiting for the GPU custody register: the card is held by someone else/.test(el.textContent); }, hrun, { timeout: 15000 });
      assert(!/slot/.test(await text(page, `#panel-live .run[data-run="${hrun}"] .run-status`)), 'not a slot');
      assert(/^Queued: waiting for the GPU custody register: the card is held by someone else…$/.test((await text(page, `#panel-live .run[data-run="${hrun}"] .run-empty`)).trim()), 'its body says what it waits for');
      await page.waitForFunction(() => /is waiting for the GPU custody register: the card is held by someone else/.test(document.getElementById('drawer-head').textContent), null, { timeout: 15000 });
      await t.areaShot(page, AREA, 'live-held-desktop');
      await page.request.post(url + '/__mock/routing/held-end', { data: {} });
      await page.waitForFunction(() => /Needs human/i.test(document.getElementById('drawer-head').textContent), null, { timeout: 15000 });
      await page.evaluate((r) => { const el = document.querySelector('#panel-live .run[data-run="' + r + '"]'); if (el) el.scrollIntoView({ block: 'center' }); }, frun);
      await t.areaShot(page, AREA, 'live-headers-desktop');
      await page.keyboard.press('Escape');
    });

    await step(AREA + ': New feature: the profile picker and the estimate (total, basis, unpriced runs, what the guard would do); the profile is posted, and Add to backlog refuses one', async () => {
      await page.goto(url + '/#/factory');
      await page.waitForSelector('.board .card');
      await page.click('#new-feature-btn');
      await page.waitForSelector('dialog[open] #pl-prof-feature');
      await page.waitForSelector('dialog[open] .pl-est .pl-est-total');
      assert(/Default: balanced/.test(await page.$eval('dialog[open] #pl-prof-feature', (s) => s.options[s.selectedIndex].textContent)), 'the default profile is named');
      assert(/≈ \$/.test(await text(page, 'dialog[open] .pl-est-cost')) && /about /.test(await text(page, 'dialog[open] .pl-est-total')), 'a total cost and time');
      const stages = await page.$$eval('dialog[open] .pl-est-stages .pl-est-stage', (els) => els.map((e) => e.dataset.stage));
      assert(canon(stages) === canon(['spec', 'build', 'review', 'approve']), 'the time per stage, in order: ' + stages);
      const u1 = await waitGet(page, /^\/api\/estimate/, () => page.selectOption('dialog[open] #pl-prof-feature', 'local-first'));
      assert(u1.searchParams.get('profile') === 'local-first' && u1.searchParams.get('kind') === 'feature', 'the estimate follows the profile: ' + u1.search);
      await page.waitForSelector('dialog[open] .pl-est [data-unpriced]');
      assert(/The cost leaves out unpriced runs: no prices for gemini-argon/.test(await text(page, 'dialog[open] .pl-est [data-unpriced]')), 'the unpriced note names the model');
      assert(/If the builder runs on gpt-sol \(openai\): reviewer-code moves to muse-spark/.test(await text(page, 'dialog[open] .pl-est-ind')), 'what the guard would do: ' + await text(page, 'dialog[open] .pl-est-ind'));
      await page.click('dialog[open] .pl-est-more > summary');
      await page.waitForSelector('dialog[open] .pl-est-row[data-agent="approver"]');
      const apRow = await text(page, 'dialog[open] .pl-est-row[data-agent="approver"]');
      assert(/claude/.test(apRow) && /Capability/.test(apRow) && /the merge gate on a <b>strong<\/b> API model/.test(apRow) && await page.locator('dialog[open] .pl-est b').count() === 0, 'a per-step row, its reason as text: ' + apRow);
      assert(/median of \d+ past jobs|default table/.test(await text(page, 'dialog[open] .pl-est-row[data-agent="approver"] .pl-est-rbasis')), 'each row says what it is based on');
      assert(/accepted/i.test(await text(page, 'dialog[open] .pl-est-row[data-agent="reviewer-security"]')), 'a judge sharing the family by choice is marked on its row');
      const u2 = await waitGet(page, /^\/api\/estimate/, () => page.selectOption('dialog[open] select[name="routing"]', 'preset:hybrid'));
      assert(u2.searchParams.get('routing') === 'hybrid', 'the estimate follows a preset routing: ' + u2.search);
      await page.selectOption('dialog[open] select[name="routing"]', '');
      await page.fill('dialog[open] #nf-feature', 'Show <b>prices</b> per seat on the pricing page');
      await page.waitForTimeout(300);
      await t.areaShot(page, AREA, 'new-feature-desktop');
      t.allowHttp(true);
      await page.click('dialog[open] button[data-action="backlog"]');
      await page.waitForSelector('dialog[open] .form-error:not([hidden]):has-text("Backlog items run with the default profile")');
      t.allowHttp(false);
      const resp = await waitPost(page, /^\/api\/jobs$/, () => page.click('dialog[open] button[data-action="run"]'));
      const body = JSON.parse(resp.request().postData());
      assert(resp.status() === 201 && body.profile === 'local-first' && body.routing === undefined, 'the profile is posted: ' + JSON.stringify(body));
      const job = (await resp.json()).job;
      assert(job.profile === 'local-first' && job.routing.builder.model === 'local', 'the job runs on the profile’s placement');
      await page.waitForSelector('dialog[open]', { state: 'detached' });
    });

    await step(AREA + ': New plan, New chat, Hand off and Change routing pick a profile too; the plan and the chat show their profile and cost', async () => {
      await page.goto(url + '/#/plans');
      await page.click('[data-action="new-plan"]');
      await page.waitForSelector('dialog[open] #pl-prof-plan');
      await page.waitForSelector('dialog[open] .pl-est[data-est="plan"] .pl-est-total');
      assert(/Estimate for the plan/.test(await text(page, 'dialog[open] .pl-est-title')) && /a plan of 3 jobs/.test(await text(page, 'dialog[open] .pl-est-basis')), 'a plan’s estimate: ' + await text(page, 'dialog[open] .pl-est-basis'));
      assert(/lead/.test(await page.$eval('dialog[open] .pl-est-stages', (e) => e.textContent)) && /× 3 jobs/.test(await text(page, 'dialog[open] .pl-est-stages')), 'a plan’s stages: the lead, then its jobs');
      await page.selectOption('dialog[open] #pl-prof-plan', ':none');
      await page.fill('dialog[open] #np-request', 'Seat-based pricing: data model, API and the pricing page.');
      await page.waitForTimeout(250);
      await t.areaShot(page, AREA, 'new-plan-desktop');
      const pr = await waitPost(page, /^\/api\/plans$/, () => page.click('dialog[open] button[data-action="ok"]'));
      assert(pr.status() === 201 && JSON.parse(pr.request().postData()).profile === '', 'No profile posts "": ' + pr.request().postData());
      const plan = (await pr.json()).plan;
      assert(plan.profile === null, 'the plan has no profile');
      await page.waitForFunction((h) => location.hash === h, '#/plans/my-app/' + plan.id);
      await page.waitForSelector('.plan-detail [data-meta="profile"]');
      assert(/no profile/.test(await text(page, '.plan-detail [data-meta="profile"]')) && /lead cost \$0/.test(await text(page, '.plan-detail [data-meta="cost"]')), 'the plan’s profile and the lead’s cost');
      await page.waitForFunction(() => { const el = document.querySelector('.plan-detail [data-meta="cost"]'); return el && !/lead cost \$0$/.test(el.textContent.trim()); }, null, { timeout: 20000 });
      assert(/lead cost (<\$0\.01|\$\d+\.\d\d)/.test(await text(page, '.plan-detail [data-meta="cost"]')), 'the lead’s cost arrives live (plan.updated): ' + await text(page, '.plan-detail [data-meta="cost"]'));
      await page.goto(url + '/#/plans/my-app/P001-csv-export-for-reports');
      await page.waitForSelector('.plan-detail [data-meta="cost"]');
      assert(/profile balanced/.test(await text(page, '.plan-detail [data-meta="profile"]')) && /lead cost \$0\.\d\d/.test(await text(page, '.plan-detail [data-meta="cost"]')), 'a finished plan’s lead cost');
      await t.areaShot(page, AREA, 'plan-head-desktop');
      // New chat
      await page.goto(url + '/#/chat');
      await page.click('#new-chat-btn');
      await page.waitForSelector('dialog[open] #pl-prof-chat:not([hidden])', { state: 'attached' });
      await page.waitForFunction(() => { const s = document.querySelector('dialog[open] #pl-prof-chat'); return s && s.options.length > 2; });
      await page.selectOption('dialog[open] #pl-prof-chat', 'fastest');
      assert(/goes over the profile’s/.test(await text(page, 'dialog[open] .pl-prof-hint')), 'an explicit model goes over the profile');
      await t.areaShot(page, AREA, 'new-chat-desktop');
      const cr = await waitPost(page, /^\/api\/chats$/, () => page.click('dialog[open] button[data-action="ok"]'));
      assert(cr.status() === 201 && JSON.parse(cr.request().postData()).profile === 'fastest', 'the chat’s profile is posted: ' + cr.request().postData());
      assert((await cr.json()).chat.profile === 'fastest', 'chat.profile');
      await page.waitForSelector('.chat-head [data-meta="profile"]');
      assert(/profile fastest/.test(await text(page, '.chat-head [data-meta="profile"]')) && /cost \$0/.test(await text(page, '.chat-head [data-meta="cost"]')), 'the chat header');
      await page.waitForSelector('.composer textarea:not([disabled])');
      await page.fill('.composer textarea', 'What does the pricing cache key on?');
      await waitPost(page, /\/messages$/, () => page.press('.composer textarea', 'Enter'));
      await page.waitForFunction(() => { const el = document.querySelector('.chat-head [data-meta="cost"]'); return el && !/cost \$0$/.test(el.textContent.trim()); }, null, { timeout: 20000 });
      assert(/cost (<\$0\.01|\$\d+\.\d\d)/.test(await text(page, '.chat-head [data-meta="cost"]')), 'the chat’s cost arrives live (chat.updated): ' + await text(page, '.chat-head [data-meta="cost"]'));
      await page.waitForSelector('.composer textarea:not([disabled])', { timeout: 20000 });
      // An older chat: its earlier session and hand-off
      await page.goto(url + '/#/chat/c-1a2b3c4d5e');
      await page.waitForSelector('.chat-head [data-meta="sessions"]');
      assert(/1 earlier session/.test(await text(page, '.chat-head [data-meta="sessions"]')) && /profile balanced/.test(await text(page, '.chat-head [data-meta="profile"]')), 'the chat’s earlier session');
      await t.areaShot(page, AREA, 'chat-head-desktop');
      await page.click('.chat-head-actions button:has-text("Hand off")');
      await page.waitForSelector('dialog[open] #pl-prof-handoff');
      await page.waitForFunction(() => document.querySelector('dialog[open] #pl-prof-handoff').options.length > 2);
      await page.selectOption('dialog[open] #pl-prof-handoff', 'local-first');
      const hr = await waitPost(page, /\/handoff$/, () => page.click('dialog[open] button[data-action="ok"]'));
      const hjob = (await hr.json()).job;
      assert(JSON.parse(hr.request().postData()).profile === 'local-first' && hjob.profile === 'local-first' && Array.isArray(hjob.handoff_models) && hjob.handoff_models.length === 2,
        'the hand-off posts the profile and the job records the chat’s models: ' + JSON.stringify(hjob.handoff_models));
      await page.goto(url + '/#/job/' + hjob.repo + '/' + hjob.id);
      await page.waitForSelector('#drawer-head [data-handoff]');
      assert(/Written in the chat on:/.test(await text(page, '#drawer-head [data-handoff]')) && /local · qwen/.test(await text(page, '#drawer-head [data-handoff]')), 'Written in the chat on');
      await page.keyboard.press('Escape');
      // Change routing (Factory.set_routing; tests/test_gui_routing_bodies.py runs these bodies on the real Factory)
      const CR = 'my-app/002-rate-limit-signup-form';
      await page.goto(url + '/#/job/' + CR + '/routing');
      await page.waitForSelector('#panel-routing [data-action="routing"]');
      const routingPosts = [];
      const onReq = (r) => { if (r.method() === 'POST' && /\/routing$/.test(new URL(r.url()).pathname)) routingPosts.push(r.postData()); };
      page.on('request', onReq);
      const openCR = async (alts) => {
        await page.waitForSelector('dialog[open]', { state: 'detached' });
        await page.click('#panel-routing [data-action="routing"]');
        await page.waitForSelector('dialog[open] #pl-prof-routing');
        await page.waitForFunction(() => document.querySelector('dialog[open] #pl-prof-routing').options.length > 2);
        if (alts) {
          await page.click('dialog[open] .pl-alts > summary');
          await page.waitForFunction(() => { const x = document.querySelector('dialog[open] select[aria-label="approver independent alternate"]'); return x && x.options.length > 2; });
        }
      };
      const post = async () => {
        const r = await waitPost(page, /\/routing$/, () => page.click('dialog[open] button[data-action="ok"]'));
        assert(r.status() === 200, 'Change routing: ' + r.status() + ' ' + r.request().postData());
        return { body: JSON.parse(r.request().postData()), job: (await r.json()).job };
      };
      // 1. Custom with a new profile: every step is placed again from the saved settings with it
      await openCR(true);
      assert(/Keep the job’s: balanced/.test(await page.$eval('dialog[open] #pl-prof-routing', (x) => x.options[0].textContent)), 'Change routing keeps the job’s profile by default');
      assert(/Keep: claude/.test(await page.$eval('dialog[open] select[aria-label="reviewer-code independent alternate"]', (x) => x.options[0].textContent)), 'each judge keeps its snapshot alternate by default');
      await page.selectOption('dialog[open] select[aria-label="builder driver"]', 'qwen');
      await page.selectOption('dialog[open] #pl-prof-routing', 'fastest');
      assert(/Every step is placed again from your saved settings with this profile \(not only the steps you change\)/.test(await text(page, 'dialog[open] .pl-prof-hint')), 'the profile hint says it re-routes every step: ' + await text(page, 'dialog[open] .pl-prof-hint'));
      assert(/As the profile places it/.test(await page.$eval('dialog[open] select[aria-label="reviewer-code independent alternate"]', (x) => x.options[0].textContent)), 'with a profile, “Keep” does not promise the snapshot’s alternate');
      await page.selectOption('dialog[open] select[aria-label="reviewer-code independent alternate"]', 'false');
      await page.waitForTimeout(120);
      await t.areaShot(page, AREA, 'change-routing-desktop');
      const c1 = await post();
      assert(c1.body.profile === 'fastest' && c1.body.routing.builder.driver === 'qwen' && canon(c1.body.routing['reviewer-code']) === canon({ independent_alternate: false }) && c1.job.profile === 'fastest',
        'the profile, the driver and the alternate alone (the profile places the judge): ' + JSON.stringify(c1.body));
      assert(c1.job.routing['reviewer-code'].model === 'claude' && c1.job.routing['reviewer-code'].independent_alternate === false && c1.job.routing['reviewer-security'].model === 'haiku',
        'the profile placed every step, the ones left alone too: ' + JSON.stringify({ code: c1.job.routing['reviewer-code'].model, security: c1.job.routing['reviewer-security'].model }));
      // 2. Custom, only an alternate: the judge's route on its snapshot model, with the snapshot's reason
      const ap1 = c1.job.routing.approver;
      await openCR(true);
      await page.selectOption('dialog[open] select[aria-label="approver independent alternate"]', 'muse-spark');
      const c2 = await post();
      assert(canon(Object.keys(c2.body.routing)) === canon(['approver']) && c2.body.routing.approver.independent_alternate === 'muse-spark' && c2.body.routing.approver.model === ap1.model && c2.body.profile === undefined,
        'only the judge whose alternate changed: ' + JSON.stringify(c2.body));
      assert(canon(c2.body.routing.approver.reason) === canon(ap1.reason) && canon(c2.job.routing.approver.reason) === canon(ap1.reason) && c2.job.routing.approver.independent_alternate.model === 'muse-spark',
        'its reason goes with it, and the snapshot keeps it: ' + JSON.stringify(c2.job.routing.approver.reason));
      // 3. Custom moves the judge to another model; "Keep" keeps its alternate when it is of another family
      await openCR(false);
      await page.selectOption('dialog[open] select[aria-label="approver model"]', 'gpt-sol');
      const c3 = await post();
      assert(c3.body.routing.approver.model === 'gpt-sol' && c3.body.routing.approver.independent_alternate === 'muse-spark' && c3.job.routing.approver.independent_alternate.model === 'muse-spark',
        'Keep: the alternate goes with the new model: ' + JSON.stringify(c3.body));
      // ... onto the alternate's own family: the dialog says why, and nothing is sent
      await openCR(false);
      await page.selectOption('dialog[open] select[aria-label="approver model"]', 'muse-spark');
      const n0 = routingPosts.length;
      await page.click('dialog[open] button[data-action="ok"]');
      await page.waitForSelector('dialog[open] .form-error:not([hidden])');
      const famErr = await text(page, 'dialog[open] .form-error');
      assert(/approver’s independent alternate muse-spark is of the \S+ family, like its new model muse-spark/.test(famErr) && routingPosts.length === n0, 'a kept alternate of the new model’s family: ' + famErr);
      assert(await page.$eval('dialog[open] .pl-alts', (d) => d.open) && await page.evaluate(() => document.activeElement && document.activeElement.getAttribute('aria-label')) === 'approver independent alternate', 'the alternates open, on the judge’s field');
      await page.click('dialog[open] button[data-action="cancel"]');
      // 4. Saved settings: null, so each step keeps what Settings › Routing gives it (agents.<name>'s placement)
      await openCR(false);
      await page.selectOption('dialog[open] select[name="routing"]', ':saved');
      const c4 = await post();
      assert(c4.body.routing === null && !('profile' in c4.body), 'Saved settings sends null: ' + JSON.stringify(c4.body));
      assert(c4.job.routing.approver.independent_alternate.model === 'claude' && c4.job.routing.approver.reason.kind === 'capability' && c4.job.profile === 'fastest',
        'the saved alternate and reason are back; the job keeps its profile: ' + JSON.stringify(c4.job.routing.approver.reason));
      await page.waitForSelector('.toast :text("your saved settings")');
      // 5. Only a profile (Custom changes nothing): the saved settings with that profile
      await openCR(false);
      await page.selectOption('dialog[open] #pl-prof-routing', 'local-first');
      const c5 = await post();
      assert(c5.body.routing === null && c5.body.profile === 'local-first' && c5.job.profile === 'local-first' && c5.job.routing.builder.model === 'local',
        'only a profile: ' + JSON.stringify(c5.body) + ' → builder on ' + c5.job.routing.builder.model);
      // Custom with nothing changed and no profile: the chooser says what to do (this dialog has no Default)
      await openCR(false);
      const n1 = routingPosts.length;
      await page.click('dialog[open] button[data-action="ok"]');
      await page.waitForSelector('dialog[open] .form-error:not([hidden])');
      const emptyErr = await text(page, 'dialog[open] .form-error');
      assert(/pick a routing profile, or choose Saved settings/.test(emptyErr) && !/choose Default/.test(emptyErr) && routingPosts.length === n1, 'the empty-Custom message fits this dialog: ' + emptyErr);
      await page.click('dialog[open] button[data-action="cancel"]');
      page.off('request', onReq);
      await page.waitForSelector('dialog[open]', { state: 'detached' });
      await page.keyboard.press('Escape');
    });

    await step(AREA + ': Start: the Routing step offers the profile; the review shows it, the estimate and the exact body', async () => {
      await page.goto(url + '/#/start');
      await page.waitForSelector('.wz-main .opt-card[data-opt="feature"]');
      await page.click('.wz-main .opt-card[data-opt="feature"]');
      await page.click('.wz-foot [data-action="wz-next"]');
      await page.waitForSelector('.wz-main .opt-card[data-opt="my-app"]');
      await page.click('.wz-main .opt-card[data-opt="my-app"]');
      await page.click('.wz-foot [data-action="wz-next"]');
      await page.waitForSelector('#wz-describe-text');
      await page.fill('#wz-describe-text', 'Cache the seat price list');
      await page.click('.wz-foot [data-action="wz-next"]');
      await page.waitForSelector('#wz-routing-profile');
      await page.selectOption('#wz-routing-profile', 'profile:local-first');
      await page.waitForTimeout(150);
      await t.areaShot(page, AREA, 'start-routing-desktop');
      await page.click('.wz-foot [data-action="wz-next"]');
      await page.waitForSelector('.wz-main .opt-card[data-opt="merge"]');
      await page.click('.wz-foot [data-action="wz-next"]');
      await page.waitForSelector('.wz-run .pl-est .pl-est-total');
      const raw = await text(page, '.wz-raw pre');
      assert(raw.includes('"profile": "local-first"'), 'the review body carries the profile: ' + raw);
      assert(/local-first/.test(await text(page, '.wz-kv')), 'the review names the profile');
      assert(/profile local-first/.test(await text(page, '.wz-run .pl-est-head')), 'the estimate is the profile’s');
      await t.areaShot(page, AREA, 'start-review-desktop');
      // a chat from Start: its profile is a field of the conversation step
      await page.goto(url + '/#/start/what');
      await page.waitForSelector('.wz-main .opt-card[data-opt="chat"]');
      await page.click('.wz-main .opt-card[data-opt="chat"]');
      await page.click('.wz-foot [data-action="wz-next"]');
      await page.waitForSelector('.wz-main .opt-card[data-opt="my-app"]');
      await page.click('.wz-main .opt-card[data-opt="my-app"]');
      await page.click('.wz-foot [data-action="wz-next"]');
      await page.waitForSelector('#wz-describe-profile');
      await page.selectOption('#wz-describe-profile', 'profile:fastest');
      await t.areaShot(page, AREA, 'start-chat-desktop');
      await page.click('.wz-foot [data-action="wz-next"]');
      await page.waitForSelector('.wz-raw pre', { state: 'attached' });
      const craw = await page.$eval('.wz-raw pre', (e) => e.textContent);
      assert(craw.includes('POST /api/chats') && craw.includes('"profile": "fastest"') && /fastest/.test(await text(page, '.wz-kv')), 'the chat’s body carries the profile: ' + craw);
    });

    await step(AREA + ': Settings: Profiles (default, placements), a judge’s placement, model and provider placement; the server’s 400 shows verbatim', async () => {
      await page.goto(url + '/#/settings/profiles');
      await page.waitForSelector('#set-profiles .pl-profile[data-profile="local-first"]');
      assert(await page.locator('.settings-nav a[data-sec="profiles"]').count() === 1, 'Profiles is a Settings section');
      assert(await page.inputValue('#pl-default-profile') === 'balanced', 'the default profile');
      await page.selectOption('#pl-default-profile', 'local-first');
      assert(/Unsaved changes in Profiles$/.test((await text(page, '.save-bar .save-msg')).trim()) && await page.locator('.settings-nav a[data-sec="profiles"] .dirty-dot:not([hidden])').count() === 1,
        'the default profile is a change in Profiles: ' + await text(page, '.save-bar .save-msg'));
      const s1 = await waitPost(page, /^\/api\/config$/, () => page.click('.save-bar button:has-text("Save changes")'));
      assert(s1.status() === 200 && canon(JSON.parse(s1.request().postData())) === canon({ default_profile: 'local-first' }), 'only the default is sent: ' + s1.request().postData());
      await page.waitForSelector('.toast-ok :text("Settings saved")');
      // a placement with an effort Claude does not take: the server says why, as it wrote it
      const prof = page.locator('#set-profiles .pl-profile[data-profile="local-first"]');
      if (!(await prof.getAttribute('open') !== null)) await prof.locator('summary').click();
      const effs = await page.$$eval('#set-profiles select[aria-label="local-first approver effort"] option', (os) => os.map((o) => o.value));
      assert(!effs.includes('minimal') && effs.includes('max'), 'Claude (Anthropic format) is offered low to max: ' + effs);
      await page.selectOption('#set-profiles select[aria-label="local-first builder effort"]', 'minimal');
      await page.selectOption('#set-profiles select[aria-label="local-first builder model"]', 'claude');
      await page.waitForFunction(() => { const s = document.querySelector('#set-profiles select[aria-label="local-first builder effort"]'); return s && /not taken by claude/.test(s.options[s.selectedIndex].textContent); });
      t.allowHttp(true);
      const bad = await waitPost(page, /^\/api\/config$/, () => page.click('.save-bar button:has-text("Save changes")'));
      assert(bad.status() === 400, 'expected 400, got ' + bad.status());
      await page.waitForSelector('#set-profiles .form-error');
      const msg = await text(page, '#set-profiles .form-error');
      assert(/profiles\.local-first\.agents\.builder on model claude: effort 'minimal' is not taken by Anthropic-format providers/.test(msg), 'the 400 verbatim on the card: ' + msg);
      t.allowHttp(false);
      await page.evaluate(() => { const el = document.getElementById('set-profiles'); if (el) el.scrollIntoView(); });
      await page.waitForTimeout(150);
      await t.areaShot(page, AREA, 'settings-profiles-desktop');
      await page.click('.save-bar button:has-text("Discard")');
      // keyboard: moving through "Step to add" adds nothing (a closed select fires change on the arrow keys); the button adds
      const fast = page.locator('#set-profiles .pl-profile[data-profile="fastest"]');
      if (await fast.getAttribute('open') === null) await fast.locator('summary').click();
      const stepsOf = () => page.$$eval('#set-profiles .pl-profile[data-profile="fastest"] .pl-place', (els) => els.map((e) => e.dataset.agent));
      const steps0 = await stepsOf();
      await page.focus('#set-profiles select[aria-label="Step to add to fastest"]');
      await page.keyboard.press('ArrowDown');
      await page.waitForTimeout(150);
      const chosen = await page.inputValue('#set-profiles select[aria-label="Step to add to fastest"]');
      assert(chosen && canon(await stepsOf()) === canon(steps0), 'an arrow key only chooses (' + chosen + '), it adds no step: ' + await stepsOf());
      await page.click('#set-profiles button[aria-label="Add step to fastest"]');
      await page.waitForFunction((n) => document.querySelectorAll('#set-profiles .pl-profile[data-profile="fastest"] .pl-place').length === n, steps0.length + 1);
      assert((await stepsOf()).includes(chosen), 'Add step adds the chosen one');
      // a step's model commits without re-rendering the page: the arrow keys keep going, its choices follow the model
      await page.evaluate(() => { document.getElementById('set-profiles').dataset.mark = 'kept'; });
      await page.focus('#set-profiles select[aria-label="fastest builder model"]');
      await page.keyboard.press('ArrowDown');
      await page.keyboard.press('ArrowDown');
      await page.waitForTimeout(150);
      assert(await page.evaluate(() => document.getElementById('set-profiles') && document.getElementById('set-profiles').dataset.mark) === 'kept' &&
        await page.evaluate(() => document.activeElement && document.activeElement.getAttribute('aria-label')) === 'fastest builder model', 'no page re-render; the focus stays on the model');
      await page.click('.save-bar button:has-text("Discard")');
      // a judge's placement in Settings › Routing: the alternate lists models of another family, with "accept"
      await page.goto(url + '/#/settings/routing');
      await page.waitForSelector('#set-routing .rt-box[data-agent="reviewer-code"] .pl-adv-btn');
      const btn = page.locator('#set-routing .rt-box[data-agent="reviewer-code"] .pl-adv-btn');
      assert(/stage fit/.test(await btn.textContent()) && await btn.getAttribute('aria-expanded') === 'false', 'the Placement button says what is set: ' + await btn.textContent());
      await btn.click();
      assert(await btn.getAttribute('aria-expanded') === 'true', 'it opens');
      const opts = await page.$$eval('#set-routing select[aria-label="reviewer-code independent alternate"] option', (os) => os.map((o) => o.value));
      assert(opts.includes('false') && opts.includes('claude') && !opts.includes('sonnet') && !opts.includes('local'), 'other families only, plus None and accept: ' + opts);
      assert(await page.locator('#set-routing .rt-box[data-agent="builder"] select[aria-label="builder independent alternate"]').count() === 0, 'only judges have an alternate');
      await page.selectOption('#set-routing select[aria-label="reviewer-code independent alternate"]', 'false');
      await page.click('#set-routing .rt-box[data-agent="approver"] .pl-adv-btn');
      await page.selectOption('#set-routing select[aria-label="approver effort"]', 'xhigh');
      const s2 = await waitPost(page, /^\/api\/config$/, () => page.click('.save-bar button:has-text("Save changes")'));
      assert(canon(JSON.parse(s2.request().postData())) === canon({ agents: { 'reviewer-code': { independent_alternate: false }, approver: { effort: 'xhigh' } } }), 'the route placement patch: ' + s2.request().postData());
      await page.waitForSelector('.toast-ok :text("Settings saved")');
      await page.evaluate(() => { const el = document.querySelector('#set-routing .rt-box[data-agent="reviewer-code"]'); if (el) el.scrollIntoView({ block: 'center' }); });
      await page.waitForTimeout(150);
      await t.areaShot(page, AREA, 'settings-route-placement-desktop');
      // a judge's Placement follows its box's model: the choices are built again, and an alternate of the new model's
      // own family is flagged before the save (the server would refuse it)
      if (await page.getAttribute('#set-routing .rt-box[data-agent="approver"] .pl-adv-btn', 'aria-expanded') !== 'true') await page.click('#set-routing .rt-box[data-agent="approver"] .pl-adv-btn');
      await page.selectOption('#set-routing select[aria-label="approver model"]', 'claude');
      await page.waitForFunction(() => { const g = document.querySelector('#set-routing select[aria-label="approver independent alternate"] optgroup'); return g && /another family than claude/.test(g.label); });
      const effs2 = await page.$$eval('#set-routing select[aria-label="approver effort"] option', (os) => os.map((o) => o.value));
      assert(!effs2.includes('minimal'), 'claude is offered low to max now: ' + effs2);
      await page.waitForSelector('#set-routing [data-err-for="agents.approver.independent_alternate"]:not([hidden])');
      const altMsg = await text(page, '#set-routing [data-err-for="agents.approver.independent_alternate"]');
      assert(/claude is of the \S+ family, like claude/.test(altMsg), 'the kept alternate is flagged in words: ' + altMsg);
      assert(await page.isDisabled('.save-bar button:has-text("Save changes")') && /“approver independent alternate”/.test(await text(page, '.save-bar .save-invalid')), 'the save waits, and the bar names the field: ' + await text(page, '.save-bar .save-invalid'));
      await page.evaluate(() => { const el = document.querySelector('#set-routing .rt-box[data-agent="approver"]'); if (el) el.scrollIntoView({ block: 'center' }); });
      await t.areaShot(page, AREA, 'settings-route-model-change-desktop');
      await page.click('.save-bar button:has-text("Discard")');
      // a reason line typed before its kind is kept, and says what it needs
      await page.click('#set-routing .rt-box[data-agent="builder"] .pl-adv-btn');
      await page.fill('#set-routing input[aria-label="builder reason text"]', 'free on the local GPU');
      await page.waitForSelector('#set-routing [data-err-for="agents.builder.reason"]:not([hidden])');
      const rdesc = await page.getAttribute('#set-routing input[aria-label="builder reason text"]', 'aria-describedby');
      assert(/Pick what kind of reason/.test(await text(page, '#set-routing [data-err-for="agents.builder.reason"]')) && rdesc && await page.locator('#' + rdesc.split(' ').pop() + '[data-err-for="agents.builder.reason"]').count() === 1,
        'the line waits for a kind, and its input points at why');
      await page.selectOption('#set-routing select[aria-label="builder reason"]', 'cost');
      await page.waitForSelector('#set-routing [data-err-for="agents.builder.reason"]', { state: 'hidden' });
      assert(await page.inputValue('#set-routing input[aria-label="builder reason text"]') === 'free on the local GPU' && !(await page.isDisabled('.save-bar button:has-text("Save changes")')), 'the line is kept with its kind');
      await page.click('.save-bar button:has-text("Discard")');
      // model placement: prices for an unpriced model, a family, a build; a lone price_in is refused verbatim
      await page.goto(url + '/#/settings/models');
      await page.waitForSelector('#set-models .mdl-row[data-model="gemini-argon"] .pl-adv-btn');
      await page.click('#set-models .mdl-row[data-model="gemini-argon"] .pl-adv-btn');
      await page.fill('#set-models input[aria-label="gemini-argon price in"]', '1.25');
      t.allowHttp(true);
      const bad2 = await waitPost(page, /^\/api\/config$/, () => page.click('.save-bar button:has-text("Save changes")'));
      assert(bad2.status() === 400, 'a lone price is refused');
      await page.waitForSelector('#set-models .form-error:has-text("set price_in and price_out together")');
      t.allowHttp(false);
      await page.fill('#set-models input[aria-label="gemini-argon price out"]', '10');
      assert(/google family/.test(await page.getAttribute('#set-models input[aria-label="gemini-argon family"]', 'placeholder')) || /google/.test(await page.getAttribute('#set-models input[aria-label="gemini-argon family"]', 'placeholder')), 'the family field shows the resolved family');
      const s3 = await waitPost(page, /^\/api\/config$/, () => page.click('.save-bar button:has-text("Save changes")'));
      assert(s3.status() === 200 && canon(JSON.parse(s3.request().postData())) === canon({ models: { 'gemini-argon': { price_in: 1.25, price_out: 10 } } }), 'the price patch: ' + s3.request().postData());
      await page.waitForSelector('.toast-ok :text("Settings saved")');
      await page.click('#set-models .mdl-row[data-model="local"] .pl-adv-btn');
      await page.waitForSelector('#set-models .mdl-row[data-model="local"] .pl-build');
      assert(await page.locator('#set-models .mdl-row[data-model="local"] .pl-build').count() === 2, 'the local model’s two builds');
      assert(await page.inputValue('#set-models input[aria-label="local build 2 context tokens"]') === '262144' && await page.locator('#set-models input[aria-label="local build 2 max tokens"]').count() === 1, 'a build’s own limits');
      await page.fill('#set-models input[aria-label="local build 2 max tokens"]', '8192.5');
      assert(/whole number/.test(await page.getAttribute('#set-models input[aria-label="local build 2 max tokens"]', 'title')), 'a fraction is refused here, before the server');
      // ... in words under the field (a phone has no hover), linked to it, and named by the save bar
      const numErr = '#set-models [data-err-for="models.local.builds[1].max_tokens"]';
      await page.waitForSelector(numErr + ':not([hidden])');
      const mdesc = await page.getAttribute('#set-models input[aria-label="local build 2 max tokens"]', 'aria-describedby');
      assert(/whole number/i.test(await text(page, numErr)) && mdesc && await page.locator('#' + mdesc.split(' ').pop()).getAttribute('data-err-for') === 'models.local.builds[1].max_tokens', 'the error line, linked: ' + mdesc);
      assert(/“local build 2 max tokens”: A whole number above 0/.test(await text(page, '.save-bar .save-invalid')), 'the save bar names it: ' + await text(page, '.save-bar .save-invalid'));
      await page.evaluate(() => { const el = document.querySelector('#set-models .mdl-row[data-model="local"] .pl-builds'); if (el) el.scrollIntoView({ block: 'center' }); });
      await t.areaShot(page, AREA, 'settings-invalid-number-desktop');
      await page.fill('#set-models input[aria-label="local build 2 max tokens"]', '8192');
      const s5 = await waitPost(page, /^\/api\/config$/, () => page.click('.save-bar button:has-text("Save changes")'));
      const b5 = JSON.parse(s5.request().postData());
      assert(s5.status() === 200 && b5.models.local.builds[1].max_tokens === 8192 && b5.models.local.builds.length === 2, 'builds go as a whole list: ' + s5.request().postData());
      await page.waitForSelector('.toast-ok :text("Settings saved")');
      await page.waitForSelector('#set-models .mdl-row[data-model="local"] .pl-build');
      await page.evaluate(() => { const el = document.querySelector('#set-models .mdl-row[data-model="local"]'); if (el) el.scrollIntoView({ block: 'start' }); });
      await page.waitForTimeout(150);
      await t.areaShot(page, AREA, 'settings-model-placement-desktop');
      // provider placement: kind, swap, slots and the custody register
      await page.goto(url + '/#/settings/providers');
      await page.waitForSelector('#set-providers .prov-row[aria-label="Provider tabby"] .pl-adv-btn');
      await page.click('#set-providers .prov-row[aria-label="Provider tabby"] .pl-adv-btn');
      assert(await page.inputValue('#set-providers select[aria-label="tabby kind"]') === 'tabby' && await page.isChecked('#pl-flag-tabby-allow_model_swap'), 'Tabby’s kind and swap');
      const tabbyRow = page.locator('#set-providers .prov-row[aria-label="Provider tabby"]');
      assert(await tabbyRow.getByRole('checkbox', { name: 'May load another model on request (allow_model_swap)', exact: true }).count() === 1 &&
        await tabbyRow.getByRole('checkbox', { name: 'Loads its model on the first request (lazy_load)', exact: true }).count() === 1, 'the boxes are named by their visible labels');
      // the custody line follows the boxes as they stand (custody.needs_lease: lazy_load or allow_model_swap)
      await page.waitForSelector('#set-providers .prov-row[aria-label="Provider tabby"] .pl-custody-line');
      assert(/Its runs take a request lease/.test(await text(page, '#set-providers .prov-row[aria-label="Provider tabby"] .pl-custody-line')), 'tabby needs a lease');
      await page.uncheck('#pl-flag-tabby-allow_model_swap');
      assert(/No call here takes a lease .*after the save/.test(await text(page, '#set-providers .prov-row[aria-label="Provider tabby"] .pl-custody-line')), 'unticked: no lease after the save: ' + await text(page, '#set-providers .prov-row[aria-label="Provider tabby"] .pl-custody-line'));
      await page.check('#pl-flag-tabby-allow_model_swap');
      assert(/Its runs take a request lease\./.test(await text(page, '#set-providers .prov-row[aria-label="Provider tabby"] .pl-custody-line')), 'ticked again');
      assert(await page.inputValue('#set-providers input[aria-label="tabby custody url"]') === 'https://127.0.0.1:7300', 'the custody register');
      assert(/Contract proposed/.test(await text(page, '#set-providers .prov-row[aria-label="Provider tabby"] .pl-custody-set legend')), 'the contract is labelled proposed');
      await page.fill('#set-providers input[aria-label="tabby tokens per second"]', '95');
      await page.check('#pl-flag-tabby-lazy_load');
      await page.fill('#set-providers input[aria-label="tabby custody timeout"]', '75');
      assert(/At most 60/.test(await page.getAttribute('#set-providers input[aria-label="tabby custody timeout"]', 'title')), 'custody.timeout is at most 60 s');
      await page.fill('#set-providers input[aria-label="tabby custody timeout"]', '8');
      await page.fill('#set-providers textarea[aria-label="tabby custody models"]', 'qwen3.8-flash-next = flash-next-exl3\nnot a pair');
      await page.waitForSelector('#set-providers [data-err-for="providers.tabby.custody.models"]:not([hidden])');
      await page.fill('#set-providers textarea[aria-label="tabby custody models"]', 'qwen3.8-flash-next = flash-next-exl3');
      const s4 = await waitPost(page, /^\/api\/config$/, () => page.click('.save-bar button:has-text("Save changes")'));
      assert(canon(JSON.parse(s4.request().postData())) === canon({ providers: { tabby: { tokens_per_second: 95, lazy_load: true, custody: { timeout: 8, models: { 'qwen3.8-flash-next': 'flash-next-exl3' } } } } }),
        'the provider patch: ' + s4.request().postData());
      assert(/follows tabby/i.test(await text(page, '#set-providers .prov-row[aria-label="Provider tabby-raw"] .pl-custody-line')) && await page.locator('#set-providers input[aria-label="tabby-raw custody url"]').count() === 0,
        'an entry that follows another’s custody is edited there');
      await page.waitForSelector('.toast-ok :text("Settings saved")');
      await page.evaluate(() => { const el = document.querySelector('#set-providers .prov-row[aria-label="Provider tabby"]'); if (el) el.scrollIntoView({ block: 'start' }); });
      await page.waitForTimeout(150);
      await t.areaShot(page, AREA, 'settings-provider-placement-desktop');
      // the register view follows the save: "Register status ›" shows the new timeout and names, not the old answer
      await page.waitForSelector('#set-providers .prov-row[aria-label="Provider tabby"] .pl-custody-line a');
      await page.click('#set-providers .prov-row[aria-label="Provider tabby"] .pl-custody-line a');
      await page.waitForSelector('#routing-root .pl-cust[data-provider="tabby"]');
      await page.waitForFunction(() => { const kv = document.querySelector('#routing-root .pl-cust[data-provider="tabby"] .sf-kv'); return kv && /Answer timeout\s*8 s/.test(kv.textContent) && /flash-next-exl3/.test(kv.textContent); }, null, { timeout: 15000 });
      assert(/Register’s name for this server\s*gpu0/.test(await text(page, '#routing-root .pl-cust[data-provider="tabby"] .sf-kv')), 'the register’s name for the server, in plain words');
    });

    await step(AREA + ': switching custody to a systemd credential: the name is checked as the server does, and clearing the file clears the conflict (no stale error)', async () => {
      await page.goto(url + '/#/settings/providers');
      await page.waitForSelector('#set-providers .prov-row[aria-label="Provider tabby"] .pl-adv-btn');
      if (!(await page.locator('#set-providers input[aria-label="tabby custody url"]').isVisible())) await page.click('#set-providers .prov-row[aria-label="Provider tabby"] .pl-adv-btn');
      const f = (k) => '#set-providers input[aria-label="tabby custody ' + k + '"]';
      const err = (k) => '#set-providers [data-err-for="providers.tabby.custody.' + k + '"]';
      const shown = async (k) => (await page.locator(err(k)).count()) && !(await page.locator(err(k)).isHidden()) ? await text(page, err(k)) : '';
      const saveBtn = '.save-bar button:has-text("Save changes")';
      assert(await page.inputValue(f('token_file')) === '/etc/ai-stack/custody.token', 'tabby reads its token from a file');
      // plainhttp.check_credential_name: not starting with a dot or a dash, no '..'
      for (const bad of ['-token', '.token', 'a..b', 'a/b']) {
        await page.fill(f('token_credential'), bad);
        assert(/not starting with a dot or a dash, no slash and no “\.\.”/.test(await shown('token_credential')), 'refused like the server: ' + bad + ' → ' + await shown('token_credential'));
      }
      // the natural order: the credential first (a conflict with the file, on both fields), then clear the file
      await page.fill(f('token_credential'), 'custody-token');
      assert(/Name one place for the token/.test(await shown('token_credential')) && /Name one place for the token/.test(await shown('token_file')), 'the conflict on both fields');
      assert(await page.isDisabled(saveBtn), 'no save while they conflict');
      await page.fill(f('token_file'), '');
      assert(await shown('token_credential') === '' && await shown('token_file') === '', 'the conflict is gone with the file: ' + await shown('token_credential'));
      const s1 = await waitPost(page, /^\/api\/config$/, () => page.click(saveBtn));
      assert(canon(JSON.parse(s1.request().postData())) === canon({ providers: { tabby: { custody: { token_file: null, token_credential: 'custody-token' } } } }), 'only the credential: ' + s1.request().postData());
      await page.waitForSelector('.toast-ok :text("Settings saved")');
      // and back: the file first, then clear the credential
      await page.waitForSelector(f('token_file'));
      await page.fill(f('token_file'), '/etc/ai-stack/custody.token');
      assert(/Name one place for the token/.test(await shown('token_file')), 'the conflict, the other way round');
      await page.fill(f('token_credential'), '');
      assert(await shown('token_credential') === '' && await shown('token_file') === '', 'cleared with the credential');
      const s2 = await waitPost(page, /^\/api\/config$/, () => page.click(saveBtn));
      assert(canon(JSON.parse(s2.request().postData())) === canon({ providers: { tabby: { custody: { token_file: '/etc/ai-stack/custody.token', token_credential: null } } } }), 'the file again: ' + s2.request().postData());
    });

    await step(AREA + ': the model dropdowns and the Doctor: each model names its family; the placement checks explain themselves', async () => {
      await page.goto(url + '/#/settings/routing');
      await page.waitForSelector('#set-routing select[aria-label="builder model"]');
      const label = await page.$eval('#set-routing select[aria-label="builder model"] option[value="local"]', (o) => o.textContent);
      assert(/qwen family/.test(label), 'a model option names its family: ' + label);
      await page.goto(url + '/#/doctor');
      await page.waitForSelector('.check-row .pl-doc-hint');
      const hints = await page.$$eval('.pl-doc-hint', (els) => els.map((e) => e.dataset.hint));
      for (const n of ['placement', 'local servers', 'provider tabby loaded model', 'provider nanogpt models', 'custody tabby', 'custody strata', 'drift source checker']) assert(hints.includes(n), 'a hint under ' + n + ': ' + hints);
      // a systemd credential (token_credential) the server cannot read: the line that fixes it, to copy (the shell-only
      // "custody <p> credential" warning never reaches the server's doctor)
      assert(await page.locator('.pl-doc-hint[data-hint$=" credential"]').count() === 0, 'no shell-only credential row');
      const fixes = await page.$$eval('.pl-doc-fix code', (els) => els.map((e) => e.textContent));
      assert(fixes.length >= 1 && fixes.every((x) => x === 'LoadCredential=strata-custody-token:<path to the token file>'), 'the LoadCredential= line: ' + fixes);
      assert(await page.getAttribute('.pl-doc-hint[data-hint="placement"] a', 'href') === '#/settings/profiles', 'the placement check links to Profiles');
      await page.evaluate(() => { const el = document.querySelector('.pl-doc-hint[data-hint="placement"]'); if (el) el.scrollIntoView({ block: 'center' }); });
      await t.areaShot(page, AREA, 'doctor-desktop');
      // the "local servers" advice names a button two steps into the setup wizard: its link goes straight there
      const look = '.check-row:has(.check-name:text-is("local servers")) a[data-action="look-around-step"]';
      assert(await page.getAttribute(look, 'href') === '#/setup/local-details', 'the local servers advice links to the Server details step');
      await page.click(look);
      await page.waitForSelector('.wz-main[data-step="local-details"] [data-action="wz-look-around"]');
      await page.goto(url + '/#/doctor');
      await page.waitForSelector('.check-row .pl-doc-hint');
    });

    await step(AREA + ': #/placement: how placement works, an estimate explorer, the profiles, the GPU custody register (fenced, a follower, held and lost leases, a denial; Read now) and the reasons', async () => {
      await page.goto(url + '/#/placement');
      await page.waitForSelector('#view-routing:not([hidden]) #routing-root .view-title');
      await page.waitForSelector('#routing-root .pl-cust[data-provider="tabby"]');
      await page.waitForSelector('#routing-root .pl-est .pl-est-total');
      assert(await page.locator('#routing-root ol.proc li').count() === 5, 'a numbered procedure');
      assert(await page.locator('#routing-root .pl-prof-card').count() === 3, 'the three profiles');
      const strata = await text(page, '#routing-root .pl-cust[data-provider="strata"]');
      assert(/generation went back/.test(strata) && /from 17 to 3/.test(strata) && /has not been read/.test(strata), 'a fenced register without residency: ' + strata);
      assert(/follows tabby/.test(await text(page, '#routing-root .pl-cust[data-provider="tabby-raw"]')), 'a follower names the entry it follows');
      const tabby = await text(page, '#routing-root .pl-cust[data-provider="tabby"]');
      assert(/needs a lease/.test(tabby) && /L-7f3a9c/.test(tabby) && /The register restarted/.test(tabby) && /gpu0\/qwen3\.8-flash-next/.test(tabby), 'held and lost leases: ' + tabby);
      assert(await page.locator('#routing-root img').count() === 0 && tabby.includes("<img src=x onerror=alert('purpose')>"), 'a lease’s purpose is text');
      await page.click('#routing-root .pl-cust[data-provider="tabby"] .pl-hist > summary');
      const hist = await text(page, '#routing-root .pl-cust[data-provider="tabby"] .pl-hist');
      assert(/Lease denied: another lease covers it/.test(hist) && /Lease denied: the card cannot fit this model now/.test(hist), 'denials in words (reason_codes): ' + hist);
      assert(/Waits up to 900s: the card is held by someone else/.test(hist) && /The hold ended after 60\.2s: lease granted/.test(hist), 'a HELD wait and how it ended: ' + hist);
      assert(/Not released: the register restarted, so it no longer knows this lease/.test(hist), 'not sent to a register that restarted: ' + hist);
      assert(/When the card is held\s*a HELD answer waits up to 900s, once per run; FULL never waits/.test(tabby) && /the systemd credential strata-custody-token/.test(strata), 'the held wait and a credential: ' + tabby.slice(0, 400) + ' | ' + strata.slice(0, 400));
      assert(/a HELD answer does not wait/.test(await text(page, '#routing-root .pl-cust[data-provider="tabby-raw"]')), 'no Overflow after: no wait');
      const u = await waitGet(page, /^\/api\/custody/, () => page.click('#routing-root [data-action="custody-read"]'));
      assert(u.searchParams.get('refresh') === '1', 'Read now asks with refresh=1');
      const u2 = await waitGet(page, /^\/api\/estimate/, () => page.selectOption('#pl-page-kind', 'plan'));
      assert(u2.searchParams.get('kind') === 'plan', 'the explorer estimates a plan');
      await page.waitForTimeout(300);
      await page.evaluate(() => { document.getElementById('main').scrollTop = 0; });
      await t.areaShot(page, AREA, 'placement-desktop');
      await page.evaluate(() => { const el = document.getElementById('pl-custody'); if (el) el.scrollIntoView(); });
      await t.areaShot(page, AREA, 'placement-custody-desktop');
      await noOverflow(page, 'placement 1440');
    });

    await step(AREA + ': at 390 px (light): the placement page, the Runs tab, the drawer header, New feature’s estimate and the Settings placement fit without a sideways scroll', async () => {
      await phone.goto(url + '/#/placement');
      await phone.waitForSelector('#routing-root .pl-cust[data-provider="tabby"]');
      await phone.waitForSelector('#routing-root .pl-est .pl-est-total');
      await phone.waitForTimeout(250);
      await noOverflow(phone, 'placement 390');
      await t.areaShot(phone, AREA, 'placement-phone');
      await phone.evaluate(() => { const el = document.getElementById('pl-custody'); if (el) el.scrollIntoView(); });
      const kvCols = await phone.$$eval('#routing-root .pl-cust .sf-kv', (els) => els.map((e) => getComputedStyle(e).gridTemplateColumns.trim().split(/\s+/).length));
      assert(kvCols.length && kvCols.every((n) => n === 1), 'a custody card’s labels sit above their values on a phone: ' + kvCols);
      await t.areaShot(phone, AREA, 'placement-custody-phone');
      await phone.goto(url + '/#/job/' + SHOWCASE);
      await phone.waitForSelector('#drawer-head .pl-ind-block');
      await phone.waitForTimeout(300);
      await noOverflow(phone, 'drawer head 390');
      assert(await phone.locator('#drawer-head .pl-ind-reduced').isVisible(), 'the reduced badge is in the open on a phone');
      // The head scrolls when it is tall; its title keeps its lines (a flex item would otherwise shrink to nothing).
      assert(await phone.$eval('#drawer-title', (e) => e.getBoundingClientRect().height) > 16, 'the job title shows on a phone');
      await t.areaShot(phone, AREA, 'drawer-head-phone');
      await phone.goto(url + '/#/job/' + SHOWCASE + '/runs');
      await phone.waitForSelector('#panel-runs:not([hidden]) .pl-run');
      await phone.waitForTimeout(350);
      await noOverflow(phone, 'runs tab 390');
      await t.areaShot(phone, AREA, 'runs-tab-phone');
      await phone.evaluate(() => { const el = document.querySelector('#panel-runs .pl-runs'); if (el) el.scrollIntoView(); });
      await t.areaShot(phone, AREA, 'runs-tab-rows-phone');
      await phone.click('#tab-live');
      await phone.waitForSelector('#panel-live .run[data-run="10-reviewer-ui"] .run-ext .pl-ind-reduced');
      await noOverflow(phone, 'live 390');
      assert(await phone.locator('#panel-live .run[data-run="10-reviewer-ui"] .pl-ind-reduced').isVisible(), 'the reduced badge stays visible on a phone');
      await phone.evaluate(() => { const el = document.querySelector('#panel-live .run[data-run="02-builder"]'); if (el) el.scrollIntoView({ block: 'start' }); });
      await phone.waitForTimeout(200);
      await t.areaShot(phone, AREA, 'live-headers-phone');
      await phone.evaluate(() => { const b = document.querySelector('#drawer .drawer-body'); if (b) b.scrollTop = 0; });
      await phone.keyboard.press('Escape');
      await phone.goto(url + '/#/factory');
      await phone.waitForSelector('.board .card');
      await phone.click('#new-feature-btn');
      await phone.waitForSelector('dialog[open] .pl-est .pl-est-total');
      await phone.selectOption('dialog[open] #pl-prof-feature', 'fastest');
      await phone.waitForFunction(() => /profile fastest/.test((document.querySelector('dialog[open] .pl-est-head') || {}).textContent || ''));
      await phone.click('dialog[open] .pl-est-more > summary');
      await phone.waitForTimeout(250);
      const dw = await phone.evaluate(() => { const b = document.querySelector('dialog[open] .modal-body'); return [b.scrollWidth, b.clientWidth]; });
      assert(dw[0] <= dw[1] + 1, 'the estimate fits the phone dialog: ' + dw);
      await t.areaShot(phone, AREA, 'new-feature-phone');
      await phone.click('dialog[open] button[data-action="cancel"]');
      // New plan, Change routing, New chat and a chat's and a plan's header
      await phone.goto(url + '/#/plans');
      await phone.click('[data-action="new-plan"]');
      await phone.waitForSelector('dialog[open] .pl-est[data-est="plan"] .pl-est-total');
      await phone.waitForTimeout(200);
      await t.areaShot(phone, AREA, 'new-plan-phone');
      await phone.click('dialog[open] button[data-action="cancel"]');
      await phone.goto(url + '/#/plans/my-app/P001-csv-export-for-reports');
      await phone.waitForSelector('.plan-detail [data-meta="cost"]');
      await noOverflow(phone, 'plan head 390');
      await t.areaShot(phone, AREA, 'plan-head-phone');
      await phone.goto(url + '/#/job/my-app/002-rate-limit-signup-form/routing');
      await phone.waitForSelector('#panel-routing [data-action="routing"]');
      await phone.click('#panel-routing [data-action="routing"]');
      await phone.waitForSelector('dialog[open] #pl-prof-routing:not([hidden])', { state: 'attached' });
      await phone.waitForFunction(() => document.querySelector('dialog[open] #pl-prof-routing').options.length > 2);
      await phone.click('dialog[open] .pl-alts > summary');
      await phone.waitForTimeout(200);
      const cw = await phone.evaluate(() => { const b = document.querySelector('dialog[open] .modal-body'); return [b.scrollWidth, b.clientWidth]; });
      assert(cw[0] <= cw[1] + 1, 'Change routing fits the phone dialog: ' + cw);
      await phone.evaluate(() => { const el = document.querySelector('dialog[open] .pl-prof'); if (el) el.scrollIntoView({ block: 'start' }); });
      await t.areaShot(phone, AREA, 'change-routing-phone');
      await phone.click('dialog[open] button[data-action="cancel"]');
      await phone.keyboard.press('Escape');
      await phone.goto(url + '/#/chat');
      await phone.click('#new-chat-btn');
      await phone.waitForFunction(() => { const s = document.querySelector('dialog[open] #pl-prof-chat'); return s && s.options.length > 2; });
      await t.areaShot(phone, AREA, 'new-chat-phone');
      await phone.click('dialog[open] button[data-action="cancel"]');
      await phone.goto(url + '/#/chat/c-1a2b3c4d5e');
      await phone.waitForSelector('.chat-head [data-meta="sessions"]');
      await noOverflow(phone, 'chat head 390');
      await t.areaShot(phone, AREA, 'chat-head-phone');
      // Start: the Routing step's profile and the review's estimate
      await phone.goto(url + '/#/start');
      await phone.waitForSelector('.wz-main .opt-card[data-opt="feature"]');
      await phone.click('.wz-main .opt-card[data-opt="feature"]');
      await phone.click('.wz-foot [data-action="wz-next"]');
      await phone.waitForSelector('.wz-main .opt-card[data-opt="my-app"]');
      await phone.click('.wz-main .opt-card[data-opt="my-app"]');
      await phone.click('.wz-foot [data-action="wz-next"]');
      await phone.waitForSelector('#wz-describe-text');
      await phone.fill('#wz-describe-text', 'Cache the seat price list');
      await phone.click('.wz-foot [data-action="wz-next"]');
      await phone.waitForSelector('#wz-routing-profile');
      await phone.selectOption('#wz-routing-profile', 'profile:fastest');
      await phone.evaluate(() => { const el = document.getElementById('wz-routing-profile'); if (el) el.scrollIntoView({ block: 'center' }); });
      await noOverflow(phone, 'start routing 390');
      await t.areaShot(phone, AREA, 'start-routing-phone');
      await phone.click('.wz-foot [data-action="wz-next"]');
      await phone.waitForSelector('.wz-main .opt-card[data-opt="merge"]');
      await phone.click('.wz-foot [data-action="wz-next"]');
      await phone.waitForSelector('.wz-run .pl-est .pl-est-total');
      await phone.evaluate(() => { const el = document.querySelector('.wz-run .pl-est'); if (el) el.scrollIntoView({ block: 'center' }); });
      await noOverflow(phone, 'start review 390');
      await t.areaShot(phone, AREA, 'start-review-phone');
      await phone.goto(url + '/#/start/what');
      await phone.waitForSelector('.wz-main .opt-card[data-opt="chat"]');
      await phone.click('.wz-main .opt-card[data-opt="chat"]');
      await phone.click('.wz-foot [data-action="wz-next"]');
      await phone.waitForSelector('.wz-main .opt-card[data-opt="my-app"]');
      await phone.click('.wz-foot [data-action="wz-next"]');
      await phone.waitForSelector('#wz-describe-profile');
      await phone.evaluate(() => { const el = document.getElementById('wz-describe-profile'); if (el) el.scrollIntoView({ block: 'center' }); });
      await noOverflow(phone, 'start chat 390');
      await t.areaShot(phone, AREA, 'start-chat-phone');
      await phone.goto(url + '/#/settings/models');
      await phone.waitForSelector('#set-models .mdl-row[data-model="local"] .pl-adv-btn');
      await phone.click('#set-models .mdl-row[data-model="local"] .pl-adv-btn');
      await phone.evaluate(() => { const el = document.querySelector('#set-models .mdl-row[data-model="local"] .pl-adv-body'); if (el) el.scrollIntoView({ block: 'start' }); });
      await phone.waitForTimeout(250);
      await noOverflow(phone, 'settings model placement 390');
      await t.areaShot(phone, AREA, 'settings-model-placement-phone');
      // an invalid number says why under the field, and the save bar names it (no hover on a phone)
      await phone.fill('#set-models input[aria-label="local build 2 max tokens"]', '0.5');
      await phone.waitForSelector('#set-models [data-err-for="models.local.builds[1].max_tokens"]:not([hidden])');
      assert(await phone.locator('#set-models [data-err-for="models.local.builds[1].max_tokens"]').isVisible() && /local build 2 max tokens/.test(await text(phone, '.save-bar .save-invalid')), 'the reason is in sight on a phone');
      await phone.click('.save-bar [data-action="show-invalid"]');
      assert(await phone.evaluate(() => document.activeElement && document.activeElement.getAttribute('aria-label')) === 'local build 2 max tokens', 'Show it goes to the field');
      await phone.evaluate(() => { const el = document.querySelector('#set-models .mdl-row[data-model="local"] .pl-builds'); if (el) el.scrollIntoView({ block: 'center' }); });
      await noOverflow(phone, 'settings invalid number 390');
      await t.areaShot(phone, AREA, 'settings-invalid-number-phone');
      await phone.click('.save-bar button:has-text("Discard")');
      await phone.goto(url + '/#/settings/profiles');
      await phone.waitForSelector('#set-profiles .pl-profile');
      await phone.waitForTimeout(300);
      await noOverflow(phone, 'settings profiles 390');
      await t.areaShot(phone, AREA, 'settings-profiles-phone');
      await phone.goto(url + '/#/settings/providers');
      await phone.waitForSelector('#set-providers .prov-row[aria-label="Provider tabby"] .pl-adv-btn');
      await phone.click('#set-providers .prov-row[aria-label="Provider tabby"] .pl-adv-btn');
      await phone.evaluate(() => { const el = document.querySelector('#set-providers .prov-row[aria-label="Provider tabby"]'); if (el) el.scrollIntoView({ block: 'start' }); });
      await phone.waitForTimeout(250);
      await noOverflow(phone, 'settings provider placement 390');
      await t.areaShot(phone, AREA, 'settings-provider-placement-phone');
      await phone.goto(url + '/#/settings/routing');
      await phone.waitForSelector('#set-routing .rt-box[data-agent="approver"] .pl-adv-btn');
      await phone.click('#set-routing .rt-box[data-agent="approver"] .pl-adv-btn');
      await phone.evaluate(() => { const el = document.querySelector('#set-routing .rt-box[data-agent="approver"]'); if (el) el.scrollIntoView({ block: 'start' }); });
      await phone.waitForTimeout(250);
      await noOverflow(phone, 'settings route placement 390');
      await t.areaShot(phone, AREA, 'settings-route-placement-phone');
      await phone.selectOption('#set-routing select[aria-label="approver model"]', 'claude');
      await phone.waitForSelector('#set-routing [data-err-for="agents.approver.independent_alternate"]:not([hidden])');
      await phone.evaluate(() => { const el = document.querySelector('#set-routing select[aria-label="approver independent alternate"]'); if (el) el.scrollIntoView({ block: 'center' }); });
      await noOverflow(phone, 'settings route model change 390');
      await t.areaShot(phone, AREA, 'settings-route-model-change-phone');
      await phone.click('.save-bar button:has-text("Discard")');
      await phone.goto(url + '/#/doctor');
      await phone.waitForSelector('.pl-doc-hint');
      await noOverflow(phone, 'doctor 390');
      await t.areaShot(phone, AREA, 'doctor-phone');
    });
  } finally {
    await d.ctx.close();
    await p.ctx.close();
    mock.proc.kill('SIGTERM');
  }
}
