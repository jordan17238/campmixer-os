#!/usr/bin/env python3
"""Runs the real bridge with a stand-in mixer core behind a pretend serial port, for looking at the web app
in a browser on this computer, by hand or from tests/ui_check.js.

    python3 tests/ui_server.py [port [inputs outputs]]      (4 inputs and 1 output unless told otherwise)

Then open http://127.0.0.1:PORT and sign in with the PIN it prints. It takes these lines on its input:

    core off      the mixer core goes quiet (as if unplugged)
    core on       and comes back
    pin 90517362  the PIN is changed, which signs every browser out
    quit
"""
import json
import os
import pty
import secrets
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "rootfs", "opt", "campmixer")
sys.path.insert(0, APP)
sys.path.insert(0, HERE)
import campmixer_auth  # noqa: E402
import standin_core as sc  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 18095
SHAPE = (int(sys.argv[2]), int(sys.argv[3])) if len(sys.argv) > 3 else (4, 1)
PIN = "48271935"
KEY = secrets.token_bytes(32)


def main():
    master, slave = pty.openpty()
    os.set_blocking(master, False)
    work = tempfile.mkdtemp(prefix="campmixer-ui-")
    auth_file = os.path.join(work, "web-auth.json")

    def write_auth(pin):
        with open(auth_file + ".new", "w") as handle:
            json.dump(campmixer_auth.derive(KEY, pin), handle)
        os.replace(auth_file + ".new", auth_file)

    write_auth(PIN)
    bridge = subprocess.Popen([sys.executable, os.path.join(APP, "bridge.py"), "--port", str(PORT), "--bind", "127.0.0.1",
                               "--serial", os.ttyname(slave), "--auth", auth_file, "--state", os.path.join(work, "sign-in.json")])
    core, quiet = sc.StandInCore(*SHAPE), threading.Event()
    if SHAPE == (4, 1):
        core.channels[0]["level"], core.channels[1]["level"], core.channels[2]["level"] = -60, -100, sc.OFF
        core.channels[3].update(level=-120, muted=True)
    else:
        for number, channel in enumerate(core.channels[: SHAPE[0]]):
            channel.update(level=sc.OFF if number % 5 == 4 else -30 * (number % 4) - 30, muted=number % 7 == 6)

    def report(channel, what):
        value = ("muted" if channel["muted"] else "unmuted") if what == sc.MUTE else str(channel["level"])
        print("SET %s %s" % (channel["name"], value), flush=True)

    core.on_set = report

    def read():
        try:
            data = os.read(master, 4096)
        except BlockingIOError:
            return b""
        return b"" if quiet.is_set() else data

    def write(data):
        while data and not quiet.is_set():
            try:
                data = data[os.write(master, data):]
            except BlockingIOError:
                time.sleep(0.002)

    threading.Thread(target=core.serve, args=(read, write), daemon=True).start()
    print("READY http://127.0.0.1:%d PIN %s" % (PORT, PIN), flush=True)
    try:
        for line in sys.stdin:
            words = line.split()
            if words == ["core", "off"]:
                quiet.set()
            elif words == ["core", "on"]:
                quiet.clear()
            elif len(words) == 2 and words[0] == "pin":
                write_auth(words[1])
            elif words == ["quit"]:
                break
            print("OK " + " ".join(words), flush=True)
    finally:
        bridge.terminate()
        bridge.wait(timeout=5)


if __name__ == "__main__":
    main()
