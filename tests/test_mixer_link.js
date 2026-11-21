// Checks the web app's own message code (www/mixer-link.js) with Node: the bytes it sends and understands,
// the fader's scale, and the practice core. The example messages are shared with the Python tests.
'use strict';
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const L = require(path.join(__dirname, '..', 'rootfs', 'opt', 'campmixer', 'www', 'mixer-link.js'));
const vectors = JSON.parse(fs.readFileSync(path.join(__dirname, 'mixer_vectors.json'), 'utf8'));

const hex = bytes => Buffer.from(bytes).toString('hex');
const unhex = text => new Uint8Array(Buffer.from(text, 'hex'));

// --- what the app sends
assert.strictEqual(hex(L.getState()), vectors.to_core[0].hex);
for (const v of vectors.to_core.slice(1)) assert.strictEqual(hex(L.set(v.channel, v.set, v.value)), v.hex, v.what);

// --- what the app understands
const state = L.decode(unhex(vectors.state.hex));
assert.strictEqual(state.type, 'state');
assert.strictEqual(state.version, 1);
assert.strictEqual(state.inputs, vectors.state.inputs);
assert.deepStrictEqual(state.channels.map(c => ({ level: c.level, muted: c.muted, name: c.name })), vectors.state.channels);
assert.deepStrictEqual(state.channels.map(c => c.master), [false, false, false, false, true]);
const meters = L.decode(unhex(vectors.meters.hex));
assert.deepStrictEqual(meters, { type: 'meters', peaks: vectors.meters.peaks.map(p => p === null ? -Infinity : p) });
// A slice of a larger buffer (how a browser may hand it over) reads the same.
const padded = new Uint8Array([9, 9, 9, ...unhex(vectors.state.hex), 9]);
assert.deepStrictEqual(L.decode(padded.subarray(3, padded.length - 1)), state);
// A name in another script arrives whole.
const named = L.decode(new Uint8Array([0x81, 1, 1, 0, 0, 0, 5, 0x43, 0x61, 0x66, 0xC3, 0xA9, 0, 0, 0, 1, 0x4D]));
assert.deepStrictEqual(named.channels.map(c => c.name), ['Café', 'M']);

