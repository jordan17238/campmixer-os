// CampMixer web app: sign in with the PIN, then a fader, a mute button and a meter for every input and output
// the mixer core says it has, however many that is, and a 48 V button on each input the core can power.
// The mixer's settings live in the mixer core. This page asks for changes and shows what the core reports back,
// which is how every phone (and the front panel) stays in step.
// Kept in its own file because the page's security rules (Content-Security-Policy) allow no script written
// into a page, which is what stops a planted one from running.
(function(){
  'use strict';
  var L = window.CampMixerLink;
  var $ = function(id){ return document.getElementById(id); };
  var ws = null, online = false, retry = null, ticking = null;
  var pending = null, loopTimer = null;            // the loopback test
  var link = { open: false, up: false };           // what the Pi says about its serial link
  var mixer = null, mixerSeen = 0, strips = [];    // what the mixer core last reported, and when
  // Meters are only drawn for the strips on screen, which is what keeps a mixer with a hundred channels smooth on a phone.
  var seen = !('IntersectionObserver' in window) ? null : new IntersectionObserver(function(entries){
    entries.forEach(function(entry){
      var strip = entry.target.campmixerStrip;
      if (!strip) return;
      strip.visible = entry.isIntersecting;
      if (strip.visible) drawMeter(strip);
    });
  }, { rootMargin: '300px' });
  var practice = null, practiceTimer = null, meterTimer = null;
  var asking = null;                               // the 48 V question on screen, if there is one
  var SEND_EVERY_MS = 40;                         // while a fader is being dragged: at most 25 changes a second
  var HOLD_MS = 400;                               // after letting go, ignore the core's echoes of the way there
  var STALE_MS = 3500;                             // the core reports at least once a second; this long without is trouble

  function show(which){
    $('sign-in').hidden = which !== 'sign-in';
    $('app').hidden = which !== 'app';
  }
  function api(path, options){
    var settings = { credentials: 'same-origin', cache: 'no-store' };
    for (var key in (options || {})) settings[key] = options[key];
    return fetch(path, settings).then(function(response){
      return response.json().then(
        function(data){ return { status: response.status, data: data || {} }; },
        function(){ return { status: response.status, data: {} }; });
    });
  }
  function later(job, ms){
    clearTimeout(retry);
    retry = setTimeout(job, ms);
  }
  function make(tag, className, text){
    var element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  // ----- signing in -----

  function span(seconds){
    if (seconds < 90) return seconds + ' s';
    return 'about ' + Math.ceil(seconds / 60) + ' min';
  }
  function holdOff(seconds, before){
    // The mixer makes everyone wait after too many wrong PINs. Count the wait down, then let them try again.
    clearInterval(ticking);
    var until = Date.now() + seconds * 1000;
    function tick(){
      var left = Math.ceil((until - Date.now()) / 1000);
      if (left <= 0){
        clearInterval(ticking); ticking = null;
        $('sign-in-button').disabled = false;
        $('pin-message').textContent = before ? before + ' You can try again now.' : '';
        return;
      }
      $('sign-in-button').disabled = true;
      $('pin-message').textContent = (before ? before + ' ' : '') + 'Try again in ' + span(left) + '.';
    }
    ticking = setInterval(tick, 1000);
    tick();
  }
  function askPin(session){
    leavePractice();
    forgetMixer();                 // nothing of the mixer stays in the page of someone who is signed out
    show('sign-in');
    $('about').textContent = 'This mixer asks for a PIN.';
    if (!session.ready){
      $('sign-in-button').disabled = true;
      $('pin-message').textContent = 'No PIN is set up on the mixer, so nobody can sign in. On the Pi, run: sudo campmixer pin --new';
      later(check, 5000);
    } else if (session.wait > 0) holdOff(session.wait, 'Too many wrong PINs have been tried.');
    else if (!ticking) $('sign-in-button').disabled = false;
  }
  function check(){
    api('/api/session').then(function(answer){
      if (answer.data.signed_in){ show('app'); connect(); }
      else askPin(answer.data);
    }, function(){
      $('about').textContent = 'Cannot reach the mixer\'s Pi. Trying again.';
      later(check, 2000);
    });
  }
  $('pin-form').addEventListener('submit', function(event){
    event.preventDefault();
    var pin = $('pin').value.replace(/[\s-]/g, '');
    if (!pin) return;
    $('sign-in-button').disabled = true;
    $('pin-message').textContent = 'Checking.';
    api('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pin: pin })
    }).then(function(answer){
      $('pin').value = '';
      clearInterval(ticking); ticking = null;
      if (answer.status === 200){
        $('pin-message').textContent = '';
        $('sign-in-button').disabled = false;
        show('app'); connect();
      } else if (answer.status === 403){
        if (answer.data.wait > 0) holdOff(answer.data.wait, 'That is not the PIN.');
        else { $('pin-message').textContent = 'That is not the PIN.'; $('sign-in-button').disabled = false; }
      } else if (answer.status === 429){
        holdOff(answer.data.wait || 30, 'Too many wrong PINs have been tried.');
      } else {
        $('pin-message').textContent = answer.data.error || 'The mixer could not check the PIN.';
        $('sign-in-button').disabled = false;
      }
    }, function(){
      $('pin-message').textContent = 'Cannot reach the mixer\'s Pi.';
      $('sign-in-button').disabled = false;
    });
  });
  $('sign-out').addEventListener('click', function(){
    api('/api/logout', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }).then(function(){
      if (ws){ ws.onclose = null; ws.close(); ws = null; }
      online = false;
      link = { open: false, up: false };
      forgetMixer();
      $('test').disabled = true;
      check();
    }, function(){});
  });

  // ----- the mixer -----

  function send(bytes){
    if (practice){
      var core = practice;
      setTimeout(function(){ if (practice === core) core.receive(bytes); }, 0);
      return true;
    }
    if (!ws || ws.readyState !== 1) return false;
    ws.send(bytes);
    return true;
  }
  function usable(){
    if (!mixer || mixer.version !== L.VERSION) return false;
    return practice ? true : link.up && Date.now() - mixerSeen < STALE_MS;
  }
  function ready(strip){ return usable() && strip.known; }
  function clearStrips(){
    closeAsk();                    // a question about a strip that is going away goes with it
    strips.forEach(function(strip){ clearTimeout(strip.sendTimer); });
    if (seen) seen.disconnect();
    strips = [];
    $('strips').textContent = '';
  }
  function forgetMixer(){
    clearStrips();
    mixer = null;
  }

  function drawStrip(strip){
    var position = L.positionOf(strip.level), text = L.levelText(strip.level);
    strip.fader.style.setProperty('--p', position.toFixed(4));
    strip.readout.textContent = text;
    strip.fader.setAttribute('aria-valuenow', strip.level === L.OFF ? L.LEVEL_MIN / 10 - 1 : strip.level / 10);
    strip.fader.setAttribute('aria-valuetext', strip.muted ? text + ', muted' : text);
    strip.mute.setAttribute('aria-pressed', strip.muted ? 'true' : 'false');
    strip.mute.textContent = strip.muted ? 'Muted' : 'Mute';
    strip.element.classList.toggle('muted', strip.muted);
    strip.element.classList.toggle('unknown', !strip.known);
    drawPhantom(strip);
  }
  function drawPhantom(strip){
    // The 48V button is there only on a channel whose core says it has that switch, and it is lit only when the
    // core says the 48 V is on. Nothing here is taken for granted: wrong either way, somebody's gear is at stake.
    var button = strip.phantomButton;
    if (!strip.phantomSwitch){
      if (button){ button.remove(); strip.phantomButton = null; }
      return;
    }
    if (!button){
      button = strip.phantomButton = make('button', 'phantom', '48V');
      button.type = 'button';
      button.addEventListener('click', function(){ if (ready(strip)) askPhantom(strip); });
      strip.head.insertBefore(button, strip.readout);
    }
    button.setAttribute('aria-pressed', strip.phantom ? 'true' : 'false');
    button.setAttribute('aria-label', strip.name + ' 48 volt phantom power');
  }

  // ----- 48 V: asked about first, both ways -----
  // On, it can damage a keyboard's or a laptop's output. Off, it silences a microphone that runs on it. So neither
  // happens on one stray touch. The answer goes to the core as "on" or "off", never as "the other way", so two
  // phones cannot undo each other; and the button changes when the core reports back, not before.
  function askPhantom(strip){
    var on = !strip.phantom;
    closeAsk();
    asking = { strip: strip, on: on, back: document.activeElement };
    $('ask-title').textContent = (on ? 'Turn on 48 V for ' : 'Turn off 48 V for ') + strip.name + '?';
    $('ask-text').textContent = on
      ? '48 V phantom power is for condenser microphones and active DI boxes. It can damage a keyboard, laptop or phone plugged into this input.'
      : 'A microphone that runs on 48 V goes silent without it.';
    $('ask-yes').textContent = on ? 'Turn on 48 V' : 'Turn off 48 V';
    $('ask').hidden = false;
    document.body.classList.add('asking');
    $('ask-no').focus();           // the safe answer is the one under the thumb and the Enter key
  }
  function closeAsk(){
    if (!asking) return;
    var back = asking.back;
    asking = null;
    $('ask').hidden = true;
    document.body.classList.remove('asking');
    if (back && back.isConnected && back.focus) back.focus({ preventScroll: true });
  }
  $('ask-no').addEventListener('click', closeAsk);
  $('ask-yes').addEventListener('click', function(){
    var question = asking;
    closeAsk();
    if (question && ready(question.strip)) send(L.set(question.strip.channel, L.PHANTOM, question.on ? 1 : 0));
  });
  $('ask').addEventListener('click', function(event){
    if (event.target === this) closeAsk();           // a touch outside the question means no
  });
  document.addEventListener('keydown', function(event){
    if (!asking) return;
    if (event.key === 'Escape'){ event.preventDefault(); closeAsk(); }
    else if (event.key === 'Tab'){                   // the keyboard stays inside the question until it is answered
      event.preventDefault();
      (document.activeElement === $('ask-no') ? $('ask-yes') : $('ask-no')).focus();
    }
  });
  function drawMeter(strip){
    // An input's meter shows what is coming in on it, fader down and muted or not: that is how to see a
    // microphone is alive before bringing it up. The master's shows what is going out.
    strip.cover.style.width = ((1 - L.meterWidth(strip.peak)) * 100).toFixed(1) + '%';
  }
  function sendLevel(strip){
    clearTimeout(strip.sendTimer); strip.sendTimer = null;
    if (strip.sent === strip.level) return;
    strip.sent = strip.level;
    strip.sentAt = Date.now();
    send(L.set(strip.channel, L.LEVEL, strip.level));
  }
  function setLevel(strip, level){
    if (level === strip.level) return;
    strip.level = level;
    drawStrip(strip);
    // Not every pixel of a drag is worth a message: the newest value goes out, at a steady rate.
    var wait = SEND_EVERY_MS - (Date.now() - strip.sentAt);
    if (wait <= 0) sendLevel(strip);
    else if (!strip.sendTimer) strip.sendTimer = setTimeout(function(){ sendLevel(strip); }, wait);
  }
  function settle(strip){              // the finger is off: send where it ended up, and let the echoes die down
    sendLevel(strip);
    strip.levelHold = Date.now() + HOLD_MS;
  }

  function buildStrip(number, label){
    // Made as soon as the core says how many channels there are. It stays greyed until the core has said
    // where that channel stands.
    var strip = { channel: number, level: L.OFF, muted: false, name: label, known: false, visible: !seen, peak: -Infinity,
                  sent: L.OFF, sentAt: 0, sendTimer: null, drag: null, levelHold: 0, muteHold: 0,
                  phantomSwitch: false, phantom: false, phantomButton: null };
    var channel = { name: label };
    var element = make('div', 'strip');
    var head = make('div', 'strip-head');
    var name = make('span', 'strip-name', channel.name);
    var down = make('button', 'step', '−'), up = make('button', 'step', '+');
    var readout = make('output', 'strip-level');
    var mute = make('button', 'mute', 'Mute');
    [down, up, mute].forEach(function(button){ button.type = 'button'; });
    down.setAttribute('aria-label', channel.name + ' down half a dB');
    up.setAttribute('aria-label', channel.name + ' up half a dB');
    [name, readout, mute].forEach(function(part){ head.appendChild(part); });

    var fader = make('div', 'fader');
    fader.setAttribute('role', 'slider');
    fader.setAttribute('tabindex', '0');
    fader.setAttribute('aria-label', channel.name + ' level');
    fader.setAttribute('aria-valuemin', L.LEVEL_MIN / 10 - 1);
    fader.setAttribute('aria-valuemax', L.LEVEL_MAX / 10);
    var track = make('div', 'fader-track');
    track.appendChild(make('div', 'fader-fill'));
    fader.appendChild(track);
    fader.appendChild(make('div', 'fader-unity'));
    fader.appendChild(make('div', 'fader-thumb'));

    var meter = make('div', 'meter'), cover = make('div', 'meter-cover');
    meter.appendChild(cover);
    // Under the name: half a dB down, the fader with its meter beneath it, half a dB up.
    var row = make('div', 'strip-row'), middle = make('div', 'strip-middle');
    [fader, meter].forEach(function(part){ middle.appendChild(part); });
    [down, middle, up].forEach(function(part){ row.appendChild(part); });
    [head, row].forEach(function(part){ element.appendChild(part); });
    strip.element = element; strip.head = head; strip.label = name; strip.readout = readout; strip.mute = mute;
    strip.fader = fader; strip.cover = cover; strip.down = down; strip.up = up;
    element.campmixerStrip = strip;
    if (seen) seen.observe(element);

    // Dragging is relative: the fader moves by as far as the finger moves, from where it was. Touching the
    // track does not make it jump there, because a jump in level is the last thing wanted in front of a crowd.
    fader.addEventListener('pointerdown', function(event){
      if (!ready(strip) || event.button > 0 || strip.drag) return;
      strip.drag = { id: event.pointerId, x: event.clientX, from: L.positionOf(strip.level), was: strip.level,
                     width: Math.max(1, fader.clientWidth - 30), armed: false };
      try { fader.setPointerCapture(event.pointerId); } catch (e) { /* older browsers */ }
      fader.focus({ preventScroll: true });
    });
    fader.addEventListener('pointermove', function(event){
      var drag = strip.drag;
      if (!drag || event.pointerId !== drag.id) return;
      var moved = event.clientX - drag.x;
      if (!drag.armed){
        if (Math.abs(moved) < 6) return;       // a touch that is really the start of a scroll moves nothing
        drag.armed = true;
        drag.x += moved > 0 ? 6 : -6;
        moved = event.clientX - drag.x;
      }
      setLevel(strip, L.levelAt(Math.max(0, Math.min(1, drag.from + moved / drag.width))));
    });
    function endDrag(event){
      var drag = strip.drag;
      if (!drag || event.pointerId !== drag.id) return;
      strip.drag = null;
      if (event.type === 'pointercancel') setLevel(strip, drag.was);   // the browser took the gesture for a scroll: put it back
      settle(strip);
    }
    fader.addEventListener('pointerup', endDrag);
    fader.addEventListener('pointercancel', endDrag);
    fader.addEventListener('keydown', function(event){
      var steps = { ArrowLeft: -1, ArrowDown: -1, ArrowRight: 1, ArrowUp: 1, PageDown: -6, PageUp: 6 }[event.key];
      if (!ready(strip) || (!steps && event.key !== 'Home')) return;
      event.preventDefault();
      setLevel(strip, event.key === 'Home' ? L.OFF : L.nudge(strip.level, steps));
      settle(strip);
    });
    function step(by){
      return function(){
        if (!ready(strip)) return;
        setLevel(strip, L.nudge(strip.level, by));
        settle(strip);
      };
    }
    down.addEventListener('click', step(-1));
    up.addEventListener('click', step(1));
    mute.addEventListener('click', function(){
      if (!ready(strip)) return;
      strip.muted = !strip.muted;
      strip.muteHold = Date.now() + HOLD_MS;
      drawStrip(strip); drawMeter(strip);
      send(L.set(strip.channel, L.MUTE, strip.muted ? 1 : 0));
    });
    drawStrip(strip); drawMeter(strip);
    return strip;
  }

  function onInfo(info){
    // The core's heartbeat: its version and how many inputs and outputs it has.
    var changed = !mixer || mixer.version !== info.version || mixer.inputs !== info.inputs || mixer.outputs !== info.outputs;
    mixer = info; mixerSeen = Date.now();
    if (changed){
      clearStrips();
      if (info.version === L.VERSION){
        var box = $('strips'), n;
        box.appendChild(make('h2', 'group', info.inputs === 1 ? 'Input' : 'Inputs'));
        for (n = 0; n < info.inputs; n++) strips.push(buildStrip(n, 'Input ' + (n + 1)));
        strips.forEach(function(strip){ box.appendChild(strip.element); });
        box.appendChild(make('h2', 'group', info.outputs === 1 ? 'Output' : 'Outputs'));
        for (n = 0; n < info.outputs; n++){
          strips.push(buildStrip(info.inputs + n, info.outputs === 1 ? 'Output' : 'Output ' + (n + 1)));
          box.appendChild(strips[strips.length - 1].element);
        }
        send(L.getState());            // a different mixer than a moment ago: ask where everything stands
      }
    }
    drawAvailability();
  }
  function onChannel(update){
    var strip = strips[update.channel], now = Date.now();
    if (!strip) return;
    if (update.name !== strip.name){
      strip.name = update.name; strip.label.textContent = update.name;
      strip.fader.setAttribute('aria-label', update.name + ' level');
      strip.down.setAttribute('aria-label', update.name + ' down half a dB');
      strip.up.setAttribute('aria-label', update.name + ' up half a dB');
    }
    // The core has the last word, except on a fader that a finger is on (or has only just left).
    if (!strip.known || (!strip.drag && !strip.sendTimer && now >= strip.levelHold)){
      strip.level = update.level; strip.sent = update.level;
    }
    if (!strip.known || now >= strip.muteHold) strip.muted = update.muted;
    if (update.phantomSwitch !== strip.phantomSwitch || update.phantom !== strip.phantom){
      strip.phantomSwitch = update.phantomSwitch; strip.phantom = update.phantom;
      if (asking && asking.strip === strip) closeAsk();   // what the question was about has changed under it
    }
    strip.known = true;
    drawStrip(strip);
  }
  function onMeters(meters){
    clearTimeout(meterTimer);
    strips.forEach(function(strip, n){
      var peak = n < meters.peaks.length ? meters.peaks[n] : -Infinity;
      strip.peak = Math.max(peak, strip.peak - 1.5);       // rise at once, fall back gently
      if (strip.visible) drawMeter(strip);
    });
    meterTimer = setTimeout(function(){                    // nothing for a while: the meters drop, not freeze
      strips.forEach(function(strip){ strip.peak = -Infinity; drawMeter(strip); });
    }, 400);
  }
  function fromCore(bytes){
    var message = L.decode(bytes);
    if (!message) return;
    if (message.type === 'info') onInfo(message);
    else if (message.type === 'channel') onChannel(message);
    else if (message.type === 'meters') onMeters(message);
  }

  function drawAvailability(){
    var ok = usable(), note = '';
    if (!ok){
      if (mixer && mixer.version !== L.VERSION) note = 'The mixer core speaks a newer version of the link (' + mixer.version + ') than this app (' + L.VERSION + '). Update CampMixer OS.';
      else if (!online) note = 'No contact with the mixer\'s Pi.';
      else if (!link.open) note = 'The Pi\'s serial port is not available, so the mixer core cannot be reached. The details are below.';
      else if (!link.up) note = 'No signal from the mixer core. These controls work once the mixer core is wired up and running.';
      else if (!mixer) note = 'The mixer core is connected but has not reported its settings yet.';
      else note = 'The mixer core has stopped reporting its settings.';
    }
    if (!ok) closeAsk();           // nothing can be switched on a mixer that is out of reach
    $('mixer').classList.toggle('unavailable', !ok);
    $('mixer-waiting').hidden = ok;
    $('mixer-note').textContent = note;
    $('practice-start').hidden = ok || !!practice;
    $('practice-banner').hidden = !practice;
  }

  function startPractice(){
    if (practice) return;
    // The same number of inputs and outputs as the mixer that was last heard from, if there was one.
    var shape = mixer && mixer.version === L.VERSION ? mixer : { inputs: 4, outputs: 1 }, beat = 0;
    forgetMixer();
    practice = new L.PracticeCore(fromCore, shape.inputs, shape.outputs);
    practiceTimer = setInterval(function(){
      if (!practice) return;
      practice.meters();
      if (++beat % 20 === 0) practice.heartbeat();
    }, 50);
    send(L.getState());
    drawAvailability();
  }
  function leavePractice(){
    if (!practice) return;
    practice = null;
    clearInterval(practiceTimer);
    forgetMixer();
    drawAvailability();
    send(L.getState());
  }
  $('practice-start').addEventListener('click', startPractice);
  $('practice-leave').addEventListener('click', leavePractice);
  setInterval(function(){ if (!$('app').hidden) drawAvailability(); }, 1000);

  // ----- the Pi's own report, and the loopback test -----

  function hex(bytes){
    var out = [];
    for (var i = 0; i < bytes.length && i < 24; i++) out.push((bytes[i] < 16 ? '0' : '') + bytes[i].toString(16));
    return out.join(' ') + (bytes.length > 24 ? ' … (' + bytes.length + ' bytes)' : '');
  }
  function same(a, b){
    if (a.length !== b.length) return false;
    for (var i = 0; i < a.length; i++) if (a[i] !== b[i]) return false;
    return true;
  }
  function render(s){
    $('about').textContent = 'CampMixer OS ' + s.version + ' on ' + s.hostname;
    var report = s.link, pill = $('link');
    var cameUp = report.up && !link.up;
    link = { open: report.open, up: report.up };
    if (!report.open){
      pill.textContent = 'Serial port not available'; pill.className = 'pill bad';
      $('link-detail').textContent = report.error || 'The port could not be opened.';
    } else if (report.up){
      pill.textContent = 'Connected'; pill.className = 'pill ok';
      $('link-detail').textContent = 'Frames are arriving from the mixer core.';
    } else {
      pill.textContent = 'No signal from the mixer core'; pill.className = 'pill';
      $('link-detail').textContent = 'The serial port is open, but nothing valid has arrived in the last two seconds.';
    }
    $('rx').textContent = report.rx_frames;
    $('tx').textContent = report.tx_frames;
    $('errors').textContent = report.crc_errors + report.format_errors;
    $('clients').textContent = s.clients;
    var net = s.network || {};
    if (net.wifi) $('wifi').textContent = (net.wifi.mode === 'own' ? 'Own network: ' : 'Joined: ') + net.wifi.name;
    else $('wifi').textContent = 'Not in use';
    var list = (net.addresses || []).map(function(a){ return a.address + ' (' + a.interface + ')'; });
    $('addresses').textContent = list.length ? list.join(', ') : 'none yet';
    $('storage').textContent = s.locked ? 'Locked. Safe to pull the power.' : 'Unlocked. Changes are kept.';
    var wrong = (s.sign_in || {}).wrong_pins || 0;
    $('wrong-pins').textContent = wrong ? wrong + ' since start-up' : 'none since start-up';
    $('wrong-pins').className = wrong >= 20 ? 'warn' : '';

    // A real mixer core has turned up while practising: stop pretending, so nobody mistakes one for the other.
    if (practice && report.up) leavePractice();
    // Ask the core for its settings when it appears, and again for as long as it has not answered.
    if (!practice && report.up && (cameUp || !usable() || strips.some(function(strip){ return !strip.known; }))) send(L.getState());
    drawAvailability();
  }
  function onFrame(bytes){
    $('last').textContent = hex(bytes);
    if (pending && same(bytes, pending.bytes)){
      var ms = Math.round(performance.now() - pending.sent);
      pending = null; clearTimeout(loopTimer);
      $('result').textContent = 'Came back in ' + ms + ' ms. The serial port and the bridge work.';
      return;
    }
    if (!practice) fromCore(bytes);
  }
  function connect(){
    if (ws) return;
    ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws');
    ws.binaryType = 'arraybuffer';
    ws.onopen = function(){
      online = true;
      $('test').disabled = false;
      send(L.getState());
    };
    ws.onclose = function(){
      ws = null; online = false;
      link = { open: false, up: false };
      $('test').disabled = true;
      $('about').textContent = 'Lost contact with the mixer\'s Pi. Trying again.';
      drawAvailability();
      later(check, 1500);  // signed out (the PIN was changed), or just out of range? check() finds out
    };
    ws.onmessage = function(ev){
      if (typeof ev.data === 'string'){
        var msg; try { msg = JSON.parse(ev.data); } catch (e) { return; }
        if (msg.type === 'status') render(msg);
        else if (msg.type === 'error') $('result').textContent = 'The bridge said: ' + msg.error + '.';
      } else onFrame(new Uint8Array(ev.data));
    };
  }
  $('test').addEventListener('click', function(){
    if (!ws || ws.readyState !== 1) return;
    var bytes = new Uint8Array(9);
    crypto.getRandomValues(bytes);
    bytes[0] = 0x7e;
    pending = { bytes: bytes, sent: performance.now() };
    ws.send(bytes);
    $('result').textContent = 'Sent. Waiting for it to come back.';
    clearTimeout(loopTimer);
    loopTimer = setTimeout(function(){
      if (!pending) return;
      pending = null;
      $('result').textContent = 'Nothing came back. Check the jumper between pins 8 and 10.';
    }, 1500);
  });
  check();
})();
