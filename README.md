# CampMixer OS 0.4.4

Its own operating system for the Camp PA Mixer's Raspberry Pi, built on Raspberry Pi OS Lite (64-bit).
You write one image to a microSD card and the Pi starts up as the mixer's remote. There is no install step.

This folder is the source for that image.

## What the OS does

- **Web app at http://mixer.local, behind a PIN.** A fader, a mute button and a level meter for every input and every output the mixer has, however many that is, expansion units included. See *The web app* and *Who can reach it* below.
- **Wi-Fi.** It joins a network you have listed when that network is in range, so the mixer sits on the venue's LAN. Otherwise it makes its own network, `CampMixer`.
- **Ethernet.** Plug it into any network and it takes an address. A cable straight to a laptop works too, within a few seconds, on a 169.254 address. It keeps asking for a proper address in the background, so a router that starts up after the mixer is still picked up without touching the cable.
- **SSH** is on: `ssh mixer@mixer.local`.
- **Serial link** to the mixer core on header pins 8 and 10, 115200 baud, with the console kept off it.
- **Read-only card.** After its first start nothing writes to the microSD card, not even while starting up, and the power can be pulled at any time.
- **Hardware watchdog.** If the Pi hangs it restarts itself. The mixer's audio and knobs never depend on the Pi.
- **Bluetooth off.** The built-in Wi-Fi radio is left alone whenever a USB Wi-Fi adaptor is fitted.

## Getting the image

The image is built by Raspberry Pi's own image builder (pi-gen) with one extra stage, `image/stage-campmixer`. The build needs a machine with open internet access.

**On GitHub.** Every push to this repository builds the image and publishes it under *Releases* as `CampMixerOS-<version>-build<n>.img.xz`. You can also start a build from the *Actions* tab.

**On your own Linux machine.** With Docker and git installed:

    cd image
    ./build-image.sh

The image lands in `image/build/pi-gen/deploy/`.

One password is built into the image and into this source: the standard Wi-Fi password, `plumisland39`, which every CampMixer starts with and which is therefore public. No login password, PIN or key is built in.

## First start

