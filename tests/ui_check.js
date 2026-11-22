// Drives the web app in a real browser (headless Chrome, phone-sized, touch) against tests/ui_server.py:
// signing in, the faders, mute, 48 V, two phones staying in step, the mixer core going away, practice mode, a PIN change.
// Takes pictures along the way. Not part of the automatic checks, because it needs Chrome:
//
//   npm install puppeteer-core            (anywhere; point NODE_PATH at its node_modules)
//   CHROME=/usr/bin/google-chrome node tests/ui_check.js [folder for the pictures]
//
// To check a mixer that is already running (with a mixer core, or a stand-in, on its serial port) instead:
//   CAMPMIXER_URL=http://mixer.local CAMPMIXER_PIN=48271935 node tests/ui_check.js
// That moves faders and mutes on the mixer it is pointed at. It leaves the 48 V alone unless CAMPMIXER_48V=yes
// says the first input has nothing plugged into it that 48 V could hurt (a stand-in has nothing at all).
// CAMPMIXER_SLOW=5 allows five times as long for everything, for a slow machine.
'use strict';
const assert = require('assert');
const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const puppeteer = require('puppeteer-core');

const SHOTS = path.resolve(process.argv[2] || 'ui-shots');
const BASE = process.env.CAMPMIXER_URL || null;       // set to check a running mixer instead of starting the stand-in
const GIVEN_PIN = process.env.CAMPMIXER_PIN || null;
const SLOW = Number(process.env.CAMPMIXER_SLOW || 1);
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms * (ms >= 100 ? SLOW : 1)));

async function until(what, test, ms = 6000) {
  const started = Date.now();
  for (;;) {
    const value = await test();
    if (value) return value;
    if (Date.now() - started > ms * SLOW) throw new Error('timed out waiting for: ' + what);
    await sleep(50);
  }
}

function startServer(port = '18095', ...shape) {
  const server = spawn('python3', [path.join(__dirname, 'ui_server.py'), port, ...shape], { stdio: ['pipe', 'pipe', 'inherit'] });
  const lines = [];
  let waiting = [];
  server.stdout.setEncoding('utf8');
  let rest = '';
  server.stdout.on('data', chunk => {
    rest += chunk;
    let at;
    while ((at = rest.indexOf('\n')) >= 0) {
      lines.push(rest.slice(0, at));
      rest = rest.slice(at + 1);
    }
    waiting = waiting.filter(check => !check());
  });
  const line = prefix => new Promise(resolve => {
    const check = () => {
      const found = lines.findIndex(text => text.startsWith(prefix));
      if (found < 0) return false;
      resolve(lines.splice(found, 1)[0]);
      return true;
    };
    if (!check()) waiting.push(check);
  });
  return {
    line,
    lines,
    say: async text => { server.stdin.write(text + '\n'); await line('OK ' + text); },
    stop: () => { server.stdin.write('quit\n'); },
  };
}

// What one strip shows.
const strip = (page, n) => page.evaluate(n => {
  const el = document.querySelectorAll('#strips .strip')[n];
  if (!el) return null;
  const fader = el.querySelector('.fader');
  return {
    name: el.querySelector('.strip-name').textContent,
    level: el.querySelector('.strip-level').textContent,
    muted: el.querySelector('.mute').getAttribute('aria-pressed') === 'true',
    mutedLook: el.classList.contains('muted'),
    p: parseFloat(fader.style.getPropertyValue('--p')),
    now: fader.getAttribute('aria-valuenow'),
    text: fader.getAttribute('aria-valuetext'),
    meter: parseFloat(el.querySelector('.meter-cover').style.width),
  };
}, n);
const faderBox = (page, n) => page.evaluate(n => {
  const fader = document.querySelectorAll('#strips .fader')[n];
  fader.scrollIntoView({ block: 'center' });
  const box = fader.getBoundingClientRect(), thumb = fader.querySelector('.fader-thumb').getBoundingClientRect();
  return { left: box.left, width: box.width, y: box.top + box.height / 2, thumb: thumb.left + thumb.width / 2 };
}, n);
async function drag(page, fromX, fromY, dx, dy = 0, steps = 12) {
  await page.touchscreen.touchStart(fromX, fromY);
  for (let i = 1; i <= steps; i++) {
    await page.touchscreen.touchMove(fromX + dx * i / steps, fromY + dy * i / steps);
    await sleep(15);
  }
  await page.touchscreen.touchEnd();
}
// The 48V button of every strip: 'true' or 'false' as the page shows it, or null where there is no such button.
const phantoms = page => page.evaluate(() => Array.from(document.querySelectorAll('#strips .strip')).map(el => {
  const button = el.querySelector('.phantom');
  return button ? button.getAttribute('aria-pressed') : null;
}));
const pressPhantom = (page, n) => page.evaluate(n => document.querySelectorAll('#strips .strip')[n].querySelector('.phantom').click(), n);
// The names that do not fit their strip and are shown cut short.
const cutNames = page => page.evaluate(() => Array.from(document.querySelectorAll('#strips .strip-name'))
  .filter(name => name.scrollWidth > name.clientWidth).map(name => name.textContent));
