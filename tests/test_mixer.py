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
for channel, wanted in zip(example.channels, VECTORS["state"]["channels"]):
    channel.update(wanted)
assert example.state().hex() == VECTORS["state"]["hex"]
assert example.handle(bytes.fromhex(VECTORS["to_core"][0]["hex"])) == [example.state()]
for vector in VECTORS["to_core"][1:]:
    answers = example.handle(bytes.fromhex(vector["hex"]))
    assert answers == [example.state()], vector
    assert example.sets[-1] == (vector["channel"], vector["set"], vector["value"]), vector
assert [c["level"] for c in example.channels] == [-60, sc.OFF, 35, 35, 0] and example.channels[4]["muted"] and not example.channels[3]["muted"]
assert [sc.StandInCore.meter_byte(p) for p in (0, -6, -60, float("-inf"), -12.5, 3, -127, -126.9)] == [0, 12, 120, 255, 25, 0, 255, 254]
assert bytes([sc.METERS, 0, 12, 120, 255, 25]).hex() == VECTORS["meters"]["hex"]
# What it will not act on: no such channel, no such setting, cut short, not a message of this version.
before = example.state()
for ignored in ("0209010000", "0200630000", "020001ff", "02", "00", "7f0102", "810104", ""):
    assert example.handle(bytes.fromhex(ignored)) == [] and example.state() == before, ignored
assert sc.clamp_level(-601) == sc.OFF and sc.clamp_level(-600) == -600 and sc.clamp_level(101) == 100 and sc.clamp_level(32767) == 100


def decode_state(payload):
    assert payload[0] == sc.STATE and payload[1] == 1
    channels, at = [], 3
    for _ in range(payload[2] + 1):
        level, flags, size = struct.unpack(">hBB", payload[at:at + 4])
        channels.append({"level": level, "muted": bool(flags & 1), "name": payload[at + 4:at + 4 + size].decode()})
        at += 4 + size
    assert at == len(payload)
    return channels


def set_message(channel, what, value):
    return bytes([sc.SET, channel, what]) + struct.pack(">h", value)


async def next_state(ws, wanted=None, timeout=3.0):
    """The next STATE from the core (the one matching, if a check is given)."""
    deadline = time.monotonic() + timeout
    while True:
        message = await asyncio.wait_for(ws.recv(), max(0.01, deadline - time.monotonic()))
        if isinstance(message, bytes) and message[0] == sc.STATE:
            channels = decode_state(message)
            if wanted is None or wanted(channels):
                return channels


async def main():
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

    core, stop = sc.StandInCore(), threading.Event()

    def read():
        try:
            return os.read(master, 4096)
        except BlockingIOError:
            return b""

    thread = threading.Thread(target=core.serve, args=(read, lambda data: os.write(master, data), stop.is_set), daemon=True)
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
        async with connect() as phone, connect() as tablet:
            # A phone asks where things stand when it connects.
            await phone.send(bytes([sc.GET_STATE]))
            channels = await next_state(phone)
            assert [c["name"] for c in channels] == ["MIC 1", "MIC 2", "KEYS", "LAPTOP", "MASTER"]
            assert all(c["level"] == 0 and not c["muted"] for c in channels)

            # One phone moves a fader: the core takes it, and tells every phone.
            await phone.send(set_message(0, sc.LEVEL, -60))
            for device in (phone, tablet):
                channels = await next_state(device, lambda cs: cs[0]["level"] == -60)
                assert [c["level"] for c in channels] == [-60, 0, 0, 0, 0]
            assert core.channels[0]["level"] == -60 and core.sets[-1] == (0, sc.LEVEL, -60)

            # The other mutes the master.
            await tablet.send(set_message(4, sc.MUTE, 1))
            for device in (phone, tablet):
                assert (await next_state(device, lambda cs: cs[4]["muted"]))[4] == {"level": 0, "muted": True, "name": "MASTER"}
            await tablet.send(set_message(4, sc.MUTE, 0))
            await next_state(phone, lambda cs: not cs[4]["muted"])

            # A drag is a run of changes; the last one is where it ends up, on both.
            for level in range(-60, -161, -5):
                await phone.send(set_message(1, sc.LEVEL, level))
            for device in (phone, tablet):
                await next_state(device, lambda cs: cs[1]["level"] == -160)
            assert len([s for s in core.sets if s[0] == 1]) == 21  # none were lost on the way

            # Off, and the limits: the core decides what a level may be, and the phones show what it decided.
            await phone.send(set_message(2, sc.LEVEL, sc.OFF))
            await next_state(tablet, lambda cs: cs[2]["level"] == sc.OFF)
            await phone.send(set_message(3, sc.LEVEL, 5000))
            await next_state(tablet, lambda cs: cs[3]["level"] == 100)
            await phone.send(set_message(3, sc.LEVEL, -5000))
            await next_state(tablet, lambda cs: cs[3]["level"] == sc.OFF)

            # Meters arrive about 20 times a second, one value for each input and one for the master.
            started, meters = time.monotonic(), []
            while time.monotonic() - started < 2.0:
                message = await asyncio.wait_for(phone.recv(), 2)
                if isinstance(message, bytes) and message[0] == sc.METERS:
                    meters.append(message)
            assert 25 <= len(meters) <= 50, len(meters)
            # An input's meter shows what is coming in even with its fader off (KEYS, LAPTOP); the master's what goes out.
            assert all(len(m) == 6 and all(value < 255 for value in m[1:]) for m in meters)
            await tablet.send(set_message(4, sc.MUTE, 1))
            await next_state(phone, lambda cs: cs[4]["muted"])
            for _ in range(8):
                message = b""
                while not (isinstance(message, bytes) and message[:1] == bytes([sc.METERS])):
                    message = await asyncio.wait_for(phone.recv(), 2)
            assert message[5] == 255 and message[1] < 255, message.hex()  # master muted: nothing goes out, the inputs still show
            await tablet.send(set_message(4, sc.MUTE, 0))
            await next_state(phone, lambda cs: not cs[4]["muted"])

            # With no change at all, the core still says where things stand at least once a second.
            await asyncio.sleep(0.3)
            try:
                while True:  # (first, read off everything that has piled up while the other phone was being watched)
                    await asyncio.wait_for(tablet.recv(), 0.002)
            except asyncio.TimeoutError:
                pass
            started, states = time.monotonic(), 0
            while time.monotonic() - started < 3.2:
                message = await asyncio.wait_for(tablet.recv(), 2)
                states += isinstance(message, bytes) and message[0] == sc.STATE
            assert 2 <= states <= 5, states

            # The Pi counts the link as up, and passes on nothing but what was sent.
            report = None
            while report is None:
                message = await asyncio.wait_for(phone.recv(), 2)
                if isinstance(message, str) and json.loads(message)["type"] == "status":
                    report = json.loads(message)
            assert report["link"]["up"] and report["link"]["crc_errors"] + report["link"]["format_errors"] == 0, report["link"]
            assert report["clients"] == 2 and report["link"]["tx_frames"] == len(core.sets) + 1, (report["link"], len(core.sets))

        # Without the cookie there is no way to move anything.
        try:
            async with websockets.asyncio.client.connect("ws://127.0.0.1:%d/ws" % PORT, proxy=None) as stranger:
                await stranger.send(set_message(4, sc.LEVEL, 100))
            raise AssertionError("got onto the socket without signing in")
        except websockets.exceptions.InvalidStatus as refused:
            assert refused.response.status_code == 401
        await asyncio.sleep(0.3)
        assert core.channels[4]["level"] == 0

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
    print("mixer (volume, mute, meters): all checks passed")


asyncio.run(main())
