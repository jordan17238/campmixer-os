// CampMixer: the messages the app and the mixer core exchange (PROTOCOL.md, layer 2, version 2),
// the fader's scale, and a practice core that lives in the page for trying the controls with no mixer.
// The mixer core says how many inputs and outputs it has; nothing here assumes a number.
// No page, no network: plain functions, so the same file is checked with Node (tests/test_mixer_link.js).
(function(root){
  'use strict';

  var VERSION = 2;
  var GET_STATE = 0x01, SET = 0x02, METERS = 0x82, INFO = 0x83, CHANNEL = 0x84;
  var LEVEL = 1, MUTE = 2;
  var OFF = -32768;               // a level of "no sound at all"
  var LEVEL_MIN = -600, LEVEL_MAX = 100, STEP = 5;   // tenths of a dB: -60.0 to +10.0, in half-dB steps
  var MAX_CHANNELS = 250;         // inputs and outputs together: a channel's number has to fit in one byte

  // ----- messages -----

  function getState(){ return new Uint8Array([GET_STATE]); }

  function set(channel, what, value){
    var bytes = new Uint8Array(5), view = new DataView(bytes.buffer);
    bytes[0] = SET; bytes[1] = channel; bytes[2] = what;
    view.setInt16(3, value);      // high byte first
    return bytes;
  }

  function utf8(bytes){
    if (typeof TextDecoder !== 'undefined') return new TextDecoder('utf-8').decode(bytes);
    var out = '';
    for (var i = 0; i < bytes.length; i++) out += String.fromCharCode(bytes[i]);
    return out;
  }

  // What a message from the core says, or null if it is not one this app understands (those are ignored).
  function decode(bytes){
    if (!bytes || !bytes.length) return null;
    if (bytes[0] === INFO){
      if (bytes.length < 2) return null;
      if (bytes[1] !== VERSION) return { type: 'info', version: bytes[1], inputs: 0, outputs: 0 };   // say so, do not guess
      if (bytes.length < 4 || bytes[2] < 1 || bytes[3] < 1 || bytes[2] + bytes[3] > MAX_CHANNELS) return null;
      return { type: 'info', version: VERSION, inputs: bytes[2], outputs: bytes[3] };
    }
    if (bytes[0] === CHANNEL){
      if (bytes.length < 6) return null;
      var size = bytes[5];
      if (size > 16 || bytes.length < 6 + size) return null;
      return {
        type: 'channel', channel: bytes[1],
        level: new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength).getInt16(2),
        muted: (bytes[4] & 1) === 1,
        name: utf8(bytes.subarray(6, 6 + size))
      };
    }
    if (bytes[0] === METERS){
      var peaks = [];
      for (var i = 1; i < bytes.length; i++) peaks.push(bytes[i] === 255 ? -Infinity : (0 - bytes[i]) / 2);   // dB below full scale
      return { type: 'meters', peaks: peaks };
    }
    return null;
  }

  // ----- the fader's scale -----
  // Left end is off. Then the travel is shared out the way a mixing desk does it: fine around 0 dB,
  // where the work is done, and coarse at the bottom. 0 dB sits three quarters of the way along.
  var SCALE = [[0.03, -600], [0.25, -300], [0.5, -100], [0.75, 0], [1, 100]];

  function clampLevel(level){
    if (level === OFF || !(level >= LEVEL_MIN)) return OFF;   // also anything that is not a number
    return Math.min(LEVEL_MAX, Math.round(level / STEP) * STEP);
  }

  function levelAt(position){          // 0..1 along the fader -> tenths of a dB, or OFF
    if (!(position > SCALE[0][0] / 2)) return OFF;
    if (position <= SCALE[0][0]) return LEVEL_MIN;
    if (position >= 1) return LEVEL_MAX;
    for (var i = 1; i < SCALE.length; i++){
      if (position <= SCALE[i][0]){
        var a = SCALE[i - 1], b = SCALE[i];
        return clampLevel(a[1] + (b[1] - a[1]) * (position - a[0]) / (b[0] - a[0]));
      }
    }
    return LEVEL_MAX;
  }

  function positionOf(level){          // tenths of a dB, or OFF -> 0..1 along the fader
    if (level === OFF || level < LEVEL_MIN) return 0;
    if (level >= LEVEL_MAX) return 1;
    for (var i = 1; i < SCALE.length; i++){
      if (level <= SCALE[i][1]){
        var a = SCALE[i - 1], b = SCALE[i];
        return a[0] + (b[0] - a[0]) * (level - a[1]) / (b[1] - a[1]);
      }
    }
    return 1;
  }

  function nudge(level, steps){        // one press of + or -: half a dB, and off sits just below the bottom
    if (level === OFF) return steps > 0 ? LEVEL_MIN : OFF;
    var next = level + steps * STEP;
    return next < LEVEL_MIN ? OFF : clampLevel(next);
  }

  function levelText(level){
    if (level === OFF || level < LEVEL_MIN) return 'Off';
    var db = level / 10;
    return (db > 0 ? '+' : db < 0 ? '−' : '') + Math.abs(db).toFixed(1) + ' dB';
  }

  function meterWidth(peak){           // dB below full scale -> 0..1 along the meter (-60 dB and below shows nothing)
    if (!(peak > -60)) return 0;
    return Math.min(1, (peak + 60) / 60);
  }

  // ----- a practice core -----
  // Answers the same messages the real mixer core does, so the controls can be tried with no mixer connected.
  // It lives in the page and is connected to nothing: no sound anywhere changes. Any number of inputs and outputs.
  function PracticeCore(deliver, inputs, outputs){
    this.deliver = deliver;            // called with each message the core "sends"
    this.inputs = Math.max(1, Math.min(MAX_CHANNELS - 1, inputs || 4));
    this.outputs = Math.max(1, Math.min(MAX_CHANNELS - this.inputs, outputs || 1));
    this.channels = [];
    var usual = [['MIC 1', -60], ['MIC 2', -100], ['KEYS', OFF], ['LAPTOP', -120]];
    for (var n = 0; n < this.inputs; n++){
      if (this.inputs === 4) this.channels.push({ level: usual[n][1], muted: n === 3, name: usual[n][0] });
      else this.channels.push({ level: n % 5 === 4 ? OFF : -30 * (n % 4) - 30, muted: n % 7 === 6, name: 'IN ' + (n + 1) });
    }
    for (var m = 0; m < this.outputs; m++){
      this.channels.push({ level: m ? -60 : 0, muted: false, name: this.outputs === 1 ? 'MASTER' : m ? 'OUT ' + (m + 1) : 'MAIN' });
    }
    this.phase = 0;
  }
  PracticeCore.prototype.info = function(){ return new Uint8Array([INFO, VERSION, this.inputs, this.outputs]); };
  PracticeCore.prototype.channel = function(n){
    var channel = this.channels[n], level = channel.level & 0xFFFF;
    var parts = [CHANNEL, n, level >> 8, level & 0xFF, channel.muted ? 1 : 0, channel.name.length];
    for (var i = 0; i < channel.name.length; i++) parts.push(channel.name.charCodeAt(i) & 0x7F);
    return new Uint8Array(parts);
  };
  PracticeCore.prototype.receive = function(bytes){
    if (bytes[0] === GET_STATE){
      this.deliver(this.info());
      for (var n = 0; n < this.channels.length; n++) this.deliver(this.channel(n));
    }
    if (bytes[0] !== SET || bytes.length < 5) return;
    var channel = this.channels[bytes[1]];
    if (!channel) return;
    var value = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength).getInt16(3);
    if (bytes[2] === LEVEL) channel.level = clampLevel(value);
    else if (bytes[2] === MUTE) channel.muted = value !== 0;
    else return;
    this.deliver(this.channel(bytes[1]));
  };
  PracticeCore.prototype.meters = function(){   // call 20 times a second: something lifelike to look at
    this.phase += 1;
    var bytes = [METERS], loudest = -Infinity, phase = this.phase, inputs = this.inputs;
    this.channels.forEach(function(channel, n){
      var peak;
      if (n >= inputs){
        // What goes out of that output: the loudest input that is up and not muted, through the output's own fader.
        peak = channel.muted || channel.level === OFF ? -Infinity : loudest + channel.level / 10;
      } else {
        // What comes in on that input, whatever its fader and mute are doing.
        peak = -18 + 9 * Math.sin(phase / (7 + 3 * (n % 9))) + 5 * Math.sin(phase / (1.7 + n % 5));
        if (!channel.muted && channel.level !== OFF) loudest = Math.max(loudest, peak + channel.level / 10);
      }
      bytes.push(peak > -127 ? Math.max(0, Math.min(254, Math.round(-peak * 2))) : 255);
    });
    this.deliver(new Uint8Array(bytes));
  };
  PracticeCore.prototype.heartbeat = function(){ this.deliver(this.info()); };   // call once a second, as the real core does

  var api = {
    VERSION: VERSION, GET_STATE: GET_STATE, SET: SET, METERS: METERS, INFO: INFO, CHANNEL: CHANNEL, LEVEL: LEVEL, MUTE: MUTE,
    OFF: OFF, LEVEL_MIN: LEVEL_MIN, LEVEL_MAX: LEVEL_MAX, STEP: STEP, MAX_CHANNELS: MAX_CHANNELS,
    getState: getState, set: set, decode: decode, clampLevel: clampLevel, levelAt: levelAt, positionOf: positionOf,
    nudge: nudge, levelText: levelText, meterWidth: meterWidth, PracticeCore: PracticeCore
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.CampMixerLink = api;
})(typeof self !== 'undefined' ? self : this);
