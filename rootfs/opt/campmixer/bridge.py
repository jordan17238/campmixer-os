#!/usr/bin/env python3
"""CampMixer bridge.

Serves the web app's files and relays frames between the mixer core's serial link and every
browser connected to the /ws WebSocket. Python standard library only, so it needs no packages.

Open to anyone who can reach the Pi (none of it says anything about the mixer):
  GET  /             the web app's own files (under --www)
  GET  /api/session  whether this browser is signed in, and how long it must wait before trying a PIN
  POST /api/login    {"pin": "..."}; answers with a sign-in cookie if the PIN is right
  POST /api/logout   takes the cookie away again

Only with a sign-in cookie:
  GET  /api/status   link and network state as JSON (also allowed from the Pi itself, for "campmixer status")
  GET  /ws           WebSocket. A binary message is one frame payload, in either direction.
                     A text message from the bridge is a JSON status report, sent once a second.

How the PIN is checked and what it does and does not protect is in campmixer_auth.py.
"""
import argparse
import asyncio
import base64
import contextlib
import hashlib
import html
import ipaddress
import json
import logging
import math
import mimetypes
import os
import re
import signal
import socket
import struct
import sys
import termios
import time
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import campmixer_auth as auth  # noqa: E402
from campmixer_link import MAX_PAYLOAD, FrameDecoder, encode_frame  # noqa: E402

LOG = logging.getLogger("campmixer.bridge")
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_WS_MESSAGE = 64 * 1024
MAX_BODY = 1024  # the only thing ever posted here is a PIN
MAX_SIGN_INS_QUEUED = 8
MAX_CONNECTIONS = 256  # open at once, well under what the process may hold, so its own work never runs short
MAX_CONNECTIONS_PER_ADDRESS = 32
REQUEST_TIMEOUT_S = 30  # for a whole request and its answer; a WebSocket, once open, has its own rule
COOKIE_LIFE_S = 400 * 24 * 3600  # as long as browsers allow; changing the PIN is what ends a sign-in
MAX_SERIAL_BACKLOG = 64 * 1024
LINK_TIMEOUT_S = 2.0
CLIENT_SILENCE_S = 35
BAUD_RATES = {
    9600: termios.B9600,
    57600: termios.B57600,
    115200: termios.B115200,
    230400: termios.B230400,
    460800: termios.B460800,
    921600: termios.B921600,
}
REASONS = {
    200: "OK",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    413: "Content Too Large",
    426: "Upgrade Required",
    429: "Too Many Requests",
    503: "Service Unavailable",
}
# Sent with every answer. The app's files load nothing from anywhere else, may not be framed by another
# page (so nobody can lay their own page over the mixer's buttons), and run no script written into a page.
SECURITY_HEADERS = (
    "X-Content-Type-Options: nosniff",
    "X-Frame-Options: DENY",
    "Referrer-Policy: no-referrer",
    "Cross-Origin-Resource-Policy: same-origin",
    "Content-Security-Policy: default-src 'self'; connect-src 'self' ws: wss:; img-src 'self' data:; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
)

mimetypes.add_type("application/manifest+json", ".webmanifest")
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("image/svg+xml", ".svg")


def sd_notify(message):
    """Tell systemd about readiness and liveness. Does nothing when not run by systemd."""
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address[0] == "@":
        address = "\0" + address[1:]
    with contextlib.suppress(OSError), socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        # Never wait here: this runs in the same loop as everything else, and a message that systemd is too
        # busy to take is better lost (it restarts a bridge it hears nothing from) than a bridge standing still.
        sock.setblocking(False)
        sock.sendto(message.encode(), address)


def ws_frame(opcode, payload=b""):
    size = len(payload)
    if size < 126:
        head = bytes([0x80 | opcode, size])
    elif size < 65536:
        head = bytes([0x80 | opcode, 126]) + struct.pack("!H", size)
    else:
        head = bytes([0x80 | opcode, 127]) + struct.pack("!Q", size)
    return head + payload


