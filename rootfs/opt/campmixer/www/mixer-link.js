// CampMixer: the messages the app and the mixer core exchange (PROTOCOL.md, layer 2, version 1),
// the fader's scale, and a practice core that lives in the page for trying the controls with no mixer.
// No page, no network: plain functions, so the same file is checked with Node (tests/test_mixer_link.js).
(function(root){
  'use strict';

  var VERSION = 1;
  var GET_STATE = 0x01, SET = 0x02, STATE = 0x81, METERS = 0x82;
  var LEVEL = 1, MUTE = 2;
  var OFF = -32768;               // a level of "no sound at all"
  var LEVEL_MIN = -600, LEVEL_MAX = 100, STEP = 5;   // tenths of a dB: -60.0 to +10.0, in half-dB steps

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
    var view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    if (bytes[0] === STATE){
      if (bytes.length < 3) return null;
      var state = { type: 'state', version: bytes[1], inputs: bytes[2], channels: [] };
      if (state.version !== VERSION) return state;          // a newer core: say so, do not guess at its layout
      if (state.inputs < 1 || state.inputs > 16) return null;
      var at = 3;
      for (var n = 0; n <= state.inputs; n++){
        if (at + 4 > bytes.length) return null;
        var size = bytes[at + 3];
        if (size > 16 || at + 4 + size > bytes.length) return null;
        state.channels.push({
          level: view.getInt16(at),
          muted: (bytes[at + 2] & 1) === 1,
          name: utf8(bytes.subarray(at + 4, at + 4 + size)),
          master: n === state.inputs
        });
        at += 4 + size;
      }
      return state;
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
  // It lives in the page and is connected to nothing: no sound anywhere changes.
  function PracticeCore(deliver){
    this.deliver = deliver;            // called with each message the core "sends"
    this.channels = [
      { level: -60, muted: false, name: 'MIC 1' }, { level: -100, muted: false, name: 'MIC 2' },
      { level: OFF, muted: false, name: 'KEYS' }, { level: -120, muted: true, name: 'LAPTOP' },
      { level: 0, muted: false, name: 'MASTER' }
    ];
    this.phase = 0;
  }
  PracticeCore.prototype.state = function(){
    var parts = [STATE, VERSION, this.channels.length - 1];
    this.channels.forEach(function(channel){
      var level = channel.level & 0xFFFF;
      parts.push(level >> 8, level & 0xFF, channel.muted ? 1 : 0, channel.name.length);
      for (var i = 0; i < channel.name.length; i++) parts.push(channel.name.charCodeAt(i) & 0x7F);
    });
    return new Uint8Array(parts);
  };
  PracticeCore.prototype.receive = function(bytes){
    if (bytes[0] === GET_STATE) this.deliver(this.state());
    if (bytes[0] !== SET || bytes.length < 5) return;
    var channel = this.channels[bytes[1]];
    if (!channel) return;
    var value = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength).getInt16(3);
    if (bytes[2] === LEVEL) channel.level = clampLevel(value);
    else if (bytes[2] === MUTE) channel.muted = value !== 0;
    else return;
    this.deliver(this.state());
  };
  PracticeCore.prototype.meters = function(){   // call 20 times a second: something lifelike to look at
    this.phase += 1;
    var bytes = [METERS], loudest = -Infinity, master = this.channels[this.channels.length - 1], phase = this.phase;
    this.channels.forEach(function(channel, n){
      var peak;
      if (channel === master){
        // What goes out to the speaker: the loudest input that is up and not muted, through the master.
        peak = master.muted || master.level === OFF ? -Infinity : loudest + master.level / 10;
      } else {
        // What comes in on that input, whatever its fader and mute are doing.
        peak = -18 + 9 * Math.sin(phase / (7 + 3 * n)) + 5 * Math.sin(phase / (1.7 + n));
        if (!channel.muted && channel.level !== OFF) loudest = Math.max(loudest, peak + channel.level / 10);
      }
      bytes.push(peak > -127 ? Math.max(0, Math.min(254, Math.round(-peak * 2))) : 255);
    });
    this.deliver(new Uint8Array(bytes));
  };

  var api = {
    VERSION: VERSION, GET_STATE: GET_STATE, SET: SET, STATE: STATE, METERS: METERS, LEVEL: LEVEL, MUTE: MUTE,
    OFF: OFF, LEVEL_MIN: LEVEL_MIN, LEVEL_MAX: LEVEL_MAX, STEP: STEP,
    getState: getState, set: set, decode: decode, clampLevel: clampLevel, levelAt: levelAt, positionOf: positionOf,
    nudge: nudge, levelText: levelText, meterWidth: meterWidth, PracticeCore: PracticeCore
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  else root.CampMixerLink = api;
})(typeof self !== 'undefined' ? self : this);
