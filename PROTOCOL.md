# CampMixer link protocol

Two layers. Layer 1 is the framing between the Pi and the mixer core (the Teensy), and what a browser sees of it.
Layer 2 is what the bytes inside a frame mean: the messages the web app and the mixer core exchange for volume, mute and meters.

The Pi only carries layer 2. It does not read it, and the mixer's settings never live on the Pi.

# Layer 1: framing

## Serial link

- 115200 baud, 8 data bits, no parity, 1 stop bit, 3.3 V logic, through the optocouplers.
- Pi header pin 8 (GPIO14, TXD) sends to the Teensy. Pin 10 (GPIO15, RXD) receives from it. Pin 6 is ground on the Pi side of the isolation.

One frame:

    COBS( payload + CRC16(payload) ) + 0x00

- **Payload**: 1 to 1024 bytes.
- **CRC16**: CRC-16/CCITT-FALSE. Polynomial 0x1021, start value 0xFFFF, no bit reflection, no final XOR, high byte first. The CRC of the ASCII text `123456789` is 0x29B1.
- **COBS** (Consistent Overhead Byte Stuffing) removes every zero byte from the body, so a 0x00 always means "end of frame". A receiver that joins mid-stream drops bytes until the next 0x00 and is then in step.
- Extra 0x00 bytes between frames are allowed and ignored.
- A frame with a bad CRC is dropped. Nothing is re-sent at this layer.

On the Teensy this is the PacketSerial library (COBS) plus FastCRC's `CRC16::ccitt()`.
PacketSerial ends a block of exactly 254 non-zero bytes with one extra 0x01. The Pi accepts both forms.

The Pi counts the link as up when a valid frame has arrived in the last 2 seconds, so the Teensy should send something at least once a second. The meter frames the design already calls for (20 a second) cover it.

Reference code: `rootfs/opt/campmixer/campmixer_link.py`, checked by `tests/test_link.py`.

## What a browser sees

`ws://mixer.local/ws`, open only to a browser that has signed in with the PIN (it carries the sign-in cookie).

- **Binary message, browser to Pi**: one payload. The Pi frames it and sends it to the Teensy.
- **Binary message, Pi to browser**: one payload received from the Teensy, sent to every connected browser.
- **Text message, Pi to browser**: JSON. `{"type":"status", ...}` once a second (link state, counters, addresses), or `{"type":"error","error":"..."}` when a payload could not be sent.
- Text messages from the browser are reserved and ignored for now.
- Only pages served by the mixer itself may open the socket.

`GET /api/status` returns the same status report as JSON, to a signed-in browser or to the Pi itself.

Signing in: `POST /api/login` with `{"pin": "..."}` as `application/json`. The answer sets the cookie. `GET /api/session` says whether this browser is signed in and how long it must wait before trying a PIN. `POST /api/logout` takes the cookie away.

# Layer 2: mixer messages, version 1

A payload is one message: a type byte, then that message's fields. Numbers longer than one byte are sent high byte first.

The mixer core is the one place the mixer's settings live. The app asks for a change and shows what the core reports back. That is how every phone, and the front panel, stay in step: a change made anywhere comes back to everybody as the core's new state.

**Channels** are numbered from 0. The inputs come first (0 to 3 on a four-input mixer), then the master, which is always the last one (4).

## To the mixer core

| Type | Name | Fields | Meaning |
|---|---|---|---|
| `0x01` | GET_STATE | none | Asks for a STATE. The app sends it when it connects, and again each second for as long as it has had no answer. |
| `0x02` | SET | channel (1 byte), setting (1 byte), value (2 bytes, signed) | Changes one setting on one channel. The core applies it, within what it allows, and answers with STATE. |

Settings:

| Setting | Name | Value |
|---|---|---|
| 1 | Level | Tenths of a decibel: -600 (-60.0 dB) to +100 (+10.0 dB). -32768 means off, no sound at all. The core treats anything below -600 as off and anything above +100 as +100. The app works in half-decibel steps. |
| 2 | Mute | 0 is not muted, anything else is muted. |
| 3 to 7 | (kept for the channel menu) | Gain, 48 V, ground lift, high-pass and feedback filter. Not in version 1: the core ignores them. |

A SET for a channel or a setting the core does not have, or one that is cut short, is ignored and gets no answer.

## From the mixer core

| Type | Name | Fields | Meaning |
|---|---|---|---|
| `0x81` | STATE | version (1 byte, 1), number of inputs (1 byte), then for each channel, inputs first and master last: level (2 bytes, signed), flags (1 byte), length of the name (1 byte, at most 16), the name (UTF-8) | Where everything stands. Flags: bit 0 is mute; the other bits are 0 for now. |
| `0x82` | METERS | one byte for each channel, inputs first and master last | The highest level since the last METERS, in half-decibel steps below full scale: 0 is full scale (clipping), 1 is -0.5 dB, 254 is -127 dB, 255 is silence. |

The core sends:

- **STATE** in answer to every GET_STATE and every SET it acts on, whenever a setting changes at the front panel, and at least once a second regardless, so a phone that missed one catches up.
- **METERS** 20 times a second. An input's meter shows what is coming in on it (after its gain, before its fader and mute), so a microphone can be seen to be alive before it is brought up. The master's meter shows what is going out.

The app treats the mixer as out of reach, and greys its controls, when the Pi reports the link down or no STATE has arrived for 3.5 seconds.

A receiver ignores any message whose type it does not know, and any message too short for its type. A core that speaks a later version says so in STATE's version byte, and an app that does not know that version shows no controls rather than guess.

## Examples

These are checked by the tests, against both the web app's code and the stand-in core (`tests/mixer_vectors.json`).

| Message | Bytes (hex) |
|---|---|
| Ask for the state | `01` |
| Input 1 to -6.0 dB | `02 00 01 FF C4` |
| Input 3 to +3.5 dB | `02 02 01 00 23` |
| Input 2 off | `02 01 01 80 00` |
| Master muted | `02 04 02 00 01` |
| METERS: input 1 clipping, input 2 at -6 dB, input 3 at -60 dB, input 4 silent, master at -12.5 dB | `82 00 0C 78 FF 19` |

A STATE from a four-input mixer with MIC 1 at -6.0 dB, MIC 2 at -10.5 dB, KEYS off, LAPTOP at +3.5 dB and muted, MASTER at 0.0 dB:

    81 01 04
    FF C4 00 05 4D 49 43 20 31          MIC 1    -6.0 dB
    FF 97 00 05 4D 49 43 20 32          MIC 2   -10.5 dB
    80 00 00 04 4B 45 59 53             KEYS     off
    00 23 01 06 4C 41 50 54 4F 50       LAPTOP   +3.5 dB, muted
    00 00 00 06 4D 41 53 54 45 52       MASTER    0.0 dB

## Trying it without a mixer

- In the web app, "Try the controls without a mixer" runs a practice core inside the page. It is connected to nothing.
- `tests/standin_core.py` is the same thing on the far side of a real serial line: `python3 tests/standin_core.py /dev/ttyUSB0`, with a USB serial adaptor wired to the Pi's header.