class WsError(Exception):
    def __init__(self, code, reason):
        super().__init__(reason)
        self.code = code


class SerialLink:
    """The UART to the mixer core. Reopens by itself if the port goes away."""

    def __init__(self, loop, path, baud, on_frame):
        self.loop = loop
        self.path = path
        self.baud = baud
        self.on_frame = on_frame
        self.fd = None
        self.error = "not opened yet" if path else "no serial port configured"
        self.decoder = FrameDecoder()
        self.tx_frames = 0
        self.tx_dropped = 0
        self.last_frame = None
        self._out = bytearray()
        self._writing = False

    def open(self):
        if self.fd is not None or not self.path:
            return
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as exc:
            self.error = exc.strerror or str(exc)
            return
        try:
            attrs = termios.tcgetattr(fd)
            attrs[0] = 0  # input: no translation, no flow control
            attrs[1] = 0  # output: raw
            attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL  # 8N1, ignore modem lines
            attrs[3] = 0  # no echo, no line editing, no signals
            attrs[4] = attrs[5] = BAUD_RATES[self.baud]
            attrs[6][termios.VMIN] = 1
            attrs[6][termios.VTIME] = 0
            termios.tcsetattr(fd, termios.TCSANOW, attrs)
            termios.tcflush(fd, termios.TCIOFLUSH)
        except (termios.error, OSError) as exc:
            os.close(fd)
            self.error = str(exc)
            return
        self.fd = fd
        self.error = ""
        self.decoder.reset()
        self._out.clear()
        self.loop.add_reader(fd, self._readable)
        LOG.info("serial link open on %s at %d baud", self.path, self.baud)

    def _close(self, reason):
        if self.fd is None:
            return
        with contextlib.suppress(Exception):
            self.loop.remove_reader(self.fd)
        if self._writing:
            with contextlib.suppress(Exception):
                self.loop.remove_writer(self.fd)
            self._writing = False
        with contextlib.suppress(OSError):
            os.close(self.fd)
        self.fd = None
        self.error = reason
        self._out.clear()
        LOG.warning("serial link closed: %s", reason)

    def close(self):
        self._close("bridge stopping")

    def _readable(self):
        try:
            data = os.read(self.fd, 4096)
        except BlockingIOError:
            return
        except OSError as exc:
            self._close(exc.strerror or str(exc))
            return
        if not data:
            self._close("port closed")
            return
        for payload in self.decoder.feed(data):
            self.last_frame = time.monotonic()
            self.on_frame(payload)

    def send(self, payload):
        """Queue one payload for the mixer core. Returns False if it could not be queued."""
        if self.fd is None or len(self._out) > MAX_SERIAL_BACKLOG:
            self.tx_dropped += 1
            return False
        self._out += encode_frame(payload)
        self.tx_frames += 1
        self._flush()
        return True

    def _flush(self):
        if self.fd is None:
            return
        try:
            written = os.write(self.fd, self._out)
        except BlockingIOError:
            written = 0
        except OSError as exc:
            self._close(exc.strerror or str(exc))
            return
        del self._out[:written]
        if self._out and not self._writing:
            self.loop.add_writer(self.fd, self._flush)
            self._writing = True
        elif not self._out and self._writing:
            self.loop.remove_writer(self.fd)
            self._writing = False

    def status(self):
        age = None if self.last_frame is None else round(time.monotonic() - self.last_frame, 2)
        return {
            "port": self.path or None,
            "open": self.fd is not None,
            "error": self.error,
            "up": age is not None and age <= LINK_TIMEOUT_S,
            "last_frame_age_s": age,
            "rx_frames": self.decoder.frames,
            "tx_frames": self.tx_frames,
            "tx_dropped": self.tx_dropped,
            "crc_errors": self.decoder.crc_errors,
            "format_errors": self.decoder.format_errors,
        }


