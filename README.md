# CampMixer OS 0.2.1

Its own operating system for the Camp PA Mixer's Raspberry Pi, built on Raspberry Pi OS Lite (64-bit).
You write one image to a microSD card and the Pi starts up as the mixer's remote. There is no install step.

This folder is the source for that image.

## What the OS does

- **Web app at http://mixer.local.** For now a status page with a serial loopback test. The real mixer app drops into the same place later.
- **Wi-Fi.** It joins a network you have listed when that network is in range, so the mixer sits on the venue's LAN. Otherwise it makes its own network, `CampMixer`.
- **Ethernet.** Plug it into any network and it takes an address. A cable straight to a laptop works too.
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
2. Before the first start: take the card out and put it back in, open `campmixer.txt` on the `bootfs` drive and type in `WIFI_PASSWORD` and `LOGIN_PASSWORD` (8 characters or more each), and any networks to join. Doing this now saves pulling the card later to read made-up ones.
3. Put the card in the Pi and power it. The first start takes about two minutes and restarts itself once (twice if the Pi's own bootloader gets an update): it grows the file system to fill the card, makes its SSH keys, sets the passwords and locks the card.
4. Open **http://mixer.local**, or log in with `ssh mixer@mixer.local`.

Any password you left blank is made up on that first start and written into `campmixer.txt`. Put the card back in your computer to read it. A login password you chose yourself is removed from the file once it has been set.

`sudo` asks for that same login password.

## The settings file

`campmixer.txt` on the card's `bootfs` partition. Edit it with the card in a computer, or use the commands below. Changes made in the file take effect at the next start.

    WIFI_NAME=CampMixer          the mixer's own network
    WIFI_PASSWORD=...            8 to 63 characters
    WIFI_BAND=2.4                2.4 or 5
    WIFI_CHANNEL=6
    JOIN_WIFI_1=Church Hall      up to four networks to join when in range
    JOIN_WIFI_1_PASSWORD=...
    WIFI_RADIO=auto              auto, usb or onboard
    LOGIN_PASSWORD=              read once, on the first start

How the radio decides, at every start:

1. If a listed network is in range, join it. Phones on that network open http://mixer.local.
2. If none is in range, or the password is wrong, run the mixer's own network.
3. If a joined network drops out for 30 seconds, switch to the mixer's own network and stay there until the next start.

One radio does one job at a time, so while it is on a venue's Wi-Fi the `CampMixer` network is not there. Ethernet is separate and always works alongside either.

Networks that need a username, or a sign-in web page, cannot be joined.

There is no built-in Wi-Fi password. If `WIFI_PASSWORD` is blank or cannot be used (it needs 8 to 63 characters), the mixer makes one up at start-up and writes it into the file. The commands refuse a value the mixer could not use, and change nothing.

## Commands

    campmixer status                          what it is doing now, and which radio it is using
    sudo campmixer wifi                       settings in use, with passwords
    sudo campmixer wifi join "Church Hall" "its password"
    sudo campmixer wifi forget "Church Hall"
    sudo campmixer wifi own --name "Hall Mixer" --password "something long"
    sudo campmixer lock                       card read-only
    sudo campmixer unlock                     card writable, for changes and updates
    sudo campmixer password                   change your login password (unlock first)
    campmixer logs

The Wi-Fi commands take effect straight away and also work while the card is locked.

## Keeping the card alive

A microSD card wears out from writes, so the aim is no writes at all.

- **Locked** (the normal state): the whole system runs on a copy-on-write layer in memory, and the boot partition is mounted read-only. Nothing reaches the card. Anything changed while locked is gone at the next start, which is the point.
- Being locked is one word, `overlayroot=tmpfs`, in `cmdline.txt`. Everything else follows from it at start-up: the boot partition goes read-only, and the stock file system check is skipped, because that check writes two blocks to the card every time it runs.
- Logs stay in memory, capped at 24 MB.
- Swap is compressed memory only. Raspberry Pi OS's default also keeps a swap file on the card; that is switched off.
- Scheduled jobs that write (package checks, log rotation, manual-page index, package database backup, file system scrub) are off.
- At start-up the network profiles are rewritten only if `campmixer.txt` changed.
- The only writes left are the ones you ask for: `campmixer wifi ...` rewrites `campmixer.txt`, and updates happen while unlocked.

Reading does not wear a card. The Pi reads the system from the card once at start-up and after that runs from memory, so in use the card sits idle.

**To change something by hand:** `sudo campmixer unlock`, `sudo reboot`, make the change, `sudo campmixer lock`, `sudo reboot`.

**To update the OS:** write the newer image to the card. Copy `campmixer.txt` off the card first and put it back afterwards to keep your settings.

**If it will not start after locking:** put the card in a computer, open `cmdline.txt` on `bootfs`, delete the word `overlayroot=tmpfs` and save. That unlocks it.

## Try it before the mixer exists

- Open http://mixer.local. The link shows "No signal from the mixer core", which is right: there is no Teensy yet.
- **Loopback test:** join header pins 8 and 10 with a jumper wire and press *Send a test frame*. "Came back in N ms" proves the serial port, the bridge and the WebSocket.
- Without the USB adaptor, the Pi's built-in radio makes the `CampMixer` network on the bench. Once the Alfa is plugged in at start-up, that takes over and the built-in radio goes quiet.

## Wiring to the Teensy

| Pi header pin | Signal | Goes to |
| --- | --- | --- |
| 8 (GPIO14) | TXD, 3.3 V | optocoupler, then Teensy RX |
| 10 (GPIO15) | RXD, 3.3 V | optocoupler, from Teensy TX |
| 6 | Ground | Pi side of the optocouplers |
| 1 | 3.3 V | Pi side of the optocouplers |

The framing on that link is in `PROTOCOL.md`.

## What is in this folder

| Path | What it is |
| --- | --- |
| `rootfs/` | Every file CampMixer OS adds to Raspberry Pi OS, laid out as on the card |
| `install.sh` | Puts those files in place and edits the boot settings. The image build runs it |
| `campmixer.txt` | The settings file as it ships |
| `image/` | The pi-gen stage and the build script |
| `.github/workflows/`, `ci/` | The GitHub build |
| `tests/` | The checks |
| `PROTOCOL.md` | The serial framing and what a browser sees |

`install.sh` also runs on a Pi that already has stock Raspberry Pi OS Lite, if you ever want the layer without writing an image: copy this folder to `bootfs`, then `sudo bash /boot/firmware/campmixer-os/install.sh`.

## What was tested, and what was not

Tested in a Linux workspace, not on a Pi:

- The bridge, end to end, against a pretend serial port and a standard WebSocket client: both directions, damaged frames, oversize messages, cross-site refusal, the serial port vanishing.
- The framing, against the published COBS examples and the CRC check value.
- Every generated network profile, read back by NetworkManager 1.46's own parser, including awkward names and passwords.
- The radio's join-or-own decisions, against a scripted stand-in for NetworkManager.
- The first-start password set-up, against a stand-in for the system's password tool.
- The installer's edits, against the stock Raspberry Pi OS `config.txt` and `cmdline.txt`, for both an image build and a running Pi.

Not tested, because it needs a build machine or the real hardware:

- **The image build itself.** The stage follows pi-gen's rules as of November 2026 but has never been run.
- Starting up on a Pi 4, the first-start sequence, the access point, joining a venue network, the lock and unlock cycle, the watchdog.
- The Alfa AWUS036ACM. Its driver and firmware are part of stock Raspberry Pi OS, but it has not been plugged in.

Expect the first build or the first start to need a fix or two. The build log, `campmixer logs` and `campmixer status` are the things to send back.

To re-run the checks: `bash ci/checks.sh` (it makes a Python environment with `websockets` in it and runs everything in `tests/`).
