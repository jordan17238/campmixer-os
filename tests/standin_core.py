#!/usr/bin/env python3
"""A stand-in for the mixer core (the Teensy), for testing the web app with no mixer.

It speaks PROTOCOL.md layer 2, version 2: it keeps a level and a mute for each input and each output (any number
of either) and a 48 V switch for each input, says how many there are, answers GET_STATE and SET, and sends METERS
20 times a second. It changes no sound and powers nothing: there is nothing there.

Used by the tests. It can also be run by hand against a serial port, for example a USB serial adaptor wired
to the Pi's header pins 8 and 10 (crossed over) and ground:

    python3 standin_core.py /dev/ttyUSB0 [inputs outputs]
"""
import math
import os
import struct
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "rootfs", "opt", "campmixer"))
from campmixer_link import FrameDecoder, encode_frame  # noqa: E402

VERSION = 2
GET_STATE, SET, METERS, INFO, CHANNEL = 0x01, 0x02, 0x82, 0x83, 0x84
LEVEL, MUTE, PHANTOM = 1, 2, 4
FLAG_MUTE, FLAG_PHANTOM_ON, FLAG_PHANTOM_SWITCH = 1, 2, 4  # in a CHANNEL's flags
OFF = -32768
LEVEL_MIN, LEVEL_MAX = -600, 100  # tenths of a dB
MAX_CHANNELS = 250


def clamp_level(value):
    """What the core makes of a level it is asked for: below the bottom is off, above the top is the top."""
    if value < LEVEL_MIN:
        return OFF
    return min(LEVEL_MAX, value)


class StandInCore:
    """Any number of inputs and outputs. The channels are numbered inputs first, then outputs."""

    def __init__(self, inputs=4, outputs=1, names=None):
        assert inputs >= 1 and outputs >= 1 and inputs + outputs <= MAX_CHANNELS
        self.inputs, self.outputs = inputs, outputs
        if names is None:
            names = ["MIC 1", "MIC 2", "KEYS", "LAPTOP"] if inputs == 4 else ["IN %d" % (n + 1) for n in range(inputs)]
            names += ["MASTER"] if outputs == 1 else ["MAIN"] + ["OUT %d" % (n + 2) for n in range(outputs - 1)]
        # Every input can supply 48 V, as on the Camp PA Mixer, and starts with it off. An output has no such switch.
        self.channels = [{"level": 0, "muted": False, "name": name, "phantom_switch": number < inputs, "phantom": False}
                         for number, name in enumerate(names)]
        assert len(self.channels) == inputs + outputs
        self.sets = []  # every SET that was acted on, for the tests to look at
        self.on_set = None  # called with (channel, what) after each of them
        self.ticks = 0
        self.next_refresh = 0

    def info(self):
        return bytes([INFO, VERSION, self.inputs, self.outputs])

    def channel(self, number):
        channel = self.channels[number]
        name = channel["name"].encode("utf-8")[:16]
        flags = FLAG_MUTE if channel["muted"] else 0
        if channel["phantom_switch"]:
            flags |= FLAG_PHANTOM_SWITCH | (FLAG_PHANTOM_ON if channel["phantom"] else 0)
        return bytes([CHANNEL, number]) + struct.pack(">hBB", channel["level"], flags, len(name)) + name

    def meters(self):
        """Something lifelike. An input's meter shows what is coming in, whatever its fader and mute are doing;
        an output's shows what is going out: the loudest input that is up and not muted, through that output."""
        self.ticks += 1
        out, loudest = bytearray([METERS]), -math.inf
        for number, channel in enumerate(self.channels[: self.inputs]):
            source = -18 + 9 * math.sin(self.ticks / (7 + 3 * (number % 9)))
            if channel["level"] != OFF and not channel["muted"]:
                loudest = max(loudest, source + channel["level"] / 10)
            out.append(self.meter_byte(source))
        for channel in self.channels[self.inputs:]:
            out.append(self.meter_byte(-math.inf if channel["level"] == OFF or channel["muted"] else loudest + channel["level"] / 10))
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
            return [self.info()] + [self.channel(number) for number in range(len(self.channels))]
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
        elif what == PHANTOM and channel["phantom_switch"]:
            channel["phantom"] = value != 0
        else:
            return []  # a setting this core does not have, or 48 V asked of a channel with no such switch
        self.sets.append((number, what, value))
        if self.on_set:
            self.on_set(channel, what)
        return [self.channel(number)]  # only what changed: this is what keeps a big mixer's serial link from filling up

    def refresh(self, count):
        """The next few channels in turn, so that every channel is said again every few seconds, asked or not."""
        out = []
        for _ in range(count):
            out.append(self.channel(self.next_refresh))
            self.next_refresh = (self.next_refresh + 1) % len(self.channels)
        return out

    def serve(self, read, write, stop=lambda: False):
        """Run against a serial line: read() returns the bytes waiting (or b''), write(bytes) sends."""
        decoder, next_meters, next_info = FrameDecoder(), time.monotonic(), time.monotonic()
        per_tick = max(1, -(-len(self.channels) // 80))  # every channel within four seconds, at 20 ticks a second
        while not stop():
            for payload in decoder.feed(read()):
                for answer in self.handle(payload):
                    write(encode_frame(answer))
            now = time.monotonic()
            if now >= next_meters:
                write(encode_frame(self.meters()))
                for answer in self.refresh(per_tick):
                    write(encode_frame(answer))
                next_meters = max(now, next_meters + 0.05)
            if now >= next_info:  # the heartbeat: once a second, asked or not
                write(encode_frame(self.info()))
                next_info = now + 1.0
            time.sleep(0.005)


def main():
    import termios

    if len(sys.argv) not in (2, 4):
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
        elif what == PHANTOM:
            print("%s: 48 V %s" % (channel["name"], "on" if channel["phantom"] else "off"))
        else:
            print("%s: %s" % (channel["name"], "off" if channel["level"] == OFF else "%+.1f dB" % (channel["level"] / 10)))

    core = StandInCore(*(int(word) for word in sys.argv[2:4]))
    core.on_set = report
    print("Stand-in mixer core on %s. Ctrl-C to stop." % sys.argv[1])
    try:
        core.serve(read, lambda data: os.write(fd, data))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