class Client:
    """One browser on the WebSocket."""

    def __init__(self, writer):
        self.writer = writer
        self.queue = asyncio.Queue(maxsize=256)
        self.open = True
        self.close_sent = False

    def send(self, opcode, payload=b""):
        if not self.open or self.close_sent:
            return  # after the goodbye, nothing more: a browser counts anything else as an error
        if opcode == 0x8:
            self.close_sent = True
        try:
            self.queue.put_nowait(ws_frame(opcode, payload))
        except asyncio.QueueFull:
            # A stalled phone must not hold up the others: drop it, it will reconnect.
            self.open = False
            self.writer.transport.abort()

    async def pump(self):
        try:
            while True:
                data = await self.queue.get()
                self.writer.write(data)
                await self.writer.drain()
        except (ConnectionError, OSError):
            self.open = False


LOCAL_ONLY_SUFFIXES = (".local", ".lan", ".home", ".internal", ".localdomain", ".home.arpa", ".localhost")


def is_own_name(host):
    """Whether a request's Host header is one that can only have been reached from inside the local network.

    A web page on the internet can point its own domain name at the mixer's address, and a browser then
    treats that page and the mixer as one site (DNS rebinding), so the page could work the mixer. That
    takes a public domain name. A bare address, a name without dots and a name under one of the suffixes
    used only inside local networks cannot be set up from outside, so those are the names answered to.
    """
    host = host.strip().lower()
    if not host:
        return True  # no Host header at all: not a browser, so not some other site's page
    if host.startswith("["):
        return "]" in host  # an IPv6 address
    name = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    name = name.rstrip(".")
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    if not re.fullmatch(r"[a-z0-9_-]+(\.[a-z0-9_-]+)*", name):
        return False
    return "." not in name or name.endswith(LOCAL_ONLY_SUFFIXES)


def cookie_value(headers, name):
    for part in headers.get("cookie", "").split(";"):
        key, sep, value = part.strip().partition("=")
        if sep and key == name:
            return value
    return ""


def same_origin(headers):
    """False for a request that a page from some other site made the browser send (cross-site request forgery).

    Browsers say where a POST comes from. Anything that is not a browser has no page to be tricked by.
    """
    if headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
        return False
    origin = headers.get("origin")
    return not origin or urllib.parse.urlsplit(origin).netloc.lower() == headers.get("host", "").lower()


def peer_address(writer):
    """(address, whether it is this Pi itself) for the other end of a connection."""
    peer = writer.get_extra_info("peername")
    host = str(peer[0]) if peer else ""
    try:
        address = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return host or "unknown", False
    if address.version == 6 and address.ipv4_mapped:
        address = address.ipv4_mapped
    return str(address), address.is_loopback


def root_is_overlay(mounts="/proc/self/mounts"):
    """True when the card is locked: the system is running on a layer in memory over a read-only card."""
    locked = False
    try:
        with open(mounts, errors="replace") as handle:
            for line in handle:
                fields = line.split()
                if len(fields) >= 3 and fields[1] == "/":
                    locked = fields[2] == "overlay"  # the last mount on / is the one in use
    except OSError:
        pass
    return locked


