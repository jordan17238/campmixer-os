# CampMixer link protocol, layer 1

This is the framing between the Pi and the mixer core (the Teensy), and what a browser sees of it.
What the bytes inside a frame mean is the mixer app's business and is not defined here yet.

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

`ws://mixer.local/ws`

- **Binary message, browser to Pi**: one payload. The Pi frames it and sends it to the Teensy.
- **Binary message, Pi to browser**: one payload received from the Teensy, sent to every connected browser.
- **Text message, Pi to browser**: JSON. `{"type":"status", ...}` once a second (link state, counters, addresses), or `{"type":"error","error":"..."}` when a payload could not be sent.
- Text messages from the browser are reserved and ignored for now.
- Only pages served by the mixer itself may open the socket.

`GET /api/status` returns the same status report as JSON.

A change made on one phone reaches the others because the Teensy answers every change with its new state, and the Pi passes that answer to everybody.
