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
assert.deepStrictEqual(L.decode(unhex(vectors.info.hex)), { type: 'info', version: 2, inputs: 4, outputs: 1 });
for (const v of vectors.channels) {
  assert.deepStrictEqual(L.decode(unhex(v.hex)),
    { type: 'channel', channel: v.channel, level: v.level, muted: v.muted, phantomSwitch: v.phantom_switch, phantom: v.phantom, name: v.name }, v.name);
}
// A CHANNEL's flags: bit 0 is mute, bit 2 says the channel has a 48 V switch, bit 1 that the 48 V is on. A channel
// with no switch is never shown as powered, whatever bit 1 says, and the unit number in bits 4 to 7 changes nothing.
for (let flags = 0; flags < 256; flags++) {
  const got = L.decode(new Uint8Array([0x84, 3, 0, 0, flags, 1, 0x41]));
  assert.deepStrictEqual([got.muted, got.phantomSwitch, got.phantom], [(flags & 1) === 1, (flags & 4) === 4, (flags & 6) === 6], 'flags ' + flags);
}
assert.strictEqual(L.PHANTOM, 4);
const meters = L.decode(unhex(vectors.meters.hex));
assert.deepStrictEqual(meters, { type: 'meters', peaks: vectors.meters.peaks.map(p => p === null ? -Infinity : p) });
// A slice of a larger buffer (how a browser may hand it over) reads the same.
const one = unhex(vectors.channels[3].hex);
const padded = new Uint8Array([9, 9, 9, ...one, 9]);
assert.deepStrictEqual(L.decode(padded.subarray(3, padded.length - 1)), L.decode(one));
// A name in another script arrives whole.
assert.strictEqual(L.decode(new Uint8Array([0x84, 7, 0, 0, 0, 5, 0x43, 0x61, 0x66, 0xC3, 0xA9])).name, 'Café');
// Any number of inputs and outputs, up to what a one-byte channel number allows.
assert.deepStrictEqual(L.decode(new Uint8Array([0x83, 2, 1, 1])), { type: 'info', version: 2, inputs: 1, outputs: 1 });
assert.deepStrictEqual(L.decode(new Uint8Array([0x83, 2, 200, 50])), { type: 'info', version: 2, inputs: 200, outputs: 50 });
assert.deepStrictEqual(L.decode(new Uint8Array([0x83, 2, 48, 16])), { type: 'info', version: 2, inputs: 48, outputs: 16 });
for (const bad of [[0x83, 2, 0, 1], [0x83, 2, 1, 0], [0x83, 2, 200, 51], [0x83, 2, 255, 255], [0x83, 2, 4], [0x83, 2], [0x83]]) {
  assert.strictEqual(L.decode(new Uint8Array(bad)), null, JSON.stringify(bad));
}
assert.strictEqual(L.decode(new Uint8Array([0x84, 249, 0xFF, 0xC4, 1, 0])).channel, 249);

// Anything else is ignored, never guessed at and never an error.
for (let cut = 0; cut < one.length; cut++) assert.ok(L.decode(one.subarray(0, cut)) === null, 'a CHANNEL cut to ' + cut + ' bytes was accepted');
for (const junk of ['', '00', '01', '02', '7e0102', '80', '81', '810104', '85', 'ff', '8400000000' + '11' + '41'.repeat(17), '840000000005414243']) {
  assert.strictEqual(L.decode(unhex(junk)), null, junk);
}
assert.strictEqual(L.decode(null), null);
assert.strictEqual(L.decode(undefined), null);
let rng = 12345;
const random = () => (rng = (rng * 1103515245 + 12345) & 0x7fffffff) / 0x7fffffff;
for (let n = 0; n < 20000; n++){
  const bytes = new Uint8Array(Math.floor(random() * 40));
  for (let i = 0; i < bytes.length; i++) bytes[i] = Math.floor(random() * 256);
  if (random() < 0.8 && bytes.length) bytes[0] = [0x82, 0x83, 0x84][Math.floor(random() * 3)];
  if (random() < 0.5 && bytes.length > 1 && bytes[0] === 0x83) bytes[1] = 2;
  const got = L.decode(bytes);                       // must not throw, whatever arrives
  if (got && got.type === 'info' && got.version === 2) assert.ok(got.inputs >= 1 && got.outputs >= 1 && got.inputs + got.outputs <= 250);
  if (got && got.type === 'channel') assert.ok(typeof got.name === 'string' && Number.isInteger(got.level) && typeof got.muted === 'boolean');
  if (got && got.type === 'channel') assert.ok(typeof got.phantomSwitch === 'boolean' && typeof got.phantom === 'boolean' && (got.phantomSwitch || !got.phantom));
}
// A core that speaks another version is reported as such, with no attempt to read the rest.
assert.deepStrictEqual(L.decode(new Uint8Array([0x83, 3, 4, 1, 9, 9])), { type: 'info', version: 3, inputs: 0, outputs: 0 });

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
assert.deepStrictEqual(heard[0], { type: 'info', version: 2, inputs: 4, outputs: 1 });
assert.strictEqual(heard.length, 6);
assert.deepStrictEqual(heard.slice(1).map(m => [m.type, m.channel, m.name]),
  [['channel', 0, 'MIC 1'], ['channel', 1, 'MIC 2'], ['channel', 2, 'KEYS'], ['channel', 3, 'LAPTOP'], ['channel', 4, 'MASTER']]);
