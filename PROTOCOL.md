# CampMixer link protocol

Two layers. Layer 1 is the framing between the Pi and the mixer core (the Teensy), and what a browser sees of it.
Layer 2 is what the bytes inside a frame mean: the messages the web app and the mixer core exchange for volume, mute and meters, for however many inputs and outputs the mixer has.

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

# Layer 2: mixer messages, version 2

A payload is one message: a type byte, then that message's fields. Numbers longer than one byte are sent high byte first.

The mixer core is the one place the mixer's settings live. The app asks for a change and shows what the core reports back. That is how every phone, and the front panel, stay in step: a change made anywhere comes back to everybody from the core.

**The mixer core says how many inputs and outputs it has.** Nothing on the Pi or in the app assumes a number, so the same CampMixer OS drives a mixer with one input and one output or one with dozens of each. Every input and every output is a **channel** with a level, a mute, a name and a meter. Channels are numbered from 0: the inputs first, then the outputs. On the Camp PA Mixer that is inputs 0 to 3 and one output, channel 4.

Inputs and outputs together can number up to 250 (a channel's number is one byte). What limits a real mixer first is the serial link: see *How many channels* below.

## To the mixer core

| Type | Name | Fields | Meaning |
|---|---|---|---|
| `0x01` | GET_STATE | none | Asks where everything stands. The core answers with INFO and then a CHANNEL for every channel. The app sends it when it connects, and again each second for as long as it has not heard about every channel. |
| `0x02` | SET | channel (1 byte), setting (1 byte), value (2 bytes, signed) | Changes one setting on one channel. The core applies it, within what it allows, and answers with a CHANNEL for that channel. |

Settings:

| Setting | Name | Value |
|---|---|---|
| 1 | Level | Tenths of a decibel: -600 (-60.0 dB) to +100 (+10.0 dB). -32768 means off, no sound at all. The core treats anything below -600 as off and anything above +100 as +100. The app works in half-decibel steps. |
| 2 | Mute | 0 is not muted, anything else is muted. |
| 3 to 7 | (kept for the channel menu) | Gain, 48 V, ground lift, high-pass and feedback filter. Not in this version: the core ignores them. |

A SET for a channel or a setting the core does not have, or one that is cut short, is ignored and gets no answer.

## From the mixer core

| Type | Name | Fields | Meaning |
|---|---|---|---|
| `0x83` | INFO | version (1 byte, 2), number of inputs (1 byte, at least 1), number of outputs (1 byte, at least 1) | The core's heartbeat: which version it speaks and how many channels it has. |
| `0x84` | CHANNEL | channel (1 byte), level (2 bytes, signed), flags (1 byte), length of the name (1 byte, at most 16), the name (UTF-8) | Where one channel stands. Flags: bit 0 is mute; bits 4 to 7 say which unit the channel is on (see *Linked units*); the other bits are 0 for now. |
| `0x82` | METERS | one byte for each channel, inputs first, then outputs | The highest level since the last METERS, in half-decibel steps below full scale: 0 is full scale (clipping), 1 is -0.5 dB, 254 is -127 dB, 255 is silence. |

The core sends:

- **INFO** at least once a second, and first in its answer to GET_STATE.
- **CHANNEL** for every channel in answer to GET_STATE; for the one channel concerned in answer to a SET it acts on, and whenever a setting changes at the front panel; and for every channel in turn, so that each is said again at least once every five seconds, asked or not. That way a phone that missed one catches up, and a change costs one small message however big the mixer is.
- **METERS** 20 times a second. An input's meter shows what is coming in on it (after its gain, before its fader and mute), so a microphone can be seen to be alive before it is brought up. An output's meter shows what is going out of it.

What reaches which output is the core's business in this version: the app sets each output's level and mute.

The app builds a strip for every channel as soon as INFO has told it how many there are, and greys each one until its CHANNEL has arrived. It treats the mixer as out of reach, and greys everything, when the Pi reports the link down or no INFO has arrived for 3.5 seconds.

A receiver ignores any message whose type it does not know, and any message too short for its type. A core that speaks another version says so in INFO's version byte, and an app that does not know that version shows no controls rather than guess.

## Linked units

A main unit can have expansion units chained below it, each the same mixer without a Pi (the design document has the hardware). The Pi never talks to them. The main unit's core answers for the whole chain as one mixer, and it speaks this same protocol to the unit below it, taking the Pi's place: it sends GET_STATE and SET down, and gets INFO, CHANNEL and METERS back. Each expansion unit does the same towards the unit below it.

- **Numbering.** A core counts its own inputs first, then the inputs of the units below it in chain order, then its own outputs, then theirs. A main unit with one expansion unit is 8 inputs and 2 outputs: inputs 0 to 3 are the main unit's and 4 to 7 the expansion unit's, output 8 is the main unit's and 9 the expansion unit's.
- **Which unit.** Bits 4 to 7 of a CHANNEL's flags hold the unit the channel is on, counted from the core that is answering: 0 for its own channels. A core that passes a CHANNEL up from below adds 1 to that number, and stops at 15. Names are passed up as they are. The web app does not use the unit number yet, so two units' inputs show under one heading.
- **Coming and going.** When a unit below appears, or has sent no INFO for 3 seconds, the core above sends INFO at once with the new counts, and then a CHANNEL for every channel. The web app redraws its strips whenever the counts change.
- **A change in flight.** Channel numbers shift when the counts change: with one expansion unit the main output moves from 4 to 8. So after its counts change, a core ignores SET until it has had a GET_STATE, which the web app sends as soon as it sees the new INFO.
- **Meters.** A core adds the peaks from below to its own METERS message, in the numbering above, using the last ones it received.
- **Load.** Each lead carries every channel below it, so the table in *How many channels* applies to each lead by itself.

## How many channels

The Pi only passes messages on, so it is not what sets the limit. The serial link is. At 115200 baud it carries about 11,500 bytes a second, and the meters are the steady load: one byte a channel, 20 times a second.

| Channels (inputs and outputs together) | Steady load at 115200 baud |
|---|---|
| 5 (the Camp PA Mixer) | about 2 percent |
| 56 (48 inputs, 8 outputs) | about 15 percent |
| 100 | about 25 percent |
| 250 | about 55 percent |

That leaves room at any size the numbering allows, but a core with more than a hundred or so channels should run the link faster. The bridge takes `--baud` up to 921600 (in `campmixer-bridge.service`), and the core has to match.

In the app, the meters of strips that are off the screen are not redrawn, which is what keeps a long mixer smooth on a phone.

## Examples

These are checked by the tests, against both the web app's code and the stand-in core (`tests/mixer_vectors.json`).

| Message | Bytes (hex) |
|---|---|
| Ask where everything stands | `01` |
| Channel 0 to -6.0 dB | `02 00 01 FF C4` |
| Channel 2 to +3.5 dB | `02 02 01 00 23` |
| Channel 1 off | `02 01 01 80 00` |
| Channel 4 muted | `02 04 02 00 01` |
| INFO: version 2, 4 inputs, 1 output | `83 02 04 01` |
| METERS for those five: channel 0 clipping, channel 1 at -6 dB, channel 2 at -60 dB, channel 3 silent, the output at -12.5 dB | `82 00 0C 78 FF 19` |

The CHANNEL messages of a four-input, one-output mixer:

    84 00 FF C4 00 05 4D 49 43 20 31          channel 0, MIC 1, -6.0 dB
    84 01 FF 97 00 05 4D 49 43 20 32          channel 1, MIC 2, -10.5 dB
    84 02 80 00 00 04 4B 45 59 53             channel 2, KEYS, off
    84 03 00 23 01 06 4C 41 50 54 4F 50       channel 3, LAPTOP, +3.5 dB, muted
    84 04 00 00 00 06 4D 41 53 54 45 52       channel 4, MASTER (the output), 0.0 dB

## Trying it without a mixer

- In the web app, "Try the controls without a mixer" runs a practice core inside the page. It is connected to nothing.
- `tests/standin_core.py` is the same thing on the far side of a real serial line: `python3 tests/standin_core.py /dev/ttyUSB0`, with a USB serial adaptor wired to the Pi's header. Add two numbers to play a mixer of another size, such as `16 4` for 16 inputs and 4 outputs.
