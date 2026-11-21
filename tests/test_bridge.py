#!/usr/bin/env python3
"""Runs the real bridge against a pretend serial port and a standard WebSocket client, and tries to get past its PIN.

Needs: pip install websockets
"""
import asyncio
import json
import os
import pty
import random
import re
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "rootfs", "opt", "campmixer")
sys.path.insert(0, APP)
from campmixer_link import FrameDecoder, encode_frame  # noqa: E402

import websockets.asyncio.client  # noqa: E402
from websockets.exceptions import ConnectionClosed, InvalidStatus  # noqa: E402

import bridge  # noqa: E402
import campmixer_auth  # noqa: E402


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
BASE = "http://127.0.0.1:%d" % PORT  # from the Pi itself: may read the status without signing in, and nothing more
WS = "ws://127.0.0.1:%d/ws" % PORT


def outside_address():
    """An address of this machine other than 127.0.0.1. A connection to it counts as coming from the network."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # nothing is sent: this only asks which address would be used
        address = probe.getsockname()[0]
    except OSError:
        address = "127.0.0.1"
    finally:
        probe.close()
    if address.startswith("127."):
        raise SystemExit("these checks need a network address other than loopback, to play someone on the network")
    return address


OUTSIDE = outside_address()
FAR = "http://%s:%d" % (OUTSIDE, PORT)  # from the network: has to sign in
FAR_WS = "ws://%s:%d/ws" % (OUTSIDE, PORT)
PIN, NEW_PIN = "48271935", "90517362"
KEY = secrets.token_bytes(32)
WORK = tempfile.mkdtemp(prefix="campmixer-bridge-")
AUTH_FILE = os.path.join(WORK, "web-auth.json")
STATE_FILE = os.path.join(WORK, "sign-in.json")
LOG_FILE = os.path.join(WORK, "bridge.log")


def write_auth(pin, key=KEY):
    with open(AUTH_FILE + ".new", "w") as handle:
        json.dump(campmixer_auth.derive(key, pin), handle)
    os.replace(AUTH_FILE + ".new", AUTH_FILE)


# Straight to the bridge, whatever proxy the machine running the tests is set up to use.
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def connect(url, **options):
    return websockets.asyncio.client.connect(url, proxy=None, **options)


def http(path, method="GET", base=BASE, headers=None, body=None):
    request = urllib.request.Request(base + path, method=method, headers=headers or {}, data=body)
    try:
        with DIRECT.open(request, timeout=5) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def raw_http(request_bytes, host="127.0.0.1", wait=3):
    with socket.create_connection((host, PORT), timeout=wait) as sock:
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


def wrong_pins():
    return status()["sign_in"]["wrong_pins"]


def post(path, body, base=FAR, ctype="application/json", **headers):
    """(status, answer as JSON or None, headers)."""
    extra = {name.replace("_", "-"): value for name, value in headers.items()}
    if ctype:
        extra["Content-Type"] = ctype
    code, answer_headers, answer = http(path, "POST", base, extra, body if isinstance(body, bytes) else json.dumps(body).encode())
    try:
        return code, json.loads(answer), answer_headers
    except ValueError:
        return code, None, answer_headers


def sign_in(pin, base=FAR, **headers):
    code, answer, answer_headers = post("/api/login", {"pin": pin}, base, **headers)
    cookie = answer_headers.get("Set-Cookie", "")
    return code, answer, cookie.split(";")[0] if cookie.startswith("campmixer=") and code == 200 else None


def with_cookie(cookie, path="/api/status", base=FAR):
    return http(path, base=base, headers={"Cookie": cookie})[0]


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


async def refused(url, code, **options):
    try:
        async with connect(url, **options):
            raise AssertionError("got onto the WebSocket at %s with %r" % (url, options))
    except InvalidStatus as exc:
        assert exc.response.status_code == code, (exc.response.status_code, code, options)


class Bridge:
    """The real bridge.py as its own process, the way systemd runs it."""

    def __init__(self, serial_path):
        self.notify_path = os.path.join(tempfile.mkdtemp(), "notify")
        self.notify = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.notify.bind(self.notify_path)
        self.notify.settimeout(5)
        self.log = open(LOG_FILE, "ab")
        self.process = subprocess.Popen(
            [sys.executable, os.path.join(APP, "bridge.py"), "--port", str(PORT), "--serial", serial_path,
             "--auth", AUTH_FILE, "--state", STATE_FILE],
            env=dict(os.environ, NOTIFY_SOCKET=self.notify_path, WATCHDOG_USEC="2000000"), stderr=self.log,
        )
        assert self.notify.recv(64) == b"READY=1"
        assert self.notify.recv(64) == b"WATCHDOG=1"
        self.heard = []  # like systemd, keep taking what it sends
        threading.Thread(target=self.listen, daemon=True).start()

    def listen(self):
        self.notify.settimeout(None)
        try:
            while True:
                message = self.notify.recv(64)
                if not message:
                    break
                self.heard.append(message)
        except OSError:
            pass

    def stop(self):
        self.process.send_signal(signal.SIGTERM)
        assert self.process.wait(timeout=5) == 0
        assert b"STOPPING=1" in self.heard and b"WATCHDOG=1" in self.heard, self.heard[-5:]
        self.notify.close()
        self.log.close()


async def main():
    master, slave = pty.openpty()
    os.set_blocking(master, False)
    serial_path = os.ttyname(slave)
    write_auth(PIN)
    running = Bridge(serial_path)
    try:
        for _ in range(50):  # the serial port opens just after start-up
            if status()["link"]["open"]:
                break
            time.sleep(0.05)

        # --- what anyone on the network gets without the PIN: the page that asks for it, and nothing about the mixer
        code, headers, page = http("/", base=FAR)
        assert code == 200 and b"<title>CampMixer</title>" in page and headers["Content-Type"].startswith("text/html")
        assert http("/manifest.webmanifest", base=FAR)[1]["Content-Type"].startswith("application/manifest+json")
        assert http("/icon.svg", base=FAR)[1]["Content-Type"].startswith("image/svg+xml")
        assert http("/app.js", base=FAR)[1]["Content-Type"].startswith("text/javascript")
        assert http("/mixer-link.js", base=FAR)[1]["Content-Type"].startswith("text/javascript")
        assert http("/app.css", base=FAR)[1]["Content-Type"].startswith("text/css")
        code, headers, body = http("/", "HEAD", FAR)
        assert code == 200 and body == b"" and int(headers["Content-Length"]) > 1000
        assert http("/nothing-here", base=FAR)[0] == 404
        assert http("/", "POST", FAR)[0] == 405
        for sneaky in (b"/../../../etc/passwd", b"/%2e%2e/%2e%2e/etc/passwd", b"/..%2fbridge.py", b"//etc/passwd",
                       b"/..%2f..%2f..%2frun%2fcampmixer%2fweb-auth.json", b"/%2e%2e/campmixer_auth.py"):
            reply = raw_http(b"GET " + sneaky + b" HTTP/1.1\r\nHost: x\r\n\r\n", OUTSIDE)
            assert reply.startswith(b"HTTP/1.1 404"), (sneaky, reply[:40])
        assert raw_http(b"NONSENSE\r\n\r\n", OUTSIDE).startswith(b"HTTP/1.1 400")

        # The page runs no script and no style written into it, loads nothing from elsewhere and cannot be framed.
        policy = headers["Content-Security-Policy"]
        assert "default-src 'self'" in policy and "frame-ancestors 'none'" in policy and "unsafe" not in policy, policy
        assert headers["X-Frame-Options"] == "DENY" and headers["X-Content-Type-Options"] == "nosniff"
        assert headers["Referrer-Policy"] == "no-referrer" and headers["Cross-Origin-Resource-Policy"] == "same-origin"
        text = page.decode()
        assert re.findall(r"<script\b[^>]*>", text) == ['<script src="/mixer-link.js">', '<script src="/app.js">'] and "<style" not in text
        assert not re.search(r"\sstyle\s*=|\son[a-z]+\s*=|javascript:", text, re.I), "something written into the page"
        assert "http://" not in text and "https://" not in text and "//" not in text.replace("</", "")

        code, headers, body = http("/api/session", base=FAR)
        assert code == 200 and json.loads(body) == {"signed_in": False, "ready": True, "wait": 0}, body
        assert headers["Cache-Control"] == "no-store"
        for path in ("/api/status", "/api/status?x=1", "/api/status/"):
            code, _headers, body = http(path, base=FAR)
            assert code in (401, 404) and b"version" not in body and b"hostname" not in body, (path, code, body)
        assert http("/api/status", base=FAR)[0] == 401 and http("/api/status", "HEAD", FAR)[0] == 401
        await refused(FAR_WS, 401)
        await refused(FAR_WS, 401, origin=FAR)
        await refused(WS, 401)  # nor from the Pi itself: only the status report is open there, for "campmixer status"
        first = status()
        assert first["link"]["open"] and not first["link"]["up"] and first["clients"] == 0
        assert first["sign_in"] == {"ready": True, "wrong_pins": 0}
        os.close(slave)

        # --- things that are not a sign-in at all are turned away, and do not use up anyone's tries
        assert http("/api/login", base=FAR)[0] == 404 and http("/api/login", "PUT", FAR)[0] == 405
        assert http("/api/login", "DELETE", FAR)[0] == 405 and http("/api/login", "OPTIONS", FAR)[0] == 405
        right = json.dumps({"pin": PIN}).encode()
        assert post("/api/login", right, ctype="text/plain")[0] == 400  # what a form on another site could send
        assert post("/api/login", ("pin=" + PIN).encode(), ctype="application/x-www-form-urlencoded")[0] == 400
        assert post("/api/login", right, ctype=None)[0] == 400
        for origin in ("http://evil.example", "http://%s:%d" % (OUTSIDE, PORT + 1), "http://%s" % OUTSIDE, "null",
                       "https://evil.example:%d" % PORT, "http://127.0.0.1:%d" % PORT):
            code, _answer, cookie = sign_in(PIN, Origin=origin)  # the right PIN, sent by some other site's page
            assert code == 403 and cookie is None, origin
        for site in ("cross-site", "same-site"):
            assert sign_in(PIN, Sec_Fetch_Site=site)[0] == 403, site
        for junk in (b"", b"{", b"[]", b"null", b'"48271935"', b'{"pin": 48271935}', b'{"pin": null}', b'{"pin": ["48271935"]}',
                     b'{"PIN": "48271935"}', b"[" * 1000, b'{"a":' * 200, b"\xff\xfe\x00", b'{"pin": "48271935"'):
            assert post("/api/login", junk)[0] == 400, junk[:30]
        head = "POST /api/login HTTP/1.1\r\nHost: %s\r\nContent-Type: application/json\r\n" % OUTSIDE
        assert raw_http((head + "\r\n").encode() + right, OUTSIDE).startswith(b"HTTP/1.1 400")  # no length given
        assert raw_http((head + "Transfer-Encoding: chunked\r\n\r\n").encode() + b"12\r\n" + right + b"\r\n0\r\n\r\n",
                        OUTSIDE).startswith(b"HTTP/1.1 400")
        assert raw_http((head + "Content-Length: 5000\r\n\r\n").encode() + right, OUTSIDE).startswith(b"HTTP/1.1 413")
        assert raw_http((head + "Content-Length: -5\r\n\r\n").encode() + right, OUTSIDE).startswith(b"HTTP/1.1 400")
        assert raw_http((head + "Content-Length: 99999999999999999999\r\n\r\n").encode(), OUTSIDE).startswith(b"HTTP/1.1 413")
        assert wrong_pins() == 0 and not os.path.exists(STATE_FILE)

        # --- the PIN itself
        code, answer, cookie = sign_in("48271936")
        assert code == 403 and answer == {"ok": False, "wait": 0} and cookie is None
        assert wrong_pins() == 1
        with open(STATE_FILE) as handle:
            assert json.load(handle)["wrong_total"] == 1  # written down the moment it happens
        for almost in ("4827193", "482719355", "48271934"):
            assert sign_in(almost)[0] == 403, almost
        assert wrong_pins() == 4
        for slip in ("", "0000000048271935", "48271935a", "４８２７１９３５", "482", "pin", "48271935; DROP", " "):
            assert sign_in(slip)[0] == 403, slip
        assert wrong_pins() == 4  # not PINs at all: they cannot be right, and are not held against anyone
        code, answer, cookie = sign_in("4827 1935")  # typed with a space, as it is easier to read
        assert code == 200 and answer == {"ok": True} and cookie, (code, answer)
        full = post("/api/login", {"pin": PIN})[2]["Set-Cookie"]
        assert "HttpOnly" in full and "SameSite=Strict" in full and "Path=/" in full and "Max-Age=34560000" in full, full
        assert "Domain" not in full  # for this mixer only, not for every name that ends the same way
        assert wrong_pins() == 4 and with_cookie(cookie) == 200
        code, _headers, body = http("/api/session", base=FAR, headers={"Cookie": cookie})
        assert json.loads(body)["signed_in"] is True
        assert with_cookie("other=1; " + cookie + "; theme=dark") == 200  # among other cookies
        assert with_cookie(cookie, "/api/session") == 200 and with_cookie(cookie, "/") == 200

        # --- cookies nobody was given
        token = cookie.split("=", 1)[1]
        nonce, signature = token.split(".")
        elsewhere = campmixer_auth.Verifier(campmixer_auth.derive(secrets.token_bytes(32), PIN)).new_token()  # another mixer, same PIN
        for forged in ("campmixer=", "campmixer=x", "campmixer=" + token[:-1], "campmixer=" + token + "x", "campmixer=" + nonce + ".",
                       "campmixer=." + signature, "campmixer=" + elsewhere, "campmixer=" + nonce + "." + elsewhere.split(".")[1],
                       "campmixer=" + "A" * 22 + "." + "A" * 43, "CAMPMIXER=" + token, "campmixer_=" + token, "xcampmixer=" + token,
                       "session=" + token, "campmixer=true", "campmixer=admin", "campmixer=" + "x" * 5000, "campmixer=%s" % token.upper(),
                       'campmixer="%s"' % token, "campmixer=" + token.replace(".", "%2E"), token):
            assert with_cookie(forged) == 401, forged[:60]
            await refused(FAR_WS, 401, additional_headers={"Cookie": forged})
        for header in ({"Authorization": "Basic " + "YWRtaW46YWRtaW4="}, {"Authorization": "Bearer " + token}, {"X-Forwarded-For": "127.0.0.1"},
                       {"Forwarded": "for=127.0.0.1"}, {"X-Real-IP": "127.0.0.1"}, {"Host": "localhost"}, {"Host": "127.0.0.1"},
                       {"X-Campmixer": token}):
            code, _headers, body = http("/api/status", base=FAR, headers=header)
            assert code == 401 and b"version" not in body, header

        # --- WebSocket, checked with the 'websockets' library as the browser
        signed = {"additional_headers": {"Cookie": cookie}}
        async with connect(FAR_WS, **signed) as phone, connect(FAR_WS, origin=FAR, **signed) as laptop:
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
            assert report["sign_in"] == {"ready": True, "wrong_pins": 4}

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

        # A page from another site cannot use the socket, even in a browser that is signed in to the mixer.
        await refused(FAR_WS, 403, origin="http://somewhere-else.example", **signed)
        await refused(FAR_WS, 403, origin="http://%s:%d" % (OUTSIDE, PORT + 1), **signed)

        # A page that points its own domain name at the mixer (DNS rebinding) is told to go away, on every path.
        key = "dGhlIHNhbXBsZSBub25jZQ=="
        rebound = raw_http(("GET /ws HTTP/1.1\r\nHost: rebind.attacker.example\r\nOrigin: http://rebind.attacker.example\r\nCookie: %s\r\n"
                            "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: %s\r\n\r\n"
                            % (cookie, key)).encode())
        assert rebound.startswith(b"HTTP/1.1 403") and b"101 Switching" not in rebound, rebound[:80]
        for path in ("/", "/api/status", "/icon.svg", "/api/session"):
            answer = raw_http(("GET %s HTTP/1.1\r\nHost: rebind.attacker.example\r\nCookie: %s\r\n\r\n" % (path, cookie)).encode())
            assert answer.startswith(b"HTTP/1.1 403") and b"own name or address" in answer and b'"version"' not in answer, (path, answer[:80])
            assert ("http://%s.local/" % socket.gethostname()).encode() in answer
        rebound = raw_http(("POST /api/login HTTP/1.1\r\nHost: rebind.attacker.example\r\nContent-Type: application/json\r\n"
                            "Content-Length: %d\r\n\r\n" % len(right)).encode() + right, OUTSIDE)
        assert rebound.startswith(b"HTTP/1.1 403") and b"Set-Cookie" not in rebound, rebound[:80]
        for host in ("mixer.local", "127.0.0.1:%d" % PORT, "mixer.lan", "mixer"):
            answer = raw_http(("GET /api/status HTTP/1.1\r\nHost: %s\r\n\r\n" % host).encode())
            assert answer.startswith(b"HTTP/1.1 200") and b'"version"' in answer, (host, answer[:80])
            answer = raw_http(("GET /api/status HTTP/1.1\r\nHost: %s\r\n\r\n" % host).encode(), OUTSIDE)
            assert answer.startswith(b"HTTP/1.1 401") and b'"version"' not in answer, (host, answer[:80])
        assert raw_http(b"GET /api/status HTTP/1.0\r\n\r\n").startswith(b"HTTP/1.1 200")  # no Host header: not a browser
        assert raw_http(b"HEAD / HTTP/1.1\r\nHost: rebind.attacker.example\r\n\r\n").endswith(b"\r\n\r\n")  # HEAD: no body

        time.sleep(0.3)
        assert status()["clients"] == 0

        # --- guessing: after five wrong PINs from one address, that address waits, right PIN or not
        for n in range(4):  # (signing in wiped this address's slate, so it has its five tries again)
            code, answer, _cookie = sign_in("%08d" % (30517246 + n))
            assert code == 403 and answer["wait"] == 0, answer
        code, answer, _cookie = sign_in("11111111")
        assert code == 403 and answer["wait"] == 30 and wrong_pins() == 9, answer
        code, answer, headers = post("/api/login", {"pin": PIN})
        assert code == 429 and 28 <= answer["wait"] <= 30 and headers["Retry-After"] == str(answer["wait"]) and "Set-Cookie" not in headers
        for _ in range(30):
            assert sign_in("%08d" % random.randrange(10**8))[0] == 429
        assert wrong_pins() == 9  # turned away unread: hammering does not even count
        assert 25 <= json.loads(http("/api/session", base=FAR)[2])["wait"] <= 30
        assert json.loads(http("/api/session")[2])["wait"] == 0  # someone else is not held up by it
        assert with_cookie(cookie) == 200  # and whoever is signed in already stays in
        code, _answer, local_cookie = sign_in(PIN, BASE)
        assert code == 200 and local_cookie and wrong_pins() == 9

        # --- restarting the bridge neither forgets the count nor signs anyone out
        running.stop()
        running = Bridge(serial_path)
        assert wrong_pins() == 9 and sign_in(PIN)[0] == 429
        assert with_cookie(cookie) == 200 and with_cookie(local_cookie, base=BASE) == 200

        # --- changing the PIN signs everyone out, wherever they are
        async with connect(WS, additional_headers={"Cookie": local_cookie}) as phone:
            await next_text(phone, "status")
            write_auth(NEW_PIN)
            try:
                while True:
                    await asyncio.wait_for(phone.recv(), 4)
            except ConnectionClosed as closed:
                assert closed.rcvd is not None and closed.rcvd.code == 4001, closed
        assert with_cookie(cookie) == 401 and with_cookie(local_cookie, "/api/session", BASE) == 200
        assert json.loads(http("/api/session", headers={"Cookie": local_cookie})[2])["signed_in"] is False
        await refused(WS, 401, additional_headers={"Cookie": local_cookie})
        assert sign_in(PIN, BASE)[0] == 403 and wrong_pins() == 10  # the old PIN is just another wrong one now
        code, _answer, cookie = sign_in(NEW_PIN, BASE)
        assert code == 200 and with_cookie(cookie, base=BASE) == 200 and with_cookie(cookie) == 200

        # --- signing out
        code, answer, headers = post("/api/logout", {}, Cookie=cookie)
        assert code == 200 and "campmixer=;" in headers["Set-Cookie"] and "Max-Age=0" in headers["Set-Cookie"]
        assert post("/api/logout", {}, Cookie=cookie, Origin="http://evil.example")[0] == 403

        # --- no PIN to check against (the file is gone, or is not what it should be): nobody gets in
        os.rename(AUTH_FILE, AUTH_FILE + ".away")
        time.sleep(1.5)
        assert json.loads(http("/api/session")[2]) == {"signed_in": False, "ready": False, "wait": 0}
        assert json.loads(http("/api/session", base=FAR, headers={"Cookie": cookie})[2])["signed_in"] is False
        assert with_cookie(cookie) == 401 and sign_in(NEW_PIN, BASE)[0] == 503 and status()["sign_in"]["ready"] is False
        await refused(WS, 401, additional_headers={"Cookie": cookie})
        for rubbish in ("", "{}", "not json", '{"v": 1}', json.dumps(dict(campmixer_auth.derive(KEY, NEW_PIN), session_key="00"))):
            with open(AUTH_FILE, "w") as handle:
                handle.write(rubbish)
            time.sleep(1.2)
            assert with_cookie(cookie) == 401 and sign_in(NEW_PIN, BASE)[0] == 503, rubbish
        os.replace(AUTH_FILE + ".away", AUTH_FILE)
        time.sleep(1.5)
        assert with_cookie(cookie) == 200 and wrong_pins() == 10

        # --- a four-digit PIN works, and brings its stricter allowance of wrong tries with it
        with open(STATE_FILE) as handle:
            assert json.load(handle)["limit"] == [20, 180]
        write_auth("4827")
        time.sleep(1.5)
        assert with_cookie(cookie) == 401  # the PIN changed: signed out
        assert sign_in("4828", BASE)[0] == 403 and wrong_pins() == 11
        with open(STATE_FILE) as handle:
            record = json.load(handle)
        assert record["limit"] == [10, 900] and record["budget"] <= 9.01, record
        code, _answer, short_cookie = sign_in("4827", BASE)
        assert code == 200 and with_cookie(short_cookie, base=BASE) == 200
        write_auth(NEW_PIN)
        time.sleep(1.5)
        code, _answer, cookie = sign_in(NEW_PIN, BASE)
        assert code == 200 and with_cookie(cookie) == 200 and with_cookie(short_cookie) == 401

        # --- someone holding many connections open gets no more than their share, and the rest carry on
        held = []
        for _ in range(60):
            sock = socket.create_connection((OUTSIDE, PORT), timeout=3)
            sock.sendall(b"GET / HTTP/1.1\r\n")  # and then nothing
            held.append(sock)
        time.sleep(0.3)
        dropped = 0
        for sock in held:
            sock.settimeout(0.05)
            try:
                dropped += sock.recv(1) == b""
            except (socket.timeout, BlockingIOError):
                pass
            except OSError:
                dropped += 1
        assert dropped == 60 - 32, dropped
        assert http("/")[0] == 200 and status()["link"]["open"]  # others are served meanwhile
        for sock in held:
            sock.close()
        time.sleep(0.3)
        assert http("/", base=FAR)[0] == 200

        # --- rubbish of every kind must not stop it
        rng = random.Random(7)
        seeds = [b"GET / HTTP/1.1\r\nHost: x\r\n\r\n", b"POST /api/login HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: 18\r\n\r\n"
                 + b'{"pin":"00000000"}', ("GET /ws HTTP/1.1\r\nHost: x\r\nCookie: %s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                                           "Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: %s\r\n\r\n" % (cookie, key)).encode() + bytes(64),
                 b"GET /api/status HTTP/1.1\r\nCookie: campmixer=" + token.encode() + b"\r\n\r\n"]
        for round_number in range(400):
            data = bytearray(rng.choice(seeds))
            for _ in range(rng.randrange(1, 6)):
                kind = rng.randrange(4)
                at = rng.randrange(len(data) + 1)
                if kind == 0:
                    data[at:at] = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 40)))
                elif kind == 1 and data:
                    del data[at:at + rng.randrange(1, 20)]
                elif kind == 2 and data:
                    data[min(at, len(data) - 1)] = rng.randrange(256)
                else:
                    data += rng.choice([b"\r\n\r\n", b"\x00" * 50, b"A" * 70000, b"\xff" * 300, b"\r\n" * 200])
            try:
                with socket.create_connection((OUTSIDE if round_number % 2 else "127.0.0.1", PORT), timeout=2) as sock:
                    sock.settimeout(0.05)
                    sock.sendall(bytes(data))
                    try:
                        sock.recv(4096)
                    except OSError:
                        pass
            except OSError:
                pass
        time.sleep(0.5)
        assert running.process.poll() is None and http("/", base=FAR)[0] == 200 and with_cookie(cookie) == 200
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

        running.stop()
    finally:
        if running.process.poll() is None:
            running.process.kill()
        with open(LOG_FILE, errors="replace") as handle:
            errors = handle.read()
        if "Traceback" in errors:
            print(errors)
            raise SystemExit("the bridge logged an exception")
        assert PIN not in errors and NEW_PIN not in errors and "11111111" not in errors  # no PIN, right or wrong, in the log
    print("bridge: all checks passed")


asyncio.run(main())
