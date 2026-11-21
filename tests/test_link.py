#!/usr/bin/env python3
"""Checks the link layer against the published COBS examples and the CRC check value."""
import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "rootfs", "opt", "campmixer"))
from campmixer_link import MAX_PAYLOAD, FrameDecoder, cobs_decode, cobs_encode, crc16, encode_frame  # noqa: E402


def h(text):
    return bytes.fromhex(text)


# Examples from the COBS article on Wikipedia (Cheshire and Baker's encoding).
VECTORS = [
    ("00", "0101"),
    ("0000", "010101"),
    ("001100", "01021101"),
    ("11220033", "0311220233"),
    ("11223344", "0511223344"),
    ("11000000", "0211010101"),
]
for raw, encoded in VECTORS:
    assert cobs_encode(h(raw)) == h(encoded), raw
    assert cobs_decode(h(encoded)) == h(raw), raw

run_254 = bytes(range(1, 255))
assert cobs_encode(run_254) == b"\xff" + run_254
assert cobs_encode(bytes(range(0, 255))) == b"\x01\xff" + run_254
assert cobs_encode(bytes(range(1, 256))) == b"\xff" + run_254 + b"\x02\xff"
assert cobs_encode(bytes(range(2, 256)) + b"\x00") == b"\xff" + bytes(range(2, 256)) + b"\x01\x01"
assert cobs_encode(bytes(range(3, 256)) + b"\x00\x01") == b"\xfe" + bytes(range(3, 256)) + b"\x02\x01"
assert cobs_encode(b"") == b"\x01"
# PacketSerial on the Teensy ends a full block with an extra 0x01. Both forms must decode the same.
assert cobs_decode(b"\xff" + run_254 + b"\x01") == run_254

for bad in (b"", b"\x05\x01", b"\x02\x00"):
    try:
        cobs_decode(bad)
    except ValueError:
        pass
    else:
        raise AssertionError("accepted bad COBS %r" % bad)

assert crc16(b"123456789") == 0x29B1
assert crc16(b"") == 0xFFFF

rng = random.Random(1234)
for size in list(range(0, 40)) + [253, 254, 255, 256, 507, 508, 509, 762, 1000, 1026]:
    for zero_share in (0.0, 0.02, 0.5, 1.0):
        data = bytes(0 if rng.random() < zero_share else rng.randrange(1, 256) for _ in range(size))
        encoded = cobs_encode(data)
        assert 0 not in encoded
        assert len(encoded) <= size + size // 254 + 1
        assert cobs_decode(encoded) == data, (size, zero_share)

# Frames: split anywhere, survive noise, reject damage.
payloads = [b"\x01", b"\x00" * 10, bytes(range(256)), rng.randbytes(MAX_PAYLOAD), b"hello mixer"]
stream = b"".join(encode_frame(p) for p in payloads)
for chunk in (1, 2, 7, 64, len(stream)):
    decoder = FrameDecoder()
    got = []
    for start in range(0, len(stream), chunk):
        got += decoder.feed(stream[start : start + chunk])
    assert got == payloads, chunk
    assert decoder.frames == len(payloads) and decoder.crc_errors == 0 and decoder.format_errors == 0

decoder = FrameDecoder()
good = encode_frame(b"good")
damaged = bytearray(encode_frame(b"damaged"))
damaged[2] ^= 0x10
assert decoder.feed(b"\x00\x00" + good) == [b"good"]  # idle delimiters carry nothing
assert decoder.feed(bytes(damaged)) == [] and decoder.crc_errors == 1
assert decoder.feed(b"\x02\x41\x00") == [] and decoder.format_errors == 1  # too short to hold a CRC
assert decoder.feed(b"\x41" * 5000) == [] and decoder.format_errors == 2  # runaway noise is dropped
assert decoder.feed(b"tail-of-noise\x00" + good) == [b"good"]  # and it picks up again at the next frame
assert decoder.feed(good[:3]) == [] and decoder.feed(good[3:]) == [b"good"]

for bad_payload in (b"", bytes(MAX_PAYLOAD + 1)):
    try:
        encode_frame(bad_payload)
    except ValueError:
        pass
    else:
        raise AssertionError("accepted a payload of %d bytes" % len(bad_payload))

# The same limit holds for frames coming in: one byte over is not delivered, the limit itself is.
def frame_of(payload):
    return cobs_encode(payload + crc16(payload).to_bytes(2, "big")) + b"\x00"


strict = FrameDecoder()
full = rng.randbytes(MAX_PAYLOAD)
assert strict.feed(frame_of(full)) == [full] and strict.format_errors == 0
for extra in (1, 2, 6):
    before = strict.format_errors
    assert strict.feed(frame_of(rng.randbytes(MAX_PAYLOAD + extra))) == [], extra
    assert strict.format_errors == before + 1 and strict.crc_errors == 0
assert strict.feed(frame_of(b"still listening")) == [b"still listening"]

print("link layer: all checks passed")