const latest = () => heard[heard.length - 1];
core.receive(L.set(0, L.LEVEL, -35));
assert.deepStrictEqual([heard.length, latest().channel, latest().level], [7, 0, -35]);   // one change, one answer: just that channel
core.receive(L.set(0, L.LEVEL, 2000));
assert.strictEqual(latest().level, 100);
core.receive(L.set(1, L.LEVEL, -700));
assert.deepStrictEqual([latest().channel, latest().level], [1, L.OFF]);
core.receive(L.set(4, L.MUTE, 1));
assert.deepStrictEqual([latest().channel, latest().muted], [4, true]);
core.receive(L.set(4, L.MUTE, 0));
assert.strictEqual(latest().muted, false);
const before = heard.length;
core.receive(L.set(9, L.LEVEL, 0));                  // no such channel
core.receive(L.set(0, 77, 0));                       // no such setting
core.receive(new Uint8Array([L.SET, 0, 1]));         // cut short
core.receive(new Uint8Array([0x55]));
assert.strictEqual(heard.length, before);
// 48 V: every input has the switch and starts with it off; the output has none. On and off are said outright.
core.receive(L.getState());
assert.deepStrictEqual(heard.slice(-5).map(m => [m.phantomSwitch, m.phantom]), [[true, false], [true, false], [true, false], [true, false], [false, false]]);
core.receive(L.set(1, L.PHANTOM, 1));
assert.deepStrictEqual([latest().channel, latest().phantom, latest().muted, latest().level], [1, true, false, L.OFF]);   // and nothing else about it moved
core.receive(L.set(1, L.PHANTOM, 1));                // asked again: still on, not flipped back
assert.strictEqual(latest().phantom, true);
core.receive(L.set(1, L.PHANTOM, 0));
assert.deepStrictEqual([latest().channel, latest().phantom], [1, false]);
core.receive(L.set(0, L.PHANTOM, -3));               // anything but 0 is on
assert.deepStrictEqual([latest().channel, latest().phantom], [0, true]);
core.receive(L.set(0, L.MUTE, 1));
assert.deepStrictEqual([latest().muted, latest().phantom], [true, true]);   // muting leaves the 48 V alone
core.receive(L.set(0, L.MUTE, 0));
core.receive(L.set(0, L.PHANTOM, 0));
const quiet = heard.length;
core.receive(L.set(4, L.PHANTOM, 1));                // the output has no 48 V switch: nothing, and no answer
core.receive(L.set(0, 3, 1));                        // gain is not in this version
assert.strictEqual(heard.length, quiet);
core.receive(L.getState());
assert.ok(heard.slice(-5).every(m => m.phantom === false), 'a 48 V was left on');
core.receive(L.set(4, L.LEVEL, 0));
heard.length = 0;
for (let n = 0; n < 200; n++) core.meters();
// An input's meter shows what is coming in, whatever its fader and mute are doing. An output's shows what goes out.
assert.ok(heard.every(m => m.type === 'meters' && m.peaks.length === 5 && m.peaks.every(p => p === -Infinity || (p <= 0 && p >= -127))));
assert.ok(heard.every(m => m.peaks.slice(0, 4).every(p => p > -60)), 'an input shows nothing on its meter');
assert.ok(heard.some(m => m.peaks[4] > -40), 'the output never shows on its meter');
heard.length = 0;
core.receive(L.set(4, L.MUTE, 1));
for (let n = 0; n < 20; n++) core.meters();
assert.ok(heard.filter(m => m.type === 'meters').every(m => m.peaks[4] === -Infinity && m.peaks[0] > -60), 'a muted output shows on its meter');
core.heartbeat();
assert.deepStrictEqual(latest(), { type: 'info', version: 2, inputs: 4, outputs: 1 });

// A big one: 48 inputs and 8 outputs, and the most that fits.
for (const [ins, outs] of [[48, 8], [1, 1], [200, 50], [16, 2]]) {
  const said = [];
  const big = new L.PracticeCore(bytes => said.push(L.decode(bytes)), ins, outs);
  big.receive(L.getState());
  assert.deepStrictEqual(said[0], { type: 'info', version: 2, inputs: ins, outputs: outs });
  assert.strictEqual(said.length, 1 + ins + outs);
  assert.deepStrictEqual(said.slice(1).map(m => m.channel), Array.from({ length: ins + outs }, (_, n) => n));
  assert.ok(said.slice(1).every(m => m.name.length >= 1 && m.name.length <= 16));
  assert.ok(said.slice(1).every(m => m.phantomSwitch === (m.channel < ins) && !m.phantom), 'the 48 V switches are not on the inputs alone');
  big.receive(L.set(ins + outs - 1, L.LEVEL, -45));  // the last output
  assert.deepStrictEqual([said[said.length - 1].channel, said[said.length - 1].level], [ins + outs - 1, -45]);
  big.receive(L.set(ins + outs, L.LEVEL, 0));        // one past the end: nothing
  assert.strictEqual(said.length, 2 + ins + outs);
  said.length = 0;
  big.meters();
  assert.strictEqual(said[0].peaks.length, ins + outs);
}

console.log('mixer messages (web app): all checks passed');
