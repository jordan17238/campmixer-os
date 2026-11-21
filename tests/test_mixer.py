#!/usr/bin/env python3
"""Volume, mute and meters, end to end without a mixer: two signed-in phones, the real bridge, a pretend serial
port and a stand-in mixer core on the far side of it. Also checks the shared example messages (PROTOCOL.md).

Needs: pip install websockets
"""
import asyncio
import json
import os
import pty
import secrets
import signal
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "rootfs", "opt", "campmixer")
sys.path.insert(0, APP)
sys.path.insert(0, HERE)
import campmixer_auth  # noqa: E402
import standin_core as sc  # noqa: E402
import websockets.asyncio.client  # noqa: E402

PORT = 18090
PIN = "48271935"

# --- the stand-in core says exactly what PROTOCOL.md's examples say
with open(os.path.join(HERE, "mixer_vectors.json")) as handle:
    VECTORS = json.load(handle)
example = sc.StandInCore()
for channel, wanted in zip(example.channels, VECTORS["channels"]):
    channel.update(level=wanted["level"], muted=wanted["muted"], name=wanted["name"])
assert example.info().hex() == VECTORS["info"]["hex"]
assert [example.channel(n).hex() for n in range(5)] == [v["hex"] for v in VECTORS["channels"]]
assert example.handle(bytes.fromhex(VECTORS["to_core"][0]["hex"])) == [example.info()] + [example.channel(n) for n in range(5)]
for vector in VECTORS["to_core"][1:]:
    answers = example.handle(bytes.fromhex(vector["hex"]))
    assert answers == [example.channel(vector["channel"])], vector  # one change, one answer: just that channel
    assert example.sets[-1] == (vector["channel"], vector["set"], vector["value"]), vector
assert [c["level"] for c in example.channels] == [-60, sc.OFF, 35, 35, 0] and example.channels[4]["muted"] and not example.channels[3]["muted"]
assert [sc.StandInCore.meter_byte(p) for p in (0, -6, -60, float("-inf"), -12.5, 3, -127, -126.9)] == [0, 12, 120, 255, 25, 0, 255, 254]
assert bytes([sc.METERS, 0, 12, 120, 255, 25]).hex() == VECTORS["meters"]["hex"]
# What it will not act on: no such channel, no such setting, cut short, not a message of this version.
before = [example.channel(n) for n in range(5)]
for ignored in ("0209010000", "0200630000", "020001ff", "02", "00", "7f0102", "810104", "83020401", ""):
    assert example.handle(bytes.fromhex(ignored)) == [] and [example.channel(n) for n in range(5)] == before, ignored
assert sc.clamp_level(-601) == sc.OFF and sc.clamp_level(-600) == -600 and sc.clamp_level(101) == 100 and sc.clamp_level(32767) == 100
# Any number of inputs and outputs: the answer to "where does everything stand" is one small message a channel.
big = sc.StandInCore(48, 8)
answers = big.handle(bytes([sc.GET_STATE]))
assert answers[0] == bytes([sc.INFO, 2, 48, 8]) and len(answers) == 57 and max(len(a) for a in answers) <= 22
assert [a[1] for a in answers[1:]] == list(range(56)) and len(big.meters()) == 57
assert big.handle(bytes([sc.SET, 55, sc.MUTE, 0, 1])) == [big.channel(55)] and big.handle(bytes([sc.SET, 56, sc.MUTE, 0, 1])) == []
assert [m[1] for m in big.refresh(60)] == list(range(56)) + [0, 1, 2, 3]  # round and round, so each is said again in turn
assert len(sc.StandInCore(200, 50).handle(bytes([sc.GET_STATE]))) == 251


def decode_channel(payload):
    assert payload[0] == sc.CHANNEL
    level, flags, size = struct.unpack(">hBB", payload[2:6])
    assert len(payload) == 6 + size
    return payload[1], {"level": level, "muted": bool(flags & 1), "name": payload[6:].decode()}