const visible = (page, selector) => page.evaluate(selector => {
  const el = document.querySelector(selector);
  return !!el && !el.hidden && el.getClientRects().length > 0;
}, selector);
const text = (page, selector) => page.evaluate(selector => document.querySelector(selector).textContent, selector);
async function signIn(page, pin) {
  await until('the sign-in form', () => visible(page, '#sign-in'));
  await page.evaluate(() => { document.querySelector('#pin').value = ''; });
  await page.type('#pin', pin);
  await page.tap('#sign-in-button');
}

(async () => {
  fs.mkdirSync(SHOTS, { recursive: true });
  const server = BASE ? null : startServer();
  let url = BASE, pin = GIVEN_PIN;
  if (server) {
    const ready = (await server.line('READY')).split(' ');
    url = ready[1]; pin = ready[3];
  }
  const browser = await puppeteer.launch({
    executablePath: process.env.CHROME || '/usr/bin/google-chrome',
    args: ['--no-sandbox', '--no-proxy-server', '--disable-gpu'],
    protocolTimeout: 180000 * SLOW,
  });
  const problems = [];
  const phoneView = { width: 390, height: 844, deviceScaleFactor: 2, isMobile: true, hasTouch: true };
  async function newPhone(context) {
    const page = await context.newPage();
    await page.setViewport(phoneView);
    page.on('console', message => {
      // (Chrome notes every answer that is not a 200. A refused PIN is such an answer, and is meant to be.)
      const refusal = /Failed to load resource/.test(message.text()) && /\/api\/login$/.test((message.location() || {}).url || '');
      if (message.type() === 'error' && !refusal) problems.push('console: ' + message.text());
    });
    page.on('pageerror', error => problems.push('script error: ' + error.message));
    await page.evaluateOnNewDocument(() => {
      document.addEventListener('securitypolicyviolation', event => console.error('blocked by the page\'s security rules: ' + event.violatedDirective + ' ' + event.blockedURI));
    });
    return page;
  }
  const shot = (page, name) => page.screenshot({ path: path.join(SHOTS, name + '.png'), fullPage: true });
  try {
    const context = await browser.createBrowserContext();
    const phone = await newPhone(context);

    // --- signing in
    await phone.goto(url, { waitUntil: 'networkidle0', timeout: 30000 * SLOW });
    await until('the sign-in form', () => visible(phone, '#sign-in'));
    assert.ok(!(await visible(phone, '#app')), 'the mixer shows before signing in');
    assert.strictEqual(await phone.evaluate(() => document.querySelectorAll('#strips .strip').length), 0);
    await shot(phone, '01-sign-in');
    await signIn(phone, '11112222');
    await until('the wrong-PIN message', async () => (await text(phone, '#pin-message')).includes('That is not the PIN'));
    await shot(phone, '02-wrong-pin');
    assert.ok(!(await visible(phone, '#app')));
    const cookiesBefore = await context.cookies();
    assert.strictEqual(cookiesBefore.length, 0, 'a cookie was handed out for a wrong PIN');
    await signIn(phone, pin.slice(0, 4) + ' ' + pin.slice(4));
    await until('the mixer', () => visible(phone, '#app'));
    await until('five strips', async () => (await phone.evaluate(() => document.querySelectorAll('#strips .strip').length)) === 5);
    const cookies = await context.cookies();
    assert.strictEqual(cookies.length, 1);
    assert.ok(cookies[0].httpOnly && cookies[0].sameSite === 'Strict' && cookies[0].name === 'campmixer', JSON.stringify(cookies[0]));
    assert.strictEqual(await phone.evaluate(() => document.cookie), '', 'the page\'s own script can read the sign-in cookie');
    await until('meters moving', async () => (await strip(phone, 0)).meter < 100);
    await sleep(300);
    await shot(phone, '03-mixer');

    if (server) {
      assert.deepStrictEqual((await Promise.all([0, 1, 2, 3, 4].map(n => strip(phone, n)))).map(s => [s.name, s.level, s.muted]),
        [['MIC 1', '−6.0 dB', false], ['MIC 2', '−10.0 dB', false], ['KEYS', 'Off', false], ['LAPTOP', '−12.0 dB', true], ['MASTER', '0.0 dB', false]]);
      // A 48V button on each input, showing off, and none on the output. No name is cut short to make room for it.
      assert.deepStrictEqual(await phantoms(phone), ['false', 'false', 'false', 'false', null]);
      assert.deepStrictEqual(await cutNames(phone), [], 'a name does not fit its strip');
    }
    // Everything fits a phone's width: nothing runs off the side.
    assert.ok(await phone.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), 'the page is wider than the phone');
    const sizes = await phone.evaluate(() => Array.from(document.querySelectorAll('#strips button')).map(b => { const r = b.getBoundingClientRect(); return [Math.round(r.width), Math.round(r.height)]; }));
    assert.ok(sizes.every(([w, h]) => w >= 44 && h >= 44), 'a button is too small for a thumb: ' + JSON.stringify(sizes));

    // --- a second phone, already signed in, to watch what the first one does arrive by way of the mixer core
    const second = await newPhone(context);
    await second.goto(url, { waitUntil: 'networkidle0' });
    await until('the mixer on the second phone', async () => (await second.evaluate(() => document.querySelectorAll('#strips .strip').length)) === 5);
    await phone.bringToFront();                                         // (a tab at the back takes no touches)

    // --- the fader
    const start = await strip(phone, 0);
    let box = await faderBox(phone, 0);
    await page_tap_only(phone, box.left + box.width * 0.1, box.y);      // a touch far from the knob, no movement
    await sleep(300);
    assert.strictEqual((await strip(phone, 0)).level, start.level, 'touching the track made the level jump');
    await drag(phone, box.left + box.width * 0.2, box.y - 10, 2, 220);  // the start of a scroll, begun on the fader
    await sleep(500);
    assert.strictEqual((await strip(phone, 0)).level, start.level, 'scrolling the page moved a fader');
    await phone.evaluate(() => window.scrollTo(0, 0));

    box = await faderBox(phone, 0);
    await drag(phone, box.thumb, box.y, 60);                           // the knob, dragged to the right
    const moved = await strip(phone, 0);
    assert.ok(moved.p > start.p + 0.1 && moved.level !== start.level, 'dragging the knob did nothing: ' + JSON.stringify([start, moved]));
    await until('the second phone to follow', async () => (await strip(second, 0)).level === moved.level);
    await sleep(700);
    assert.strictEqual((await strip(phone, 0)).level, moved.level, 'the fader moved again after it was let go');
    if (server) await server.line('SET MIC 1');

    box = await faderBox(phone, 0);
    await drag(phone, box.left + box.width * 0.9, box.y, -40);          // dragged from anywhere on the track: moves by the same amount
    const back = await strip(phone, 0);
    assert.ok(back.p < moved.p - 0.08 && back.p > moved.p - 0.2, 'a drag from the track is not relative: ' + JSON.stringify([moved, back]));
    await until('the second phone to follow back', async () => (await strip(second, 0)).level === back.level);

    box = await faderBox(phone, 2);                                     // KEYS is off: drag it up from nothing, then right down again
    await drag(phone, box.thumb, box.y, 200);
    assert.notStrictEqual((await strip(phone, 2)).level, 'Off');
    box = await faderBox(phone, 2);
    await drag(phone, box.thumb, box.y, -380);
    assert.strictEqual((await strip(phone, 2)).level, 'Off');
    box = await faderBox(phone, 4);                                     // and the top is the top
    await drag(phone, box.thumb, box.y, 380);
    assert.strictEqual((await strip(phone, 4)).level, '+10.0 dB');
    await until('the second phone to show the master at the top', async () => (await strip(second, 4)).level === '+10.0 dB');
    for (let n = 0; n < 20; n++) await phone.evaluate(() => document.querySelectorAll('#strips .strip')[4].querySelectorAll('.step')[0].click());
    assert.strictEqual((await strip(phone, 4)).level, '0.0 dB');
    await until('the second phone to show the master back at 0', async () => (await strip(second, 4)).level === '0.0 dB');

    // --- the half-dB buttons and the keyboard
    const before = (await strip(phone, 1)).level;
    await phone.evaluate(() => document.querySelectorAll('#strips .strip')[1].querySelectorAll('.step')[1].click());
    assert.strictEqual((await strip(phone, 1)).level, '−9.5 dB', 'from ' + before);
    await phone.evaluate(() => document.querySelectorAll('#strips .strip')[1].querySelectorAll('.step')[0].click());
    await phone.evaluate(() => document.querySelectorAll('#strips .strip')[1].querySelectorAll('.step')[0].click());
    assert.strictEqual((await strip(phone, 1)).level, '−10.5 dB');
    await phone.evaluate(() => document.querySelectorAll('#strips .fader')[1].focus());
    await phone.keyboard.press('ArrowRight');
    assert.strictEqual((await strip(phone, 1)).level, '−10.0 dB');
    await phone.keyboard.press('PageDown');
    assert.strictEqual((await strip(phone, 1)).level, '−13.0 dB');
    await until('the second phone to follow the keys', async () => (await strip(second, 1)).level === '−13.0 dB');
    assert.strictEqual((await strip(phone, 1)).text, '−13.0 dB');

    // --- mute
    await phone.evaluate(() => document.querySelectorAll('#strips .mute')[4].click());
    assert.ok((await strip(phone, 4)).muted);
    await until('the second phone to show the master muted', async () => (await strip(second, 4)).muted && (await strip(second, 4)).mutedLook);
    await sleep(600);
    assert.ok((await strip(phone, 4)).muted, 'the mute did not hold');
    await until('the muted master\'s meter to fall', async () => (await strip(phone, 4)).meter === 100);
    assert.ok((await strip(phone, 3)).meter < 100, 'a muted input no longer shows what is coming in');
    await shot(phone, '04-master-muted');
    await second.evaluate(() => document.querySelectorAll('#strips .mute')[4].click());   // and off again, from the other phone
    await until('the first phone to show the master unmuted', async () => !(await strip(phone, 4)).muted);

    // --- 48 V: asked about first, both ways, and the button shows what the mixer core reports
    if (server || process.env.CAMPMIXER_48V === 'yes') {
      const name = (await strip(phone, 0)).name;
      const reached = () => server ? server.lines.filter(line => line.includes(' 48V ')).length : 0;
      const before48 = await strip(phone, 0);
      assert.strictEqual((await phantoms(phone))[0], 'false', 'the first input has no 48V button, or its 48 V is already on');

      // No is the easy answer: Cancel, a touch outside the question, the Escape key. None of them switches anything.
      await pressPhantom(phone, 0);
      await until('the 48 V question', () => visible(phone, '#ask'));
      assert.strictEqual(await text(phone, '#ask-title'), 'Turn on 48 V for ' + name + '?');
      assert.strictEqual(await text(phone, '#ask-yes'), 'Turn on 48 V');
      assert.strictEqual(await phone.evaluate(() => document.activeElement.id), 'ask-no', 'the question does not start on Cancel');
      const askSizes = await phone.evaluate(() => Array.from(document.querySelectorAll('#ask button')).map(b => { const r = b.getBoundingClientRect(); return [Math.round(r.width), Math.round(r.height), r.right <= window.innerWidth && r.bottom <= window.innerHeight]; }));
      assert.ok(askSizes.every(([w, h, onScreen]) => w >= 44 && h >= 44 && onScreen), 'an answer is too small or off the screen: ' + JSON.stringify(askSizes));
      await sleep(200);
      await phone.screenshot({ path: path.join(SHOTS, '04b-48v-question.png') });
      await phone.keyboard.press('Tab');                                  // the keyboard stays inside the question
      assert.strictEqual(await phone.evaluate(() => document.activeElement.id), 'ask-yes');
      await phone.keyboard.press('Tab');
      assert.strictEqual(await phone.evaluate(() => document.activeElement.id), 'ask-no');
      await phone.tap('#ask-no');
      assert.ok(!(await visible(phone, '#ask')), 'Cancel left the question up');
      await pressPhantom(phone, 0);
      await until('the question again', () => visible(phone, '#ask'));
      await phone.touchscreen.tap(195, 60);                               // above the question, on the dimmed page
      await until('the question to go at a touch outside it', async () => !(await visible(phone, '#ask')));
      await pressPhantom(phone, 0);
      await until('the question a third time', () => visible(phone, '#ask'));
      await phone.keyboard.press('Escape');
      assert.ok(!(await visible(phone, '#ask')), 'Escape left the question up');
      await sleep(500);
      assert.strictEqual((await phantoms(phone))[0], 'false', '48 V shows as on without a yes');
      assert.strictEqual(reached(), 0, 'a 48 V change reached the mixer core without a yes');

      // Yes: the core switches it, and tells every phone.
      await pressPhantom(phone, 0);
      await until('the question for a yes', () => visible(phone, '#ask'));
      await phone.tap('#ask-yes');
      await until('48 V on, on the first phone', async () => (await phantoms(phone))[0] === 'true');
      if (server) await server.line('SET ' + name + ' 48V on');
      await until('48 V on, on the second phone', async () => (await phantoms(second))[0] === 'true');
      assert.ok(!(await visible(phone, '#ask')));
      await sleep(300);
      await shot(phone, '04c-48v-on');

      // Off is asked about too. Both phones are asked at once; the second says yes, and the first one's question,
      // which no longer fits what is so, goes by itself.
      await pressPhantom(second, 0);
      await until('the question on the second phone', () => visible(second, '#ask'));
      assert.strictEqual(await text(second, '#ask-title'), 'Turn off 48 V for ' + name + '?');
      assert.strictEqual(await text(second, '#ask-yes'), 'Turn off 48 V');
      await pressPhantom(phone, 0);
      await until('the question on the first phone', () => visible(phone, '#ask'));
      await second.evaluate(() => document.querySelector('#ask-yes').click());
      await until('48 V off again on both phones', async () => (await phantoms(phone))[0] === 'false' && (await phantoms(second))[0] === 'false');
      if (server) await server.line('SET ' + name + ' 48V off');
      await until('the first phone\'s question to go', async () => !(await visible(phone, '#ask')));
      await sleep(300);
      assert.strictEqual(reached(), 0, 'more 48 V changes reached the mixer core than were asked for');
      // The fader and the mute of that input were not touched by any of it.
      const after48 = await strip(phone, 0);
      assert.deepStrictEqual([after48.level, after48.muted], [before48.level, before48.muted]);
    }

    // --- dark, and a laptop
    await phone.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'dark' }]);
    await sleep(200);
    await shot(phone, '05-dark');
    await phone.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'light' }]);
    // A narrow phone (360 wide): still nothing off the side, no name cut short, every button big enough for a thumb.
    await phone.setViewport({ ...phoneView, width: 360, height: 740 });
    await sleep(300);
    assert.ok(await phone.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), 'the page is wider than a narrow phone');
    if (server) assert.deepStrictEqual(await cutNames(phone), [], 'a name does not fit its strip on a narrow phone');
    const narrowSizes = await phone.evaluate(() => Array.from(document.querySelectorAll('#strips button')).map(b => { const r = b.getBoundingClientRect(); return [Math.round(r.width), Math.round(r.height)]; }));
    assert.ok(narrowSizes.every(([w, h]) => w >= 44 && h >= 44), 'a button is too small for a thumb on a narrow phone: ' + JSON.stringify(narrowSizes));
    await shot(phone, '05b-narrow-phone');
    await phone.setViewport(phoneView);
    await sleep(200);
    const laptop = await context.newPage();
    await laptop.setViewport({ width: 1280, height: 800 });
    await laptop.goto(url, { waitUntil: 'networkidle0' });
    await until('the mixer on the laptop', async () => (await laptop.evaluate(() => document.querySelectorAll('#strips .strip').length)) === 5);
    await sleep(300);
    await shot(laptop, '06-laptop');
    const mouseBox = await faderBox(laptop, 3);
    await laptop.mouse.move(mouseBox.thumb, mouseBox.y);
    await laptop.mouse.down();
    await laptop.mouse.move(mouseBox.thumb + 80, mouseBox.y, { steps: 8 });
    await laptop.mouse.up();
    const byMouse = await strip(laptop, 3);
    assert.notStrictEqual(byMouse.level, '−12.0 dB', 'dragging with a mouse did nothing');
    await until('the phone to follow the laptop', async () => (await strip(phone, 3)).level === byMouse.level);
    await laptop.close();
    await phone.bringToFront();

    if (server) {
      // --- the mixer core goes away, with a 48 V question still on the screen: the question goes too
      await pressPhantom(phone, 1);
      await until('a 48 V question to be up', () => visible(phone, '#ask'));
      await server.say('core off');
      await until('the phone to notice', async () => (await text(phone, '#mixer-note')).includes('No signal from the mixer core'), 8000);
      assert.ok(await phone.evaluate(() => document.querySelector('#mixer').classList.contains('unavailable')));
      assert.ok(!(await visible(phone, '#ask')), 'a 48 V question stayed up with no mixer core to put it to');
      const stuck = await strip(phone, 0);
      box = await faderBox(phone, 0);
      await drag(phone, box.thumb, box.y, 60);
      assert.strictEqual((await strip(phone, 0)).level, stuck.level, 'a fader moved with no mixer core to move');
      await phone.evaluate(() => window.scrollTo(0, 0));
      await shot(phone, '07-no-mixer-core');

      // --- practice mode: the same controls, connected to nothing, and saying so
      await phone.tap('#practice-start');
      await until('practice mode', () => visible(phone, '#practice-banner'));
      await until('practice strips', async () => (await strip(phone, 4)) !== null && (await strip(phone, 4)).name === 'MASTER');
      const sentBefore = server.lines.filter(l => l.startsWith('SET')).length;
      box = await faderBox(phone, 0);
      const practiceStart = await strip(phone, 0);
      await drag(phone, box.thumb, box.y, 50);
      assert.notStrictEqual((await strip(phone, 0)).level, practiceStart.level, 'practice fader did not move');
      await phone.evaluate(() => document.querySelectorAll('#strips .mute')[0].click());
      await until('practice mute', async () => (await strip(phone, 0)).muted);
      assert.deepStrictEqual(await phantoms(phone), ['false', 'false', 'false', 'false', null]);
      await pressPhantom(phone, 1);                                     // 48 V can be tried too, on nothing
      await until('the practice 48 V question', () => visible(phone, '#ask'));
      // (Chrome takes no notice of a tap in the first third of a second after a fader was flicked and let go,
      // the way it treats a tap that stops a scroll. The drag above ended that way, so wait it out.)
      await sleep(400);
      await phone.tap('#ask-yes');
      await until('practice 48 V', async () => (await phantoms(phone))[1] === 'true');
      await sleep(400);
      await phone.evaluate(() => window.scrollTo(0, 0));
      await shot(phone, '08-practice');
      assert.strictEqual(server.lines.filter(l => l.startsWith('SET')).length, sentBefore, 'practice mode reached the mixer core');
      assert.ok(!(await visible(second, '#practice-banner')), 'practice mode on one phone showed on another');

      // --- the core comes back: practice ends by itself, and the real settings are on show again
      await server.say('core on');
      await until('practice mode to end', async () => !(await visible(phone, '#practice-banner')), 8000);
      await until('the real mixer again', async () => (await strip(phone, 0)) !== null && (await strip(phone, 0)).level === stuck.level && !(await strip(phone, 0)).muted, 8000);
      assert.ok(!(await phone.evaluate(() => document.querySelector('#mixer').classList.contains('unavailable'))));
      assert.deepStrictEqual(await phantoms(phone), ['false', 'false', 'false', 'false', null], 'the practice 48 V outlived practice mode');

      // --- the Pi's details are folded away at the bottom
      await phone.evaluate(() => { document.querySelector('#pi').open = true; });
      await until('the link report', async () => (await text(phone, '#link')) === 'Connected');
      await shot(phone, '09-details');

      // --- the PIN is changed: every phone is back at the sign-in form, and the old PIN no longer works
      await server.say('pin 90517362');
      await until('the first phone to be signed out', () => visible(phone, '#sign-in'), 8000);
      await until('the second phone to be signed out', () => visible(second, '#sign-in'), 8000);
      assert.ok(!(await visible(phone, '#app')) && (await phone.evaluate(() => document.querySelectorAll('#strips .strip').length)) === 0);
      await signIn(phone, pin);
      await until('the old PIN to be refused', async () => (await text(phone, '#pin-message')).includes('That is not the PIN'));
      await signIn(phone, '90517362');
      await until('the mixer after the new PIN', async () => (await phone.evaluate(() => document.querySelectorAll('#strips .strip').length)) === 5);

      // --- signing out
      await phone.evaluate(() => document.querySelector('#sign-out').click());
      await until('the sign-in form after signing out', () => visible(phone, '#sign-in'));
      assert.strictEqual((await context.cookies()).length, 0);

      // --- too many wrong PINs: the page says how long to wait and stops taking tries
      for (let n = 0; n < 5; n++) {
        await signIn(phone, '5550100' + n);
        await until('a wrong-PIN answer', async () => /not the PIN|Too many/.test(await text(phone, '#pin-message')));
        await sleep(150);
      }
      await until('the wait', async () => /Try again in \d+ s/.test(await text(phone, '#pin-message')));
      assert.ok(await phone.evaluate(() => document.querySelector('#sign-in-button').disabled));
      await shot(phone, '10-wait');
    }
    if (server) {
      // --- a big mixer: 32 inputs and 6 outputs, on the same page with nothing changed but what the core says it has
      const bigServer = startServer('18097', '32', '6');
      const bigReady = (await bigServer.line('READY')).split(' ');
      try {
        const bigContext = await browser.createBrowserContext();
        const big = await newPhone(bigContext);
        await big.goto(bigReady[1], { waitUntil: 'networkidle0' });
        await signIn(big, bigReady[3]);
        await until('38 strips', async () => (await big.evaluate(() => document.querySelectorAll('#strips .strip').length)) === 38);
        await until('every strip to be filled in', async () => (await big.evaluate(() => document.querySelectorAll('#strips .strip.unknown').length)) === 0);
        assert.deepStrictEqual(await big.evaluate(() => Array.from(document.querySelectorAll('#strips .group')).map(h => h.textContent)), ['Inputs', 'Outputs']);
        assert.deepStrictEqual([(await strip(big, 0)).name, (await strip(big, 31)).name, (await strip(big, 32)).name, (await strip(big, 37)).name], ['IN 1', 'IN 32', 'MAIN', 'OUT 6']);
        assert.ok(await big.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), 'the big mixer is wider than the phone');
        const bigPhantoms = await phantoms(big);                         // a 48V button on each of the 32 inputs, none on the 6 outputs
        assert.ok(bigPhantoms.slice(0, 32).every(p => p === 'false') && bigPhantoms.slice(32).every(p => p === null), JSON.stringify(bigPhantoms));
        await sleep(400);
        await big.screenshot({ path: path.join(SHOTS, '11-big-mixer-top.png') });
        const lastBox = await faderBox(big, 37);                         // the last output, right at the bottom
        const lastBefore = await strip(big, 37);
        await drag(big, lastBox.thumb, lastBox.y, -50);
        const lastAfter = await strip(big, 37);
        assert.ok(lastAfter.p < lastBefore.p - 0.1, 'the last output did not move: ' + JSON.stringify([lastBefore, lastAfter]));
        await bigServer.line('SET OUT 6');
        await big.evaluate(() => document.querySelectorAll('#strips .mute')[20].click());   // and an input in the middle
        await bigServer.line('SET IN 21');
        await until('meters moving at the bottom', async () => (await strip(big, 37)).meter < 100);
        await sleep(300);
        await big.screenshot({ path: path.join(SHOTS, '12-big-mixer-outputs.png') });
        // Strips far off screen are not being redrawn; the one on screen is.
        const drawn = await big.evaluate(() => Array.from(document.querySelectorAll('#strips .strip')).map(el => el.campmixerStrip.visible));
        assert.ok(drawn[37] && !drawn[0], 'meters are drawn for strips that are off screen: ' + JSON.stringify(drawn));
      } finally {
        bigServer.stop();
      }
    }
    assert.deepStrictEqual(problems, [], 'the browser reported problems');
    console.log('web app in a real browser: all checks passed (pictures in ' + SHOTS + ')');
  } catch (error) {
    console.error(error);
    console.error('problems the browser reported:', problems);
    // A picture of every page as it stood when the check failed, for working out why.
    let number = 0;
    for (const page of await browser.pages()) {
      try { await page.screenshot({ path: path.join(SHOTS, 'failed-' + (++number) + '.png') }); } catch (e) { /* that page has gone */ }
    }
    process.exitCode = 1;
  } finally {
    await browser.close();
    if (server) server.stop();
  }
})();

// A touch that lands and lifts without moving.
async function page_tap_only(page, x, y) {
  await page.touchscreen.touchStart(x, y);
  await sleep(60);
  await page.touchscreen.touchEnd();
}
