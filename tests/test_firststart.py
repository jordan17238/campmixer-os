#!/usr/bin/env python3
"""Checks the image's first-start set-up: passwords from campmixer.txt, or made up and written back."""
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
FIRSTSTART = os.path.join(HERE, "..", "rootfs", "opt", "campmixer", "firststart.py")
MADE_UP = r"[a-z2-9]{4}-[a-z2-9]{4}-[a-z2-9]{4}"


def first_start(settings, chpasswd_fails=False):
    root = tempfile.mkdtemp(prefix="campmixer-first-")
    os.makedirs(root + "/boot/firmware")
    os.makedirs(root + "/sys/class/net")
    with open(root + "/boot/firmware/campmixer.txt", "w") as handle:
        handle.write(settings)
    fake = root + "/chpasswd"
    with open(fake, "w") as handle:
        handle.write("#!/bin/sh\ncat >> %s/chpasswd.log\nexit %d\n" % (root, 1 if chpasswd_fails else 0))
    os.chmod(fake, 0o755)

    def run():
        env = dict(os.environ, CAMPMIXER_ROOT=root, CAMPMIXER_SYS_NET=root + "/sys/class/net",
                   CAMPMIXER_CHPASSWD=fake, CAMPMIXER_LOGIN_USER="mixer")
        return subprocess.run([sys.executable, FIRSTSTART], env=env, capture_output=True, text=True)

    def read(path):
        with open(root + path) as handle:
            return handle.read()

    return run, read, root


# Nothing filled in: both passwords are made up, set, and written where they can be read.
run, read, root = first_start("# notes stay\nWIFI_NAME=CampMixer\nWIFI_PASSWORD=\nLOGIN_PASSWORD=\n")
done = run()
assert done.returncode == 0, done.stderr
text = read("/boot/firmware/campmixer.txt")
wifi = re.search(r"^WIFI_PASSWORD=(%s)$" % MADE_UP, text, re.M)
login = re.search(r"^LOGIN_PASSWORD=(%s)$" % MADE_UP, text, re.M)
assert wifi and login and wifi.group(1) != login.group(1), text
assert text.startswith("# notes stay\n")
assert read("/chpasswd.log") == "mixer:%s\n" % login.group(1)
assert "psk=%s\n" % wifi.group(1) in read("/etc/NetworkManager/system-connections/campmixer-ap.nmconnection")

# A second run must not change the login password again.
assert run().returncode == 0
assert read("/chpasswd.log") == "mixer:%s\n" % login.group(1)
assert read("/boot/firmware/campmixer.txt") == text

# Passwords chosen beforehand are used. The login one is then taken out of the file.
run, read, root = first_start("WIFI_PASSWORD=my wifi password\nLOGIN_PASSWORD=pa:ss word 123\nJOIN_WIFI_1=Hall\n")
assert run().returncode == 0
text = read("/boot/firmware/campmixer.txt")
assert "WIFI_PASSWORD=my wifi password\n" in text and "LOGIN_PASSWORD=\n" in text and "123" not in text
assert read("/chpasswd.log") == "mixer:pa:ss word 123\n"
assert os.path.exists(root + "/etc/NetworkManager/system-connections/campmixer-join-1.nmconnection")

# A login password that is too short is not used; one is made up instead.
run, read, root = first_start("WIFI_PASSWORD=long enough\nLOGIN_PASSWORD=short\n")
assert run().returncode == 0
assert re.search(r"^LOGIN_PASSWORD=%s$" % MADE_UP, read("/boot/firmware/campmixer.txt"), re.M)

# If the password cannot be set, say so and leave everything for another try.
run, read, root = first_start("WIFI_PASSWORD=long enough\nLOGIN_PASSWORD=\n", chpasswd_fails=True)
done = run()
assert done.returncode == 1 and "could not set the login password" in done.stderr
assert not os.path.exists(root + "/var/lib/campmixer/login-password-set")
assert read("/boot/firmware/campmixer.txt") == "WIFI_PASSWORD=long enough\nLOGIN_PASSWORD=\n"

print("first start: all checks passed")