1. In **Raspberry Pi Imager** choose *Use custom*, pick the `.img.xz` file and write it to the card. Skip the customisation step.
2. Before the first start: take the card out and put it back in, open `campmixer.txt` on the `bootfs` drive and type in `LOGIN_PASSWORD` (4 or more; longer is safer), `WEB_PIN` (4 to 12 digits), any networks to join, and a `WIFI_PASSWORD` of your own (8 characters or more) if the standard one will not do. Doing this now saves pulling the card later to read made-up ones.
3. Put the card in the Pi and power it. The first start takes about two minutes and restarts itself once (twice if the Pi's own bootloader gets an update): it grows the file system to fill the card, makes its SSH keys, sets the passwords and locks the card.
4. Open **http://mixer.local** and type the PIN, or log in with `ssh mixer@mixer.local`.

Any password or PIN you left blank is made up on that first start and written into `campmixer.txt`. Put the card back in your computer to read it. A login password you chose yourself is removed from the file once it has been set.

`sudo` asks for that same login password.

## The settings file

`campmixer.txt` on the card's `bootfs` partition. Edit it with the card in a computer, or use the commands below. Changes made in the file take effect at the next start.

    WIFI_NAME=CampMixer          the mixer's own network
    WIFI_PASSWORD=plumisland39    8 to 63 characters; every CampMixer starts with this one
    WIFI_BAND=2.4                2.4 or 5
    WIFI_CHANNEL=6
    JOIN_WIFI_1=Church Hall      up to four networks to join when in range
    JOIN_WIFI_1_PASSWORD=...
    WEB_PIN=...                  4 to 12 digits; each phone is asked for it once
    ALLOW_EASY_PIN=no            yes keeps a PIN anybody would guess
    WIFI_RADIO=auto              auto, usb or onboard
    LOGIN_PASSWORD=              read once, on the first start

How the radio decides:

1. At start-up, if a listed network is in range, join it. Phones on that network open http://mixer.local.
2. If none is in range, run the mixer's own network.
3. If a joined network drops out for 30 seconds, switch to the mixer's own network.
4. While on its own network with networks listed, it keeps an eye out for them, but only when nobody is connected to it. After a minute with nobody on, it leaves its own network for about ten seconds to look. If it finds nothing it looks again after 5 minutes of nobody, then 10, 20, 40, and hourly from there. It never looks while a phone is connected.
5. A listed network that refuses the mixer (wrong password, say) is left alone for half an hour.

So a venue network that comes up after the mixer, or comes back after an outage, is picked up again without a restart, as long as nobody is using the mixer's own network at the time. `sudo campmixer wifi join` looks straight away.

One radio does one job at a time, so while it is on a venue's Wi-Fi the `CampMixer` network is not there. Ethernet is separate and always works alongside either.

Cannot be joined: networks that need a username, networks with a sign-in web page, hidden networks, and WPA3-only networks (WPA2, and mixed WPA2/WPA3, are fine).

A phone on the mixer's own network will say it has no internet, because it has none unless the mixer's Ethernet port is plugged into a network that does. Tell the phone to stay connected anyway.

Save the file as plain text in UTF-8, which is what Notepad and TextEdit do by default now. A file in another encoding is still read, but a name with an accent or a curly apostrophe in it (`Jordan’s iPhone`) will not match the real network, and `campmixer wifi` says so.

Every CampMixer starts with the same Wi-Fi name and password, `CampMixer` and `plumisland39`, so one sticker and one sentence cover them all. That makes the password public: wherever a stranger joining the mixer's network would matter, change it, in the file or with `sudo campmixer wifi own --password ...`. Two mixers that run within range of each other also need different names, or a phone cannot tell them apart: `sudo campmixer wifi own --name "CampMixer 2"`. If `WIFI_PASSWORD` is blank or cannot be used (it needs 8 to 63 characters), the mixer makes one up at start-up and writes it into the file. There is no built-in PIN: a made-up one also replaces `WEB_PIN` when it is blank, is not 4 to 12 digits, or is one anybody would guess (1234, 1111, 1212, a year such as 1987, 123456 and the like): an 8-digit one is made up. An easy PIN stays only if `ALLOW_EASY_PIN` says `yes` (see *Who can reach it*). The commands refuse a value the mixer could not use, and change nothing.

## The web app

Open http://mixer.local on a phone or a laptop on the same network and type the PIN. Each phone is asked once and stays signed in, through restarts, until the PIN is changed.

**Any number of inputs and outputs.** The mixer core tells the Pi how many it has, and the page shows a strip for each: the inputs first, then the outputs. Nothing on the Pi is set up for a particular mixer, so the same card drives the four-input, one-output Camp PA Mixer or a much bigger one (up to 250 inputs and outputs together; `PROTOCOL.md` has what the serial link carries).

Every input and every output has:

- **A fader.** Drag it sideways. It moves by as far as your finger moves, from where it was: touching the track does not make it jump, and scrolling the page past it moves nothing. The mark three quarters of the way along is 0 dB. All the way left is off.
- **− and +** for half a decibel at a time.
- **Mute.** The button turns red and reads *Muted*.
- **A meter.** An input's meter shows what is coming in on it, fader down and muted or not, so a microphone can be seen to be alive before it is brought up. An output's shows what is going out of it.

The settings live in the mixer core, not in the phone or the Pi. A change made on one phone shows on every other phone as the core confirms it, and so will a turn of a knob on the front panel.

When the mixer core cannot be reached the controls go grey and the page says why. From there **Try the controls without a mixer** runs a practice mixer inside the page, the same size as the mixer that was last connected. It is marked as practice, is connected to nothing, and ends by itself the moment a real mixer core appears.

Not in yet: gain, 48 V, ground lift, high-pass, feedback filter, names and scenes (the front panel's menus).

## Who can reach it

**The web app asks for a PIN**, on every network. Without it a browser gets the sign-in page and nothing else: no controls, no status, nothing about the mixer.

- The PIN is `WEB_PIN` in `campmixer.txt`: 4 to 12 digits. `sudo campmixer pin` shows it, `sudo campmixer pin NEW` changes it and `sudo campmixer pin --new` makes one up. Changing it signs every phone out. (A PIN typed on the command line stays in your shell's history and in the Pi's log until the next restart, where you and root can read it. `--new`, or editing the file, leaves no such trace.)
- **Guessing is slow.** One address gets five wrong tries, then waits before each further one: 30 s, a minute, two, then five minutes every time. Everybody together gets 20 wrong tries, of which one comes back every three minutes, so changing address does not help. That caps guessing at about 500 PINs a day: centuries against the 100 million eight-digit PINs, a few years against the million six-digit ones.
- **A PIN of 4 or 5 digits gets a stricter limit**, because it has so few possibilities: everybody together gets 10 wrong tries and one comes back every quarter of an hour, about 100 a day. Against a four-digit PIN that is still about a one-in-a-hundred chance for every day someone on the network spends guessing. That is fine behind the mixer's own Wi-Fi password, where a stranger cannot reach the page at all. On a network the mixer shares with strangers, or lives on for good, use six digits or more, better eight.
- **An easy PIN is turned down unless you force it.** `sudo campmixer pin --force 1948` sets it anyway and writes `ALLOW_EASY_PIN=yes` into the file, which is what stops the next start-up from replacing it. An easy PIN gets the strict limit whatever its length, but no limit makes up for the PIN itself: 1234 falls at the first try, and a year is among the first hundred or so that anyone tries, about a day of guessing. Such a PIN is only as good as the Wi-Fi password in front of it.
- The count of wrong PINs is on the page (under *The Pi and its link*) and in `campmixer status`. It outlives a restart of the bridge.
- The price of that limit: someone who keeps feeding the page wrong PINs can keep *new* phones from signing in (with a short PIN, ten wrong tries are enough to make the next phone wait up to a quarter of an hour). Phones already signed in are not affected.
- A web site out on the internet cannot reach in through someone's browser: the mixer answers only to its own name (`mixer.local`, `mixer`, `mixer.lan` and the like) or its address, takes a PIN only from its own page, and lets only its own page use the controls.

**What the PIN does not do.** The page is plain `http`, so the PIN and the sign-in cookie cross the network unencrypted. The PIN keeps out everyone who can merely *reach* the page. It does not keep out someone who records the network's traffic at the moment a phone signs in or is in use. On the mixer's own Wi-Fi that takes the Wi-Fi password. The standard one is public, so on a mixer that still has it this is open to anyone in range who knows how. Where that matters, change the Wi-Fi password and give the new one only to people you would hand the mixer to. On a venue's network it takes access to their network gear or a deliberate attack on the network. Closing that gap needs `https`, which on a local name means a certificate warning on every phone, so it is left out for now.

**The rest of the Pi.**

- SSH needs the login password, on every network. Root cannot log in. An obvious login password in `campmixer.txt` (`1234`, `password1`, `Mixer2026!`) or one shorter than 4 characters is not used; one is made up instead. A short login password is the weakest door on the mixer, since this login has `sudo`: keep it long on any network you do not control.
- `campmixer.txt` holds the Wi-Fi password, the PIN and perhaps the login password, so the partition it is on can be read by root only. On the Pi, reading it takes `sudo`. With the card in a computer, anyone holding the card can read it: whoever has the card has the mixer.
- The part that talks to the network (the bridge) runs as its own user with no rights to anything but the serial port and the network. It never sees the PIN, only a slow hash of it, and cannot read `campmixer.txt`.
- The standard Wi-Fi password is the one secret built into the image and this source, and it is no secret. No login password, PIN or key is built in: each card makes up its own on its first start.

## Commands

    campmixer status                          what it is doing now, and which radio it is using
    sudo campmixer pin                        the PIN the web app asks for
    sudo campmixer pin 48271935               change it (every phone signs in again)
    sudo campmixer pin --new                  change it to a made-up one
    sudo campmixer pin --force 1948           change it to one the mixer would turn down as too easy
    sudo campmixer wifi                       settings in use, with passwords and the PIN
    sudo campmixer wifi join "Church Hall" "its password"
    sudo campmixer wifi forget "Church Hall"
    sudo campmixer wifi own --name "Hall Mixer" --password "something long"
    sudo campmixer lock                       card read-only
    sudo campmixer unlock                     card writable, for changes and updates
    sudo campmixer update                     put the newest CampMixer OS over this one (see below)
    sudo campmixer password                   change your login password (unlock first)
    campmixer logs

The Wi-Fi and PIN commands take effect straight away and also work while the card is locked.

## Keeping the card alive

A microSD card wears out from writes, so the aim is no writes at all.

- **Locked** (the normal state): the whole system runs on a copy-on-write layer in memory, and the boot partition is mounted read-only. Nothing reaches the card. Anything changed while locked is gone at the next start, which is the point.
- Being locked is one word, `overlayroot=tmpfs`, in `cmdline.txt`. Everything else follows from it at start-up: the boot partition goes read-only, and the stock file system check is skipped, because that check writes two blocks to the card every time it runs.
- Logs stay in memory, capped at 24 MB.
- Swap is compressed memory only. Raspberry Pi OS's default also keeps a swap file on the card; that is switched off.
- Scheduled jobs that write (package checks, log rotation, manual-page index, package database backup, file system scrub) are off.
- At start-up the network profiles are rewritten only if `campmixer.txt` changed.
- The only writes left are the ones you ask for: `campmixer wifi ...` and `campmixer pin ...` rewrite `campmixer.txt`, and updates happen while unlocked.

Reading does not wear a card. The Pi reads the system from the card once at start-up and after that runs from memory, so in use the card sits idle.

**To change something by hand:** `sudo campmixer unlock`, `sudo reboot`, make the change, `sudo campmixer lock`, `sudo reboot`.

**To update the OS** without taking the card out, over SSH, with the Pi on a network that has internet access (Ethernet is easiest):

    sudo campmixer unlock
    sudo reboot
    sudo campmixer update
    sudo reboot

`campmixer update` fetches the newest version published under Releases and runs its installer over the running system. It takes a couple of minutes, keeps your settings, passwords and PIN, locks the card again, and asks for the restart. Phones stay signed in.

Build 4 (0.3.0) has no `update` command yet. To update from it, after the unlock and restart:

    curl -fsSL https://github.com/jordan17238/campmixer-os/archive/refs/tags/v0.4.4-build11.tar.gz | tar xz -C /tmp
    sudo bash /tmp/campmixer-os-0.4.4-build11/install.sh
    sudo reboot

Or write the newer image to the card, as for a new one. Copy `campmixer.txt` off the card first and put it back afterwards to keep your settings.

**If it will not start after locking:** put the card in a computer, open `cmdline.txt` on `bootfs`, delete the word `overlayroot=tmpfs` and save. That unlocks it.

## Try it before the mixer exists

- Open http://mixer.local and sign in. The page says "No signal from the mixer core", which is right: there is no Teensy yet. **Try the controls without a mixer** lets you use the faders anyway, in practice mode.
- **Loopback test** (under *The Pi and its link to the mixer core*): join header pins 8 and 10 with a jumper wire and press *Send a test frame*. "Came back in N ms" proves the serial port, the bridge and the WebSocket.
- **A stand-in mixer core on a laptop:** with a 3.3 V USB serial adaptor wired to pins 8, 10 and 6, `python3 tests/standin_core.py /dev/ttyUSB0` answers the app the way the Teensy will, so the real page can be used over the real link.
- Without the USB adaptor, the Pi's built-in radio makes the `CampMixer` network on the bench. Once the Alfa is plugged in at start-up, that takes over and the built-in radio goes quiet.

## Wiring to the Teensy

| Pi header pin | Signal | Goes to |
| --- | --- | --- |
| 8 (GPIO14) | TXD, 3.3 V | optocoupler, then Teensy RX |
| 10 (GPIO15) | RXD, 3.3 V | optocoupler, from Teensy TX |
| 6 | Ground | Pi side of the optocouplers |
| 1 | 3.3 V | Pi side of the optocouplers |

The framing on that link, and the messages for volume, mute and meters that the Teensy's firmware has to speak, are in `PROTOCOL.md`.

## What is in this folder

| Path | What it is |
| --- | --- |
| `rootfs/` | Every file CampMixer OS adds to Raspberry Pi OS, laid out as on the card |
| `install.sh` | Puts those files in place and edits the boot settings. The image build runs it |
| `campmixer.txt` | The settings file as it ships |
| `image/` | The pi-gen stage and the build script |
| `.github/workflows/`, `ci/` | The GitHub build |
| `tests/` | The checks |
| `PROTOCOL.md` | The serial framing, what a browser sees, and the messages for volume, mute and meters |

`install.sh` also runs on a Pi that already has stock Raspberry Pi OS Lite, if you ever want the layer without writing an image: copy this folder to `bootfs`, then `sudo bash /boot/firmware/campmixer-os/install.sh`.

## What was tested, and what was not

**The checks in `tests/`**, which run in a Linux workspace and again on GitHub before every build:

- Signing in: which PINs are allowed, the hash, the cookies (forged, altered, cut short, signed by another mixer with the same PIN), and the guess limit on a clock the test moves by hand, including the most anyone can get through in a day.
- The bridge, end to end, against a pretend serial port and a standard WebSocket client, from an address that counts as "the network": nothing without the PIN, the PIN refused when another site's page sends it, the wait after five wrong PINs, a restart that neither clears the count nor signs anyone out, a PIN change that signs everyone out, a missing or damaged sign-in file letting nobody in, sixty idle connections from one address, 400 malformed requests. Also both directions of the link, damaged frames, oversize messages, which host names it answers to, the serial port vanishing.
- Volume, mute and meters, end to end: signed-in phones, the real bridge, and the stand-in mixer core on the far side of a pretend serial port, as a mixer with 4 inputs and 1 output, 1 and 1, 48 and 8, and 200 and 50. The message examples in `PROTOCOL.md` are checked against both the web app's own code (run with Node) and the stand-in core.
- The framing, against the published COBS examples and the CRC check value.
- Every generated network profile, read back by NetworkManager 1.46's own parser, including awkward names and passwords.
- The radio's decisions against a scripted stand-in for NetworkManager: start-up, slow joins, looking again when idle, refused networks, drop-outs.
- The settings file in UTF-8, old Windows encodings and UTF-16, and the rules for passwords and the PIN: nothing built in but the standard Wi-Fi name and password, unusable values refused, made-up ones saved when the file's are blank or unusable, an easy PIN kept only on the owner's say-so (in the file or with `pin --force`), each card with its own key.
- The first-start password set-up, against a stand-in for the system's password tool.
- The installer's edits, against the stock Raspberry Pi OS `config.txt` and `cmdline.txt`, for both an image build and a running Pi, and that an image carries no password, PIN or key.
- The two start-up pieces that follow the lock.

**The web app in a real browser.** `tests/ui_check.js` drives headless Chrome at phone size with touch (it needs Chrome, so it is run by hand, not on GitHub): signing in, a cookie the page's own script cannot read, dragging the faders, a touch on the track and a scroll past a fader both changing nothing, the half-dB buttons, the keyboard, mute, a second phone following the first, a laptop with a mouse, dark mode, the mixer core going away, practice mode, the core coming back, a PIN change signing both phones out, the wait after wrong PINs, and a mixer with 32 inputs and 6 outputs (every strip there, the last output dragged at the bottom of the page, meters of off-screen strips left alone). No errors from the browser and nothing blocked by the page's own security rules.

**The image itself, started in an emulator.** `CampMixerOS-0.3.0-build4` was downloaded from Releases and started in QEMU as a Raspberry Pi 3B (QEMU has no Pi 4 model; the same image starts on both). The image's own `disable-bt` overlay was applied to the emulated board so the serial port came up as it does on the Pi, and the stand-in mixer core was connected to that serial port.

- The image holds no PIN, no password and no key. On its first start it made up a PIN and a Wi-Fi password, wrote them into the settings file, made its own key (root only), set the login password, locked the card and restarted.
- Locked: **no writes to the card at all** from power-on through seven minutes of signing in, guessing and attack, counted by the kernel on both partitions. `sudo campmixer pin --new` then wrote the settings file (12 writes to the boot partition; the system partition still at none after sixteen minutes) and left the partition read-only again. The new PIN was on the card after power-off.
- From outside the Pi, without the PIN: the sign-in page and nothing else. No status, no WebSocket, no file outside the web folder (the settings file, the key and the sign-in file were asked for by name), no way in by claiming to be the Pi itself, made-up and altered cookies refused, a cookie from another mixer with the same PIN refused, the right PIN refused when sent the way another site's page would send it, a foreign host name turned away. 34 checks.
- Guessing: five wrong PINs, then a wait during which even the right PIN is turned away and further tries are not counted. Restarting the bridge, and killing it outright, cleared neither the wait nor the count, and signed nobody out. Guessing from one fresh address after another ran into the shared limit: once the tries were used up, every further address was turned away, the right PIN included.
- On the Pi: the settings file, the key and the sign-in file cannot be read by the login account without `sudo`, by the bridge's account (except its sign-in file) or by `nobody`. The bridge runs with one capability (the right to use port 80), cannot gain more, and from inside its own view could open the serial port but not the settings file, the key, `/etc/shadow`, the card or the kernel log. systemd rates its exposure 2.1 ("OK"). SSH: root login off, X11 forwarding off, configuration valid.
- The real page from the real image in headless Chrome at phone size: sign-in, the faders moving the stand-in core through the emulated serial port and a second phone following, mute, a laptop, the core unplugged, practice mode, the core back, and a PIN change from the command line signing the phone out.
- With simulated Wi-Fi radios (the Pi kernel includes them): the mixer's own network came up with the made-up password, a simulated phone joined with WPA2, got an address, resolved `mixer.local`, `mixer.lan` and `mixer`, got the sign-in page and nothing else, and signed in with the PIN.

**Since build 4** (versions 0.3.1 to 0.4.4), in the same emulator:

- Updating in place: build 4, unlocked, was updated to 0.3.1 with the `update` command's own code (the download itself came from a file, since the emulator has no internet), restarted locked as 0.3.1 with its settings, PIN and login intact, and then took a 4-digit PIN with the stricter guess limit in force. That card was later unlocked and updated the same way to 0.4.1, and again came back locked with everything intact.
- Any number of inputs and outputs, on that 0.4.1 system: headless Chrome signed in to the real page with a stand-in mixer of 48 inputs and 8 outputs on the emulated serial port. All 56 strips were filled in under a second after signing in, the last output was dragged at the bottom of the page and the change reached the core, and over ten seconds 412 messages arrived with none damaged or dropped.
- A forced easy PIN, on that 0.4.1 system while locked: `pin 1948` was turned down, `pin --force 1948` set it and wrote `ALLOW_EASY_PIN=yes`, and the boot partition was read-only again afterwards. The 34 checks from outside passed with that PIN, this time against a 4-input stand-in speaking version 2 of the protocol. After a restart the PIN was still 1948, start-up had replaced nothing, and the page took 1948 and refused the old PIN.
- A freshly flashed image, for the first time since build 4. Build 8 (0.4.2, the first with a standard Wi-Fi password) was checked against its published checksum and started from scratch. Its first start kept the standard Wi-Fi name and password, made up its own PIN, set the login password typed into the settings file, locked the card and restarted, all in 325 s. Locked, it ran its own Wi-Fi on a simulated radio with the standard password. A simulated phone that knew only that password joined with WPA2, got an address, found `mixer.local`, got the sign-in page and nothing else, and signed in with the PIN. `wifi own --password` changed the password on the locked card and back, and left the boot partition read-only.
- Build 11 (0.4.4) differs from build 8 only in the standard password itself. It was not started: its checksum, its version and the settings file inside the image were checked.
- The images of builds 5, 6 and 7 themselves were not started. Their files are the ones tested above: the source download of build 7 was compared with the copy the emulated Pi installed. Builds 9 and 10 were stopped before they were published.
- The serial-link loads in `PROTOCOL.md` are worked out from the message sizes, not measured: the emulated port does not hold data back to 115200 baud.

On builds 2 and 3, and not run again since because that code has not changed:

- Unlock, an unlocked start, lock again, and a first start cut short at its last moment finishing itself.
- Joining a listed network, the mixer's own network back 30 s after the listed one vanished, rejoining when it returned; a phone on the mixer's own network keeping the radio from leaving it; a listed network with the wrong password; the radio-choice rules.
- The wired port on a cable that can lose its link: a 169.254 address 2 s after plugging in with no DHCP, a DHCP address about 15 s after a server appeared.
- The serial link with payloads of 1 to 1024 bytes both ways, a damaged frame counted and skipped, 200 frames in a burst all delivered in order.

**Not tested, or not there yet.**

- **The mixer core's firmware does not exist yet.** The faders were tested against the stand-in core, which speaks the same messages. Until the Teensy speaks them too, the page has nothing real to move, and says so.
- Real phones. The page ran in Chrome only. Safari on an iPhone and Chrome on an Android phone have not opened it.
- Starting on an actual Pi 4, with its own firmware, and the bootloader update a Pi may apply on its first start.
- The real radios. The Wi-Fi behaviour above ran on simulated ones, so the Pi's built-in chip and the Alfa AWUS036ACM (driver and firmware are in the image) have not carried it, and nothing here says anything about range.
- The serial link on the actual pins 8 and 10, through whatever sits between the Pi and the mixer core.
- The hardware watchdog. The emulator resets the moment its watchdog is touched, so it was switched off for these runs.
- Someone recording the network's traffic. The PIN does not protect against that (see *Who can reach it*), so there was nothing to test.

If something misbehaves, `campmixer status` and `campmixer logs` are the things to send back.

To re-run the checks: `bash ci/checks.sh` (it makes a Python environment with `websockets` in it and runs everything in `tests/` that needs no browser). The browser check: `npm install puppeteer-core`, then `node tests/ui_check.js`. To look at the page yourself on a computer, with the stand-in core behind it: `python3 tests/ui_server.py`.
