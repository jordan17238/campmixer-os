// Drives the web app in a real browser (headless Chrome, phone-sized, touch) against tests/ui_server.py:
// signing in, the faders, mute, two phones staying in step, the mixer core going away, practice mode, a PIN change.
// Takes pictures along the way. Not part of the automatic checks, because it needs Chrome:
//
//   npm install puppeteer-core            (anywhere; point NODE_PATH at its node_modules)
//   CHROME=/usr/bin/google-chrome node tests/ui_check.js [folder for the pictures]
//
// To check a mixer that is already running (with a mixer core, or a stand-in, on its serial port) instead:
//   CAMPMIXER_URL=http://mixer.local CAMPMIXER_PIN=48271935 node tests/ui_check.js
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

function startServer() {
  const server = spawn('python3', [path.join(__dirname, 'ui_server.py'), '18095'], { stdio: ['pipe', 'pipe', 'inherit'] });
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
    await phone.focus('#strips .strip:nth-child(2) .fader');
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

    // --- dark, and a laptop
    await phone.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'dark' }]);
    await sleep(200);
    await shot(phone, '05-dark');
    await phone.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'light' }]);
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
      // --- the mixer core goes away
      await server.say('core off');
      await until('the phone to notice', async () => (await text(phone, '#mixer-note')).includes('No signal from the mixer core'), 8000);
      assert.ok(await phone.evaluate(() => document.querySelector('#mixer').classList.contains('unavailable')));
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
    assert.deepStrictEqual(problems, [], 'the browser reported problems');
    console.log('web app in a real browser: all checks passed (pictures in ' + SHOTS + ')');
  } catch (error) {
    console.error(error);
    console.error('problems the browser reported:', problems);
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