// Anything else is ignored, never guessed at and never an error.
const whole = unhex(vectors.state.hex);
for (let cut = 0; cut < whole.length; cut++){
  const short = L.decode(whole.subarray(0, cut));
  assert.ok(short === null, 'a STATE cut to ' + cut + ' bytes was accepted');
}
for (const junk of ['', '00', '01', '02', '7e0102', '80', '83', 'ff', '8101', '810100', '8101ff', '810111' + '00'.repeat(80),
                    '81010100000011' + '41'.repeat(17) + '0000000000']) {
  assert.strictEqual(L.decode(unhex(junk)), null, junk);
}
assert.strictEqual(L.decode(null), null);
assert.strictEqual(L.decode(undefined), null);
let rng = 12345;
const random = () => (rng = (rng * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff;
for (let n = 0; n < 20000; n++){
  const bytes = new Uint8Array(Math.floor(random() * 70));
  for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(random() * 256);
  if (random() < 0.7 && bytes.length) bytes[0] = random() < 0.5 ? 0x81 : 0x82;
  if (random() < 0.5 && bytes.length > 2) { bytes[1] = 1; bytes[2] = 1 + Math.floor(random() * 5); }
  const got = L.decode(bytes);                       // must not throw, whatever arrives
  if (got && got.type === 'state' && got.version === 1) {
    assert.strictEqual(got.channels.length, got.inputs + 1);
    for (const c of got.channels) assert.ok(typeof c.name === 'string' && Number.isInteger(c.level));
  }
}
// A core that speaks a later version is reported as such, with no attempt to read the rest.
const newer = L.decode(new Uint8Array([0x81, 2, 4, 1, 2, 3]));
assert.deepStrictEqual(newer, { type: 'state', version: 2, inputs: 4, channels: [] });

// --- the fader's scale
assert.strictEqual(L.levelAt(0), L.OFF);
assert.strictEqual(L.levelAt(0.01), L.OFF);
assert.strictEqual(L.levelAt(0.03), -600);
assert.strictEqual(L.levelAt(0.25), -300);
assert.strictEqual(L.levelAt(0.5), -100);
assert.strictEqual(L.levelAt(0.75), 0);              // 0 dB three quarters of the way along
assert.strictEqual(L.levelAt(1), 100);
assert.strictEqual(L.levelAt(7), 100);
assert.strictEqual(L.levelAt(-1), L.OFF);
assert.strictEqual(L.levelAt(NaN), L.OFF);
let last = -Infinity;
for (let p = 0; p <= 1.0001; p += 0.001){
  const level = L.levelAt(p);
  const value = level === L.OFF ? -Infinity : level;
  assert.ok(value >= last, 'the scale goes backwards at ' + p);
  assert.ok(level === L.OFF || (level >= -600 && level <= 100 && level % 5 === 0), 'odd level ' + level);
  last = value;
}
// There and back lands on the same half-dB step, so a fader does not creep when it is only touched.
for (let level = -600; level <= 100; level += 5) assert.strictEqual(L.levelAt(L.positionOf(level)), level, 'level ' + level);
assert.strictEqual(L.levelAt(L.positionOf(L.OFF)), L.OFF);
assert.strictEqual(L.positionOf(L.OFF), 0);
assert.strictEqual(L.positionOf(-9999), 0);
assert.strictEqual(L.positionOf(100), 1);
assert.strictEqual(L.positionOf(5000), 1);
assert.strictEqual(L.positionOf(0), 0.75);
// Around 0 dB, where the mixing is done, a 300-pixel fader gives better than half a dB for every two pixels.
assert.ok((L.positionOf(0) - L.positionOf(-5)) * 270 >= 1.5);

assert.strictEqual(L.nudge(0, 1), 5);
assert.strictEqual(L.nudge(0, -1), -5);
assert.strictEqual(L.nudge(100, 1), 100);            // the top is the top
assert.strictEqual(L.nudge(98, 6), 100);
assert.strictEqual(L.nudge(-600, -1), L.OFF);         // one below the bottom is off
assert.strictEqual(L.nudge(L.OFF, -1), L.OFF);
assert.strictEqual(L.nudge(L.OFF, 1), -600);
assert.strictEqual(L.nudge(L.OFF, 6), -600);
assert.strictEqual(L.nudge(-590, -6), L.OFF);
assert.strictEqual(L.clampLevel(-601), L.OFF);
assert.strictEqual(L.clampLevel(101), 100);
assert.strictEqual(L.clampLevel(-62), -60);
assert.strictEqual(L.clampLevel(NaN), L.OFF);

assert.strictEqual(L.levelText(-60), '−6.0 dB');
assert.strictEqual(L.levelText(35), '+3.5 dB');
assert.strictEqual(L.levelText(0), '0.0 dB');
assert.strictEqual(L.levelText(-600), '−60.0 dB');
assert.strictEqual(L.levelText(100), '+10.0 dB');
assert.strictEqual(L.levelText(L.OFF), 'Off');

assert.strictEqual(L.meterWidth(0), 1);
assert.strictEqual(L.meterWidth(3), 1);
assert.strictEqual(L.meterWidth(-30), 0.5);
assert.strictEqual(L.meterWidth(-60), 0);
assert.strictEqual(L.meterWidth(-Infinity), 0);
assert.strictEqual(L.meterWidth(NaN), 0);

// --- the practice core answers the way the real one must
const heard = [];
const core = new L.PracticeCore(bytes => heard.push(L.decode(bytes)));
core.receive(L.getState());
assert.strictEqual(heard.length, 1);
assert.strictEqual(heard[0].channels.length, 5);
assert.strictEqual(heard[0].channels[4].name, 'MASTER');
core.receive(L.set(0, L.LEVEL, -35));
assert.strictEqual(heard[1].channels[0].level, -35);
core.receive(L.set(0, L.LEVEL, 2000));
assert.strictEqual(heard[2].channels[0].level, 100);
core.receive(L.set(1, L.LEVEL, -700));
assert.strictEqual(heard[3].channels[1].level, L.OFF);
core.receive(L.set(4, L.MUTE, 1));
assert.strictEqual(heard[4].channels[4].muted, true);
core.receive(L.set(4, L.MUTE, 0));
assert.strictEqual(heard[5].channels[4].muted, false);
const before = heard.length;
core.receive(L.set(9, L.LEVEL, 0));                  // no such channel
core.receive(L.set(0, 77, 0));                       // no such setting
core.receive(new Uint8Array([L.SET, 0, 1]));         // cut short
core.receive(new Uint8Array([0x55]));
assert.strictEqual(heard.length, before);
core.receive(L.set(4, L.LEVEL, 0));
for (let n = 0; n < 200; n++) core.meters();
const shown = heard.filter(m => m.type === 'meters');
assert.strictEqual(shown.length, 200);
assert.ok(shown.every(m => m.peaks.length === 5 && m.peaks.every(p => p === -Infinity || (p <= 0 && p >= -127))));
// An input's meter shows what is coming in, whatever its fader and mute are doing. The master's shows what goes out.
assert.ok(shown.every(m => m.peaks.slice(0, 4).every(p => p > -60)), 'an input shows nothing on its meter');
assert.ok(shown.some(m => m.peaks[4] > -40), 'the master never shows on its meter');
heard.length = 0;
core.receive(L.set(4, L.MUTE, 1));
for (let n = 0; n < 20; n++) core.meters();
assert.ok(heard.filter(m => m.type === 'meters').every(m => m.peaks[4] === -Infinity && m.peaks[0] > -60), 'a muted master shows on its meter');
core.receive(L.set(4, L.MUTE, 0));
for (let n = 0; n < 4; n++) core.receive(L.set(n, L.LEVEL, L.OFF));
heard.length = 0;
for (let n = 0; n < 20; n++) core.meters();
assert.ok(heard.every(m => m.peaks[4] === -Infinity), 'the master shows something with every input off');

console.log('mixer messages (web app): all checks passed');
