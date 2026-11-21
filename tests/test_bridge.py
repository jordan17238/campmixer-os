#!/usr/bin/env python3
"""Runs the real bridge against a pretend serial port and a standard WebSocket client.

Needs: pip install websockets
"""
import asyncio
import json
import os
import pty
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "rootfs", "opt", "campmixer")
sys.path.insert(0, APP)
from campmixer_link import FrameDecoder, encode_frame  # noqa: E402

from websockets.asyncio.client import connect  # noqa: E402
from websockets.exceptions import InvalidStatus  # noqa: E402

import bridge  # noqa: E402


def lock_state(mounts):
    with tempfile.NamedTemporaryFile("w", suffix=".mounts", delete=False) as handle:
        handle.write(mounts)
    try:
        return bridge.root_is_overlay(handle.name)
    finally:
        os.unlink(handle.name)


# "Locked" is read from what is really mounted on /, not from what was asked for at start-up.
assert lock_state("/dev/mmcblk0p2 / ext4 rw,noatime 0 0\n") is False
assert lock_state("/dev/mmcblk0p2 /media/root-ro ext4 ro,relatime 0 0\n"
                  "overlayroot / overlay rw,relatime,lowerdir=/media/root-ro,upperdir=/media/root-rw/overlay 0 0\n") is True
assert lock_state("/dev/mmcblk0p2 / ext4 rw 0 0\noverlay /var/lib/x/merged overlay rw 0 0\n") is False
assert lock_state("") is False and bridge.root_is_overlay("/nonexistent/mounts") is False

# The mixer answers only to names that cannot have been set up from outside the local network.
for own in ("", "mixer", "MIXER", "mixer.local", "Mixer.Local:80", "mixer-2.local", "mixer.lan", "mixer.home", "mixer.home.arpa",
            "mixer.internal", "mixer.localdomain", "mixer.local.", "localhost:8080", "10.77.7.1", "10.77.7.1:80", "192.168.1.20:8080",
            "169.254.3.4", "[fe80::1]", "[fe80::1%25eth0]:80", "[::1]:8080"):
    assert bridge.is_own_name(own), own
for other in ("evil.example", "evil.example:80", "mixer.evil.example", "mixer.local.evil.example", "mixer.com", "10.77.7.1.evil.example",
              "10.77.7.1.nip.io", "a.b.c.d", "mixer.fritz.box", "mixer local", "mixer.local/", "mixer.local@evil.example", "evil.example:80:80",
              "..local", "mixer..local", "[", "é.local"):
    assert not bridge.is_own_name(other), other

PORT = 18080
BASE = "http://127.0.0.1:%d" % PORT
WS = "ws://127.0.0.1:%d/ws" % PORT


def http(path, method="GET"):
    request = urllib.request.Request(BASE + path, method=method)
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def raw_http(request_bytes):
    with socket.create_connection(("127.0.0.1", PORT), timeout=3) as sock:
        sock.sendall(request_bytes)
        data = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    return data


def status():
    return json.loads(http("/api/status")[2])


def read_serial(master, wanted=1, timeout=2.0):
    """Payloads the bridge wrote to its serial port."""
    decoder, got, deadline = FrameDecoder(), [], time.monotonic() + timeout
    while len(got) < wanted and time.monotonic() < deadline:
        try:
            got += decoder.feed(os.read(master, 4096))
        except BlockingIOError:
            time.sleep(0.01)
    return got


async def next_binary(ws, timeout=2.0):
    deadline = time.monotonic() + timeout
    while True:
        message = await asyncio.wait_for(ws.recv(), max(0.01, deadline - time.monotonic()))
        if isinstance(message, bytes):
            return message


async def next_text(ws, kind, timeout=3.0):
    deadline = time.monotonic() + timeout
    while True:
        message = await asyncio.wait_for(ws.recv(), max(0.01, deadline - time.monotonic()))
        if isinstance(message, str) and json.loads(message).get("type") == kind:
            return json.loads(message)


