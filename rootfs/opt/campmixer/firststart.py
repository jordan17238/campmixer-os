#!/usr/bin/env python3
"""CampMixer first start, for a freshly written image.

The image has no passwords in it. On its first start this sets the login password from LOGIN_PASSWORD in
campmixer.txt, or makes one up and writes it into that file, where it can be read with the card in a computer.
A login password you chose yourself is removed from the file once it is set.

(The Wi-Fi password is looked after by netconf.py at every start-up: a blank or unusable one is made up there.)
"""
import os
import pwd
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import netconf  # noqa: E402

CHPASSWD = os.environ.get("CAMPMIXER_CHPASSWD", "chpasswd")
LOGIN_DONE = netconf.ROOT / "var/lib/campmixer/login-password-set"


def login_user():
    """The account people log in with: the first ordinary user on the system."""
    name = os.environ.get("CAMPMIXER_LOGIN_USER")
    if name:
        return name
    users = sorted((user.pw_uid, user.pw_name) for user in pwd.getpwall() if 1000 <= user.pw_uid < 60000)
    return users[0][1] if users else None


def usable_login_password(password):
    return 8 <= len(password) <= 127 and all(32 <= ord(ch) <= 126 for ch in password)


def main():
    try:
        raw = netconf.read_raw()
    except OSError:
        raw = {}
    changes = {}

    if not LOGIN_DONE.exists():
        user = login_user()
        if user is None:
            netconf.log("no login account found")
            return 1
        chosen = raw.get("LOGIN_PASSWORD", "")
        password = chosen if usable_login_password(chosen) else netconf.make_password()
        done = subprocess.run([CHPASSWD], input="%s:%s\n" % (user, password), text=True, capture_output=True)
        if done.returncode != 0:
            netconf.log("could not set the login password for %s: %s" % (user, done.stderr.strip()))
            return 1
        if password == chosen:
            changes["LOGIN_PASSWORD"] = ""
            netconf.log("set the login password for %s from campmixer.txt and removed it from the file" % user)
        else:
            changes["LOGIN_PASSWORD"] = password
            netconf.log("made up a login password for %s; it is in campmixer.txt" % user)
        LOGIN_DONE.parent.mkdir(parents=True, exist_ok=True)
        LOGIN_DONE.touch()

    if changes:
        netconf.update_file(changes)
    return netconf.apply()


if __name__ == "__main__":
    sys.exit(main())