def set_message(channel, what, value):
    return bytes([sc.SET, channel, what]) + struct.pack(">h", value)


class Phone:
    """One signed-in browser: keeps what the core has said about each channel, as the web app does."""

    def __init__(self, ws):
        self.ws, self.info, self.channels, self.meters, self.status = ws, None, {}, [], None

    async def until(self, done, timeout=5.0):
        deadline = time.monotonic() + timeout
        while not done(self):
            message = await asyncio.wait_for(self.ws.recv(), max(0.01, deadline - time.monotonic()))
            if isinstance(message, str):
                report = json.loads(message)
                self.status = report if report.get("type") == "status" else self.status
            elif message[0] == sc.INFO:
                self.info = tuple(message[1:4])
            elif message[0] == sc.CHANNEL:
                number, channel = decode_channel(message)
                self.channels[number] = channel
            elif message[0] == sc.METERS:
                self.meters.append(message)
        return self


async def run_mixer(inputs, outputs):
    master, slave = pty.openpty()
    os.set_blocking(master, False)
    work = tempfile.mkdtemp(prefix="campmixer-mixer-")
    auth_file = os.path.join(work, "web-auth.json")
    with open(auth_file, "w") as handle:
        json.dump(campmixer_auth.derive(secrets.token_bytes(32), PIN), handle)
    log = open(os.path.join(work, "bridge.log"), "wb")
    bridge = subprocess.Popen(
        [sys.executable, os.path.join(APP, "bridge.py"), "--port", str(PORT), "--bind", "127.0.0.1", "--serial", os.ttyname(slave),
         "--auth", auth_file, "--state", ""], stderr=log)

    core, stop = sc.StandInCore(inputs, outputs), threading.Event()
    total, out = inputs + outputs, inputs  # how many channels, and the number of the first output

    def read():
        try:
            return os.read(master, 4096)
        except BlockingIOError:
            return b""

    def write(data):
        while data:  # (a pretend serial port can be full for a moment when a big mixer says everything at once)
            try:
                data = data[os.write(master, data):]
            except BlockingIOError:
                time.sleep(0.002)

    thread = threading.Thread(target=core.serve, args=(read, write, stop.is_set), daemon=True)
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        for _ in range(100):
            try:
                if json.loads(opener.open("http://127.0.0.1:%d/api/status" % PORT, timeout=2).read())["link"]["open"]:
                    break
            except OSError:
                pass
            time.sleep(0.05)
        thread.start()

        request = urllib.request.Request("http://127.0.0.1:%d/api/login" % PORT, data=json.dumps({"pin": PIN}).encode(),
                                         headers={"Content-Type": "application/json"})
        cookie = opener.open(request, timeout=5).headers["Set-Cookie"].split(";")[0]
        connect = lambda: websockets.asyncio.client.connect("ws://127.0.0.1:%d/ws" % PORT, proxy=None,  # noqa: E731
                                                            additional_headers={"Cookie": cookie})
        async with connect() as first, connect() as second:
            phone, tablet = Phone(first), Phone(second)
            # A phone asks where things stand when it connects: how many channels, then each of them.
            await first.send(bytes([sc.GET_STATE]))
            await phone.until(lambda p: p.info is not None and len(p.channels) == total, 10)
            assert phone.info == (2, inputs, outputs)
            assert [phone.channels[n]["name"] for n in range(total)] == [c["name"] for c in core.channels]
            assert all(c["level"] == 0 and not c["muted"] for c in phone.channels.values())

            # One phone moves a fader: the core takes it, and tells every phone about that one channel.
            await first.send(set_message(0, sc.LEVEL, -60))
            for device in (phone, tablet):
                await device.until(lambda p: p.channels.get(0, {}).get("level") == -60)
            assert core.channels[0]["level"] == -60 and core.sets[-1] == (0, sc.LEVEL, -60)

            # The other mutes the last output.
            await second.send(set_message(total - 1, sc.MUTE, 1))
            for device in (phone, tablet):
                await device.until(lambda p: p.channels.get(total - 1, {}).get("muted") is True)
            phone.meters.clear()
            await phone.until(lambda p: len(p.meters) >= 8)
            assert phone.meters[-1][total] == 255 and phone.meters[-1][1] < 255  # that output is silent; the inputs still show
            await second.send(set_message(total - 1, sc.MUTE, 0))
            await phone.until(lambda p: p.channels[total - 1]["muted"] is False)

            # A drag is a run of changes; the last one is where it ends up, on both.
            sets_before = len(core.sets)
            for level in range(-60, -161, -5):
                await first.send(set_message(inputs - 1, sc.LEVEL, level))
            for device in (phone, tablet):
                await device.until(lambda p: p.channels.get(inputs - 1, {}).get("level") == -160)
            assert len(core.sets) - sets_before == 21  # none were lost on the way

            # Off, and the limits: the core decides what a level may be, and the phones show what it decided.
            await first.send(set_message(out, sc.LEVEL, sc.OFF))
            await tablet.until(lambda p: p.channels.get(out, {}).get("level") == sc.OFF)
            await first.send(set_message(out, sc.LEVEL, 5000))
            await tablet.until(lambda p: p.channels[out]["level"] == 100)
            await first.send(set_message(out, sc.LEVEL, 0))
            await tablet.until(lambda p: p.channels[out]["level"] == 0)
            await first.send(set_message(total, sc.LEVEL, 0))  # one past the last channel: nothing happens

            # Meters arrive about 20 times a second, one value for every channel.
            phone.meters.clear()
            started = time.monotonic()
            await phone.until(lambda p: time.monotonic() - started >= 2.0)
            assert 25 <= len(phone.meters) <= 50, len(phone.meters)
            assert all(len(m) == 1 + total and all(value < 255 for value in m[1:1 + inputs]) for m in phone.meters)

            # With no change at all, a phone that has only just connected still learns everything within a few
            # seconds without asking: the core says how many channels there are every second and goes round them.
            async with connect() as third:
                late = Phone(third)
                await late.until(lambda p: p.info is not None and len(p.channels) == total, 8)
                assert late.info == (2, inputs, outputs) and late.channels == phone.channels

            # The Pi counts the link as up, and passes on nothing but what was sent.
            phone.status = None
            await phone.until(lambda p: p.status is not None)
            link = phone.status["link"]
            assert link["up"] and link["crc_errors"] + link["format_errors"] == 0 and link["tx_dropped"] == 0, link
            assert link["tx_frames"] == len(core.sets) + 2, (link, len(core.sets))  # the changes, one GET_STATE, one ignored

        # Without the cookie there is no way to move anything.
        try:
            async with websockets.asyncio.client.connect("ws://127.0.0.1:%d/ws" % PORT, proxy=None) as stranger:
                await stranger.send(set_message(out, sc.LEVEL, 100))
            raise AssertionError("got onto the socket without signing in")
        except websockets.exceptions.InvalidStatus as refused:
            assert refused.response.status_code == 401
        await asyncio.sleep(0.3)
        assert core.channels[out]["level"] == 0

        bridge.send_signal(signal.SIGTERM)
        assert bridge.wait(timeout=5) == 0
    finally:
        stop.set()
        if bridge.poll() is None:
            bridge.kill()
        log.close()
        with open(os.path.join(work, "bridge.log"), errors="replace") as handle:
            errors = handle.read()
        if "Traceback" in errors:
            print(errors)
            raise SystemExit("the bridge logged an exception")
        thread.join(timeout=2)
        os.close(master)
        os.close(slave)


async def main():
    await run_mixer(4, 1)    # the Camp PA Mixer
    await run_mixer(1, 1)    # the smallest there can be
    await run_mixer(48, 8)   # a big one
    await run_mixer(200, 50)  # the most a channel number allows
    print("mixer (volume, mute, meters, any number of inputs and outputs): all checks passed")


asyncio.run(main())