async def main():
    master, slave = pty.openpty()
    os.set_blocking(master, False)
    notify_dir = tempfile.mkdtemp()
    notify_path = os.path.join(notify_dir, "notify")
    notify = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    notify.bind(notify_path)
    notify.settimeout(5)
    env = dict(os.environ, NOTIFY_SOCKET=notify_path, WATCHDOG_USEC="2000000")
    bridge = subprocess.Popen(
        [sys.executable, os.path.join(APP, "bridge.py"), "--port", str(PORT), "--bind", "127.0.0.1",
         "--serial", os.ttyname(slave)],
        env=env, stderr=subprocess.PIPE,
    )
    try:
        assert notify.recv(64) == b"READY=1"
        assert notify.recv(64) == b"WATCHDOG=1"
        for _ in range(50):  # the serial port opens just after start-up
            if status()["link"]["open"]:
                break
            time.sleep(0.05)
        os.close(slave)

        # --- plain HTTP
        code, headers, body = http("/")
        assert code == 200 and b"<title>CampMixer</title>" in body and headers["Content-Type"].startswith("text/html")
        assert http("/manifest.webmanifest")[1]["Content-Type"].startswith("application/manifest+json")
        assert http("/icon.svg")[1]["Content-Type"].startswith("image/svg+xml")
        code, headers, body = http("/", "HEAD")
        assert code == 200 and body == b"" and int(headers["Content-Length"]) > 1000
        assert http("/nothing-here")[0] == 404
        assert http("/", "POST")[0] == 405
        assert http("/ws")[0] == 426
        for sneaky in (b"/../../../etc/passwd", b"/%2e%2e/%2e%2e/etc/passwd", b"/..%2fbridge.py", b"//etc/passwd"):
            reply = raw_http(b"GET " + sneaky + b" HTTP/1.1\r\nHost: x\r\n\r\n")
            assert reply.startswith(b"HTTP/1.1 404"), (sneaky, reply[:40])
        assert raw_http(b"NONSENSE\r\n\r\n").startswith(b"HTTP/1.1 400")
        first = status()
        assert first["link"]["open"] and not first["link"]["up"] and first["clients"] == 0
        assert first["link"]["port"] == os.ttyname(master).replace("ptmx", "") or first["link"]["port"].startswith("/dev/pts/")

        # --- WebSocket, checked with the 'websockets' library as the browser
        async with connect(WS) as phone, connect(WS, origin=BASE) as laptop:
            hello = json.loads(await asyncio.wait_for(phone.recv(), 2))
            assert hello["type"] == "status" and hello["version"]
            await phone.send(b"\x10knob 1 up")
            assert read_serial(master) == [b"\x10knob 1 up"]

            os.write(master, encode_frame(b"\x20meter data"))
            assert await next_binary(phone) == b"\x20meter data"
            assert await next_binary(laptop) == b"\x20meter data"

            await phone.send([b"split ", b"across ", b"frames"])  # a fragmented WebSocket message
            assert read_serial(master) == [b"split across frames"]
            await asyncio.wait_for(await phone.ping(), 2)

            damaged = bytearray(encode_frame(b"\x20broken"))
            damaged[3] ^= 0x01
            os.write(master, bytes(damaged))
            os.write(master, encode_frame(b"after"))
            assert await next_binary(phone) == b"after"

            await phone.send(bytes(2000))  # larger than a frame may be
            assert "payload" in (await next_text(phone, "error"))["error"]
            await phone.send("text is ignored")

            report = await next_text(laptop, "status")
            assert report["clients"] == 2 and report["link"]["up"]
            assert report["link"]["rx_frames"] == 2 and report["link"]["tx_frames"] == 2
            assert report["link"]["crc_errors"] + report["link"]["format_errors"] == 1

            # loopback, as with a jumper from TX to RX
            await laptop.send(b"\x7eloopback")
            echoed = b""
            for _ in range(200):
                try:
                    echoed = os.read(master, 4096)
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.01)
            os.write(master, echoed)
            assert await next_binary(laptop) == b"\x7eloopback"

        try:
            async with connect(WS, origin="http://somewhere-else.example"):
                raise AssertionError("a page from another site was allowed onto the socket")
        except InvalidStatus as exc:
            assert exc.response.status_code == 403

        # A page that points its own domain name at the mixer (DNS rebinding) is told to go away, on every path.
        key = "dGhlIHNhbXBsZSBub25jZQ=="
        rebound = raw_http(("GET /ws HTTP/1.1\r\nHost: rebind.attacker.example\r\nOrigin: http://rebind.attacker.example\r\n"
                            "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: %s\r\n\r\n" % key).encode())
        assert rebound.startswith(b"HTTP/1.1 403") and b"101 Switching" not in rebound, rebound[:80]
        for path in ("/", "/api/status", "/icon.svg"):
            answer = raw_http(("GET %s HTTP/1.1\r\nHost: rebind.attacker.example\r\n\r\n" % path).encode())
            assert answer.startswith(b"HTTP/1.1 403") and b"own name or address" in answer and b'"version"' not in answer, (path, answer[:80])
            assert ("http://%s.local/" % socket.gethostname()).encode() in answer
        for host in ("mixer.local", "127.0.0.1:%d" % PORT, "mixer.lan", "mixer"):
            answer = raw_http(("GET /api/status HTTP/1.1\r\nHost: %s\r\n\r\n" % host).encode())
            assert answer.startswith(b"HTTP/1.1 200") and b'"version"' in answer, (host, answer[:80])
        assert raw_http(b"GET /api/status HTTP/1.0\r\n\r\n").startswith(b"HTTP/1.1 200")  # no Host header: not a browser
        assert raw_http(b"HEAD / HTTP/1.1\r\nHost: rebind.attacker.example\r\n\r\n").endswith(b"\r\n\r\n")  # HEAD: no body

        time.sleep(0.3)
        assert status()["clients"] == 0

        # --- the serial port going away must not take the web side down
        os.close(master)
        for _ in range(60):
            if not status()["link"]["open"]:
                break
            time.sleep(0.1)
        gone = status()
        assert not gone["link"]["open"] and gone["link"]["error"]
        assert http("/")[0] == 200

        bridge.send_signal(signal.SIGTERM)
        assert bridge.wait(timeout=5) == 0
    finally:
        if bridge.poll() is None:
            bridge.kill()
        errors = bridge.stderr.read().decode()
        if "Traceback" in errors:
            print(errors)
            raise SystemExit("the bridge logged an exception")
    print("bridge: all checks passed")


asyncio.run(main())
