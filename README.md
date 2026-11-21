# CampMixer OS 0.3.0

Its own operating system for the Camp PA Mixer's Raspberry Pi, built on Raspberry Pi OS Lite (64-bit).
You write one image to a microSD card and the Pi starts up as the mixer's remote. There is no install step.

This folder is the source for that image.

## What the OS does

- **Web app at http://mixer.local, behind a PIN.** A fader, a mute button and a level meter for each input and for the master. See *The web app* and *Who can reach it* below.
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

No password is built into the image or into this source, so both are safe to keep anywhere.

## First start

1. In **Raspberry Pi Imager** choose *Use custom*, pick the `.img.xz` file and write it to the card. Skip the customisation step.
2. Before the first start: take the card out and put it back in, open `campmixer.txt` on the `bootfs` drive and type in `WIFI_PASSWORD` and `LOGIN_PASSWORD` (8 characters or more each), `WEB_PIN` (6 to 12 digits), and any networks to join. Doing this now saves pulling the card later to read made-up ones.
3. Put the card in the Pi and power it. The first start takes about two minutes and restarts itself once (twice if the Pi's own bootloader gets an update): it grows the file system to fill the card, makes its SSH keys, sets the passwords and locks the card.
4. Open **http://mixer.local** and type the PIN, or log in with `ssh mixer@mixer.local`.

Any password or PIN you left blank is made up on that first start and written into `campmixer.txt`. Put the card back in your computer to read it. A login password you chose yourself is removed from the file once it has been set.

`sudo` asks for that same login password.

## The settings file

`campmixer.txt` on the card's `bootfs` partition. Edit it with the card in a computer, or use the commands below. Changes made in the file take effect at the next start.

    WIFI_NAME=CampMixer          the mixer's own network
    WIFI_PASSWORD=...            8 to 63 characters
    WIFI_BAND=2.4                2.4 or 5
    WIFI_CHANNEL=6
    JOIN_WIFI_1=Church Hall      up to four networks to join when in range
    JOIN_WIFI_1_PASSWORD=...
    WEB_PIN=...                  6 to 12 digits; each phone is asked for it once
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

There is no built-in Wi-Fi password and no built-in PIN. If `WIFI_PASSWORD` is blank or cannot be used (it needs 8 to 63 characters), the mixer makes one up at start-up and writes it into the file. The same goes for `WEB_PIN` when it is blank, is not 6 to 12 digits, or is one anybody would guess (123456, 111111, 121212 and the like): an 8-digit one is made up. The commands refuse a value the mixer could not use, and change nothing.

## The web app

Open http://mixer.local on a phone or a laptop on the same network and type the PIN. Each phone is asked once and stays signed in, through restarts, until the PIN is changed.

Each input, and the master, has:

- **A fader.** Drag it sideways. It moves by as far as your finger moves, from where it was: touching the track does not make it jump, and scrolling the page past it moves nothing. The mark three quarters of the way along is 0 dB. All the way left is off.
- **− and +** for half a decibel at a time.
- **Mute.** The button turns red and reads *Muted*.
- **A meter.** An input's meter shows what is coming in on it, fader down and muted or not, so a microphone can be seen to be alive before it is brought up. The master's shows what is going out.

The settings live in the mixer core, not in the phone or the Pi. A change made on one phone shows on every other phone as the core confirms it, and so will a turn of a knob on the front panel.

When the mixer core cannot be reached the controls go grey and the page says why. From there **Try the controls without a mixer** runs a practice mixer inside the page. It is marked as practice, is connected to nothing, and ends by itself the moment a real mixer core appears.

Not in yet: gain, 48 V, ground lift, high-pass, feedback filter, names and scenes (the front panel's menus).

## Who can reach it

**The web app asks for a PIN**, on every network. Without it a browser gets the sign-in page and nothing else: no controls, no status, nothing about the mixer.

- The PIN is `WEB_PIN` in `campmixer.txt`: 6 to 12 digits. `sudo campmixer pin` shows it, `sudo campmixer pin NEW` changes it and `sudo campmixer pin --new` makes one up. Changing it signs every phone out.
- **Guessing is slow.** One address gets five wrong tries, then waits before each further one: 30 s, a minute, two, then five minutes every time. Everybody together gets 20 wrong tries, of which one comes back every three minutes, so changing address does not help. That caps guessing at about 500 PINs a day: centuries against the 100 million eight-digit PINs, a few years against the million six-digit ones. On a network the mixer lives on for good, use eight digits or more.
- The count of wrong PINs is on the page (under *The Pi and its link*) and in `campmixer status`. It outlives a restart of the bridge.
- The price of that limit: someone who keeps feeding the page wrong PINs can keep *new* phones from signing in. Phones already signed in are not affected.
- A web site out on the internet cannot reach in through someone's browser: the mixer answers only to its own name (`mixer.local`, `mixer`, `mixer.lan` and the like) or its address, takes a PIN only from its own page, and lets only its own page use the controls.

**What the PIN does not do.** The page is plain `http`, so the PIN and the sign-in cookie cross the network unencrypted. The PIN keeps out everyone who can merely *reach* the page. It does not keep out someone who records the network's traffic at the moment a phone signs in or is in use. On the mixer's own Wi-Fi that takes the Wi-Fi password, so give that only to people you would hand the mixer to. On a venue's network it takes access to their network gear or a deliberate attack on the network. Closing that gap needs `https`, which on a local name means a certificate warning on every phone, so it is left out for now.

**The rest of the Pi.**

- SSH needs the login password, on every network. Root cannot log in. An obvious login password in `campmixer.txt` (`password1`, `Mixer2026!`) is not used; one is made up instead.
- `campmixer.txt` holds the Wi-Fi password, the PIN and perhaps the login password, so the partition it is on can be read by root only. On the Pi, reading it takes `sudo`. With the card in a computer, anyone holding the card can read it: whoever has the card has the mixer.
- The part that talks to the network (the bridge) runs as its own user with no rights to anything but the serial port and the network. It never sees the PIN, only a slow hash of it, and cannot read `campmixer.txt`.
- No password, PIN or key is built into the image or this source. Each card makes up its own on its first start.

## Commands

    campmixer status                          what it is doing now, and which radio it is using
    sudo campmixer pin                        the PIN the web app asks for
    sudo campmixer pin 48271935               change it (every phone signs in again)
    sudo campmixer pin --new                  change it to a made-up one
    sudo campmixer wifi                       settings in use, with passwords and the PIN
    sudo campmixer wifi join "Church Hall" "its password"
    sudo campmixer wifi forget "Church Hall"
    sudo campmixer wifi own --name "Hall Mixer" --password "something long"
    sudo campmixer lock                       card read-only
    sudo campmixer unlock                     card writable, for changes and updates
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

**To update the OS:** write the newer image to the card. Copy `campmixer.txt` off the card first and put it back afterwards to keep your settings.

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

- The bridge, end to end, against a pretend serial port and a standard WebSocket client: both directions, damaged frames, oversize messages, cross-site refusal, which host names it answers to, the serial port vanishing.
- The framing, against the published COBS examples and the CRC check value.
- Every generated network profile, read back by NetworkManager 1.46's own parser, including awkward names and passwords.
- The radio's decisions against a scripted stand-in for NetworkManager: start-up, slow joins, looking again when idle, refused networks, drop-outs.
- The settings file in UTF-8, old Windows encodings and UTF-16, and the password rules: nothing built in, unusable values refused, a made-up one saved when the file's is blank or unusable.
- The first-start password set-up, against a stand-in for the system's password tool.
- The installer's edits, against the stock Raspberry Pi OS `config.txt` and `cmdline.txt`, for both an image build and a running Pi.
- The two start-up pieces that follow the lock.

**The image itself, started in an emulator.** `CampMixerOS-0.2.2-build3` was downloaded from Releases and started in QEMU as a Raspberry Pi 3B (QEMU has no Pi 4 model; the same image starts on both). The Pi kernel includes simulated Wi-Fi radios, so Wi-Fi ran on the real NetworkManager, wpa_supplicant and dnsmasq, and the image's own `disable-bt` overlay was applied to the emulated board so the serial port came up as it does on the Pi.

- First start with a settings file saved by old Windows Notepad (not UTF-8, a curly apostrophe in it) and both passwords blank: both made up and written into the file, the rest of the file left byte for byte, card locked, restart.
- Locked start: no failed units of ours and **no writes to the card at all** from power-on, counted by the kernel on both partitions.
- The mixer's own Wi-Fi: a simulated phone joined with WPA2 and the made-up password, got an address, loaded the page, and resolved `mixer.local`, `mixer.lan` and `mixer`.
- A listed network: joined on command, the mixer's own network back 30 s after the listed one vanished, and rejoined a minute after it returned with nobody on the mixer's own.
- The serial link through the emulated UART, with the bridge running as its own unprivileged user: payloads of 1 to 1024 bytes both ways, a jumper-style loopback, a damaged frame counted and skipped, 200 frames in a burst all delivered in order.
- The page refused under a foreign host name and served under its own name and address.

On build 2, with the same code for these parts or with the new code loaded over it:

- Unlock, an unlocked start (stock file system check runs, card writable), lock again, and a first start cut short at its last moment finishing itself.
- A phone connected to the mixer's own network keeps the radio from leaving it; a listed network with the wrong password sends it back to its own within 20 s and is not retried.
- The radio-choice rules really make NetworkManager leave the unwanted radio alone.
- The wired port on a cable that can lose its link: a 169.254 address 2 s after plugging in with no DHCP, a DHCP address about 15 s after a server appeared, the 169.254 address back when the lease ran out.
- Open ports: 22 and 80, plus DNS and DHCP on the mixer's own Wi-Fi and mDNS. Root cannot log in.

**Not tested: everything that needs the real hardware.**

- Starting on an actual Pi 4, with its own firmware, and the bootloader update a Pi may apply on its first start.
- The real radios. The Wi-Fi behaviour above ran on simulated ones, so the Pi's built-in chip and the Alfa AWUS036ACM (driver and firmware are in the image) have not carried it, and nothing here says anything about range.
- The serial link on the actual pins 8 and 10, through whatever sits between the Pi and the mixer core.
- The hardware watchdog. The emulator resets the moment its watchdog is touched, so it was switched off for these runs.

If something misbehaves, `campmixer status` and `campmixer logs` are the things to send back.

To re-run the checks: `bash ci/checks.sh` (it makes a Python environment with `websockets` in it and runs everything in `tests/`).