class Bridge:
    def __init__(self, loop, args):
        self.loop = loop
        self.www = Path(args.www).resolve()
        self.clients = set()
        self.serial = SerialLink(loop, args.serial, args.baud, self.on_serial_frame)
        self.started = time.monotonic()
        self.network = {"addresses": [], "wifi": None}
        self.auth_path = Path(args.auth)
        self.auth = None
        self.auth_stamp = ()
        self.throttle = auth.Throttle()
        self.throttle_path = Path(args.state) if args.state else None
        self.throttle_saved = True
        self.checking = asyncio.Lock()
        self.sign_ins_queued = 0
        self.connections = {}  # address -> connections open from it
        self.load_auth()
        self.load_throttle()
        version_file = Path(__file__).resolve().parent / "VERSION"
        self.version = version_file.read_text().strip() if version_file.is_file() else "dev"
        self.locked = root_is_overlay()

    # ----- serial side -----

    def on_serial_frame(self, payload):
        for client in list(self.clients):
            client.send(0x2, payload)

    async def serial_keeper(self):
        while True:
            self.serial.open()
            await asyncio.sleep(2)

    # ----- signing in -----

    def load_auth(self):
        """Read what netconf.py wrote for checking PINs. Called at start, then whenever that file changes.

        Without a usable file nobody can sign in. The page is never left open for want of one.
        """
        try:
            stat = self.auth_path.stat()
            stamp = (stat.st_mtime_ns, stat.st_ino, stat.st_size)
        except OSError:
            stamp = None
        if stamp == self.auth_stamp:
            return
        self.auth_stamp = stamp
        before = self.auth.session_key if self.auth else None
        try:
            self.auth = auth.Verifier.load(self.auth_path) if stamp else None
        except Exception as exc:  # whatever is wrong with it, the answer is the same: nobody gets in
            LOG.error("cannot use %s (%s)", self.auth_path, exc)
            self.auth = None
        if self.auth is None:
            LOG.warning("no PIN is set up (%s), so nobody can sign in", self.auth_path)
        else:
            self.throttle.set_limit(self.auth.limit)  # a short PIN comes with a stricter allowance of wrong tries
        if before is not None and (self.auth is None or self.auth.session_key != before):
            LOG.info("the PIN has changed: everyone is signed out")
            for client in list(self.clients):
                self.clients.discard(client)
                client.send(0x8, struct.pack("!H", 4001) + b"signed out")
                self.loop.call_later(1, client.writer.transport.abort)

    def signed_in(self, headers):
        return self.auth is not None and self.auth.token_ok(cookie_value(headers, auth.COOKIE))

    def load_throttle(self):
        """Carry on counting wrong PINs where the bridge that ran before this one stopped."""
        if self.throttle_path is None:
            return
        try:
            with open(self.throttle_path, encoding="ascii") as handle:
                self.throttle.restore(json.load(handle))
        except FileNotFoundError:
            pass
        except Exception as exc:
            LOG.error("cannot use %s (%s); counting wrong PINs from nothing", self.throttle_path, exc)

    def save_throttle(self):
        if self.throttle_path is None:
            return
        temp = self.throttle_path.with_name(self.throttle_path.name + ".tmp")
        try:
            temp.write_text(json.dumps(self.throttle.dump()), encoding="ascii")
            os.replace(temp, self.throttle_path)
            self.throttle_saved = True
        except OSError as exc:
            if self.throttle_saved:
                LOG.error("cannot write %s (%s); the count of wrong PINs will not outlast this bridge", self.throttle_path, exc)
            self.throttle_saved = False

    async def sign_in(self, writer, headers, body, source):
        if self.auth is None:
            await self.respond_json(writer, 503, {"ok": False, "error": "No PIN is set up on the mixer. See: campmixer logs"})
            return
        pin = None
        if headers.get("content-type", "").split(";")[0].strip().lower() == "application/json":
            try:
                pin = json.loads(body.decode("utf-8"))["pin"]
            except (ValueError, KeyError, TypeError, RecursionError):
                pin = None
        if not isinstance(pin, str):
            await self.respond_json(writer, 400, {"ok": False, "error": 'Send {"pin": "..."} as application/json'})
            return
        pin = auth.tidy_pin(pin)
        if not auth.looks_like_pin(pin):
            # Letters, or too few digits: a slip of the thumb, not a guess. It cannot be right and does not count.
            await self.respond_json(writer, 403, {"ok": False, "wait": math.ceil(self.throttle.wait(source))})
            return
        wait = self.throttle.wait(source)
        if wait > 0 or self.sign_ins_queued >= MAX_SIGN_INS_QUEUED:
            await self.respond_wait(writer, max(wait, 1))
            return
        self.sign_ins_queued += 1
        try:
            # One check at a time: each costs the Pi a tenth of a second of hard work, on purpose.
            async with self.checking:
                wait = self.throttle.wait(source)  # the tries may have been used up while this one queued
                if wait > 0:
                    await self.respond_wait(writer, wait)
                    return
                checker = self.auth
                # Counted as wrong first, and written down, so that nothing which interrupts the check
                # (this bridge being stopped included) lets a wrong PIN go uncounted.
                self.throttle.spend(source)
                self.save_throttle()
                right = checker is not None and await self.loop.run_in_executor(None, checker.check_pin, pin)
                if right:
                    self.throttle.refund(source)
                    self.save_throttle()
        finally:
            self.sign_ins_queued -= 1
        if right:
            LOG.info("signed in from %s", source)
            cookie = "Set-Cookie: %s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict" % (
                auth.COOKIE, checker.new_token(), COOKIE_LIFE_S)
            await self.respond_json(writer, 200, {"ok": True}, extra=(cookie,))
        else:
            LOG.warning("wrong PIN from %s (%d wrong since start-up)", source, self.throttle.wrong_total)
            await self.respond_json(writer, 403, {"ok": False, "wait": math.ceil(self.throttle.wait(source))})

    async def respond_wait(self, writer, wait):
        seconds = math.ceil(wait)
        await self.respond_json(writer, 429, {"ok": False, "wait": seconds}, extra=("Retry-After: %d" % seconds,))

    # ----- status -----

    def status(self):
        return {
            "type": "status",
            "version": self.version,
            "hostname": socket.gethostname(),
            "uptime_s": int(time.monotonic() - self.started),
            "locked": self.locked,
            "link": self.serial.status(),
            "clients": len(self.clients),
            "network": self.network,
            "sign_in": {"ready": self.auth is not None, "wrong_pins": self.throttle.wrong_total},
        }

    async def ticker(self):
        beat = 0
        while True:
            await asyncio.sleep(1)
            beat += 1
            self.load_auth()
            report = json.dumps(self.status()).encode()
            for client in list(self.clients):
                client.send(0x1, report)
                if beat % 10 == 0:
                    client.send(0x9)

    async def _run(self, *command):
        try:
            proc = await asyncio.create_subprocess_exec(
                *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
            )
        except OSError:
            return ""
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), 4)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            return ""
        return out.decode("utf-8", "replace") if proc.returncode == 0 else ""

    async def network_poll(self):
        while True:
            addresses = []
            with contextlib.suppress(ValueError, KeyError, TypeError):
                for link in json.loads(await self._run("ip", "-j", "-4", "addr") or "[]"):
                    if link.get("ifname") == "lo":
                        continue
                    for info in link.get("addr_info", []):
                        addresses.append({"interface": link["ifname"], "address": info["local"]})
            wifi = None
            current = {}
            for line in (await self._run("iw", "dev")).splitlines():
                words = line.split()
                if not words:
                    continue
                if words[0] == "Interface":
                    current = {"interface": words[1]}
                elif words[0] == "ssid" and current:
                    current["name"] = line.split("ssid", 1)[1].strip()
                elif words[0] == "type" and current:
                    current["mode"] = "own" if words[1] == "AP" else "joined"
                    if "name" in current:
                        wifi = dict(current)
            self.network = {"addresses": addresses, "wifi": wifi}
            await asyncio.sleep(5)

    async def watchdog(self):
        usec = int(os.environ.get("WATCHDOG_USEC", "0") or 0)
        if not usec:
            await asyncio.Future()  # nobody is watching (run by hand, not by systemd): nothing to do, for good
        interval = max(1.0, usec / 2_000_000)
        while True:
            sd_notify("WATCHDOG=1")
            await asyncio.sleep(interval)

    # ----- HTTP -----

    async def respond(self, writer, status, body=b"", ctype="text/plain; charset=utf-8", head_only=False, extra=(),
                      cache="no-cache"):
        lines = [
            "HTTP/1.1 %d %s" % (status, REASONS[status]),
            "Content-Type: " + ctype,
            "Content-Length: %d" % len(body),
            "Cache-Control: " + cache,
            *SECURITY_HEADERS,
            "Connection: close",
            *extra,
        ]
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + (b"" if head_only else body))
        await writer.drain()

    async def respond_json(self, writer, status, data, head_only=False, extra=()):
        body = json.dumps(data, indent=1).encode()
        await self.respond(writer, status, body, "application/json", head_only, extra, cache="no-store")

    def find_file(self, path):
        path = urllib.parse.unquote(path)
        if "\0" in path:
            return None
        if path.endswith("/"):
            path += "index.html"
        target = (self.www / path.lstrip("/")).resolve()
        if not target.is_relative_to(self.www) or not target.is_file():
            return None
        return target

    async def handle(self, reader, writer):
        source, _local = peer_address(writer)
        mine = self.connections.get(source, 0)
        if sum(self.connections.values()) >= MAX_CONNECTIONS or mine >= MAX_CONNECTIONS_PER_ADDRESS:
            writer.transport.abort()  # more open than any honest use needs: no answer, nothing held
            return
        self.connections[source] = mine + 1
        try:
            upgrade = await asyncio.wait_for(self._handle(reader, writer, source), REQUEST_TIMEOUT_S)
            if upgrade is not None:
                await self.websocket(reader, writer, *upgrade)
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.TimeoutError, OSError):
            pass
        except Exception:  # keep serving the others whatever one connection does
            LOG.exception("connection handler failed")
        finally:
            if self.connections.get(source, 0) <= 1:
                self.connections.pop(source, None)
            else:
                self.connections[source] -= 1
            with contextlib.suppress(Exception):
                writer.close()
                await asyncio.wait_for(writer.wait_closed(), 5)
            writer.transport.abort()

    async def _handle(self, reader, writer, source):
        """Answer one request. Returns (method, headers) if it is a signed-in browser asking for the WebSocket."""
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        except asyncio.LimitOverrunError:
            await self.respond(writer, 400, b"Request too large\n")
            return
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, _version = lines[0].split(" ", 2)
        except ValueError:
            await self.respond(writer, 400, b"Bad request\n")
            return
        headers = {}
        for line in lines[1:]:
            name, sep, value = line.partition(":")
            if sep:
                headers[name.strip().lower()] = value.strip()
        path = target.split("?", 1)[0]
        from_this_pi = peer_address(writer)[1]

        if not is_own_name(headers.get("host", "")):
            await self.respond(writer, 403, self.wrong_name_page(), "text/html; charset=utf-8", method == "HEAD")
            return
        if path == "/ws":
            if not self.signed_in(headers):
                await self.respond(writer, 401, b"Sign in first\n")
                return None
            return method, headers
        if method == "POST" and path in ("/api/login", "/api/logout"):
            if "transfer-encoding" in headers or not headers.get("content-length", "").isdigit():
                await self.respond(writer, 400, b"Content-Length needed\n")
                return
            if int(headers["content-length"]) > MAX_BODY:
                await self.respond(writer, 413, b"Too much\n")
                return
            body = await asyncio.wait_for(reader.readexactly(int(headers["content-length"])), 10)
            if not same_origin(headers):
                await self.respond(writer, 403, b"Cross-site request refused\n")
                return
            if path == "/api/login":
                await self.sign_in(writer, headers, body, source)
            else:
                gone = "Set-Cookie: %s=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict" % auth.COOKIE
                await self.respond_json(writer, 200, {"ok": True}, extra=(gone,))
            return
        if method not in ("GET", "HEAD"):
            await self.respond(writer, 405, b"Not allowed here\n", extra=("Allow: GET, HEAD",))
            return
        head_only = method == "HEAD"
        if path == "/api/session":
            answer = {
                "signed_in": self.signed_in(headers),
                "ready": self.auth is not None,
                "wait": math.ceil(self.throttle.wait(source)),
            }
            await self.respond_json(writer, 200, answer, head_only)
            return
        if path == "/api/status":
            if not from_this_pi and not self.signed_in(headers):
                await self.respond_json(writer, 401, {"error": "Sign in first"}, head_only)
                return
            await self.respond_json(writer, 200, self.status(), head_only)
            return
        if path.startswith("/api/"):
            await self.respond(writer, 404, b"Not found\n", head_only=head_only)
            return
        target_file = self.find_file(path)
        if target_file is None:
            await self.respond(writer, 404, b"Not found\n", head_only=head_only)
            return
        ctype = mimetypes.guess_type(target_file.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/json", "application/manifest+json"):
            ctype += "; charset=utf-8"
        await self.respond(writer, 200, target_file.read_bytes(), ctype, head_only)

    def wrong_name_page(self):
        """What a browser gets if it asked for this mixer under a name that could belong to another site."""
        name = socket.gethostname()
        links = ["http://%s.local/" % name] + ["http://%s/" % entry["address"] for entry in self.network["addresses"]]
        items = "".join('<li><a href="%s">%s</a></li>' % (html.escape(link, quote=True), html.escape(link)) for link in links)
        return (
            "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>CampMixer</title><p>The mixer answers only to its own name or address. Open one of these instead:</p>"
            "<ul>%s</ul>" % items
        ).encode()

    # ----- WebSocket -----

    async def websocket(self, reader, writer, method, headers):
        key = headers.get("sec-websocket-key")
        if method != "GET" or "websocket" not in headers.get("upgrade", "").lower() or not key:
            await self.respond(writer, 426, b"This address is the WebSocket\n", extra=("Upgrade: websocket",))
            return
        if headers.get("sec-websocket-version") != "13":
            await self.respond(writer, 400, b"WebSocket version 13 only\n", extra=("Sec-WebSocket-Version: 13",))
            return
        origin = headers.get("origin")
        if origin and urllib.parse.urlsplit(origin).netloc.lower() != headers.get("host", "").lower():
            # Only pages served by this mixer may open the socket, not a page from somewhere else.
            await self.respond(writer, 403, b"Cross-site WebSocket refused\n")
            return
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        writer.write(
            (
                "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                "Sec-WebSocket-Accept: %s\r\n\r\n" % accept
            ).encode()
        )
        await writer.drain()

        client = Client(writer)
        pump = asyncio.create_task(client.pump())
        self.clients.add(client)
        client.send(0x1, json.dumps(self.status()).encode())
        try:
            await self.ws_read_loop(reader, client)
        except WsError as exc:
            client.send(0x8, struct.pack("!H", exc.code) + str(exc).encode()[:100])
            await asyncio.sleep(0.05)
        finally:
            self.clients.discard(client)
            client.open = False
            pump.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await pump

    async def ws_read_frame(self, reader):
        first, second = await reader.readexactly(2)
        if first & 0x70:
            raise WsError(1002, "reserved bits set")
        opcode = first & 0x0F
        size = second & 0x7F
        if size == 126:
            (size,) = struct.unpack("!H", await reader.readexactly(2))
        elif size == 127:
            (size,) = struct.unpack("!Q", await reader.readexactly(8))
        if not second & 0x80:
            raise WsError(1002, "client frames must be masked")
        if size > MAX_WS_MESSAGE or (opcode >= 0x8 and (size > 125 or not first & 0x80)):
            raise WsError(1009 if opcode < 0x8 else 1002, "frame too large")
        mask = await reader.readexactly(4)
        data = await reader.readexactly(size)
        if size:
            keystream = (mask * (size // 4 + 1))[:size]
            data = (int.from_bytes(data, "big") ^ int.from_bytes(keystream, "big")).to_bytes(size, "big")
        return bool(first & 0x80), opcode, data

    async def ws_read_loop(self, reader, client):
        part_opcode = None
        parts = bytearray()
        while client.open:
            final, opcode, data = await asyncio.wait_for(self.ws_read_frame(reader), CLIENT_SILENCE_S)
            if opcode == 0x8:
                client.send(0x8, data[:2])
                await asyncio.sleep(0.05)
                return
            if opcode == 0x9:
                client.send(0xA, data)
            elif opcode == 0xA:
                pass
            elif opcode in (0x1, 0x2):
                if part_opcode is not None:
                    raise WsError(1002, "new message inside a fragmented one")
                if final:
                    self.on_ws_message(client, opcode, data)
                else:
                    part_opcode = opcode
                    parts = bytearray(data)
            elif opcode == 0x0:
                if part_opcode is None:
                    raise WsError(1002, "continuation without a start")
                parts += data
                if len(parts) > MAX_WS_MESSAGE:
                    raise WsError(1009, "message too large")
                if final:
                    self.on_ws_message(client, part_opcode, bytes(parts))
                    part_opcode = None
            else:
                raise WsError(1002, "unknown opcode")

    def on_ws_message(self, client, opcode, data):
        if opcode != 0x2:
            return  # text from the browser is reserved for later use
        if not 1 <= len(data) <= MAX_PAYLOAD:
            client.send(0x1, json.dumps({"type": "error", "error": "payload must be 1 to %d bytes" % MAX_PAYLOAD}).encode())
            return
        if not self.serial.send(data):
            client.send(0x1, json.dumps({"type": "error", "error": "serial link is not available"}).encode())


async def run(args):
    loop = asyncio.get_running_loop()
    bridge = Bridge(loop, args)
    server = await asyncio.start_server(bridge.handle, host=args.bind, port=args.port, limit=65536, reuse_address=True)
    stop = asyncio.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    tasks = [
        asyncio.create_task(bridge.serial_keeper()),
        asyncio.create_task(bridge.ticker()),
        asyncio.create_task(bridge.network_poll()),
        asyncio.create_task(bridge.watchdog()),
    ]
    LOG.info("CampMixer bridge %s serving %s on port %d", bridge.version, bridge.www, args.port)
    sd_notify("READY=1")
    # Run until told to stop. Should one of the background jobs ever end by itself, stop as well and let
    # systemd start a fresh bridge, rather than carry on half-working with nothing to show for it.
    stopping = asyncio.create_task(stop.wait())
    done, _pending = await asyncio.wait([stopping, *tasks], return_when=asyncio.FIRST_COMPLETED)
    broken = [task for task in done if task is not stopping]
    for task in broken:
        LOG.error("a background job ended by itself: %r", None if task.cancelled() else task.exception())
    sd_notify("STOPPING=1")
    stopping.cancel()
    for task in tasks:
        task.cancel()
    server.close()
    for client in list(bridge.clients):
        client.send(0x8, struct.pack("!H", 1001))
    await asyncio.sleep(0.1)
    bridge.serial.close()
    return 1 if broken else 0


def main():
    parser = argparse.ArgumentParser(description="CampMixer bridge: serial link to the mixer core, web app for phones.")
    parser.add_argument("--port", type=int, default=80, help="TCP port for the web app (default 80)")
    parser.add_argument("--bind", default=None, help="address to listen on (default: every interface)")
    parser.add_argument("--www", default=str(Path(__file__).resolve().parent / "www"), help="folder of web app files")
    parser.add_argument("--serial", default="/dev/serial0", help="serial port to the mixer core, or '' for none")
    parser.add_argument("--baud", type=int, default=115200, choices=sorted(BAUD_RATES))
    parser.add_argument("--auth", default="/run/campmixer/web-auth.json", help="what netconf.py wrote for checking PINs")
    parser.add_argument("--state", default="/run/campmixer-bridge/sign-in.json",
                        help="where the count of wrong PINs is kept between restarts of the bridge, or '' for nowhere")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
