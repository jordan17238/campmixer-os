#!/usr/bin/env python3
"""A stand-in for the mixer core (the Teensy), for testing the web app with no mixer.

It speaks PROTOCOL.md layer 2, version 1: it keeps a level and a mute for each input and the master, answers
GET_STATE and SET with STATE, and sends METERS 20 times a second. It changes no sound: there is none.

Used by the tests. It can also be run by hand against a serial port, for example a USB serial adaptor wired
to the Pi's header pins 8 and 10 (crossed over) and ground:

    python3 standin_core.py /dev/ttyUSB0
"""
import math
import os
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "rootfs", "opt", "campmixer"))
from campmixer_link import FrameDecoder, encode_frame  # noqa: E402

VERSION = 1
GET_STATE, SET, STATE, METERS = 0x01, 0x02, 0x81, 0x82
LEVEL, MUTE = 1, 2
OFF = -32768
LEVEL_MIN, LEVEL_MAX = -600, 100  # tenths of a dB


def clamp_level(value):
    """What the core makes of a level it is asked for: below the bottom is off, above the top is the top."""
    if value < LEVEL_MIN:
        return OFF
    return min(LEVEL_MAX, value)


class StandInCore:
    def __init__(self, names=("MIC 1", "MIC 2", "KEYS", "LAPTOP")):
        self.channels = [{"level": 0, "muted": False, "name": name} for name in names]
        self.channels.append({"level": 0, "muted": False, "name": "MASTER"})
        self.sets = []  # every SET that was acted on, for the tests to look at
        self.on_set = None  # called with (channel, what) after each of them
        self.ticks = 0

    def state(self):
        out = bytearray([STATE, VERSION, len(self.channels) - 1])
        for channel in self.channels:
            name = channel["name"].encode("utf-8")[:16]
            out += struct.pack(">hBB", channel["level"], 1 if channel["muted"] else 0, len(name)) + name
        return bytes(out)

    def meters(self):
        """Something lifelike. An input's meter shows what is coming in, whatever its fader and mute are doing;
        the master's shows what is going out: the loudest input that is up and not muted, through the master."""
        self.ticks += 1
        out, loudest = bytearray([METERS]), -math.inf
        for number, channel in enumerate(self.channels[:-1]):
            source = -18 + 9 * math.sin(self.ticks / (7 + 3 * number))
            if channel["level"] != OFF and not channel["muted"]:
                loudest = max(loudest, source + channel["level"] / 10)
            out.append(self.meter_byte(source))
        master = self.channels[-1]
        peak = -math.inf if master["level"] == OFF or master["muted"] else loudest + master["level"] / 10
        out.append(self.meter_byte(peak))
        return bytes(out)

    @staticmethod
    def meter_byte(peak):
        """dB below full scale, as half-dB steps: 0 is full scale, 254 is -127 dB, 255 is silence."""
        return 255 if peak <= -127 else max(0, min(254, round(-peak * 2)))

    def handle(self, payload):
        """One message from the app. Returns the messages to send back (to everybody)."""
        if not payload:
            return []
        if payload[0] == GET_STATE:
            return [self.state()]
        if payload[0] != SET or len(payload) < 5:
            return []  # not for this version, or cut short: ignored
        number, what = payload[1], payload[2]
        (value,) = struct.unpack(">h", payload[3:5])
        if number >= len(self.channels):
            return []
        channel = self.channels[number]
        if what == LEVEL:
            channel["level"] = clamp_level(value)
        elif what == MUTE:
            channel["muted"] = value != 0
        else:
            return []
        self.sets.append((number, what, value))
        if self.on_set:
            self.on_set(channel, what)
        return [self.state()]

    def serve(self, read, write, stop=lambda: False):
        """Run against a serial line: read() returns the bytes waiting (or b''), write(bytes) sends."""
        decoder, next_meters, next_state = FrameDecoder(), time.monotonic(), time.monotonic()
        while not stop():
            for payload in decoder.feed(read()):
                for answer in self.handle(payload):
                    write(encode_frame(answer))
            now = time.monotonic()
            if now >= next_meters:
                write(encode_frame(self.meters()))
                next_meters = max(now, next_meters + 0.05)
            if now >= next_state:  # at least once a second, asked or not, so a phone that missed one catches up
                write(encode_frame(self.state()))
                next_state = now + 1.0
            time.sleep(0.005)


def main():
    import termios

    if len(sys.argv) != 2:
        raise SystemExit(__doc__.strip())
    fd = os.open(sys.argv[1], os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    attrs = termios.tcgetattr(fd)
    attrs[0] = attrs[1] = attrs[3] = 0
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    attrs[4] = attrs[5] = termios.B115200
    termios.tcsetattr(fd, termios.TCSANOW, attrs)

    def read():
        try:
            return os.read(fd, 4096)
        except BlockingIOError:
            return b""

    def report(channel, what):
        if what == MUTE:
            print("%s: %s" % (channel["name"], "muted" if channel["muted"] else "unmuted"))
        else:
            print("%s: %s" % (channel["name"], "off" if channel["level"] == OFF else "%+.1f dB" % (channel["level"] / 10)))

    core = StandInCore()
    core.on_set = report
    print("Stand-in mixer core on %s. Ctrl-C to stop." % sys.argv[1])
    try:
        core.serve(read, lambda data: os.write(fd, data))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
