// Checks for Messages & Wakes (DESIGN §34 "GUI interface notes (W1)", §35 "GUI interface notes (W2, W2b, W3)"): the view module swfactory/web/views/messages.js
// against tests/gui_mock/messages.py, run by tests/gui/smoke.mjs (AREA_SUITES) after the core checks, or alone with
// GUI_ONLY=messages node tests/gui/smoke.mjs. Screenshots of every new screen, desktop 1440x900 and phone 390x844:
// t.areaShot(page, AREA, name) → $GUI_AREA_SHOTS/messages/<name>.png.
//
// run(t) gets the harness of smoke.mjs: { browser, url, startMock, step, assert, canon, waitPost, shot, cleanShot,
// noOverflow, watch, allowHttp, newPage(label, {phone, width, height, colorScheme, mockUrl}), areaShot(page, area, name) }.
// t.newPage gives a signed-in page of its own ({ctx, page, url}); close its ctx when done.

const AREA = 'messages';
const PASSPHRASE = 'amber-cedar-fjord-lantern-quartz-42';   // tests/gui_mock/messages.py
const ACK = 'I understand my AI agents can read my messages and send them anywhere';

export async function run(t) {
  const { step, assert, canon, waitPost, noOverflow } = t;
  const d = await t.newPage(AREA);
  const p = await t.newPage(AREA + '-phone', { phone: true });
  const page = d.page;
  /** A request from the page (the session cookie), for mock hooks and API checks. */
  const call = (pg, path, body, method) => pg.evaluate(async ([pp, b, m]) => {
    const r = await fetch(pp, { method: m || (b === undefined ? 'GET' : 'POST'), headers: b === undefined ? {} : { 'Content-Type': 'application/json' }, body: b === undefined ? undefined : JSON.stringify(b), credentials: 'same-origin' });
    let data = null;
    try { data = await r.json(); } catch (e) { data = null; }
    return { status: r.status, body: data };
  }, [path, body, method]);
  const mockState = (body) => call(page, '/__mock/messages/state', body);
  const body = (resp) => JSON.parse(resp.request().postData() || '{}');
  // A real page load each time (a query that changes): what the page shows comes from the server, not an earlier visit.
  let loads = 0;
  const go = async (pg, hash, sel) => { await pg.goto(t.url + '/?load=' + (++loads) + hash); await pg.waitForSelector(sel || '#view-messages:not([hidden]) #messages-root .view-title'); };
  /** Home's "Needs you" item for key, after Show all when more than four wait. */
  const homeNeed = async (pg, key) => {
    await pg.goto(t.url + '/?load=' + (++loads) + '#/home');
    await pg.waitForSelector('.needs .needs-head');
    await pg.waitForFunction(() => !/checking/.test((document.querySelector('.needs .needs-head') || {}).textContent || ''));
    if (await pg.locator('[data-action="needs-more"][aria-expanded="false"]').count()) await pg.click('[data-action="needs-more"][aria-expanded="false"]');
    await pg.waitForSelector('.needs-item[data-need="' + key + '"]');
  };
  const text = (pg, sel) => pg.textContent(sel);
  const noMarkup = async (pg, root) => {
    const n = await pg.$$eval(root + ' img, ' + root + ' script, ' + root + ' b, ' + root + ' i, ' + root + ' a[href^="javascript"]', (els) => els.length);
    assert(n === 0, root + ': markup from data was rendered as HTML (' + n + ' elements)');
  };
  /** A screenshot with sel in the middle of the screen (a note above a form, a card further down). */
  const shotAt = async (pg, sel, name) => { await pg.$eval(sel, (e) => e.scrollIntoView({ block: 'center' })); await t.areaShot(pg, AREA, name); };
  const confirmOk = async (pg) => { await pg.waitForSelector('dialog[open] button[data-action="ok"]'); await pg.click('dialog[open] button[data-action="ok"]'); };

  try {
    await step(AREA + ': the inbox lists every delivery state, a removed text, a summary, answers and a late command; hostile text stays text; desktop and phone fit', async () => {
      await go(page, '#/messages', '.mw-msg[data-message="M-000012"]');
      await page.waitForSelector('.mw-late[data-inbound="I-000008"]');
      assert(await page.getAttribute('.rail-item[data-view="messages"]', 'aria-current') === 'page', 'the rail marks Messages');
      const states = await page.$$eval('#messages-root .mw-dchip', (els) => Array.from(new Set(els.map((e) => e.dataset.state))).sort());
      assert(canon(states) === canon(['failed', 'held', 'off', 'queued', 'sending', 'sent', 'snoozed', 'unknown']), 'every delivery state: ' + states);
      const first = await page.$eval('.mw-msg[data-message="M-000012"]', (e) => ({ cls: e.className, title: e.querySelector('.mw-row-title').textContent, about: e.querySelector('.mw-about').getAttribute('href') }));
      assert(/is-unread/.test(first.cls) && /is-urgent/.test(first.cls), 'unread and urgent: ' + first.cls);
      assert(first.title === "Job 002 needs you <img src=x onerror=alert('msg')>", 'the title shows as written: ' + first.title);
      assert(first.about === '#/job/my-app/002-rate-limit-signup-form', 'about links to the job: ' + first.about);
      assert(/Text removed after 90 days/.test(await text(page, '.mw-msg[data-message="M-000005"] .mw-row-title')), 'a removed text says so');
      assert(await page.getAttribute('.mw-msg[data-message="M-000009"] .mw-in-summary', 'href') === '#/messages/m/M-000010', 'a held notice links to the summary that carried it');
      assert(/In M-000010/.test(await text(page, '.mw-msg[data-message="M-000009"] .mw-in-summary')), 'In <M-id>');
      assert(await page.$eval('.mw-msg.is-answer[data-message="M-000014"]', (e) => getComputedStyle(e).opacity === '1' && getComputedStyle(e.querySelector('.mw-row-title')).color !== getComputedStyle(document.querySelector('.mw-msg:not(.is-answer) .mw-row-title')).color),
        'an answer to a phone command is muted by colour, never by opacity (contrast)');
      const held = await page.locator('.mw-msg[data-message="M-000009"] a.mw-row-main').ariaSnapshot();
      assert(!(await page.getAttribute('.mw-msg[data-message="M-000009"] a.mw-row-main', 'aria-label')) && /phone: Held/.test(held), 'a row is named by its content, its delivery states included: ' + held);
      assert(/Unread\. Your server Urgent/.test(await page.locator('.mw-msg[data-message="M-000012"] a.mw-row-main').ariaSnapshot()), 'unread and urgent are in the name');
      assert(/You sent “Quiet until morning” from telegram .*; it arrived late and was not applied\./.test(await text(page, '.mw-late[data-inbound="I-000008"]')), 'the late command, in words');
      const order = await page.$$eval('#messages-root .mw-list > li', (els) => els.map((e) => e.dataset.message || e.dataset.inbound));
      assert(order.indexOf('I-000008') > order.indexOf('M-000012') && order.indexOf('I-000008') < order.indexOf('M-000011'), 'the late command is merged by time: ' + order.join(','));
      await noMarkup(page, '#messages-root');
      await noOverflow(page, 'inbox desktop');
      await t.areaShot(page, AREA, 'inbox-desktop');
      await go(p.page, '#/messages', '.mw-msg[data-message="M-000012"]');
      await p.page.waitForSelector('.mw-late[data-inbound="I-000008"]');
      await noOverflow(p.page, 'inbox phone');
      await t.areaShot(p.page, AREA, 'inbox-phone');
    });

    await step(AREA + ': filters go into the URL (ids and words only) and narrow the list', async () => {
      await page.check('[data-filter="unread"]');
      await page.waitForFunction(() => /unread=1/.test(location.hash) && document.querySelectorAll('#messages-root .mw-msg').length === 2);
      await page.selectOption('[data-filter="state"]', 'failed');
      await page.waitForFunction(() => /state=failed/.test(location.hash));
      await page.goto(t.url + '/#/messages?state=failed');
      await page.waitForFunction(() => Array.from(document.querySelectorAll('#messages-root .mw-msg')).map((e) => e.dataset.message).join() === 'M-000011');
      assert(!(await page.$('.mw-late')), 'late commands are not merged into a filtered list');
    });

    await step(AREA + ': a message opens over the inbox with what each phone got; Got it, Snooze, Send now and Send again post; Esc comes back', async () => {
      await go(page, '#/messages', '.mw-msg[data-message="M-000012"]');
      await page.focus('.mw-msg[data-message="M-000012"] .mw-row-link');
      await page.keyboard.press('Enter');
      await page.waitForSelector('#drawer:not([hidden]) .mw-delivery[data-channel="telegram"]');
      assert(await page.evaluate(() => location.hash) === '#/messages/m/M-000012', 'the URL carries the id only');
      assert(!/needs you/i.test(await page.title()), 'the message never goes into the page title: ' + await page.title());
      const tg = await page.$eval('#drawer .mw-delivery[data-channel="telegram"]', (e) => ({ why: e.querySelector('.mw-why').textContent, text: e.querySelector('.mw-rendered-text').textContent, buttons: Array.from(e.querySelectorAll('.mw-rendered-buttons .tag')).map((b) => b.textContent) }));
      assert(tg.why === 'Heads-up only: Telegram can read it' && tg.text === 'Something needs you.', 'the heads-up and its why: ' + JSON.stringify(tg));
      assert(canon(tg.buttons) === canon(['Got it', 'Snooze 1 hour']), 'its buttons as verbs: ' + tg.buttons);
      assert(/<b>\(raw\)<\/b>/.test(await text(page, '#drawer .mw-delivery[data-channel="phone"] .mw-rendered-text')), 'the exact text sent, as text');
      assert(await page.$('#drawer .mw-inbound[data-inbound="I-000008"]'), 'what came back for this notice');
      const kindRow = await page.$eval('#drawer .mw-facts', (dl) => { const dt = Array.from(dl.querySelectorAll('dt')).find((d) => d.textContent === 'Kind'); return dt ? dt.nextElementSibling.textContent : null; });
      assert(kindRow === 'A job stops for your decision (about your server or your jobs)', 'the kind in plain words: ' + kindRow);
      await noMarkup(page, '#drawer');
      await t.areaShot(page, AREA, 'message-desktop');
      const acked = await waitPost(page, /\/api\/messages\/M-000012\/ack$/, () => page.click('#drawer [data-action="ack"]'));
      assert(acked.status() === 200, 'ack');
      await page.waitForFunction(() => (document.querySelector('.rail-item[data-view="messages"] .rail-badge') || {}).textContent === '1');
      await page.selectOption('#drawer .mw-snooze-min', '30');
      const snooze = await waitPost(page, /\/api\/messages\/M-000012\/snooze$/, () => page.click('#drawer [data-action="snooze"]'));
      assert(canon(body(snooze)) === canon({ minutes: 30 }), 'snooze minutes: ' + snooze.request().postData());
      await page.keyboard.press('Escape');
      await page.waitForFunction(() => location.hash === '#/messages' && document.getElementById('drawer').hidden);
      await page.waitForFunction(() => document.activeElement && document.activeElement.dataset.focus === 'msg:M-000012', null, { timeout: 4000 });
      await go(page, '#/messages/m/M-000011', '#drawer:not([hidden]) .mw-delivery[data-channel="phone"]');
      assert(/rsync: <b>connection reset<\/b> see https:\/\/evil\.example\/x/.test(await text(page, '#drawer .mw-detail')), 'the hook’s detail as text');
      assert(/connection refused <i>\(ntfy\)<\/i>/.test(await text(page, '#drawer .mw-delivery[data-channel="phone"] .mw-error-text')), 'the delivery error as text');
      const retry = await waitPost(page, /\/api\/messages\/M-000011\/retry$/, () => page.click('#drawer [data-action="retry"]'));
      assert(retry.status() === 200, 'Send now');
      await page.click('#drawer [data-action="resend"]');
      assert(/it may have arrived/i.test(await text(page, 'dialog[open]')), 'Send again warns first');
      const resend = await waitPost(page, /\/api\/messages\/M-000011\/resend$/, () => page.click('dialog[open] button[data-action="ok"]'));
      assert(resend.status() === 200, 'Send again');
      await noMarkup(page, '#drawer');
      await go(p.page, '#/messages/m/M-000012', '#drawer:not([hidden]) .mw-delivery[data-channel="telegram"]');
      await noOverflow(p.page, 'message phone');
      await t.areaShot(p.page, AREA, 'message-phone');
    });

    await step(AREA + ': live events: a new notice and a failed delivery show at once, with no text in the event', async () => {
      await go(page, '#/messages', '.mw-msg[data-message="M-000012"]');
      const before = Number(await text(page, '.rail-item[data-view="messages"] .rail-badge'));
      const r = await call(page, '/__mock/messages/notice', { title: 'Job 004 failed <i>x</i>' });
      const id = r.body.message.id;
      await page.waitForSelector('.mw-msg[data-message="' + id + '"]');
      await page.waitForFunction((n) => Number((document.querySelector('.rail-item[data-view="messages"] .rail-badge') || {}).textContent) === n + 1, before);
      await call(page, '/__mock/messages/deliver', { message: 'M-000007', channel: 'telegram', state: 'failed' });
      await page.waitForSelector('.mw-msg[data-message="M-000007"] .mw-dchip[data-channel="telegram"][data-state="failed"]');
    });

    await step(AREA + ': Apply now applies a late command once; the button goes, and the API then answers “it was applied already”', async () => {
      const resp = await waitPost(page, /\/api\/notify\/inbound\/I-000008\/apply$/, () => page.click('.mw-late[data-inbound="I-000008"] [data-action="apply"]'));
      assert(resp.status() === 200, 'applied');
      await page.waitForFunction(() => /Applied from these pages/.test((document.querySelector('.mw-late[data-inbound="I-000008"]') || {}).textContent || '') && !document.querySelector('.mw-late[data-inbound="I-000008"] [data-action="apply"]'));
      t.allowHttp(true);
      const again = await call(page, '/api/notify/inbound/I-000008/apply', {});
      t.allowHttp(false);
      assert(again.status === 400 && again.body.error === 'it was applied already', 'a second apply is refused: ' + JSON.stringify(again));
    });

    await step(AREA + ': what came back: your words, forged and foreign rows as security lines, strangers with their count; a kind filter', async () => {
      await go(page, '#/messages/inbound', '.mw-inbound[data-kind="forged"]');
      assert(/is-security/.test(await page.getAttribute('.mw-inbound[data-kind="forged"]', 'class')) && /Not from your phone/.test(await text(page, '.mw-inbound[data-kind="forged"]')), 'forged: a security row');
      assert(/Not from your phone/.test(await text(page, '.mw-inbound[data-kind="foreign"]')), 'foreign: a security row');
      assert(/3 stranger messages today/.test(await text(page, '.mw-inbound[data-kind="stranger"]')), 'strangers with their count');
      const mine = await text(page, '.mw-inbound[data-kind="text"]');
      assert(/You wrote \(from phone\)/.test(mine) && /Has a link or was forwarded/.test(mine) && /<a href='javascript:alert\(1\)'>link<\/a> token=\*\*\*/.test(mine), 'your words as text, marked: ' + mine);
      assert(/Applied from these pages/.test(await text(page, '.mw-inbound[data-kind="cockpit"]')), 'the cockpit line of the late command');
      await noMarkup(page, '#messages-root');
      await t.areaShot(page, AREA, 'inbound-desktop');
      await page.selectOption('[data-filter="kind"]', 'forged');
      await page.waitForFunction(() => location.hash === '#/messages/inbound?kind=forged' && Array.from(document.querySelectorAll('.mw-inbound')).every((e) => e.dataset.kind === 'forged'));
      await go(p.page, '#/messages/inbound', '.mw-inbound[data-kind="forged"]');
      await noOverflow(p.page, 'inbound phone');
      await t.areaShot(p.page, AREA, 'inbound-phone');
    });

    await step(AREA + ': banners: stopped (Restart), settings changed outside the cockpit (Accept and restart), and a strip on every other page; Home lists them', async () => {
      await mockState({ paused: 'human', via: 'phone' });
      await go(page, '#/messages', '.mw-banner[data-banner="paused"]');
      assert(/Seraphina is stopped \(from phone at/.test(await text(page, '.mw-banner[data-banner="paused"]')), 'the stop banner names where it came from');
      await page.goto(t.url + '/?load=' + (++loads) + '#/factory');
      await page.waitForSelector('.mw-strip:not([hidden])');
      assert(/Seraphina is stopped/.test(await text(page, '.mw-strip')), 'a strip on another page');
      const strip = await page.$eval('.mw-strip', (e) => ({ pad: getComputedStyle(e).paddingLeft, display: getComputedStyle(e).display, text: e.innerText.replace(/\s+/g, ' ') }));
      assert(strip.pad !== '0px' && strip.display === 'flex' && /still come\. Open Messages/.test(strip.text), 'the strip is styled before a Messages page ever opened: ' + JSON.stringify(strip));
      await t.areaShot(page, AREA, 'strip-on-factory-desktop');
      await homeNeed(page, 'paused');
      await go(page, '#/messages', '.mw-banner[data-banner="paused"]');
      assert(await page.isHidden('.mw-strip'), 'the strip hides on this area’s own pages');
      const restart = await waitPost(page, /\/api\/wakes\/control$/, () => page.click('.mw-banner[data-banner="paused"] [data-action="restart"]'));
      assert(canon(body(restart)) === canon({ action: 'resume' }), 'Restart: ' + restart.request().postData());
      await page.waitForFunction(() => !document.querySelector('.mw-banner[data-banner="paused"]'));
      await mockState({ tamper: ['notify', 'pairing'], paused: 'tamper-check' });
      await go(page, '#/messages', '.mw-banner[data-banner="tamper"]');
      const tb = await text(page, '.mw-banner[data-banner="tamper"]');
      assert(/Settings changed outside these pages: phone message settings, paired phones\. Phones can only stop until you look\./.test(tb), 'tamper in words: ' + tb);
      assert(await page.getAttribute('.mw-banner[data-banner="tamper"] a[href="#/messages/settings"]', 'href'), 'Show the settings');
      await t.areaShot(page, AREA, 'banners-desktop');
      await go(p.page, '#/messages', '.mw-banner[data-banner="tamper"]');
      await noOverflow(p.page, 'banners phone');
      await t.areaShot(p.page, AREA, 'banners-phone');
      const acc = await waitPost(page, /\/api\/wakes\/review\/restart$/, () => page.click('.mw-banner[data-banner="tamper"] [data-action="review-restart"]'));
      assert(acc.status() === 200, 'Accept and restart');
      await page.waitForFunction(() => !document.querySelector('.mw-banner[data-banner="tamper"]') && !document.querySelector('.mw-banner[data-banner^="paused"]'));
    });

    await step(AREA + ': without the sandbox: the banner, the acknowledgement (the exact sentence or a 400), the error-styled reminder and Withdraw; the other banners and chips', async () => {
      await mockState({ reset: true });
      await mockState({ sandbox: false });
      await go(page, '#/messages', '.mw-banner[data-banner="precondition"]');
      await page.click('.mw-banner[data-banner="precondition"] [data-action="acknowledge"]');
      await page.waitForSelector('dialog[open] textarea[data-field="ack"]');
      await page.fill('dialog[open] textarea[data-field="ack"]', 'I understand');
      t.allowHttp(true);
      await page.click('dialog[open] button[data-action="ok"]');
      await page.waitForSelector('dialog[open] .form-error:not([hidden])');
      t.allowHttp(false);
      assert((await text(page, 'dialog[open] .form-error')).includes('type exactly: ' + ACK), 'the 400 names the sentence');
      await page.fill('dialog[open] textarea[data-field="ack"]', ACK);
      const ack = await waitPost(page, /\/api\/wakes\/acknowledge$/, () => page.click('dialog[open] button[data-action="ok"]'));
      assert(canon(body(ack)) === canon({ text: ACK }), 'the sentence is sent');
      await page.waitForSelector('.mw-banner.callout-err[data-banner="acknowledged"]');
      const wd = await waitPost(page, /\/api\/wakes\/acknowledge$/, () => page.click('.mw-banner[data-banner="acknowledged"] [data-action="withdraw"]'));
      assert(canon(body(wd)) === canon({ withdraw: true }), 'Withdraw');
      await page.waitForSelector('.mw-banner[data-banner="precondition"]');
      await mockState({ sandbox: true, acknowledged: true });
      await go(page, '#/messages', '.mw-banner[data-banner="acknowledged"]');
      assert(/The sandbox is on now: you can withdraw the acceptance\./.test(await text(page, '.mw-banner[data-banner="acknowledged"]')) && await page.$('.mw-banner.callout-info[data-banner="acknowledged"]'),
        'accepted while the sandbox is on: no “turn on the sandbox”');
      await waitPost(page, /\/api\/wakes\/acknowledge$/, () => page.click('.mw-banner[data-banner="acknowledged"] [data-action="withdraw"]'));
      await page.waitForFunction(() => !document.querySelector('.mw-banner[data-banner="acknowledged"]'));
      await mockState({ sandbox: true, timezone_error: "unknown time zone 'Mars/Base'", time_sync: false, quiet: true });
      await go(page, '#/messages', '.mw-banner[data-banner="timezone"]');
      assert(/Your time zone setting is wrong \(unknown time zone 'Mars\/Base'\); the machine’s zone is used\./.test(await text(page, '.mw-banner[data-banner="timezone"]')), 'time zone banner');
      assert(/Waiting for the clock after boot: nothing is sent yet/.test(await text(page, '.mw-banner[data-banner="time-sync"]')), 'clock banner');
      assert(/Do-not-disturb until 08:00/.test(await text(page, '.mw-chip[data-chip="dnd"]')), 'the do-not-disturb chip');
      await mockState({ reset: true });
    });

    await step(AREA + ': the ntfy wizard: name and address, install, prepare (the passphrase once, never stored), serve, check, the phone with a QR code, a test and pairing', async () => {
      await go(page, '#/messages/setup/ntfy/1', '.mw-wizard[data-step="1"] input[data-field="host"]');
      assert(await page.inputValue('input[data-field="host"]') === 'box.tail1234.ts.net' && await page.inputValue('input[data-field="port"]') === '8445', 'the address is pre-filled from the channel');
      await page.fill('input[data-field="host"]', 'not a host');
      await page.click('[data-action="next"]');
      await page.waitForSelector('.mw-form-error:not([hidden])');
      await page.fill('input[data-field="host"]', 'box.tail1234.ts.net');
      await t.areaShot(page, AREA, 'ntfy-1-desktop');
      const save = await waitPost(page, /^\/api\/config$/, () => page.click('[data-action="next"]'));
      assert(canon(body(save)) === canon({ wakes: { timezone: 'Europe/London' }, notify: { channels: { phone: { type: 'ntfy', enabled: true, publish_url: 'unix:/run/ntfy/ntfy.sock', public_url: 'https://box.tail1234.ts.net:8445', token_env: 'SERAPHINA_NTFY_TOKEN', reply_token_env: 'SERAPHINA_NTFY_REPLY_TOKEN' } } } }), 'step 1 body: ' + save.request().postData());
      await page.waitForSelector('.mw-wizard[data-step="2"] .mw-block[data-block="2a"]');
      await page.waitForFunction(() => document.activeElement && document.activeElement.id === 'mw-step-t' && /Install ntfy/.test(document.activeElement.textContent), null, { timeout: 4000 });
      assert(/sudo apt install ntfy/.test(await text(page, '.mw-block[data-block="2a"] .mw-cmd')), 'copy block 2a');
      await page.click('[data-action="next"]');
      await page.waitForSelector('.mw-wizard[data-step="3"] [data-action="prepare"]');
      await mockState({ installed: false });
      t.allowHttp(true);
      await page.click('[data-action="prepare"]');
      await page.waitForSelector('.mw-form-error:not([hidden])');
      t.allowHttp(false);
      assert(/ntfy is not installed yet: run copy block 2a/.test(await text(page, '.mw-form-error')), 'the 400 sends the owner back: ' + await text(page, '.mw-form-error'));
      assert(await page.$('a[href^="#/messages/setup/ntfy/2"]'), 'a way back to step 2');
      await mockState({ installed: true });
      const prep = await waitPost(page, /\/api\/notify\/ntfy\/prepare$/, () => page.click('[data-action="prepare"]'));
      assert(canon(body(prep)) === canon({ channel: 'phone' }), 'prepare body');
      await page.waitForSelector('.mw-secret .mw-secret-value');
      assert(!(await text(page, '.mw-secret-value')).includes(PASSPHRASE), 'the passphrase is hidden until Show');
      assert(await page.isDisabled('[data-action="next"]'), 'Next waits for “I wrote it down”');
      await page.click('[data-action="reveal"]');
      assert(await text(page, '.mw-secret-value') === PASSPHRASE, 'Show reveals it');
      await page.check('input[data-field="wrote"]');
      await page.waitForFunction(() => !document.querySelector('[data-action="next"]').disabled);
      await t.areaShot(page, AREA, 'ntfy-3-desktop');
      const stored = await page.evaluate(() => { const out = []; for (let i = 0; i < localStorage.length; i++) out.push(localStorage.getItem(localStorage.key(i))); return out.join('\n') + location.href; });
      assert(!stored.includes(PASSPHRASE), 'the passphrase is never in localStorage or the URL');
      await page.click('[data-action="next"]');
      await page.waitForSelector('.mw-wizard[data-step="4"] .mw-block[data-block="2c"]');
      assert(!(await page.content()).includes(PASSPHRASE), 'leaving the step clears the passphrase from the page');
      const order2c = await page.$eval('.mw-block[data-block="2c"]', (b) => { const w = b.querySelector('.callout'); const c = b.querySelector('.mw-cmds'); return !!(w && c && (w.compareDocumentPosition(c) & Node.DOCUMENT_POSITION_FOLLOWING)); });
      assert(order2c, '2c shows its warning before the command');
      assert(await page.$('.mw-block[data-block="2b"]'), 'copy block 2b');
      await page.click('[data-action="next"]');
      await page.waitForSelector('.mw-wizard[data-step="5"] .mw-check-line');
      assert(await page.locator('.mw-check-line.is-ok').count() >= 6, 'the check lines pass');
      assert(await page.locator('.mw-check-line.is-ok:has-text("ntfy does not answer on port 80 (another program")').count() === 1, 'port 80 held by another program is fine, and said so');
      await t.areaShot(page, AREA, 'ntfy-5-desktop');
      await page.click('[data-action="next"]');
      await page.waitForSelector('.mw-wizard[data-step="6"] .mw-pairbox canvas.mw-qr');
      assert((await text(page, '.mw-pairbox')).includes('https://box.tail1234.ts.net:8445'), 'the server address');
      const dark = await page.$eval('canvas.mw-qr', (c) => { const g = c.getContext('2d').getImageData(0, 0, c.width, c.height).data; let n = 0; for (let i = 0; i < g.length; i += 4) if (g[i] < 128) n++; return n; });
      assert(dark > 500, 'the QR code is drawn: ' + dark);
      await t.areaShot(page, AREA, 'ntfy-6-desktop');
      await page.click('[data-action="next"]');
      await page.waitForSelector('.mw-wizard[data-step="7"]');
      const test = await waitPost(page, /\/api\/notify\/test$/, () => page.click('[data-action="test"]'));
      assert(canon(body(test)) === canon({ channel: 'phone' }), 'the test goes to the channel');
      try { await page.waitForSelector('.mw-pair-state[data-state="paired"]', { timeout: 15000 }); } catch (e) {
        const st = await call(page, '/api/notify/pair?channel=phone');
        throw new Error('not paired: the page says ' + JSON.stringify(await text(page, '.mw-step')) + '; the server says ' + JSON.stringify(st));
      }
      // New tokens: the old phone credentials stop working, so step 7 waits for an answer newer than the last one
      // (GET /api/notify/pair keeps saying paired for ntfy once the phone ever answered).
      await page.click('a[href="#/messages/setup"]:has-text("Done")');
      await page.waitForSelector('.mw-channel[data-channel="phone"] [data-action="rotate"]');
      await page.click('.mw-channel[data-channel="phone"] [data-action="rotate"]');
      await waitPost(page, /\/api\/notify\/channels\/phone\/rotate$/, () => page.click('dialog[open] button[data-action="ok"]'));
      await page.waitForSelector('dialog[open] .mw-secret');
      await page.click('dialog[open] button[data-action="ok"]');
      await page.evaluate(() => { location.hash = '#/messages/setup/ntfy/7?channel=phone'; });
      await page.waitForSelector('.mw-wizard[data-step="7"] .mw-pair-state');
      await page.waitForTimeout(2600);   // a poll or two: the server says paired (an old answer); the page keeps waiting
      assert(await page.getAttribute('.mw-pair-state', 'data-state') === 'waiting' && /It last answered/.test(await text(page, '.mw-step')), 'after New tokens step 7 waits: ' + await text(page, '.mw-step'));
      await t.areaShot(page, AREA, 'ntfy-7-after-new-tokens-desktop');
      await waitPost(page, /\/api\/notify\/test$/, () => page.click('[data-action="test"]'));
      await page.waitForSelector('.mw-pair-state[data-state="paired"]', { timeout: 15000 });
      await go(p.page, '#/messages/setup/ntfy/1', '.mw-wizard[data-step="1"]');
      await noOverflow(p.page, 'ntfy wizard phone');
      await t.areaShot(p.page, AREA, 'ntfy-1-phone');
      await go(p.page, '#/messages/setup/ntfy/5', '.mw-wizard[data-step="5"] .mw-check-line');
      await noOverflow(p.page, 'ntfy check phone');
      await t.areaShot(p.page, AREA, 'ntfy-5-phone');
    });

    await step(AREA + ': the Telegram wizard: the bot token (never shown back), turn it on, pair with a code and a QR link, confirm the 6 digits (wrong, then right), test', async () => {
      await call(page, '/api/config', { notify: { channels: { telegram: { operational: true } } } });   // chosen in Message settings
      await go(page, '#/messages/setup/telegram/1', '.mw-wizard[data-step="1"] input[data-field="bot-token"]');
      assert(await page.getAttribute('input[data-field="bot-token"]', 'type') === 'password', 'a password field');
      await page.fill('input[data-field="bot-token"]', '123456:BOT-SECRET');
      const sec = await waitPost(page, /^\/api\/secrets$/, () => page.click('[data-action="next"]'));
      assert(canon(body(sec)) === canon({ name: 'SERAPHINA_TELEGRAM_BOT_TOKEN', value: '123456:BOT-SECRET' }), 'the token goes to the env file');
      await page.waitForSelector('.mw-wizard[data-step="2"]');
      assert(!(await page.content()).includes('BOT-SECRET'), 'the token is gone from the page');
      await page.waitForFunction(() => document.activeElement && document.activeElement.id === 'mw-step-t', null, { timeout: 4000 });
      await page.waitForSelector('input[data-field="operational"]:checked');
      const on = await waitPost(page, /^\/api\/config$/, () => page.click('[data-action="next"]:not([disabled])'));
      assert(canon(body(on)) === canon({ wakes: { timezone: 'Europe/London' }, notify: { channels: { telegram: { type: 'telegram', enabled: true, token_env: 'SERAPHINA_TELEGRAM_BOT_TOKEN' } } } }), 'step 2 body (operational kept as it was): ' + on.request().postData());
      const tgCfg = await call(page, '/api/config');
      assert(tgCfg.body.notify.channels.telegram.operational === true, 'the choice from Message settings stays');
      await call(page, '/api/config', { notify: { channels: { telegram: { operational: false } } } });
      await page.waitForSelector('.mw-wizard[data-step="3"] [data-action="pair"]');
      assert(!(await page.$('.mw-code')), 'no code until Pair is pressed');
      const pair = await waitPost(page, /\/api\/notify\/pair$/, () => page.click('[data-action="pair"]'));
      assert(canon(body(pair)) === canon({ channel: 'telegram' }), 'pair body');
      await page.waitForSelector('.mw-wizard[data-step="3"] .mw-code');
      assert(await text(page, '.mw-code') === '/start Kq7mZpX2rT9w', 'the text to send');
      const link = await page.$eval('[data-action="open-telegram"]', (a) => [a.getAttribute('href'), a.getAttribute('target'), a.getAttribute('rel')]);
      assert(link[0] === 'https://t.me/seraphina_box_bot?start=Kq7mZpX2rT9w' && link[1] === '_blank' && /noopener/.test(link[2]), 'the link: ' + link);
      assert(await page.$('.mw-pairbox canvas.mw-qr'), 'a QR code for the link');
      assert(await page.$('[data-action="copy-code"]'), 'the text to send has a Copy button');
      await t.areaShot(page, AREA, 'telegram-3-desktop');
      await page.waitForSelector('.mw-wizard[data-step="4"] .mw-pair-state[data-state="confirm"]', { timeout: 15000 });
      const who = await text(page, '.mw-pair-state');
      assert(/Sam/.test(who) && who.includes('@new_<i>phone</i>'), 'who wrote to the bot, as text: ' + who);
      t.allowHttp(true);
      await page.fill('input[data-field="code"]', 'abc');
      await page.click('[data-action="confirm"]');
      await page.waitForSelector('.mw-form-error:not([hidden])');
      t.allowHttp(false);
      assert(/the code is the 6 digits your bot sent you/.test(await text(page, '.mw-form-error')), 'a 400 for a bad code');
      await page.fill('input[data-field="code"]', '000000');
      await waitPost(page, /\/api\/notify\/pair\/confirm$/, () => page.click('[data-action="confirm"]'));
      await page.waitForFunction(() => /not right/.test((document.getElementById('mw-tg-code-h') || {}).textContent || ''));
      await t.areaShot(page, AREA, 'telegram-4-desktop');
      await page.fill('input[data-field="code"]', '123456');
      const ok = await waitPost(page, /\/api\/notify\/pair\/confirm$/, () => page.press('input[data-field="code"]', 'Enter'));
      assert(canon(body(ok)) === canon({ channel: 'telegram', code: '123456' }), 'Enter in the code field pairs: ' + ok.request().postData());
      await page.waitForSelector('.mw-wizard[data-step="5"]');
      assert(/Two-Step Verification/.test(await text(page, '.mw-step')), 'the last step says to turn on Two-Step Verification');
      // It leaked unpairs every account: Pair now (the toast's link) starts a new pairing, never the old code or "Paired."
      await page.click('a[href="#/messages/setup"]:has-text("Done")');
      await page.waitForSelector('.mw-channel[data-channel="telegram"] [data-action="leaked"]');
      await page.click('.mw-channel[data-channel="telegram"] [data-action="leaked"]');
      await page.fill('dialog[open] input[data-field="bot-token"]', '777:LEAKED-NEW');
      await waitPost(page, /\/api\/notify\/channels\/telegram\/leaked$/, () => page.click('dialog[open] button[data-action="ok"]'));
      await page.click('.toast:has-text("Pair your phone again") .toast-action');
      await page.waitForSelector('.mw-wizard[data-step="3"]');
      assert(await page.$('[data-action="pair"]') && !(await page.$('.mw-pair-state')) && !(await page.$('.mw-code')), 'after It leaked, step 3 offers Pair again: ' + await text(page, '.mw-step'));
      await t.areaShot(page, AREA, 'telegram-3-after-leaked-desktop');
      // the channel the URL names wins over the one this page remembered
      await page.evaluate(() => { location.hash = '#/messages/setup/telegram/1?channel=work-bot'; });
      await page.waitForFunction(() => { const a = document.querySelector('.mw-steps li:nth-child(2) a'); return !!a && /channel=work-bot/.test(a.getAttribute('href')); });
      await mockState({ seed_pairing: true });   // the next step starts from the seeded paired account
      await go(p.page, '#/messages/setup/telegram/3', '.mw-wizard[data-step="3"] [data-action="pair"]');
      await p.page.route('**/api/notify/pair?*', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ state: 'waiting', account: null }) }));
      await p.page.click('[data-action="pair"]');
      await p.page.waitForSelector('.mw-wizard[data-step="3"] .mw-code');
      await p.page.waitForTimeout(1200);   // the page's first reads after loading have landed (as a person reads, then long-presses)
      await p.page.evaluate(() => { const c = document.querySelector('.mw-code'); c.dataset.probe = '1'; const r = document.createRange(); r.selectNodeContents(c); getSelection().removeAllRanges(); getSelection().addRange(r); });
      await p.page.waitForTimeout(4300);   // two polls that answer "waiting"
      const kept = await p.page.evaluate(() => ({ same: !!document.querySelector('.mw-code[data-probe]'), sel: getSelection().toString() }));
      assert(kept.same && kept.sel === '/start Kq7mZpX2rT9w', 'a poll that only waits redraws nothing (a long-press selection survives): ' + JSON.stringify(kept));
      await p.page.unroute('**/api/notify/pair?*');
      await noOverflow(p.page, 'telegram wizard phone');
      await t.areaShot(p.page, AREA, 'telegram-3-phone');
    });

    await step(AREA + ': phone setup: privacy in words, health, paired accounts as text; test, unpair, new tokens (Telegram asks for the token, ntfy shows the passphrase once), lost phone', async () => {
      await go(page, '#/messages/setup', '.mw-channel[data-channel="telegram"] [data-account]');
      assert(/Private: only over your tailnet/.test(await text(page, '.mw-channel[data-channel="phone"]')), 'ntfy is private');
      assert(/Third party: Telegram \(or the server\) can read it/.test(await text(page, '.mw-channel[data-channel="telegram"]')), 'Telegram is a third party');
      assert((await text(page, '.mw-channel[data-channel="telegram"] [data-account="424242"]')).includes('Sam <img src=x>') && (await text(page, '.mw-channel[data-channel="telegram"]')).includes('@owner_<b>me</b>'), 'Telegram names as text');
      assert(/healthy <b>\(raw\)<\/b>/.test(await text(page, '.mw-channel[data-channel="phone"]')), 'health detail as text');
      await noMarkup(page, '#messages-root');
      await t.areaShot(page, AREA, 'setup-desktop');
      const test = await waitPost(page, /\/api\/notify\/test$/, () => page.click('.mw-channel[data-channel="telegram"] [data-action="test"]'));
      assert(test.status() === 202, 'a test is 202');
      await page.waitForSelector('.toast .toast-action');
      await page.click('.mw-channel[data-channel="telegram"] [data-account="424242"] [data-action="unpair"]');
      const un = await waitPost(page, /\/api\/notify\/unpair$/, () => page.click('dialog[open] button[data-action="ok"]'));
      assert(canon(body(un)) === canon({ channel: 'telegram', chat_id: 424242 }), 'unpair body');
      await page.click('.mw-channel[data-channel="telegram"] [data-action="rotate"]');
      await page.waitForSelector('dialog[open] input[data-field="bot-token"][type="password"]');
      await page.fill('dialog[open] input[data-field="bot-token"]', '999:NEW');
      const rot = await waitPost(page, /\/api\/notify\/channels\/telegram\/rotate$/, () => page.click('dialog[open] button[data-action="ok"]'));
      assert(canon(body(rot)) === canon({ token: '999:NEW' }), 'Telegram rotate sends the new token');
      await page.click('.mw-channel[data-channel="phone"] [data-action="leaked"]');
      const leak = await waitPost(page, /\/api\/notify\/channels\/phone\/leaked$/, () => page.click('dialog[open] button[data-action="ok"]'));
      assert(leak.status() === 200, 'It leaked');
      await page.waitForSelector('dialog[open] .mw-secret');
      assert(!(await text(page, 'dialog[open] .mw-secret-value')).includes(PASSPHRASE), 'the new passphrase waits for Show');
      assert(await page.locator('dialog[open] .mw-block').count() === 3, 'three copy blocks');
      await page.click('dialog[open] button[data-action="ok"]');
      await page.click('.mw-channel[data-channel="phone"] [data-action="disable"]');
      await waitPost(page, /\/api\/notify\/channels\/phone\/disable$/, () => page.click('dialog[open] button[data-action="ok"]'));
      await page.waitForSelector('.mw-channel[data-channel="phone"] [data-action="enable"]');
      assert(/This channel is turned off/.test(await text(page, '.mw-channel[data-channel="phone"]')), 'the card says it is off');
      await mockState({ sandbox: false });
      t.allowHttp(true);
      const refusedOn = await waitPost(page, /^\/api\/config$/, () => page.click('.mw-channel[data-channel="phone"] [data-action="enable"]'));
      await page.waitForFunction(() => /phone messages stay off while your AI agents run without the sandbox/.test((document.querySelector('.mw-channel[data-channel="phone"] .mw-form-error:not([hidden])') || {}).textContent || ''));
      t.allowHttp(false);
      assert(refusedOn.status() === 400 && canon(body(refusedOn)) === canon({ notify: { channels: { phone: { enabled: true } } } }), 'Turn on: ' + refusedOn.request().postData());
      await shotAt(page, '.mw-channel[data-channel="phone"]', 'setup-turn-on-refused-desktop');
      await mockState({ sandbox: true });
      await waitPost(page, /^\/api\/config$/, () => page.click('.mw-channel[data-channel="phone"] [data-action="enable"]'));
      await page.waitForSelector('.mw-channel[data-channel="phone"] [data-action="disable"]');
      assert(await page.$('.mw-channel[data-channel="phone"] .mw-form-error[hidden]'), 'the refusal goes once it is on');
      await page.click('[data-action="lost-phone"]');
      const lost = await waitPost(page, /\/api\/notify\/lost-phone$/, () => page.click('dialog[open] button[data-action="ok"]'));
      assert(lost.status() === 200, 'Lost my phone');
      await page.waitForSelector('dialog[open] .mw-done li');
      const done = await text(page, 'dialog[open] .mw-done');
      assert(/stopped publishing to phone/.test(done) && /stopped Seraphina/.test(done), 'what was done: ' + done);
      await page.click('dialog[open] button[data-action="ok"]');
      await page.waitForSelector('.mw-banner[data-banner="paused"]');
      await page.waitForSelector('.mw-channel[data-channel="phone"] [data-action="enable"]');   // Lost my phone turned it off: Turn on brings it back
      await t.areaShot(page, AREA, 'setup-after-lost-phone-desktop');
      await go(p.page, '#/messages/setup', '.mw-channel[data-channel="telegram"]');
      await noOverflow(p.page, 'setup phone');
      await t.areaShot(p.page, AREA, 'setup-phone');
      await mockState({ reset: true });
    });

    await step(AREA + ': settings: phone messages off and on, what Telegram may carry (with its warning), the rulebook (stricter only), sensitive words, event sources and an event', async () => {
      await go(page, '#/messages/settings', '#mw-rules [data-action="save-rules"]');
      assert(/powers off at 23:00 and on again at 07:00/.test(await text(page, '#mw-status')) && /from config\.json until your first change/.test(await text(page, '#mw-status')), 'the status card');
      const off = await waitPost(page, /^\/api\/config$/, () => page.click('[data-focus="notify-enabled"]'));
      assert(canon(body(off)) === canon({ notify: { enabled: false } }), 'off: ' + off.request().postData());
      t.allowHttp(true);
      const refused = await call(page, '/api/notify/pair', { channel: 'telegram' });
      t.allowHttp(false);
      assert(refused.status === 400 && /turned off \(notify\.enabled\)/.test(refused.body.error), 'pairing is refused while off');
      await page.waitForFunction(() => document.querySelector('[data-focus="notify-enabled"]') && !document.querySelector('[data-focus="notify-enabled"]').checked);
      const back = await waitPost(page, /^\/api\/config$/, () => page.click('[data-focus="notify-enabled"]'));
      assert(canon(body(back)) === canon({ notify: { enabled: true } }), 'on again');
      await page.waitForFunction(() => document.querySelector('[data-focus="notify-enabled"]').checked);
      await page.route('**/api/config', (route) => (route.request().method() === 'POST'
        ? route.fulfill({ status: 400, contentType: 'application/json', body: JSON.stringify({ error: 'notify.enabled: refused for this test' }) }) : route.continue()));
      t.allowHttp(true);
      await waitPost(page, /^\/api\/config$/, () => page.click('[data-focus="notify-enabled"]'));
      await page.waitForFunction(() => /refused for this test/.test((document.querySelector('#mw-enabled .mw-form-error') || {}).textContent || ''));
      await call(page, '/__mock/publish', { type: 'channel.health', data: { channel: 'phone', ok: true } });   // an event re-renders the page
      await page.waitForTimeout(900);
      t.allowHttp(false);
      await page.unroute('**/api/config');
      assert(await page.isChecked('[data-focus="notify-enabled"]'), 'a refused switch shows what the server still has (on)');
      assert(await page.isChecked('[data-focus="chon:phone"]') && await page.isChecked('[data-focus="chon:telegram"]'), 'each channel has its own on switch');
      await page.check('#mw-chopts input[data-repo="my-app"]');
      const safe = await waitPost(page, /^\/api\/config$/, () => page.click('[data-action="save-safe"]'));
      assert(canon(body(safe)) === canon({ notify: { safe_repos: ['my-app'] } }), 'safe projects: ' + safe.request().postData());
      await page.waitForFunction(() => /Only these projects: my-app/.test((document.querySelector('#mw-chopts') || {}).textContent || ''));
      await shotAt(page, '#mw-chopts [data-safe="repos"]', 'settings-channels-desktop');
      await page.waitForSelector('.mw-chopt[data-channel="telegram"] [data-warn="telegram"][hidden]', { state: 'attached' });
      await page.click('[data-focus="ops:telegram"]');
      await page.waitForSelector('.mw-chopt[data-channel="telegram"] [data-warn="telegram"]:not([hidden])');
      const ch = await waitPost(page, /^\/api\/config$/, () => page.click('[data-focus="save-ch:telegram"]'));
      assert(canon(body(ch)) === canon({ notify: { channels: { telegram: { operational: true, operational_names: false } } } }), 'channel body: ' + ch.request().postData());
      await page.fill('#mw-rules input[data-bound="max_body"]', '400');
      t.allowHttp(true);
      const weak = await waitPost(page, /\/api\/notify\/rules$/, () => page.click('[data-action="save-rules"]'));
      assert(weak.status() === 400, 'a weaker boundary is refused');
      await page.waitForFunction(() => /stricter, never weaker/.test((document.querySelector('#mw-rules .mw-form-error') || {}).textContent || ''));
      t.allowHttp(false);
      await page.fill('#mw-rules input[data-bound="max_body"]', '250');
      await page.selectOption('#mw-rules [data-focus="r:quiet-mode"]', 'window');
      await page.fill('#mw-rules [data-focus="r:quiet-window"]', '22:00-07:30');
      const rules = await waitPost(page, /\/api\/notify\/rules$/, () => page.click('[data-action="save-rules"]'));
      assert(canon(body(rules)) === canon({ quiet_hours: '22:00-07:30', boundary: { max_body: 250 } }), 'only the changed rules: ' + rules.request().postData());
      await page.waitForFunction(() => /notice-rules\.json: one rulebook/.test(document.querySelector('#mw-rules').textContent));
      t.allowHttp(true);
      const moved = await call(page, '/api/config', { notify: { quiet_hours: 'auto' } });
      t.allowHttp(false);
      assert(moved.status === 400 && /lives in notice-rules\.json now/.test(moved.body.error), 'config patches to the rulebook’s keys are refused once the file exists');
      await page.click('[data-action="edit-terms"]');
      await page.waitForSelector('textarea[data-field="terms"]');
      await page.waitForFunction(() => document.querySelector('textarea[data-field="terms"]').value.includes('my street <b>name</b>'));
      await page.fill('textarea[data-field="terms"]', 'Dr. Example\ninsulin');
      const terms = await waitPost(page, /\/api\/notify\/terms$/, () => page.click('[data-action="save-terms"]'));
      assert(canon(body(terms)) === canon({ text: 'Dr. Example\ninsulin' }), 'terms body');
      await page.waitForSelector('[data-action="edit-terms"]');
      assert(!(await page.content()).includes('insulin'), 'the words leave the page when the editor closes');
      await mockState({ sandbox: false });
      await page.click('[data-action="edit-terms"]');
      await page.waitForSelector('textarea[data-field="terms"]');
      t.allowHttp(true);
      await page.click('[data-action="save-terms"]');
      await page.waitForFunction(() => /readable by your AI agents/.test((document.querySelector('#mw-terms .mw-form-error') || {}).textContent || ''));
      t.allowHttp(false);
      await page.click('[data-action="cancel-terms"]');
      await mockState({ reset: true });
      await page.click('[data-action="hook-add"]');
      await page.waitForSelector('dialog[open] input[data-field="hook-name"]');
      await page.fill('dialog[open] input[data-field="hook-name"]', 'nas-check');
      await page.uncheck('dialog[open] input[data-kind="backup.ok"]');
      await page.check('dialog[open] input[data-kind="disk.low"]');
      await page.check('dialog[open] input[data-persona="seraphina"]');
      await t.areaShot(page, AREA, 'settings-add-source-desktop');
      const add = await waitPost(page, /\/api\/notify\/hooks$/, () => page.click('dialog[open] button[data-action="ok"]'));
      assert(add.status() === 201 && canon(body(add)) === canon({ name: 'nas-check', events: ['backup.failed', 'disk.low'], per_minute: 10, per_day: 200, may: ['event', 'wake'], personas: ['seraphina'] }), 'hook body: ' + add.request().postData());
      await page.waitForSelector('dialog[open] .mw-secret');
      assert(!(await text(page, 'dialog[open] .mw-secret-value')).startsWith('hk_'), 'the token waits for Show');
      await page.click('dialog[open] [data-action="reveal"]');
      assert((await text(page, 'dialog[open] .mw-secret-value')).startsWith('hk_'), 'the token, once');
      assert(await page.$('dialog[open] .mw-block[data-block="header"]'), 'its snippets');
      await page.click('dialog[open] button[data-action="ok"]');
      await page.waitForSelector('#mw-hooks [data-hook="nas-check"]');
      assert(/Reports: A backup fails, Disk space runs low · may wake Seraphina/.test(await text(page, '#mw-hooks [data-hook="nas-check"]')), 'the source in words: ' + await text(page, '#mw-hooks [data-hook="nas-check"]'));
      await page.click('#mw-hooks [data-hook="nas-check"] [data-action="hook-rotate"]');
      await waitPost(page, /\/api\/notify\/hooks\/nas-check\/rotate$/, () => page.click('dialog[open] button[data-action="ok"]'));
      await page.waitForSelector('dialog[open] .mw-secret');
      await page.click('dialog[open] button[data-action="ok"]');
      await page.click('#mw-hooks [data-hook="nas-check"] [data-action="hook-delete"]');
      await waitPost(page, /\/api\/notify\/hooks\/nas-check\/delete$/, () => page.click('dialog[open] button[data-action="ok"]'));
      await page.waitForFunction(() => !document.querySelector('#mw-hooks [data-hook="nas-check"]'));
      await page.click('[data-action="report-event"]');
      await page.selectOption('dialog[open] select[data-field="event-kind"]', 'disk.low');
      await page.fill('dialog[open] input[data-field="event-subject"]', 'srv');
      const ev = await waitPost(page, /\/api\/notify\/events$/, () => page.click('dialog[open] button[data-action="ok"]'));
      assert(ev.status() === 202 && canon(body(ev)) === canon({ kind: 'disk.low', subject: 'srv' }), 'event body');
      await go(page, '#/messages/settings', '#mw-rules');
      await t.areaShot(page, AREA, 'settings-desktop');
      await go(p.page, '#/messages/settings', '#mw-rules');
      await noOverflow(p.page, 'settings phone');
      await t.areaShot(p.page, AREA, 'settings-phone');
    });

    await step(AREA + ': the schedule: personas, the queue with the gate’s words, cancel, reminders (private phones only, moved), servers and their attestation, the breaker, stop and restart', async () => {
      await go(page, '#/schedule', '.mw-persona[data-persona="seraphina"]');
      assert(/Her instructions changed outside her page/.test(await text(page, '.mw-persona[data-persona="seraphina"]')), 'the review line');
      assert(/Muted until/.test(await text(page, '.mw-persona[data-persona="mira"]')), 'a mute with its end');
      assert((await text(page, '[data-wake="W-000043"]')).includes('Not now: someone is using the model.'), 'an owner message waiting at gate 13');
      const w44 = await text(page, '#mw-queue [data-wake="W-000044"]');
      assert(/moved to 07:45/.test(w44) && /outside text/.test(w44) && /details in the run log/.test(w44), 'moved, tainted, with a reason: ' + w44);
      assert(/<b>\(raw\)<\/b>/.test(await text(page, '#mw-queue [data-wake="W-000042"]')), 'gate words as text');
      assert(await page.$('#mw-queue [data-wake="W-000045"][data-state="running"] a[href="#/chat/c-1a2b3c4d5e"]'), 'a running chat check-in');
      await page.click('#mw-queue [data-wake="W-000046"] [data-action="cancel-wake"]');
      await waitPost(page, /\/api\/wakes\/W-000046\/cancel$/, () => page.click('dialog[open] button[data-action="ok"]'));
      await page.waitForFunction(() => !document.querySelector('#mw-queue [data-wake="W-000046"]'));
      assert(/Call the plumber <b>before 5<\/b>/.test(await text(page, '#mw-reminders [data-reminder="R-000001"]')), 'reminder text as text');
      await page.click('#mw-reminders .mw-add > summary');
      const shows = await page.$$eval('#mw-reminders input[data-channel]', (els) => els.map((e) => e.dataset.channel));
      assert(canon(shows) === canon(['phone']), 'show_on offers the private phones only: ' + shows);
      const tomorrow = new Date(Date.now() + 86400000);
      const day = tomorrow.getFullYear() + '-' + String(tomorrow.getMonth() + 1).padStart(2, '0') + '-' + String(tomorrow.getDate()).padStart(2, '0');
      await page.fill('#mw-reminders input[data-field="rem-at"]', day + 'T23:30');
      await page.fill('#mw-reminders textarea[data-field="rem-text"]', 'Take the bins out');
      await page.check('#mw-reminders input[data-channel="phone"]');
      const rem = await waitPost(page, /\/api\/wakes\/reminders$/, () => page.click('[data-action="add-reminder"]'));
      const rb = body(rem);
      assert(rem.status() === 201 && rb.text === 'Take the bins out' && canon(rb.show_on) === canon(['phone']) && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$/.test(rb.at), 'reminder body: ' + rem.request().postData());
      if (/T23:30:00Z$/.test(rb.at)) await page.waitForSelector('#mw-reminders .callout');
      await page.click('#mw-reminders [data-reminder="R-000001"] [data-action="cancel-reminder"]');
      await page.waitForFunction(() => /Cancelled/.test(document.querySelector('#mw-reminders [data-reminder="R-000001"]').textContent));
      assert(/This address is Open WebUI \(or a proxy\), not a model server/.test(await text(page, '#mw-servers [data-server="openwebui"]')), 'a detected Open WebUI address');
      const att = await waitPost(page, /\/api\/wakes\/servers\/strata\/attest$/, () => page.click('#mw-servers [data-focus="attest:strata"]'));
      assert(canon(body(att)) === canon({ local: true }), 'attest body');
      await t.areaShot(page, AREA, 'schedule-desktop');
      await mockState({ breaker: 'more than 20 model wakes in an hour' });
      await go(page, '#/schedule', '.mw-banner[data-banner="breaker"]');
      assert(/Seraphina stopped herself: more than 20 model wakes in an hour/.test(await text(page, '.mw-banner[data-banner="breaker"]')), 'the breaker');
      await waitPost(page, /\/api\/wakes\/control$/, () => page.click('.mw-banner[data-banner="breaker"] [data-action="restart"]'));
      await page.waitForFunction(() => !document.querySelector('.mw-banner[data-banner="breaker"]'));
      await page.click('[data-action="stop-all"]');
      const stop = await waitPost(page, /\/api\/wakes\/control$/, () => page.click('dialog[open] button[data-action="ok"]'));
      assert(canon(body(stop)) === canon({ action: 'pause' }), 'Stop Seraphina');
      await page.waitForSelector('.mw-banner[data-banner="paused"]');
      await waitPost(page, /\/api\/wakes\/control$/, () => page.click('.mw-summary [data-action="restart"]'));
      await page.waitForFunction(() => !document.querySelector('.mw-banner[data-banner="paused"]'));
      await go(p.page, '#/schedule', '.mw-persona[data-persona="seraphina"]');
      await noOverflow(p.page, 'schedule phone');
      await t.areaShot(p.page, AREA, 'schedule-phone');
    });

    await step(AREA + ': a persona’s page: stop with an end time and restart, the review of a change outside the page, the clean tick, a stale save (409) and the approved text back', async () => {
      await go(page, '#/schedule/persona/seraphina', '.mw-phead [data-action="stop"]');
      assert(await page.isDisabled('.mw-phead [data-action="run-now"]') && /review the change/i.test(await page.getAttribute('.mw-phead [data-action="run-now"]', 'title')),
        'Run now waits for the review of the outside change (her next wake would stop her)');
      assert(/Her next wake stops her until you review the change/.test(await text(page, '#messages-root .mw-body .callout-human')), 'not stopped yet, said so');
      await page.fill('[data-focus="p:until"]', '18:30');
      const stop = await waitPost(page, /\/api\/wakes\/control$/, () => page.click('[data-action="stop"]'));
      assert(canon(body(stop)) === canon({ action: 'pause', persona: 'seraphina', until: '18:30' }), 'stop her until: ' + stop.request().postData());
      await page.waitForSelector('.mw-phead [data-action="restart-persona"]');
      const rs = await waitPost(page, /\/api\/wakes\/control$/, () => page.click('[data-action="restart-persona"]'));
      assert(canon(body(rs)) === canon({ action: 'resume', persona: 'seraphina' }), 'restart her');
      const snd = await waitPost(page, /\/api\/personas\/seraphina\/sound$/, () => page.click('[data-action="sound"]'));
      assert(snd.status() === 200, 'Allow sound');
      await t.areaShot(page, AREA, 'persona-overview-desktop');
      await go(page, '#/schedule/persona/seraphina/instructions', '#mw-p-review .mw-diff');
      assert(await page.locator('#mw-p-review .mw-dl.is-add').count() === 1 && await page.locator('#mw-p-review .mw-dl.is-del').count() === 1, 'the diff, line by line');
      assert((await text(page, '#mw-p-review .mw-diff')).includes('<b>Never</b>'), 'the diff as text');
      await noMarkup(page, '#messages-root');
      await t.areaShot(page, AREA, 'persona-instructions-desktop');
      await waitPost(page, /\/api\/personas\/seraphina\/integrity\/approve$/, () => page.click('[data-action="approve"]'));
      await page.waitForFunction(() => !document.querySelector('#mw-p-review'));
      await page.waitForFunction(() => !document.querySelector('.mw-phead [data-action="run-now"]').disabled);
      const pg = await call(page, '/api/personas/seraphina');
      const clean = await waitPost(page, /\/api\/personas\/seraphina\/privacy$/, () => page.click('[data-focus="soul:clean"]'));
      assert(canon(body(clean)) === canon({ what: 'soul', ref: pg.body.soul.file_sha256, clean: true }), 'the clean tick names the exact file: ' + clean.request().postData());
      // Let the page settle: the tick reads her page again, and a field replaced in the middle of fill() takes the
      // new text after the old one (Playwright selects, then inserts).
      await page.waitForTimeout(1500);
      await page.fill('textarea[data-field="soul"]', 'You are Seraphina. Be brief.');
      const saved = await waitPost(page, /\/api\/personas\/seraphina\/soul$/, () => page.click('[data-action="save-soul"]'));
      assert(canon(body(saved)) === canon({ text: 'You are Seraphina. Be brief.', base_sha256: pg.body.soul.file_sha256 }), 'save body: ' + saved.request().postData());
      await page.waitForFunction(() => document.querySelector('textarea[data-field="soul"]') && document.querySelector('textarea[data-field="soul"]').value === 'You are Seraphina. Be brief.');
      // Let the page settle first: the save's own persona.updated event makes it read her page again shortly after.
      await page.waitForTimeout(1500);
      const opened = (await call(page, '/api/personas/seraphina')).body.soul.file_sha256;
      // The owner starts typing; the file changes outside the page and an event refreshes the page under the edit.
      await page.fill('textarea[data-field="soul"]', 'You are Seraphina. Be very brief.');
      await call(page, '/__mock/messages/soul-changed', { persona: 'seraphina', quiet: true });
      await call(page, '/__mock/publish', { type: 'persona.updated', data: { persona: 'seraphina', what: 'soul' }, agent: 'Seraphina' });
      await page.waitForFunction(() => /Her file changed while you were editing/.test((document.querySelector('#mw-p-soul .callout-warn') || {}).textContent || ''));
      assert(await page.inputValue('textarea[data-field="soul"]') === 'You are Seraphina. Be very brief.', 'the refresh keeps what was typed');
      await shotAt(page, '#mw-p-soul .callout-warn', 'persona-instructions-stale-desktop');
      t.allowHttp(true);
      const stale = await waitPost(page, /\/api\/personas\/seraphina\/soul$/, () => page.click('[data-action="save-soul"]'));
      await page.waitForFunction(() => /reload and edit again/.test((document.querySelector('#mw-p-soul .callout-warn') || {}).textContent || ''));
      await page.waitForTimeout(150);
      t.allowHttp(false);
      assert(stale.status() === 409 && body(stale).base_sha256 === opened, 'the save sends the fingerprint it opened, so the outside change is refused, not overwritten: ' + JSON.stringify(body(stale)).slice(0, 120));
      assert(/the instructions changed since you opened them: reload and edit again/.test(await text(page, '#mw-p-soul .callout-warn')), 'the 409, in its words');
      assert(await page.inputValue('textarea[data-field="soul"]') === 'You are Seraphina. Be very brief.', 'what was typed is kept');
      await page.click('[data-focus="soul:reload"]');
      await page.waitForFunction(() => /Edited by hand\./.test((document.querySelector('textarea[data-field="soul"]') || {}).value || ''), null, { timeout: 8000 });
      await page.waitForSelector('#mw-p-review');
      assert(!(await page.$('#mw-p-soul .callout-warn')), 'the stale note goes with the reload');
      await waitPost(page, /\/api\/personas\/seraphina\/integrity\/restore$/, () => page.click('[data-action="restore"]'));
      await page.waitForFunction(() => !document.querySelector('#mw-p-review'));
      await go(p.page, '#/schedule/persona/seraphina/instructions', 'textarea[data-field="soul"]');
      await noOverflow(p.page, 'persona instructions phone');
      await t.areaShot(p.page, AREA, 'persona-instructions-phone');
    });

    await step(AREA + ': her memory: the weekly review (a 409 makes it stale, then Reject), versions read and rolled back, a save with its base version, who wrote it; a stale review only rejects; external memory hides it', async () => {
      await go(page, '#/schedule/persona/seraphina/memory', '#mw-p-consolidation [data-action="consolidation-accept"]');
      assert(/The owner prefers mornings for news\./.test(await text(page, '#mw-p-consolidation')), 'the proposed lines');
      assert(/Seraphina/.test(await text(page, '#mw-p-writes [data-by="persona"]')), 'a write by the persona');
      await t.areaShot(page, AREA, 'persona-memory-desktop');
      await call(page, '/__mock/messages/memory-bumped', { persona: 'seraphina', quiet: true });
      t.allowHttp(true);
      const acc = await waitPost(page, /\/api\/personas\/seraphina\/consolidation\/accept$/, () => page.click('[data-action="consolidation-accept"]'));
      assert(acc.status() === 409, 'Accept answers 409 when the summary changed');
      await page.waitForFunction(() => /Your summary changed since this was proposed \(now version 4\)/.test((document.querySelector('#mw-p-consolidation') || {}).textContent || ''));
      t.allowHttp(false);
      assert(await page.isDisabled('[data-action="consolidation-accept"]'), 'then only Reject');
      await waitPost(page, /\/api\/personas\/seraphina\/consolidation\/reject$/, () => page.click('[data-action="consolidation-reject"]'));
      await page.waitForFunction(() => !document.querySelector('#mw-p-consolidation'));
      await page.click('#mw-p-versions [data-version="1"] [data-action="read-version"]');
      await page.waitForSelector('dialog[open] .sf-pre');
      assert(/The owner likes short notes\./.test(await text(page, 'dialog[open] .sf-pre')), 'a version’s text');
      await page.click('dialog[open] button[data-action="ok"]');
      await page.click('#mw-p-versions [data-version="2"] [data-action="rollback"]');
      const rb = await waitPost(page, /\/api\/personas\/seraphina\/memory\/rollback$/, () => page.click('dialog[open] button[data-action="ok"]'));
      assert(canon(body(rb)) === canon({ version: 2 }), 'rollback body');
      await page.waitForSelector('#mw-p-versions [data-version="5"]');
      await page.waitForTimeout(1200);   // the rollback's own persona.updated settles
      // She writes version 6 while the owner edits version 5: the event refreshes the page, the base stays 5, the save is a 409.
      await page.fill('textarea[data-field="memory"]', 'The owner rewrote the summary.');
      await call(page, '/__mock/messages/memory-bumped', { persona: 'seraphina' });
      await page.waitForFunction(() => /Version 6 was written by her while you were editing version 5/.test((document.querySelector('#mw-p-memory .callout-warn') || {}).textContent || ''));
      assert(await page.inputValue('textarea[data-field="memory"]') === 'The owner rewrote the summary.', 'what was typed stays');
      await shotAt(page, '#mw-p-memory .callout-warn', 'persona-memory-stale-desktop');
      t.allowHttp(true);
      const staleMem = await waitPost(page, /\/api\/personas\/seraphina\/memory$/, () => page.click('[data-action="save-memory"]'));
      await page.waitForTimeout(300);
      t.allowHttp(false);
      assert(staleMem.status() === 409 && body(staleMem).base_version === 5, 'her version 6 is not overwritten: ' + staleMem.status() + ' ' + staleMem.request().postData());
      assert((await call(page, '/api/personas/seraphina')).body.memory.version === 6, 'version 6 stays hers');
      await page.click('[data-focus="mem:reload"]');
      await page.waitForFunction(() => /She added a line\./.test((document.querySelector('textarea[data-field="memory"]') || {}).value || '') && !document.querySelector('#mw-p-memory .callout-warn'));
      await page.fill('textarea[data-field="memory"]', 'Short notes. Backups at 02:00.');
      await page.check('[data-focus="mem:clean"]');
      const mem = await waitPost(page, /\/api\/personas\/seraphina\/memory$/, () => page.click('[data-action="save-memory"]'));
      assert(canon(body(mem)) === canon({ text: 'Short notes. Backups at 02:00.', base_version: 6, clean: true }), 'memory body: ' + mem.request().postData());
      await go(page, '#/schedule/persona/mira/memory', '#mw-p-consolidation');
      assert(await page.isDisabled('[data-action="consolidation-accept"]') && /now version 1/.test(await text(page, '#mw-p-consolidation')), 'a stale review can only be rejected');
      await mockState({ external_memory: true });
      await go(page, '#/schedule/persona/seraphina/memory', '#messages-root .callout-info');
      assert(/kept by her own tools/.test(await text(page, '#messages-root .mw-body')) && !(await page.$('textarea[data-field="memory"]')), 'external memory: no editor');
      await mockState({ reset: true });
      await go(p.page, '#/schedule/persona/seraphina/memory', '#mw-p-memory');
      await noOverflow(p.page, 'persona memory phone');
      await t.areaShot(p.page, AREA, 'persona-memory-phone');
    });

    await step(AREA + ': journal, thread (a labelled reply, a decline, an untrusted reply), runs, a staged re-import and her inbox; stops that cannot restart or end', async () => {
      await go(page, '#/schedule/persona/seraphina/journal', '#mw-p-journal [data-entry="J-000003"]');
      await waitPost(page, /\/api\/personas\/seraphina\/journal\/J-000003\/retract$/, () => page.click('[data-entry="J-000003"] [data-action="retract"]'));
      await page.waitForFunction(() => !document.querySelector('[data-entry="J-000003"]'));
      await page.fill('[data-focus="j:before"]', '2026-01-01');
      await page.click('[data-action="forget"]');
      const fg = await waitPost(page, /\/api\/personas\/seraphina\/forget$/, () => page.click('dialog[open] button[data-action="ok"]'));
      assert(canon(body(fg)) === canon({ before: '2026-01-01' }), 'forget body');
      await go(page, '#/schedule/persona/seraphina/thread', '.mw-tmsg.is-decline');
      assert(/Declined/.test(await text(page, '.mw-tmsg.is-decline')) && (await text(page, '.mw-tmsg.is-decline')).includes("<img src=x onerror=alert('decline')>"), 'a decline, styled, as text');
      assert(/Written after reading outside text/.test(await text(page, '.mw-tmsg[data-message="T-000006"]')), 'an untrusted reply is marked');
      await page.fill('textarea[data-field="thread"]', 'Hello <b>there</b>');
      const sent = await waitPost(page, /\/api\/personas\/seraphina\/messages$/, () => page.click('[data-action="send-thread"]'));
      assert(sent.status() === 202 && canon(body(sent)) === canon({ text: 'Hello <b>there</b>' }), 'send body');
      await page.waitForFunction(() => Array.from(document.querySelectorAll('.mw-tmsg.is-mine')).some((e) => e.textContent.includes('Hello <b>there</b>')));
      await noMarkup(page, '#messages-root');
      await t.areaShot(page, AREA, 'persona-thread-desktop');
      await go(page, '#/schedule/persona/seraphina/runs', '#mw-p-runs [data-run="04-wake"]');
      await page.click('[data-run="04-wake"] [data-action="open-run"]');
      await page.waitForSelector('[data-run="04-wake"] .mw-runline');
      assert(/tokenizer: same/.test(await text(page, '[data-run="04-wake"] .mw-runlog')) && (await text(page, '[data-run="04-wake"] .mw-runlog')).includes('<script>x</script>'), 'run log lines as text');
      await t.areaShot(page, AREA, 'persona-runs-desktop');
      await go(page, '#/schedule/persona/mira', '#mw-p-reimport');
      assert(/Runs from the imported copy of Open WebUI['’]s kate/.test(await text(page, '#messages-root .mw-body')), 'the copy label');
      assert(/Not read yet/.test(await text(page, '#mw-p-inbox [data-note="N-000002"]')) && /Read on her wake/.test(await text(page, '#mw-p-inbox [data-note="N-000001"]')), 'her inbox, delivered or not');
      assert((await text(page, '#mw-p-reimport')).includes('{{CURRENT_LOCATION}}'), 'flagged placeholders');
      await t.areaShot(page, AREA, 'persona-mira-desktop');
      await waitPost(page, /\/api\/personas\/mira\/reimport\/accept$/, () => page.click('[data-action="reimport-accept"]'));
      await page.waitForFunction(() => !document.querySelector('#mw-p-reimport'));
      await go(page, '#/schedule/persona/mira/thread', '.mw-tmsg .mw-label');
      assert(/from the imported copy: Mira \(version v-41\)/.test(await text(page, '.mw-tmsg .mw-label')), 'a labelled reply');
      await mockState({ until_unread: true });
      await go(page, '#/schedule/persona/seraphina', '.mw-phead [data-stop]');
      assert(/until you restart her \(the end time could not be read\)/.test(await text(page, '.mw-phead [data-stop]')), 'an unreadable end time');
      await mockState({ integrity_stop: true });
      await go(page, '#/schedule/persona/seraphina', '.mw-phead [data-stop="integrity"]');
      assert(!(await page.$('[data-action="restart-persona"]')), 'no Restart until the change is reviewed');
      await mockState({ reset: true });
      await go(p.page, '#/schedule/persona/seraphina/thread', '.mw-tmsg.is-decline');
      await noOverflow(p.page, 'persona thread phone');
      await t.areaShot(p.page, AREA, 'persona-thread-phone');
      await go(p.page, '#/schedule/persona/mira', '.mw-phead');
      await noOverflow(p.page, 'persona overview phone');
      await t.areaShot(p.page, AREA, 'persona-overview-phone');
    });

    await step(AREA + ': each delivery’s outcome in words (DESIGN §40): sent succeeded, a failure, and an unknown outcome that only Send again sends again', async () => {
      // the earlier steps sent M-000011 again and moved M-000012 on: the deliveries are set here, as the Notifier records them
      await call(page, '/__mock/messages/deliver', { message: 'M-000011', channel: 'telegram', state: 'unknown' });
      await call(page, '/__mock/messages/deliver', { message: 'M-000011', channel: 'phone', state: 'failed' });
      for (const channel of ['phone', 'telegram']) await call(page, '/__mock/messages/deliver', { message: 'M-000012', channel, state: 'sent' });
      await go(page, '#/messages/m/M-000011', '#drawer:not([hidden]) .mw-delivery[data-channel="telegram"][data-state="unknown"]');
      const outs = await page.$$eval('#drawer .mw-delivery', (els) => els.map((e) => [e.dataset.channel, (e.querySelector('.mw-outcome') || {}).dataset ? e.querySelector('.mw-outcome').dataset.outcome : null,
        (e.querySelector('.mw-outcome') || { textContent: '' }).textContent, (e.querySelector('.mw-outcome .pill') || { className: '' }).className]));
      const tg = outs.find((o) => o[0] === 'telegram');
      const ph = outs.find((o) => o[0] === 'phone');
      assert(tg[1] === 'unknown' && /^Outcome unknown: it may or may not have happened It may or may not have arrived: the phone service did not confirm it, so it is never sent again by itself\. Look at your phone first: Send again is the only way to send it again\.$/.test(tg[2]) && /tone-warn/.test(tg[3]), 'unknown, amber: ' + JSON.stringify(tg));
      assert(ph[1] === 'failed' && /^Failed refused, or it never left the harness/.test(ph[2]) && /tone-err/.test(ph[3]), 'failed: ' + JSON.stringify(ph));
      assert(await page.locator('#drawer [data-action="resend"]').count() === 1, 'Send again is there for the unknown one');
      await shotAt(page, '#drawer .mw-delivery[data-channel="telegram"]', 'message-outcome-desktop');
      await page.keyboard.press('Escape');
      // a failed try that reached some accounts (notify's partial): never "nothing arrived"
      await call(page, '/__mock/messages/deliver', { message: 'M-000011', channel: 'telegram', state: 'failed', partial: '111:7' });
      await go(page, '#/messages/m/M-000011', '#drawer:not([hidden]) .mw-delivery[data-channel="telegram"][data-state="failed"]');
      const part = await text(page, '#drawer .mw-delivery[data-channel="telegram"] .mw-outcome');
      assert(/^Failed some of your accounts already have it, and it was refused for the others/.test(part) && !/nothing arrived/.test(part), 'a partial failure: ' + part);
      await shotAt(page, '#drawer .mw-delivery[data-channel="telegram"]', 'message-outcome-partial-desktop');
      await page.keyboard.press('Escape');
      await call(page, '/__mock/messages/deliver', { message: 'M-000011', channel: 'telegram', state: 'unknown', partial: '' });
      await go(page, '#/messages/m/M-000012', '#drawer:not([hidden]) .mw-delivery[data-channel="telegram"][data-state="sent"]');
      await page.waitForFunction(() => document.querySelectorAll('#drawer .mw-outcome[data-outcome="succeeded"]').length === 2, null, { timeout: 5000 }).catch(() => {});
      const sent = await page.$$eval('#drawer .mw-outcome', (els) => els.map((e) => [e.dataset.outcome, e.textContent]));
      assert(sent.length === 2 && sent.every((o) => o[0] === 'succeeded' && /^Succeeded the phone service confirmed it\.$/.test(o[1])), 'sent reads succeeded: ' + JSON.stringify(sent));
      await page.keyboard.press('Escape');
    });

    await step(AREA + ': a wake’s command scope on her page: no tools, read-only tools with the default commands, and [] that reads files and harness status but runs no commands', async () => {
      await go(page, '#/schedule/persona/seraphina', '#mw-p-settings');
      const scope = () => text(page, '#mw-p-settings [data-part="commands"]');
      assert(/^None: she has no tools during a wake, so she runs no commands$/.test(await scope()), 'no tools: ' + await scope());
      await mockState({ wake: { tools: 'read_only' } });
      await go(page, '#/schedule/persona/seraphina', '#mw-p-settings');
      assert(/^She reads files and harness status, and may run \(with no network\): git status, git log$/.test(await scope()), 'the default: ' + await scope());
      await mockState({ wake: { tools: 'read_only', commands: [] } });
      await go(page, '#/schedule/persona/seraphina', '#mw-p-settings');
      assert(/^She reads files and harness status; runs no commands$/.test(await scope()), 'commands []: ' + await scope());
      await t.areaShot(page, AREA, 'persona-commands-desktop');
      // her commands set, the read ledger on and no bwrap: none runs (wakes.wake_run_commands), and both pages say why
      await mockState({ wake: { tools: 'read_only', commands: [['git', 'status'], ['df', '-h']] }, ledger: true, sandbox: false });
      await go(page, '#/schedule/persona/seraphina', '#mw-p-settings');
      assert(/runs no commands now\. Her commands are off: the read ledger is on and the sandbox \(bwrap\) cannot be used here/.test(await scope()), 'off beside the ledger: ' + await scope());
      await shotAt(page, '#mw-p-settings', 'persona-commands-off-desktop');
      await go(page, '#/schedule/setup', '#mw-wp-seraphina [data-part="commands-off"]');
      assert(/Her status commands are off now\. The read ledger is on and the sandbox \(bwrap\) cannot be used here/.test(await text(page, '#mw-wp-seraphina [data-part="commands-off"]')), 'setup says it too');
      assert(/her status commands run only inside the sandbox \(bwrap\); without it she runs none/.test(await text(page, '#mw-wp-seraphina .mw-switch:has([data-focus="wp:seraphina:ledger"])')), 'the ledger switch warns');
      await mockState({ sandbox: true });
      await go(page, '#/schedule/persona/seraphina', '#mw-p-settings');
      assert(/may run \(with no network\): git status, df -h$/.test(await scope()), 'with bwrap they run: ' + await scope());
      await mockState({ reset: true });
    });

    await step(AREA + ': Changes before import on a wake persona imported from Open WebUI: the baseline sentence, her receipts as reported (not verified first, never green), a missing one, attach by id (a 400 as its words) and detach', async () => {
      await go(page, '#/schedule/persona/mira', '#mw-p-import');
      const sec = '#mw-p-import';
      const baseline = await text(page, sec + ' [data-part="baseline"]');
      assert(/^At the first import the harness had no earlier copy of this profile, so that import is the baseline: there is no diff of what changed before it/.test(baseline), 'the baseline first, word for word: ' + baseline);
      const rows = await page.$$eval(sec + ' .bi-row', (els) => els.map((e) => ({ id: e.dataset.receipt, state: e.dataset.state, text: e.textContent, green: !!e.querySelector('.tone-ok, .tag-ok'),
        order: (() => { const hs = Array.from(e.querySelectorAll('.bi-h4')).map((x) => x.textContent); return hs.length === 2 ? /^Not verified/.test(hs[0]) && /^Verified/.test(hs[1]) : null; })() })));
      assert(canon(rows.map((r) => r.id)) === canon(['r-20261025-013', 'r-20261025-014', 'r-20261022-002', 'r-20261024-008', 'r-20261025-011', 'r-20261019-000']), 'in the order attached: ' + rows.map((r) => r.id));
      assert(rows.every((r) => !r.green), 'never green');
      assert(rows.filter((r) => r.state !== 'missing').every((r) => r.order === true && /Reported by the operator/.test(r.text)), 'labelled, not verified above verified');
      const row = (id) => rows.find((r) => r.id === id);
      assert(/outcome unknown \(reported\): it may or may not have happened/.test(row('r-20261025-013').text) && /The change: outcome unknown: it may or may not have happened; check before retrying/.test(row('r-20261025-013').text), 'unknown: ' + row('r-20261025-013').text);
      assert(/<i>now<\/i>/.test(row('r-20261025-013').text) && await page.locator(sec + ' i, ' + sec + ' img, ' + sec + ' script').count() === 0, 'her intent stays text');
      assert(/Nothing verified/.test(row('r-20261022-002').text) && /File gone/.test(row('r-20261024-008').text), 'nothing verified, and a file gone');
      assert(row('r-20261019-000').state === 'missing' && /Missing/.test(row('r-20261019-000').text) && /The receipts index no longer holds this receipt\./.test(row('r-20261019-000').text), 'the missing row: ' + row('r-20261019-000').text);
      assert(/attached by the owner|by the owner/.test(row('r-20261025-014').text) && /by the operator/.test(row('r-20261025-013').text), 'who attached each');
      const tones = await page.$$eval(sec + ' .bi-row[data-receipt="r-20261025-013"] .bi-outcome .pill', (els) => els.map((e) => e.className));
      assert(tones.every((c) => /tone-warn/.test(c)), 'unknown is amber: ' + tones);
      assert(/tone-err/.test(await page.getAttribute(sec + ' .bi-row[data-receipt="r-20261025-014"] .bi-outcome .pill', 'class')), 'a reported failure is red');
      await shotAt(page, sec, 'persona-before-import-desktop');
      // attach: an unknown id is refused in the server's words, as the field's error; nothing changes
      await page.click(sec + ' .bi-form > summary');
      await page.fill(sec + ' [data-field="before-import-ids"]', 'r-nope');
      t.allowHttp(true);
      await waitPost(page, /\/api\/personas\/mira\/before-import$/, () => page.click(sec + ' [data-action="attach"]'));
      await page.waitForFunction((s2) => /unknown receipt id r-nope: only a receipt the harness has read can be attached \(scan her receipts first\)/.test((document.querySelector(s2 + ' .bi-err') || {}).textContent || ''), sec);
      t.allowHttp(false);
      // pick by id from her receipts, then attach as the operator
      await page.fill(sec + ' [data-field="before-import-ids"]', '');
      await page.click(sec + ' [data-action="pick-receipts"]');
      await page.waitForSelector(sec + ' .bi-pick-list input[data-receipt="r-20261025-010"]');
      assert(await page.locator(sec + ' .bi-pick-list input[data-receipt="r-20261025-013"]').count() === 0, 'an attached one is not offered again');
      await page.check(sec + ' .bi-pick-list input[data-receipt="r-20261025-010"]');
      await page.selectOption(sec + ' [data-field="before-import-by"]', 'operator');
      const att = await waitPost(page, /\/api\/personas\/mira\/before-import$/, () => page.click(sec + ' [data-action="attach"]'));
      assert(canon(body(att)) === canon({ attach: ['r-20261025-010'], by: 'operator' }), 'attach by id: ' + att.request().postData());
      await page.waitForSelector(sec + ' .bi-row[data-receipt="r-20261025-010"]');
      const det = await waitPost(page, /\/api\/personas\/mira\/before-import$/, () => page.click(sec + ' .bi-row[data-receipt="r-20261019-000"] [data-action="detach"]'));
      assert(canon(body(det)) === canon({ detach: ['r-20261019-000'], by: 'operator' }), 'detach a missing one: ' + det.request().postData());
      await page.waitForFunction((s2) => !document.querySelector(s2 + ' .bi-row[data-receipt="r-20261019-000"]'), sec);
      await go(p.page, '#/schedule/persona/mira', '#mw-p-import');
      await noOverflow(p.page, 'before import phone');
      await shotAt(p.page, '#mw-p-import', 'persona-before-import-phone');
      await call(page, '/__mock/operator/scenario', { name: 'normal' });
    });

    await step(AREA + ': the read ledger of her own memory (opt-in): versions per file, show, compare, prepare a restore that applies nothing, erase with a confirm; a database that could not be read; all private, all text', async () => {
      await mockState({ external_memory: true });
      await go(page, '#/schedule/persona/seraphina/memory', '#mw-p-ledger .mw-ledger-file');
      let led = await text(page, '#mw-p-ledger');
      assert(/The read ledger is off: the harness keeps no copy of what it reads\. Turn it on in Persona setup/.test(led) && /what it kept before is still listed/.test(led), 'off, its old copies still listed: ' + led.slice(0, 300));
      await mockState({ ledger: true });
      await go(page, '#/schedule/persona/seraphina/memory', '#mw-p-ledger .mw-ledger-file');
      led = await text(page, '#mw-p-ledger');
      assert(/The read ledger is on/.test(led) && /Private/.test(led) && /It never writes her memory\./.test(led), 'on and private: ' + led.slice(0, 300));
      assert(/The ledger is at its cap: 3 older copies were removed \(the newest of each file is kept\)\./.test(led), 'the cap: ' + led.slice(0, 500));
      assert(/could not be read \(database is locked\)/.test(led) && !/\b0 rows\b/.test(led), 'a database read that failed is never 0 rows');
      const files = await page.$$eval('#mw-p-ledger .mw-ledger-file', (els) => els.map((e) => [e.dataset.file, e.querySelectorAll('.mw-row').length]));
      assert(canon(files) === canon([['diary.md', 1], ['notes.md', 3], ['people.md', 1]]), 'files and versions: ' + JSON.stringify(files));
      const shownNames = await page.$$eval('#mw-p-ledger .mw-ledger-file .mw-h4 code', (els) => els.map((e) => e.innerText));
      assert(canon(shownNames) === canon(['diary.md', 'notes.md', 'people.md']), 'file names as they are, never in capitals: ' + JSON.stringify(shownNames));
      assert(/A secret taken out/.test(await text(page, '#mw-p-ledger .mw-ledger-file[data-file="notes.md"]')) && /Read in part/.test(await text(page, '#mw-p-ledger .mw-ledger-file[data-file="diary.md"]')), 'redacted and partial reads say so');
      assert(await page.locator('#mw-p-ledger .mw-ledger-file[data-file="diary.md"] [data-action="ledger-restore"]').count() === 0, 'no restore from a partial read');
      assert(await page.locator('#mw-p-ledger .mw-ledger-erased li').count() === 1 && /erased/.test(await text(page, '#mw-p-ledger .mw-ledger-erased')), 'an erased version as a fingerprint');
      const newest = page.locator('#mw-p-ledger .mw-ledger-file[data-file="notes.md"] .mw-row').first();
      const oldest = page.locator('#mw-p-ledger .mw-ledger-file[data-file="notes.md"] .mw-row').last();
      await oldest.locator('[data-action="ledger-show"]').click();
      await page.waitForSelector('dialog[open] .mw-ledger-text');
      assert(/Private: her memory as the harness read it/.test(await text(page, 'dialog[open]')) && /^The owner likes short notes\.\n?$/.test(await text(page, 'dialog[open] .mw-ledger-text')), 'show: the text, private');
      assert(!/notes\.md/.test(await page.title()), 'nothing of hers in the page title');
      await confirmOk(page);
      await oldest.locator('[data-action="ledger-diff"]').click();
      await page.waitForSelector('dialog[open] .mw-diff');
      assert(/\+Backups run at 02:00\./.test(await text(page, 'dialog[open] .mw-diff')) && /<script>alert\('ledger'\)<\/script>/.test(await text(page, 'dialog[open] .mw-diff')) && await page.locator('dialog[open] script').count() === 0, 'compare: a diff, as text');
      await confirmOk(page);
      const rs = await waitPost(page, /\/api\/personas\/seraphina\/external\/restore$/, () => oldest.locator('[data-action="ledger-restore"]').click());
      assert(canon(body(rs)) === canon({ sha256: await oldest.getAttribute('data-version'), file: 'notes.md' }), 'restore names the version and the file: ' + rs.request().postData());
      await page.waitForSelector('dialog[open] .mw-ledger-text');
      const prep = await text(page, 'dialog[open]');
      assert(/The harness never writes her memory: her own tools or you apply this\./.test(prep) && /write this text back to that file in her folder/.test(prep) && /From her newest read to this version/.test(prep), 'prepared, never applied: ' + prep.slice(0, 400));
      await page.waitForTimeout(250);   // the dialog's fade-in, so the shot shows it whole
      await t.areaShot(page, AREA, 'persona-ledger-restore-desktop');
      await confirmOk(page);
      const before = await page.locator('#mw-p-ledger .mw-ledger-erased li').count();
      await newest.locator('[data-action="ledger-erase"]').click();
      await page.waitForSelector('dialog[open]');
      assert(/Its copy is deleted and the ledger keeps only its fingerprint; her own note is not touched\./.test(await text(page, 'dialog[open]')), 'erase asks first');
      const er = await waitPost(page, /\/api\/personas\/seraphina\/external\/erase$/, () => page.click('dialog[open] button[data-action="ok"]'));
      assert(Object.keys(body(er)).join() === 'sha256', 'erase sends the sha256 only');
      await page.waitForFunction((n) => document.querySelectorAll('#mw-p-ledger .mw-ledger-erased li').length === n + 1, before);
      await noMarkup(page, '#mw-p-ledger');
      await shotAt(page, '#mw-p-ledger', 'persona-ledger-desktop');
      await go(p.page, '#/schedule/persona/seraphina/memory', '#mw-p-ledger .mw-ledger-file');
      await noOverflow(p.page, 'ledger phone');
      await shotAt(p.page, '#mw-p-ledger', 'persona-ledger-phone');
      await mockState({ reset: true });
    });

    await step(AREA + ': chat check-ins: each chat’s switch as it stands, its waiting and running check-ins, turned off and on', async () => {
      await go(page, '#/schedule/chats', '.mw-chatrow[data-chat="c-1a2b3c4d5e"] input[data-chat]:not([disabled])');
      await page.waitForSelector('.mw-chatrow[data-chat="c-9f8e7d6c5b"] input[data-chat]:not([disabled])');
      assert(await page.isChecked('.mw-chatrow[data-chat="c-1a2b3c4d5e"] input[data-chat]') && !(await page.isChecked('.mw-chatrow[data-chat="c-9f8e7d6c5b"] input[data-chat]')), 'the switches read back');
      assert(await page.$('.mw-chatrow[data-chat="c-1a2b3c4d5e"] [data-wake="W-000045"]'), 'its running check-in');
      await t.areaShot(page, AREA, 'chats-desktop');
      const offr = await waitPost(page, /\/api\/chats\/c-1a2b3c4d5e\/wakes$/, () => page.click('.mw-chatrow[data-chat="c-1a2b3c4d5e"] input[data-chat]'));
      assert(canon(body(offr)) === canon({ allow: false }), 'off');
      const onr = await waitPost(page, /\/api\/chats\/c-9f8e7d6c5b\/wakes$/, () => page.click('.mw-chatrow[data-chat="c-9f8e7d6c5b"] input[data-chat]'));
      assert(canon(body(onr)) === canon({ allow: true }), 'on');
      await page.waitForFunction(() => document.querySelector('.mw-chatrow[data-chat="c-9f8e7d6c5b"] input[data-chat]').checked);
      await go(p.page, '#/schedule/chats', '.mw-chatrow');
      await noOverflow(p.page, 'chats phone');
      await t.areaShot(p.page, AREA, 'chats-phone');
    });

    await step(AREA + ': persona setup sends only what changed and shows the 400 for gh in her status commands', async () => {
      await go(page, '#/schedule/setup', '[data-focus="wp:seraphina:cmdmode"]');
      assert(await page.inputValue('[data-focus="wp:seraphina:cmdmode"]') === 'default' && await page.locator('[data-focus="wp:seraphina:commands"]').isHidden(), 'no commands key: Default, and no list to type');
      await page.selectOption('[data-focus="wp:seraphina:cmdmode"]', 'list');
      await page.fill('[data-focus="wp:seraphina:commands"]', 'gh run list');
      t.allowHttp(true);
      await waitPost(page, /^\/api\/config$/, () => page.click('[data-action="save-wakes"]'));
      await page.waitForFunction(() => /runs with no network/.test((document.querySelector('#messages-root .mw-form-error') || {}).textContent || ''));
      await page.waitForTimeout(150);
      t.allowHttp(false);
      await page.fill('[data-focus="wp:seraphina:commands"]', 'git status');
      const ok = await waitPost(page, /^\/api\/config$/, () => page.click('[data-action="save-wakes"]'));
      assert(canon(body(ok)) === canon({ wakes: { personas: { seraphina: { wake: { commands: [['git', 'status']] } } } } }), 'only the change: ' + ok.request().postData());
      // DESIGN §35: a missing key and [] are different settings; an emptied list is never saved as []
      await go(page, '#/schedule/setup', '[data-focus="wp:seraphina:cmdmode"]');
      assert(await page.inputValue('[data-focus="wp:seraphina:cmdmode"]') === 'list' && await page.inputValue('[data-focus="wp:seraphina:commands"]') === 'git status', 'the list as saved');
      await page.fill('[data-focus="wp:seraphina:commands"]', '');
      const posts = [];
      const onReq = (r) => { if (r.method() === 'POST' && /\/api\/config$/.test(r.url())) posts.push(r); };
      page.on('request', onReq);
      await page.click('[data-action="save-wakes"]');
      await page.waitForFunction(() => /type at least one command, or choose “No commands”/.test((document.querySelector('#messages-root .mw-form-error') || {}).textContent || ''));
      page.off('request', onReq);
      assert(posts.length === 0, 'an emptied list is not saved: ' + posts.length);
      await page.selectOption('[data-focus="wp:seraphina:cmdmode"]', 'none');
      const none = await waitPost(page, /^\/api\/config$/, () => page.click('[data-action="save-wakes"]'));
      assert(canon(body(none)) === canon({ wakes: { personas: { seraphina: { wake: { commands: [] } } } } }), 'No commands saves []: ' + none.request().postData());
      await go(page, '#/schedule/setup', '[data-focus="wp:seraphina:cmdmode"]');
      assert(await page.inputValue('[data-focus="wp:seraphina:cmdmode"]') === 'none', 'an existing [] reads No commands');
      await page.selectOption('[data-focus="wp:seraphina:cmdmode"]', 'default');
      const def = await waitPost(page, /^\/api\/config$/, () => page.click('[data-action="save-wakes"]'));
      assert(canon(body(def)) === canon({ wakes: { personas: { seraphina: { wake: { commands: null } } } } }), 'Default takes the key out: ' + def.request().postData());
      await page.selectOption('[data-focus="wp:seraphina:cmdmode"]', 'none');
      await t.areaShot(page, AREA, 'persona-setup-desktop');
      await go(p.page, '#/schedule/setup', '[data-focus="wp:seraphina:cmdmode"]');
      await noOverflow(p.page, 'persona setup phone');
      await t.areaShot(p.page, AREA, 'persona-setup-phone');
    });

    await step(AREA + ': chat check-ins’ status commands: Default leaves the key out, No commands saves [], These commands the list; an emptied list is never saved (DESIGN §35)', async () => {
      const sel = '[data-focus="ws:chat:cmdmode"]';
      await go(page, '#/schedule/setup', sel);
      assert(await page.inputValue(sel) === 'default' && await page.locator('[data-focus="ws:chat:commands"]').isHidden(), 'no key: Default, and no list to type');
      assert(/gh run view, gh run list, gh pr checks, git status and git log/.test(await page.textContent(sel + ' option[value="default"]')), 'Default names the five commands');
      await page.selectOption(sel, 'list');
      await page.fill('[data-focus="ws:chat:commands"]', 'gh run view\ngit status');
      const ok = await waitPost(page, /^\/api\/config$/, () => page.click('[data-action="save-wakes"]'));
      assert(canon(body(ok)) === canon({ wakes: { chat_wakes: { commands: [['gh', 'run', 'view'], ['git', 'status']] } } }), 'only the change: ' + ok.request().postData());
      await go(page, '#/schedule/setup', sel);
      assert(await page.inputValue(sel) === 'list' && await page.inputValue('[data-focus="ws:chat:commands"]') === 'gh run view\ngit status', 'the list as saved');
      await page.fill('[data-focus="ws:chat:commands"]', '');
      const posts = [];
      const onReq = (r) => { if (r.method() === 'POST' && /\/api\/config$/.test(r.url())) posts.push(r); };
      page.on('request', onReq);
      await page.click('[data-action="save-wakes"]');
      await page.waitForFunction(() => /Chat check-ins: type at least one command, or choose “No commands”/.test((document.querySelector('#messages-root .mw-form-error') || {}).textContent || ''));
      page.off('request', onReq);
      assert(posts.length === 0, 'an emptied list is not saved: ' + posts.length);
      await page.selectOption(sel, 'none');
      const none = await waitPost(page, /^\/api\/config$/, () => page.click('[data-action="save-wakes"]'));
      assert(canon(body(none)) === canon({ wakes: { chat_wakes: { commands: [] } } }), 'No commands saves []: ' + none.request().postData());
      await go(page, '#/schedule/setup', sel);
      assert(await page.inputValue(sel) === 'none', 'an existing [] reads No commands');
      await page.selectOption(sel, 'default');
      const def = await waitPost(page, /^\/api\/config$/, () => page.click('[data-action="save-wakes"]'));
      assert(canon(body(def)) === canon({ wakes: { chat_wakes: { commands: null } } }), 'Default takes the key out: ' + def.request().postData());
      await go(page, '#/schedule/setup', sel);
      assert(await page.inputValue(sel) === 'default', 'the key gone reads Default again');
      await go(p.page, '#/schedule/setup', sel);
      await noOverflow(p.page, 'chat check-in commands phone');
    });

    await step(AREA + ': events in the server’s own shapes reach an open persona page: agent is her display name, a stop elsewhere, the gate’s integrity stop', async () => {
      await mockState({ reset: true });
      await go(page, '#/schedule/persona/seraphina/runs', '#mw-p-runs');
      const read = () => page.waitForRequest((r) => new URL(r.url()).pathname === '/api/personas/seraphina' && r.method() === 'GET', { timeout: 3000 });
      // wake.decided carries no persona; agent is "Seraphina" (WakeManager._publish), never "seraphina"
      const [decided] = await Promise.all([read(), call(page, '/__mock/publish', { type: 'wake.decided', data: { wake: 'W-000123', run: '07-wake', actions: [], taint: [], declined: false }, agent: 'Seraphina' })]);
      assert(decided, 'wake.decided with her display name re-reads her page');
      await go(page, '#/schedule/persona/seraphina', '.mw-phead [data-action="stop"]');
      // stopped from elsewhere (the CLI, a phone): wake.paused {scope: seraphina}; the page was read under 4 s ago, so only the event can show it
      await call(page, '/api/wakes/control', { action: 'pause', persona: 'seraphina' });
      await page.waitForSelector('.mw-phead [data-action="restart-persona"]', { timeout: 2500 });
      await waitPost(page, /\/api\/wakes\/control$/, () => page.click('[data-action="restart-persona"]'));
      await page.waitForSelector('.mw-phead [data-action="stop"]');
      // her next wake reached gate check 4 with a changed file: the stop, an inbox notice (notify.queued persona.integrity), wake.paused by integrity
      await call(page, '/__mock/messages/soul-changed', { persona: 'seraphina' });
      await page.waitForSelector('.mw-phead [data-stop="integrity"]', { timeout: 2500 });
      assert(await page.isDisabled('.mw-phead [data-action="run-now"]') && !(await page.$('[data-action="restart-persona"]')), 'stopped by the gate: no Run now, no Restart');
      assert(/She is stopped until you review the change/.test(await text(page, '#messages-root .mw-body .callout-human')), 'the stop, in words');
      await t.areaShot(page, AREA, 'persona-integrity-stop-desktop');
      await go(p.page, '#/schedule/persona/seraphina', '.mw-phead [data-stop="integrity"]');
      await noOverflow(p.page, 'persona integrity stop phone');
      await t.areaShot(p.page, AREA, 'persona-integrity-stop-phone');
      await go(page, '#/schedule/persona/seraphina/instructions', '#mw-p-review [data-action="restore"]');
      await waitPost(page, /\/api\/personas\/seraphina\/integrity\/restore$/, () => page.click('[data-action="restore"]'));
      await page.waitForFunction(() => !document.querySelector('#mw-p-review') && !document.querySelector('.mw-phead [data-stop]'));
      await mockState({ reset: true });
    });

    await step(AREA + ': Show older messages: the older rows stay through the next events, read again with as many rows as are shown', async () => {
      const filled = await call(page, '/__mock/messages/fill', { count: 130 });
      assert(filled.status === 200, 'filler notices');
      await go(page, '#/messages', '[data-action="older"]');
      assert(await page.locator('#messages-root .mw-msg').count() === 100, 'the first page is 100 rows');
      const reads = [];
      const onReq = (r) => { const u = new URL(r.url()); if (u.pathname === '/api/messages' && Number(u.searchParams.get('limit')) > 100) reads.push(u.search); };
      page.on('request', onReq);
      await page.click('[data-action="older"]');
      await page.waitForFunction(() => document.querySelectorAll('#messages-root .mw-msg').length > 100);
      const shown = await page.locator('#messages-root .mw-msg').count();
      await page.waitForTimeout(3500);   // the reviewer's case: an event some seconds later, while the owner reads the older rows
      await call(page, '/__mock/messages/deliver', { message: 'M-000007', channel: 'telegram', state: 'failed' });
      await page.waitForTimeout(1500);
      page.off('request', onReq);
      const after = await page.locator('#messages-root .mw-msg').count();
      assert(after >= shown && reads.length > 0, 'older rows stay after an event, read again with limit ' + reads.join(' ') + ': ' + JSON.stringify({ shown, after }));
      await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
      await t.areaShot(page, AREA, 'inbox-older-desktop');
    });

    await step(AREA + ': Home shows the area’s card and its needs; the light theme on the desktop and the dark one on a phone', async () => {
      await mockState({ tamper: ['terms'] });
      await homeNeed(page, 'tamper');
      assert(/Settings changed outside these pages/.test(await text(page, '.needs-item[data-need="tamper"]')), 'tamper in Needs you');
      await mockState({ reset: true });
      const l = await t.newPage(AREA + '-light', { colorScheme: 'light' });
      await go(l.page, '#/schedule', '.mw-persona');
      await t.areaShot(l.page, AREA, 'schedule-light-desktop');
      await l.ctx.close();
      const dk = await t.newPage(AREA + '-phone-dark', { phone: true, colorScheme: 'dark' });
      await go(dk.page, '#/messages', '.mw-msg');
      await noOverflow(dk.page, 'inbox phone dark');
      await t.areaShot(dk.page, AREA, 'inbox-dark-phone');
      await dk.ctx.close();
    });
  } finally {
    await call(page, '/__mock/messages/state', { reset: true }).catch(() => {});
    await d.ctx.close();
    await p.ctx.close();
  }
}
