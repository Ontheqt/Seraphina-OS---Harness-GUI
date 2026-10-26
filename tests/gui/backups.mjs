// Checks for Backups (DESIGN §41 "GUI interface notes (disk protection)"; docs/seraphina/BACKUPS_SNAPRAID.md §12.5,
// §12.6, §13.2 U1, U6, U7, U8), phase P1: the view module swfactory/web/views/backups.js against
// tests/gui_mock/backups.py, run by tests/gui/smoke.mjs (AREA_SUITES), or alone with GUI_ONLY=backups node
// tests/gui/smoke.mjs. Screenshots of every new screen, desktop 1440x900 and phone 390x844: t.areaShot(page, AREA,
// name) → $GUI_AREA_SHOTS/backups/<name>.png.
//
// run(t) gets the harness of smoke.mjs: { browser, url, startMock, step, assert, canon, waitPost, shot, cleanShot,
// noOverflow, watch, allowHttp, newPage(label, {phone, width, height, colorScheme, mockUrl}), areaShot(page, area, name) }.

const AREA = 'backups';
const SENTENCES = [
  'Disk protection can bring back a dead data disk, or deleted and damaged files, exactly as they were at its last run.',
  'It can do that only until its next run, which makes deletions permanent, and it never had anything added since.',
  'It keeps no older versions, sits on this same machine, covers only the disks listed below, and an administrator can delete it: it works beside your backups, never instead of them.',
];
const WATCHING = 'Seraphina is only watching: nothing is protected by it yet.';
// The words table's internal terms (BACKUPS_SNAPRAID.md §12.5): never on a novice screen (U7).
const INTERNAL = /\b(array|parity|sync|scrub|content\s+file|fix|stripe|helper|held\s+sync|release)\w*/i;
// Buttons whose verbs are not allowed in P1 (§3.10): never drawn.
const LATER_BUTTONS = /Protect the latest changes|Check for hidden damage|Stop safely|Bring files back|Skip the next run|Protect these changes|Try the guard|Repair it|Start protecting/;
// state: [tone, code] of the light (snapraid.light over the mock's facts)
const STATES = {
  absent: ['muted', 'absent'], no_reader: ['muted', 'no_reader'], off: ['muted', 'off'], not_set_up: ['muted', 'not_set_up'],
  unsafe: ['muted', 'unsafe'], unreachable: ['muted', 'unreachable'], never: ['warn', 'never_protected'], healthy: ['ok', 'ok'],
  would_hold: ['ok', 'ok'], interrupted: ['warn', 'interrupted'], bad_blocks: ['err', 'bad_blocks'], failing_disk: ['err', 'smart_fail'],
  missing_disk: ['err', 'disk_missing'], health_unknown: ['warn', 'health_unknown'], outside_run: ['warn', 'outside'],
  unsandboxed: ['ok', 'ok'], stale: ['muted', 'stale'], damaged_list: ['err', 'content_problem'], stale_failing: ['err', 'smart_fail'],
  taking_out: ['warn', 'taking_out'], with_care: ['warn', 'read_with_care'],
};
// The protection service's route (§12.6): state → [tone, code] (snapraidd.light over the mock's daemon view)
const DAEMON = {
  daemon_no_link: ['ok', 'ok'], daemon_healthy: ['ok', 'ok'], daemon_read_only: ['ok', 'ok'], daemon_auth_off: ['err', 'auth_off'],
  daemon_beyond: ['warn', 'beyond_loopback'], daemon_down: ['warn', 'down'], daemon_frozen: ['warn', 'frozen'], daemon_running: ['ok', 'ok'],
  daemon_unknown: ['warn', 'unknown_outcome'], daemon_lapsed: ['warn', 'lapsed'], daemon_two: ['warn', 'two_schedulers'],
  daemon_report_failed: ['warn', 'report_failed'], daemon_engine_old: ['err', 'engine_old'], daemon_damage: ['err', 'bad_blocks'], daemon_skip: ['ok', 'ok'],
};
// The words that never reach a novice screen on this route (§12.5, U7's list grown)
const SERVICE_TERMS = /\b(daemon|threshold|hold[ -]off|undelete|heal|engine)\b/i;
const READY = ['healthy', 'would_hold', 'unsandboxed', 'never', 'interrupted', 'bad_blocks', 'failing_disk', 'missing_disk', 'health_unknown', 'outside_run', 'stale',
  'damaged_list', 'stale_failing', 'taking_out', 'with_care'];

