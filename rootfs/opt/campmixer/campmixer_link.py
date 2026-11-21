"""CampMixer serial link layer.

One frame on the wire is:

    COBS( payload + CRC16(payload) ) + 0x00

* The payload is 1 to MAX_PAYLOAD bytes. Its meaning belongs to the mixer app, not to this layer.
* CRC16 is CRC-16/CCITT-FALSE: polynomial 0x1021, start value 0xFFFF, no reflection, no final
  XOR, sent high byte first. The check value for b"123456789" is 0x29B1.
* COBS removes every zero byte from the body, so 0x00 only ever marks the end of a frame.

On the Teensy the same framing is the PacketSerial library (COBS) plus FastCRC's CRC16::ccitt().
"""

MAX_PAYLOAD = 1024


def _crc_table():
    table = []
    for byte in range(256):
        crc = byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
        table.append(crc & 0xFFFF)
    return tuple(table)


_CRC_TABLE = _crc_table()


def crc16(data, crc=0xFFFF):
    """CRC-16/CCITT-FALSE of data."""
    for byte in data:
        crc = ((crc << 8) & 0xFFFF) ^ _CRC_TABLE[(crc >> 8) ^ byte]
    return crc


def cobs_encode(data):
    """Consistent Overhead Byte Stuffing. The result contains no zero bytes."""
    out = bytearray()
    runs = bytes(data).split(b"\x00")
    last = len(runs) - 1
    for index, run in enumerate(runs):
        full_blocks = 0
        while len(run) >= 254:
            out.append(0xFF)
            out += run[:254]
            run = run[254:]
            full_blocks += 1
        # A run that ends the packet exactly on a full block needs no closing code byte.
        if run or index != last or full_blocks == 0:
            out.append(len(run) + 1)
            out += run
    return bytes(out)


def cobs_decode(encoded):
    """Reverse of cobs_encode. Raises ValueError on data that is not valid COBS."""
    encoded = bytes(encoded)
    if not encoded:
        raise ValueError("empty COBS block")
    if 0 in encoded:
        raise ValueError("zero byte inside COBS block")
    out = bytearray()
    index = 0
    size = len(encoded)
    while index < size:
        code = encoded[index]
        index += 1
        end = index + code - 1
        if end > size:
            raise ValueError("COBS block runs past the end")
        out += encoded[index:end]
        index = end
        if code != 0xFF and index < size:
            out.append(0)
    return bytes(out)


def encode_frame(payload):
    """Payload bytes to one frame, ready to write to the serial port."""
    payload = bytes(payload)
    if not 1 <= len(payload) <= MAX_PAYLOAD:
        raise ValueError("payload must be 1 to %d bytes" % MAX_PAYLOAD)
    return cobs_encode(payload + crc16(payload).to_bytes(2, "big")) + b"\x00"


class FrameDecoder:
    """Feed it whatever bytes arrive; it hands back the payloads of complete, valid frames."""

    def __init__(self, max_payload=MAX_PAYLOAD):
        body = max_payload + 2  # payload and its CRC
        self._body_limit = body
        self._limit = body + body // 254 + 2  # the same once encoded, which is what sits in the buffer
        self._buffer = bytearray()
        self._discarding = False
        self.frames = 0
        self.crc_errors = 0
        self.format_errors = 0

    def reset(self):
        """Forget any half-received frame (the counters are kept)."""
        self._buffer.clear()
        self._discarding = False

    def feed(self, data):
        payloads = []
        self._buffer += data
        while True:
            end = self._buffer.find(0)
            if end < 0:
                break
            block = bytes(self._buffer[:end])
            del self._buffer[: end + 1]
            if self._discarding:
                self._discarding = False
                continue
            if not block:
                continue  # back-to-back delimiters carry nothing
            try:
                body = cobs_decode(block)
            except ValueError:
                self.format_errors += 1
                continue
            if len(body) < 3 or len(body) > self._body_limit:
                self.format_errors += 1
                continue
            payload, received = body[:-2], int.from_bytes(body[-2:], "big")
            if crc16(payload) != received:
                self.crc_errors += 1
                continue
            self.frames += 1
            payloads.append(payload)
        if len(self._buffer) > self._limit:
            # Too long to be a frame: drop it and everything up to the next delimiter.
            self._buffer.clear()
            self._discarding = True
            self.format_errors += 1
        return payloads