async function setState(t, page, state) {
  const r = await page.request.post(t.url + '/__mock/backups/' + state, { data: {} });
  t.assert(r.status() === 200, 'mock state ' + state + ': ' + r.status());
}
// A fresh load after the mock's state changed (the page keeps its last answer until an event or a visit asks again);
// phase: the card's state once loaded (ready, off, no_reader, absent).
async function openCard(page, url, phase) {
  await page.goto(url + '/#/backups');
  await page.reload();
  await page.waitForSelector('#view-backups:not([hidden]) .home-card[data-part="protection"]' + (phase ? '[data-state="' + phase + '"]' : '') + ' .bk-light');
}
const novice = (page) => page.$eval('.home-card[data-part="protection"]', (card) => {
  const copy = card.cloneNode(true);
  copy.querySelectorAll('.tag-mono, code').forEach((e) => e.remove());
  return copy.innerText;
});

export async function run(t) {
  const { step, assert, noOverflow } = t;
  const d = await t.newPage(AREA);
  const p = await t.newPage(AREA + '-phone', { phone: true });
  const page = d.page;
  try {
    await step(AREA + ': every P1 state draws the card with its light, reason and buttons, the three sentences word for word, no button of a later phase, and green never on stale facts (U1, U7)', async () => {
      for (const [state, [tone, code]] of Object.entries(STATES)) {
        await setState(t, page, state);
        await openCard(page, t.url);
        await page.waitForFunction((c) => document.querySelector('.bk-light').dataset.code === c, code, { timeout: 5000 });
        const light = await page.$eval('.bk-light', (e) => ({ tone: e.dataset.tone, reason: e.querySelector('.bk-reason').textContent }));
        assert(light.tone === tone, state + ': tone ' + light.tone + ' (want ' + tone + ')');
        assert(light.reason.length > 10 && /\.$/.test(light.reason.trim()) || state === 'unreachable' || state === 'unsafe', state + ': a reason sentence: ' + light.reason);
        const sentences = await page.$$eval('.bk-sentences [data-sentence]', (els) => els.map((e) => e.textContent));
        assert(t.canon(sentences) === t.canon(SENTENCES), state + ': the three sentences word for word');
        const text = await page.textContent('#backups-root');
        assert(!LATER_BUTTONS.test(text), state + ': no button of a later phase: ' + (text.match(LATER_BUTTONS) || [])[0]);
        const words = await novice(page);
        assert(!INTERNAL.test(words), state + ': an internal term on the card: ' + (words.match(INTERNAL) || [])[0]);
        if (READY.includes(state)) {
          assert(text.includes(WATCHING), state + ': the P1 line');
          assert(await page.locator('[data-action="check-status"]').count() === 1 && await page.locator('[data-action="check-diff"]').count() === 1, state + ': the read buttons');
          const lab = await page.textContent('.bk-lab');
          assert(/^Not checked on this server yet: .+\. Until they are, Seraphina only reads\.$/.test(lab.trim()) && !/L\d|sudo|unit/.test(lab), state + ': one plain lab line: ' + lab);
        }
        if (state === 'off') assert(!text.includes(WATCHING), 'off: no "only watching" line under "not watching"');
        if (state === 'stale_failing') {
          assert(await page.textContent('.bk-note') === 'Last checked 5 hours ago.', 'stale and red: red stays, the age on a second line');
          assert(await page.locator('.bk-last-known').count() === 0, 'no "unknown" while red');
        }
        if (state === 'damaged_list') assert(light.reason === "The protection's file list has a problem.", 'a damaged file list is red: ' + light.reason);
        if (state === 'stale') {
          assert(/^Last checked 5 hours ago\.$/.test(light.reason.trim()), 'stale: ' + light.reason);
          assert(/^unknown \(last known: protected up to/.test(await page.textContent('.bk-last-known')), 'stale keeps the last known state, never green');
        }
        if (state === 'no_reader') assert(await page.locator('[data-action="set-up"]').count() === 1, 'no reader: the setup offer');
        if (state === 'off') assert(await page.locator('[data-action="turn-on"]').count() === 1, 'off: Turn it on');
        if (state === 'unsandboxed') assert(/sandbox is off/.test(await page.textContent('[data-warning="agents_unsandboxed"]')), 'the agents line');
        // BACKUPS_SNAPRAID §8.3 item 2: a disk being taken out is amber and listed under Not covered, by its label
        const notCovered = await page.$$eval('.bk-not-covered li', (els) => els.map((e) => e.textContent));
        const leaving = 'Disk 3, which is being taken out of protection';
        if (state === 'taking_out') {
          assert(light.reason === 'Disk 3 is being taken out of protection.', 'taking out: ' + light.reason);
          assert(notCovered.includes(leaving) && notCovered[notCovered.length - 1] === 'every change made after the last protection run', 'taking out: Not covered lists the disk: ' + JSON.stringify(notCovered));
        } else assert(!notCovered.includes(leaving), state + ': no disk being taken out under Not covered');
        if (READY.includes(state)) assert(/^No versioned backup exists yet\./.test((await page.textContent('[data-part="versioned"]')).replace(/^Versioned backups/, '').trim()), state + ': the versioned line says none exists');
        if (state === 'missing_disk') assert(/Disk 2 is not mounted where it should be\./.test(light.reason), 'missing disk: ' + light.reason);
        if (['healthy', 'failing_disk', 'would_hold', 'stale_failing', 'damaged_list', 'taking_out'].includes(state)) await t.areaShot(page, AREA, 'card-' + state + '-desktop');
      }
      await setState(t, page, 'healthy');
    });

    await step(AREA + ': every state of the protection service draws the card with its light, reason, facts and only the buttons it offers; no internal term (U9, U7)', async () => {
      for (const [state, [tone, code]] of Object.entries(DAEMON)) {
        await setState(t, page, state);
        await page.goto(t.url + '/#/backups');
        await page.reload();
        await page.waitForSelector('#view-backups:not([hidden]) .home-card[data-part="protection"][data-route] .bk-light');
        const light = await page.$eval('.bk-light', (e) => ({ tone: e.dataset.tone, code: e.dataset.code, reason: e.querySelector('.bk-reason').textContent }));
        assert(light.tone === tone && light.code === code, state + ': ' + JSON.stringify(light) + ' (want ' + tone + ' ' + code + ')');
        assert(/\.$/.test(light.reason.trim()), state + ': a reason sentence');
        const words = await novice(page);
        assert(!INTERNAL.test(words) && !SERVICE_TERMS.test(words), state + ': an internal term: ' + ((words.match(INTERNAL) || words.match(SERVICE_TERMS) || [])[0]));
        assert(t.canon(await page.$$eval('.bk-sentences [data-sentence]', (els) => els.map((e) => e.textContent))) === t.canon(SENTENCES), state + ': the three sentences');
        const has = async (a) => (await page.locator('[data-action="act-' + a + '"]').count()) > 0;
        const readOnly = ['daemon_no_link', 'daemon_read_only', 'daemon_auth_off', 'daemon_engine_old'].includes(state);
        assert(!readOnly || !(await has('maintenance') || await has('hold_off') || await has('heal') || await has('stop')), state + ': no action button where the link or the checks refuse');
        if (state === 'daemon_healthy') assert(await has('maintenance') && await has('hold_off'), 'healthy: Protect the latest changes and Skip the next run');
        if (state === 'daemon_two') assert(!(await has('maintenance')) && await has('hold_off'), 'two schedulers: nothing that starts a run; a skip stays');
        if (state === 'daemon_running') {
          assert(await has('stop') && !(await has('maintenance')), 'running: Stop safely, no second run');
          assert(/Running now: protecting the latest changes, 37%/.test(await page.textContent('[data-part="running"]')), 'the run in words with its progress');
        }
        if (state === 'daemon_frozen') {
          assert(/Waiting for you: 25 deleted, 3 changed\./.test(await page.textContent('[data-part="frozen"]')), 'the freeze in counts');
          assert(await page.locator('[data-part="frozen"] [data-action="review"]').count() === 1 && await has('accept'), 'See what changed, and the accept');
        }
        if (state === 'daemon_damage') assert(await has('heal'), 'damage: Repair it');
        if (state === 'daemon_no_link') assert(await page.locator('[data-action="set-up-link"]').count() === 1, 'no link: the setup offer');
        if (state === 'daemon_read_only') assert(await page.locator('.bk-read-only').count() === 1, 'read only: says so');
        if (state === 'daemon_skip') assert(/run will be skipped\. A restart of the protection service undoes this\./.test(await page.textContent('[data-part="undo"]')) && await page.locator('[data-part="undo"] [data-action="act-hold_off"]').count() === 1, 'the skip line and Don’t skip');
        if (state === 'daemon_unknown') assert(/Outcome unknown: it may or may not have happened/.test(await page.textContent('[data-part="asked"]')), 'the unknown outcome in §40’s words');
        assert(/Next run: (tonight|tomorrow|today) at 02:00\./.test(await page.textContent('[data-fact="window"]')), state + ': the next run');
        if (['daemon_healthy', 'daemon_frozen', 'daemon_running', 'daemon_auth_off', 'daemon_unknown'].includes(state)) await t.areaShot(page, AREA, 'service-' + state + '-desktop');
      }
    });

    await step(AREA + ': each action asks for a go-ahead that says what will happen, with one button; an accept needs the list opened and the typed count; a changed fact sends the owner back (U10)', async () => {
      await setState(t, page, 'daemon_healthy');
      await page.goto(t.url + '/#/backups');
      await page.reload();
      await page.waitForSelector('[data-action="act-hold_off"]');
      await page.click('[data-action="act-hold_off"]');
      await page.waitForSelector('dialog.modal[open] .bk-approval[data-action="hold_off"]');
      const skip = await page.textContent('dialog.modal[open] .modal-body');
      assert(/Only that one run is skipped\./.test(skip) && /A restart of the protection service undoes this\./.test(skip), 'the skip says what it does: ' + skip);
      assert(await page.locator('dialog.modal[open] .modal-foot .btn-primary').count() === 1, 'one button');
      await t.areaShot(page, AREA, 'approve-skip-desktop');
      const approved = await t.waitPost(page, /\/api\/snapraid\/actions\/A-\d+\/approve$/, () => page.click('dialog.modal[open] .modal-foot .btn-primary'));
      assert(JSON.parse(approved.request().postData()).shown_sha256, 'the press carries the hash of what was shown');
      await page.waitForSelector('[data-part="asked"] [data-outcome="succeeded"]');
      const asked = await page.textContent('[data-part="asked"]');
      assert(/Succeeded The protection service took it\./.test(asked), 'the outcome in DESIGN §40\'s words: ' + asked);
      assert(await page.locator('[data-part="asked"] [data-outcome="succeeded"] .pill.tone-ok').count() === 1, 'its word as a green pill');
      await setState(t, page, 'daemon_running');
      await page.goto(t.url + '/#/backups');
      await page.reload();
      await page.waitForSelector('[data-action="act-stop"]');
      assert(await page.locator('text=/\bKill\b/').count() === 0, 'no Kill control');
      await page.click('[data-action="act-stop"]');
      await page.waitForSelector('dialog.modal[open] .bk-approval[data-action="stop"]');
      assert(/protecting the latest changes \(the protection run\), 37%/.test(await page.textContent('dialog.modal[open] [data-part="what"]')), 'Stop names the step');
      assert(/Don't turn the server off for a minute/.test(await page.textContent('dialog.modal[open]')), 'Stop safely says what it does');
      await page.click('dialog.modal[open] [data-action="cancel"]');
      await setState(t, page, 'daemon_frozen');
      await page.goto(t.url + '/#/backups');
      await page.reload();
      await page.waitForSelector('[data-action="act-accept"]');
      t.allowHttp(true);    // a refused preview (400) and a changed fact (409) are answers the page shows
      await page.click('[data-action="act-accept"]');
      await page.waitForFunction(() => /Open the list of changes first/.test(document.body.textContent), null, { timeout: 5000 });
      await page.goto(t.url + '/#/backups/review');
      await page.waitForSelector('.bk-changes .bk-change');
      await page.click('[data-action="act-accept"]');
      await page.waitForSelector('dialog.modal[open] .bk-approval[data-action="accept"]');
      assert(/may be more than you looked at/.test(await page.textContent('dialog.modal[open]')), 'the unbounded-accept sentence');
      assert(await page.locator('dialog.modal[open] [data-focus="typed"]').count() === 1, 'the typed count is asked for');
      await t.areaShot(page, AREA, 'approve-accept-desktop');
      await page.fill('dialog.modal[open] [data-focus="typed"]', '12');
      await page.click('dialog.modal[open] .modal-foot .btn-primary');
      await page.waitForFunction(() => { const e = document.querySelector('dialog.modal[open] .form-error'); return e && !e.hidden && /Type the number/.test(e.textContent); });
      await page.fill('dialog.modal[open] [data-focus="typed"]', '30');
      await page.request.post(t.url + '/__mock/backups/changed', { data: {} });
      await page.click('dialog.modal[open] .modal-foot .btn-primary');
      await page.waitForFunction(() => { const e = document.querySelector('dialog.modal[open] .form-error'); return e && !e.hidden && /Something changed since you looked/.test(e.textContent); });
      await page.click('dialog.modal[open] [data-action="cancel"]');
      t.allowHttp(false);
    });

    await step(AREA + ': the review page pages through 3,000 entries, filters by disk and kind, shows hostile names as text and the per-disk counts, and brings chosen files back after a go-ahead (U11)', async () => {
      await setState(t, page, 'daemon_frozen');
      await page.goto(t.url + '/#/backups');
      await page.reload();
      await page.click('[data-part="frozen"] [data-action="review"]');
      await page.waitForSelector('.bk-changes .bk-change');
      assert(/1–500 of 3k/.test(await page.textContent('.bk-range')), 'the first page: ' + await page.textContent('.bk-range'));
      assert(/Disk 1: 1\.8k removed/.test(await page.textContent('.bk-per-disk')), 'per-disk counts: ' + await page.textContent('.bk-per-disk'));
      await page.click('[data-action="next"]');
      await page.waitForFunction(() => /501–1000/.test(document.querySelector('.bk-range').textContent));
      await page.selectOption('[data-focus="r-disk"]', 'd2');
      await page.waitForFunction(() => /of 1\.2k$/.test(document.querySelector('.bk-range').textContent));
      await page.selectOption('[data-focus="r-disk"]', '');
      await page.fill('[data-focus="r-q"]', 'notes/');
      await page.press('[data-focus="r-q"]', 'Enter');
      await page.waitForFunction(() => /of 8$/.test(document.querySelector('.bk-range').textContent));
      const paths = await page.$$eval('.bk-path', (els) => els.map((e) => e.textContent));
      assert(paths.includes("Notes/<img src=x onerror=alert('backups')>.jpg") && paths.some((x) => x.length > 600), 'hostile names as text');
      assert(await page.locator('#backups-root img, #backups-root script').count() === 0, 'no markup rendered');
      const boxes = page.locator('.bk-change input[data-pick]');
      await boxes.nth(0).check();
      await page.waitForSelector('.bk-one-disk');
      assert(await boxes.nth(1).isDisabled() && /one disk at a time/.test(await page.textContent('.bk-one-disk')), 'files come back one disk at a time: a name on another disk cannot join the choice');
      await boxes.nth(2).check();
      await page.waitForFunction(() => /\(2 chosen\)/.test(document.querySelector('[data-action="act-undelete"]').textContent));
      await page.click('[data-action="act-undelete"]');
      await page.waitForSelector('dialog.modal[open] .bk-approval[data-action="undelete"]');
      const words = await page.textContent('dialog.modal[open]');
      assert(/2 files and folders chosen/.test(words) && /Missing files only: nothing that exists is replaced\./.test(words) && /readable only by an administrator/.test(words), 'what an undelete does: ' + words);
      await t.areaShot(page, AREA, 'review-undelete-desktop');
      await page.click('dialog.modal[open] .modal-foot .btn-primary');
      await page.waitForFunction(() => !document.querySelector('dialog.modal[open]'));
      await noOverflow(page, 'review 1440');
      await t.areaShot(page, AREA, 'review-desktop');
    });

    await step(AREA + ': a run of the protection service and one of its steps, its messages as text (U9)', async () => {
      await setState(t, page, 'daemon_frozen');
      await page.goto(t.url + '/#/backups');
      await page.reload();
      await page.waitForSelector('.bk-druns [data-step="5"]');
      assert(/Stopped for you to look at the changes\./.test(await page.textContent('.bk-druns')), 'the run’s verdict in words');
      await page.click('.bk-druns [data-step="5"]');
      await page.waitForSelector('.bk-messages');
      assert(/^#\/backups\/run\/5$/.test(await page.evaluate(() => location.hash)), 'a step has its own route');
      assert((await page.textContent('.bk-messages')).includes('<b>not bold</b>'), 'messages are text');
      await t.areaShot(page, AREA, 'step-desktop');
    });

    await step(AREA + ': the protection service at 390 px: card, review page and a step, no sideways scroll (U13)', async () => {
      const m = p.page;
      for (const state of ['daemon_frozen', 'daemon_running', 'daemon_unknown', 'daemon_no_link']) {
        await setState(t, m, state);
        await m.goto(t.url + '/#/backups');
        await m.reload();
        await m.waitForSelector('.home-card[data-part="protection"][data-route] .bk-light');
        await noOverflow(m, 'service ' + state + ' 390');
        if (['daemon_frozen', 'daemon_running'].includes(state)) await t.areaShot(m, AREA, 'service-' + state + '-phone');
      }
      await setState(t, m, 'daemon_frozen');
      await m.goto(t.url + '/#/backups/review');
      await m.waitForSelector('.bk-changes .bk-change');
      await noOverflow(m, 'review 390');
      await t.areaShot(m, AREA, 'review-phone');
      await m.goto(t.url + '/#/backups/run/5');
      await m.waitForSelector('.bk-messages');
      await noOverflow(m, 'step 390');
      await setState(t, m, 'healthy');
    });

    await step(AREA + ': the card at 390 px: no sideways scroll on any page, disks stacked, long names wrapped (U8)', async () => {
      const m = p.page;
      for (const state of ['healthy', 'failing_disk', 'would_hold', 'absent', 'unsandboxed', 'stale_failing', 'damaged_list']) {
        await setState(t, m, state);
        await openCard(m, t.url);
        await noOverflow(m, 'backups ' + state + ' 390');
        if (['healthy', 'failing_disk', 'stale_failing'].includes(state)) await t.areaShot(m, AREA, 'card-' + state + '-phone');
      }
      await setState(t, m, 'healthy');
      await openCard(m, t.url);
      await m.click('[data-action="details"]');
      await m.waitForSelector('.bk-runs [data-run="J-41"]');
      await noOverflow(m, 'details 390');
      await m.evaluate(() => document.querySelector('[data-part="details"]').scrollIntoView()); // the card may re-render meanwhile
      await t.areaShot(m, AREA, 'details-phone');
      await setState(t, m, 'would_hold');
      await m.goto(t.url + '/#/backups/changes/G-7');
      await m.waitForSelector('.bk-changes .bk-change');
      await noOverflow(m, 'changes 390');
      await t.areaShot(m, AREA, 'changes-phone');
      await m.goto(t.url + '/#/backups/setup');
      await m.waitForSelector('.bk-blocks .bk-block');
      await noOverflow(m, 'setup 390');
      await t.areaShot(m, AREA, 'setup-phone');
      await m.goto(t.url + '/#/backups/run/J-41');
      await m.waitForSelector('.bk-output');
      await noOverflow(m, 'run 390');
      await t.areaShot(m, AREA, 'run-phone');
      await setState(t, m, 'healthy');
    });

    await step(AREA + ': the guard report and its change list: 3,000 entries a page at a time, grouped by folder, replaced files shown, hostile names as text, filters and search', async () => {
      await setState(t, page, 'would_hold');
      await openCard(page, t.url);
      await page.click('[data-action="see-changes"]');
      await page.waitForSelector('.bk-changes .bk-change');
      assert(/^#\/backups\/changes\/G-7$/.test(await page.evaluate(() => location.hash)), 'the change list has its own route');
      assert(/wait for you to look first/.test(await page.textContent('[data-part="verdict"]')), 'the verdict in words');
      assert(/report only/i.test(await page.textContent('[data-part="verdict"]')), 'says it is a report');
      assert(await page.locator('.bk-checks .check-row').count() === 11, 'G1 to G11');
      assert(await page.locator('.bk-guard-runs a[data-run]').count() === 2, 'the report links its runs');
      assert(await page.getAttribute('.bk-checks [data-check="G10"]', 'data-result') === 'hold', 'G10 would hold');
      assert(/1–500 of 3k/.test(await page.textContent('.bk-range')), 'the first page: ' + await page.textContent('.bk-range'));
      assert(/1\.8k removed under Photos\/2019\//.test(await page.textContent('.bk-groups')), 'grouped by top folders: ' + await page.textContent('.bk-groups'));
      await page.click('[data-action="next"]');
      await page.waitForFunction(() => /501–1000/.test(document.querySelector('.bk-range').textContent));
      await page.selectOption('[data-focus="f-kind"]', 'replaced');
      await page.waitForFunction(() => /of 120$/.test(document.querySelector('.bk-range').textContent));
      assert(/replaced by a similar file: Films\/better cut 0\.mkv/.test(await page.textContent('.bk-changes')), 'replaced shown with its new file');
      await page.selectOption('[data-focus="f-kind"]', '');
      await page.fill('[data-focus="f-q"]', 'notes/');
      // an event redraws the page while the words are typed and not yet applied: they stay
      const reloaded = page.waitForResponse((r) => /\/api\/snapraid$/.test(r.url()));
      await setState(t, page, 'would_hold');
      await reloaded;
      await page.waitForTimeout(150);
      assert(await page.inputValue('[data-focus="f-q"]') === 'notes/', 'the typed words survive a redraw');
      await page.press('[data-focus="f-q"]', 'Enter');
      await page.waitForFunction(() => /of 8$/.test(document.querySelector('.bk-range').textContent));
      const paths = await page.$$eval('.bk-path', (els) => els.map((e) => e.textContent));
      assert(paths.includes("Notes/<img src=x onerror=alert('backups')>.jpg"), 'markup shows as text');
      assert(paths.some((x) => x.includes('‮')), 'a right-to-left override stays in the text');
      assert(paths.includes('Notes/esc\\daped.txt') && paths.includes('Notes/two\\\\slashes.txt'), 'escaped forms show as names');
      assert(paths.some((x) => x.length > 600), 'a 600-character name');
      assert(await page.locator('#backups-root img, #backups-root script').count() === 0, 'no markup rendered');
      await page.fill('[data-focus="f-q"]', '');
      await page.press('[data-focus="f-q"]', 'Enter');
      await page.waitForFunction(() => /of 3k$/.test(document.querySelector('.bk-range').textContent));
      await t.areaShot(page, AREA, 'changes-desktop');
      await noOverflow(page, 'changes 1440');
    });

    await step(AREA + ': a run is reached by clicking (Details, a guard report) and pages its output by bytes; the setup page with its copy blocks, each move its own checked step', async () => {
      await setState(t, page, 'healthy');
      await openCard(page, t.url);
      await page.click('[data-action="details"]');
      await page.waitForSelector('.bk-runs [data-run="J-41"] a');
      assert(await page.locator('.bk-lab-list [data-lab]').count() === 3, 'Details lists the three lab checks with their ids');
      assert(/L8: .*not run: needs a server started by systemd/.test(await page.textContent('.bk-lab-list')), 'each says why it was not run');
      await page.evaluate(() => document.querySelector('[data-part="details"]').scrollIntoView()); // the card may re-render meanwhile
      await t.areaShot(page, AREA, 'details-desktop');
      await page.click('.bk-runs [data-run="J-41"] a');
      await page.waitForSelector('.bk-output');
      assert(/^#\/backups\/run\/J-41$/.test(await page.evaluate(() => location.hash)), 'Details opens the run page');
      assert(/snapraid -c \/etc\/snapraid\.conf -l \S+ status$/.test(await page.textContent('[data-part="argv"]')), 'the exact command line');
      const out = await page.textContent('.bk-output');
      assert(out.includes("<img src=x onerror=alert('backups')>") && out.includes('<b>not bold</b>'), 'the output is text');
      assert(await page.locator('[data-action="more"]').count() === 1, 'more output on request');
      while (await page.locator('[data-action="more"]').count()) {
        const before = (await page.textContent('.bk-output')).length;
        await page.click('[data-action="more"]');
        await page.waitForFunction((n) => document.querySelector('.bk-output').textContent.length > n, before);
      }
      const all = await page.textContent('.bk-output');
      for (const line of ['line 0: ', 'line 199: ', 'line 399: ', 'not-utf8-']) assert(all.split(line).length === 2, 'paged by bytes: ' + line + ' once');
      await t.areaShot(page, AREA, 'run-desktop');
      await setState(t, page, 'would_hold');
      await page.goto(t.url + '/#/backups/changes/G-7');
      await page.waitForSelector('.bk-guard-runs a[data-run="J-40"]');
      await page.click('.bk-guard-runs a[data-run="J-40"]');
      await page.waitForFunction(() => location.hash === '#/backups/run/J-40' && document.querySelector('.bk-output'));
      await page.goto(t.url + '/#/backups/setup');
      await page.waitForSelector('.bk-blocks .bk-block');
      const titles = await page.$$eval('.bk-blocks .bk-block .check-name', (els) => els.map((e) => e.textContent));
      assert(t.canon(titles) === t.canon(['Check SnapRAID', 'Install the disk protection reader', 'Install its settings', 'Allow Seraphina to call it, and nothing else', 'Check']), 'the five blocks: ' + titles);
      assert(/<stage the files first>/.test(await page.textContent('.bk-blocks')), 'before staging the blocks say so');
      await t.waitPost(page, /\/api\/snapraid\/setup\/stage$/, () => page.click('[data-action="stage"]'));
      await page.waitForFunction(() => /snapraid\/setup\/sf-snapraid/.test(document.querySelector('.bk-blocks').textContent));
      assert(/\.new/.test(await page.textContent('.bk-block[data-block="2"]')) && /if not/i.test(await page.textContent('.bk-block[data-block="2"]')), 'install to .new, then check, then move');
      const firsts = await page.$$eval('.bk-blocks .bk-block > .check-main > pre', (els) => els.map((e) => e.textContent));
      assert(firsts.length === 4 && firsts.every((x) => !/(^|\s)mv\s/.test(x)), 'no first step moves a file: ' + firsts.join(' | '));
      const thens = await page.$$eval('.bk-blocks .bk-then pre', (els) => els.map((e) => e.textContent));
      assert(thens.length === 3 && /sha256sum -c && sudo mv /.test(thens[0]) && /explain helper-config > \/dev\/null && sudo mv /.test(thens[1]) && /visudo -cf \S+ && sudo mv /.test(thens[2]), 'every move is its own step, chained on its check: ' + thens.join(' | '));
      assert(await page.locator('.bk-blocks [data-action="copy-then"]').count() === 3, 'the move step has its own Copy');
      assert(await page.locator('.bk-link-blocks .bk-block').count() === 8, 'the link has its own blocks');
      assert(/notify_result = \/usr\/local\/libexec\/seraphina\/sf-snapraidd notify result/.test(await page.textContent('.bk-daemon-lines')), 'the lines the service needs');
      await t.waitPost(page, /\/api\/snapraid\/setup\/check$/, () => page.click('[data-action="check"]'));
      await page.waitForSelector('.callout-info');
      assert(await page.locator('[data-part="removal"] .bk-block').count() === 2, 'the removal blocks');
      await t.areaShot(page, AREA, 'setup-desktop');
      await noOverflow(page, 'setup 1440');
    });

    await step(AREA + ': Check now asks for a read (202) and the card comes back; Turn it on saves snapraid.enabled', async () => {
      await setState(t, page, 'healthy');
      await openCard(page, t.url);
      const r = await t.waitPost(page, /\/api\/snapraid\/refresh$/, () => page.click('[data-action="check-diff"]'));
      assert(r.status() === 202, 'refresh answers 202: ' + r.status());
      assert(JSON.parse(r.request().postData()).what === 'diff', 'what: diff');
      await setState(t, page, 'off');
      await openCard(page, t.url, 'off');
      await page.click('[data-action="turn-on"]');
      await page.waitForSelector('dialog.modal[open]');
      const saved = await t.waitPost(page, /\/api\/config$/, () => page.click('dialog.modal[open] .modal-foot .btn-primary'));
      assert(t.canon(JSON.parse(saved.request().postData())) === t.canon({ snapraid: { enabled: true } }), 'only snapraid.enabled');
      await page.waitForFunction(() => document.querySelector('.bk-light') && document.querySelector('.bk-light').dataset.code === 'ok', null, { timeout: 8000 });
      await setState(t, page, 'healthy');
    });

    await step(AREA + ': Home: amber with no versioned backup however green, err when the light is red; failing and stale in Needs you; no planned Backups card (U6)', async () => {
      // Home keeps each card's last answer; a fresh load reads the state just set.
      const homeCard = async (state, want) => {
        await setState(t, page, state);
        await page.goto(t.url + '/#/home');
        await page.reload();
        await page.waitForFunction((re) => { const s = document.querySelector('.home-card[data-section="backups"] .home-status-text'); return s && new RegExp(re).test(s.textContent); }, want, { timeout: 8000 });
        return page.$eval('.home-card[data-section="backups"]', (e) => ({ cls: e.className, status: e.querySelector('.home-status-text').textContent, detail: (e.querySelector('.home-card-detail') || {}).textContent || '' }));
      };
      const off = await homeCard('off', '^Disk protection: Seraphina is not watching');
      assert(!/: seraphina/.test(off.status), 'a reason that starts with a name keeps its capital: ' + off.status);
      const green = await homeCard('healthy', '^Disk protection: protected up to');
      assert(/has-warn/.test(green.cls) && /No versioned backup exists yet/.test(green.detail), 'amber with no versioned backup: ' + JSON.stringify(green));
      assert(await page.locator('.home-card.is-planned[data-section="backups"]').count() === 0, 'the planned Backups card is gone');
      const red = await homeCard('failing_disk', '^Disk protection: disk 2');
      assert(/has-err/.test(red.cls), 'err when red: ' + red.cls);
      await page.waitForSelector('.needs-item[data-need="backups:smart_fail"][data-tone="err"]');
      await t.areaShot(page, AREA, 'home-failing-desktop');
      const staleRed = await homeCard('stale_failing', '^Disk protection: disk 2 says it is failing');
      assert(/has-err/.test(staleRed.cls), 'red stays red on Home while the facts are old: ' + staleRed.cls);
      await page.waitForSelector('.needs-item[data-need="backups:smart_fail"][data-tone="err"]');
      for (const [state, need, tone] of [['daemon_frozen', 'frozen', 'warn'], ['daemon_unknown', 'unknown_outcome', 'warn'], ['daemon_lapsed', 'lapsed', 'warn'],
        ['daemon_two', 'two_schedulers', 'warn'], ['daemon_auth_off', 'auth_off', 'err']]) {
        await homeCard(state, '^Disk protection: ');
        const shown = await page.$$eval('.needs-item', (els) => els.map((e) => e.dataset.need));
        if (!shown.includes('backups:' + need) && await page.locator('[data-action="needs-more"]').count()) await page.click('[data-action="needs-more"]');
        await page.waitForSelector('.needs-item[data-need="backups:' + need + '"][data-tone="' + tone + '"]', { timeout: 5000 });
      }
      await homeCard('stale', '^Disk protection: last checked');
      const needs = await page.$$eval('.needs-item', (els) => els.map((e) => e.dataset.need));
      const more = await page.locator('[data-action="needs-more"]').count();
      assert(needs.includes('backups:stale') || more === 1, 'stale is among Needs you: ' + needs);
      if (more) {
        await page.click('[data-action="needs-more"]');
        await page.waitForSelector('.needs-item[data-need="backups:stale"][data-tone="warn"]');
      }
      await setState(t, page, 'healthy');
    });
  } finally {
    await d.ctx.close();
    await p.ctx.close();
  }
}
