#!/usr/bin/env python3
"""CampMixer network settings.

Turns /boot/firmware/campmixer.txt into NetworkManager profiles and looks after the Wi-Fi radio.

  netconf.py apply                  write the profiles (runs at every start-up, before NetworkManager)
  netconf.py radio                  join a listed Wi-Fi network if one is in range, otherwise run the
                                    mixer's own network (runs after NetworkManager)
  netconf.py show [--passwords]     print the settings in use and any problems in the file
  netconf.py set KEY=VALUE ...      change lines in campmixer.txt
  netconf.py join NAME [PASSWORD]   add a Wi-Fi network to join
  netconf.py forget NAME            remove one

Python standard library only.
"""
import contextlib
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(os.environ.get("CAMPMIXER_ROOT", "/"))
TESTING = "CAMPMIXER_ROOT" in os.environ
BOOT = ROOT / "boot/firmware"
SETTINGS_FILE = BOOT / "campmixer.txt"
NM_PROFILES = ROOT / "etc/NetworkManager/system-connections"
NM_CONF = ROOT / "etc/NetworkManager/conf.d/90-campmixer.conf"
NM_STATE = ROOT / "var/lib/NetworkManager/NetworkManager.state"
SYS_NET = Path(os.environ.get("CAMPMIXER_SYS_NET", "/sys/class/net"))
NMCLI = os.environ.get("CAMPMIXER_NMCLI", "nmcli")
TIME_SCALE = float(os.environ.get("CAMPMIXER_TIME_SCALE", "1"))

AP_ADDRESS = "10.77.7.1/24"
AP_PROFILE = "campmixer-ap"
ETH_PROFILE = "campmixer-eth"
ETH_DIRECT_PROFILE = "campmixer-eth-direct"
JOIN_PREFIX = "campmixer-join-"
JOIN_SLOTS = 4
DEFAULT_NAME = "CampMixer"
FALLBACK_PASSWORD = "campmixer-setup"
ONBOARD_DRIVERS = ("brcmfmac",)
CHANNELS = {"2.4": tuple(range(1, 14)), "5": (36, 40, 44, 48, 149, 153, 157, 161, 165)}
DEFAULT_CHANNEL = {"2.4": 6, "5": 36}
KNOWN_KEYS = ["WIFI_NAME", "WIFI_PASSWORD", "WIFI_BAND", "WIFI_CHANNEL", "WIFI_RADIO", "LOGIN_PASSWORD"] + [
    key for slot in range(1, JOIN_SLOTS + 1) for key in ("JOIN_WIFI_%d" % slot, "JOIN_WIFI_%d_PASSWORD" % slot)
]
# Fixed namespace, so each profile keeps the same identity from one start-up to the next.
UUID_SPACE = uuid.UUID("6f1d3c52-7a1e-4c0b-9a57-63616d706d78")
HEADER = "# Written by CampMixer from campmixer.txt at every start-up. Edit that file, not this one.\n"


def log(message):
    print("campmixer-net: " + message, file=sys.stderr, flush=True)


def pause(seconds):
    time.sleep(seconds * TIME_SCALE)


# ---------------------------------------------------------------- settings file


def parse_file(text):
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip().upper()] = value
    return values


def read_raw():
    return parse_file(SETTINGS_FILE.read_text(encoding="utf-8-sig", errors="replace"))


def valid_name(name):
    return 0 < len(name.encode("utf-8")) <= 32 and all(ord(ch) >= 32 and ord(ch) != 127 for ch in name)


def valid_passphrase(password):
    return 8 <= len(password) <= 63 and all(32 <= ord(ch) <= 126 for ch in password)


def valid_join_password(password):
    return password == "" or valid_passphrase(password) or bool(re.fullmatch(r"[0-9A-Fa-f]{64}", password))


class Settings:
    def __init__(self):
        self.name = DEFAULT_NAME
        self.password = FALLBACK_PASSWORD
        self.band = "2.4"
        self.channel = DEFAULT_CHANNEL["2.4"]
        self.radio = "auto"
        self.joins = []  # (slot, name, password)
        self.problems = []


def load_settings():
    settings = Settings()
    try:
        raw = read_raw()
    except OSError as exc:
        settings.problems.append("cannot read %s (%s); using the built-in settings" % (SETTINGS_FILE, exc.strerror))
        raw = {}

    name = raw.get("WIFI_NAME", "")
    if valid_name(name):
        settings.name = name
    elif name:
        settings.problems.append("WIFI_NAME must be 1 to 32 characters; using %s" % DEFAULT_NAME)

    password = raw.get("WIFI_PASSWORD", "")
    if valid_passphrase(password):
        settings.password = password
    else:
        settings.problems.append(
            "WIFI_PASSWORD must be 8 to 63 plain characters; using the fallback password %s" % FALLBACK_PASSWORD
        )

    band = raw.get("WIFI_BAND", "2.4").lower().replace("ghz", "").strip()
    if band in ("2.4", "2", "bg"):
        settings.band = "2.4"
    elif band in ("5", "a"):
        settings.band = "5"
    else:
        settings.problems.append("WIFI_BAND must be 2.4 or 5; using 2.4")
    settings.channel = DEFAULT_CHANNEL[settings.band]
    channel = raw.get("WIFI_CHANNEL", "")
    if channel:
        if channel.isdigit() and int(channel) in CHANNELS[settings.band]:
            settings.channel = int(channel)
        else:
            settings.problems.append(
                "WIFI_CHANNEL %s is not a %s GHz channel; using %d" % (channel, settings.band, settings.channel)
            )

    radio = raw.get("WIFI_RADIO", "auto").lower() or "auto"
    if radio in ("auto", "usb", "onboard"):
        settings.radio = radio
    else:
        settings.problems.append("WIFI_RADIO must be auto, usb or onboard; using auto")

    for slot in range(1, JOIN_SLOTS + 1):
        join_name = raw.get("JOIN_WIFI_%d" % slot, "")
        join_password = raw.get("JOIN_WIFI_%d_PASSWORD" % slot, "")
        if not join_name:
            continue
        if not valid_name(join_name):
            settings.problems.append("JOIN_WIFI_%d is not a usable network name; skipped" % slot)
        elif join_name == settings.name:
            settings.problems.append("JOIN_WIFI_%d is the mixer's own network name; skipped" % slot)
        elif not valid_join_password(join_password):
            settings.problems.append("JOIN_WIFI_%d_PASSWORD must be 8 to 63 characters, or blank; skipped" % slot)
        else:
            settings.joins.append((slot, join_name, join_password))
    return settings


def file_value(value):
    if value != value.strip() or value[:1] in ("'", '"'):
        return '"%s"' % value
    return value


@contextlib.contextmanager
def writable_boot():
    """The boot partition is read-only when the mixer is locked. Open it just long enough to write."""
    read_only = False
    if not TESTING:
        with contextlib.suppress(OSError):
            for line in Path("/proc/mounts").read_text().splitlines():
                fields = line.split()
                if len(fields) >= 4 and fields[1] == str(BOOT):
                    read_only = "ro" in fields[3].split(",")
    if read_only:
        subprocess.run(["mount", "-o", "remount,rw", str(BOOT)], check=True)
    try:
        yield
    finally:
        if read_only:
            subprocess.run(["sync"], check=False)
            subprocess.run(["mount", "-o", "remount,ro", str(BOOT)], check=False)


def write_if_changed(path, text, mode=None):
    """Write a file only when its content differs, so an unchanged start-up writes nothing to the card.

    mode is left out for files on the boot partition: FAT has no permissions to set.
    """
    with contextlib.suppress(OSError):
        if path.read_text() == text and (mode is None or (path.stat().st_mode & 0o777) == mode):
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if mode is None else mode)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    if mode is not None:
        os.chmod(temp, mode)
    os.replace(temp, path)
    return True


def update_file(changes):
    """Change KEY=VALUE lines in campmixer.txt and keep every other line as it was."""
    try:
        lines = SETTINGS_FILE.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    except OSError:
        lines = ["# CampMixer settings"]
    pending = dict(changes)
    out = []
    for line in lines:
        match = re.match(r"\s*([A-Za-z0-9_]+)\s*=", line)
        key = match.group(1).upper() if match else None
        if key in pending:
            out.append("%s=%s" % (key, file_value(pending.pop(key))))
        else:
            out.append(line)
    for key, value in pending.items():
        out.append("%s=%s" % (key, file_value(value)))
    with writable_boot():
        write_if_changed(SETTINGS_FILE, "\n".join(out) + "\n")


# ---------------------------------------------------------------- NetworkManager profiles


def kf_string(value):
    """Escape a value the way GLib key files (NetworkManager's profile format) expect."""
    escaped = value.replace("\\", "\\\\")
    body = escaped.lstrip(" ")
    return "\\s" * (len(escaped) - len(body)) + body


def kf_ssid(name):
    """A network name as NetworkManager writes it: text if plain ASCII, otherwise a list of bytes."""
    data = name.encode("utf-8")
    if all(32 <= byte <= 126 for byte in data):
        return kf_string(name.replace(";", "\\;"))
    return "".join("%d;" % byte for byte in data)


def profile_uuid(profile_id):
    return str(uuid.uuid5(UUID_SPACE, profile_id))


def ap_profile(settings):
    return HEADER + (
        "[connection]\nid={id}\nuuid={uuid}\ntype=wifi\nautoconnect=true\nautoconnect-priority=100\n\n"
        "[wifi]\nband={band}\nchannel={channel}\nmode=ap\npowersave=2\nssid={ssid}\n\n"
        "[wifi-security]\ngroup=ccmp;\nkey-mgmt=wpa-psk\npairwise=ccmp;\nproto=rsn;\npsk={psk}\n\n"
        "[ipv4]\naddress1={address}\nmethod=shared\n\n"
        "[ipv6]\nmethod=disabled\n"
    ).format(
        id=AP_PROFILE,
        uuid=profile_uuid(AP_PROFILE),
        band="bg" if settings.band == "2.4" else "a",
        channel=settings.channel,
        ssid=kf_ssid(settings.name),
        psk=kf_string(settings.password),
        address=AP_ADDRESS,
    )


def join_profile(slot, name, password):
    profile_id = "%s%d" % (JOIN_PREFIX, slot)
    security = "[wifi-security]\nkey-mgmt=wpa-psk\npsk=%s\n\n" % kf_string(password) if password else ""
    return HEADER + (
        "[connection]\nid={id}\nuuid={uuid}\ntype=wifi\nautoconnect=true\nautoconnect-priority={priority}\n\n"
        "[wifi]\nmode=infrastructure\nssid={ssid}\n\n"
        "{security}"
        "[ipv4]\nmethod=auto\n\n"
        "[ipv6]\nmethod=auto\n"
    ).format(id=profile_id, uuid=profile_uuid(profile_id), priority=200 - slot, ssid=kf_ssid(name), security=security)


def eth_profile():
    # One try at DHCP. If nothing answers in 30 s, campmixer-eth-direct takes over.
    return HEADER + (
        "[connection]\nid={id}\nuuid={uuid}\ntype=ethernet\nautoconnect=true\nautoconnect-priority=50\n"
        "autoconnect-retries=1\n\n"
        "[ipv4]\ndhcp-timeout=30\nmay-fail=false\nmethod=auto\n\n"
        "[ipv6]\nmethod=auto\n"
    ).format(id=ETH_PROFILE, uuid=profile_uuid(ETH_PROFILE))


def eth_direct_profile():
    # A cable straight to a laptop: both ends pick a 169.254.x.x address and mixer.local still works.
    return HEADER + (
        "[connection]\nid={id}\nuuid={uuid}\ntype=ethernet\nautoconnect=true\nautoconnect-priority=40\n\n"
        "[ipv4]\nmethod=link-local\n\n"
        "[ipv6]\nmethod=link-local\n"
    ).format(id=ETH_DIRECT_PROFILE, uuid=profile_uuid(ETH_DIRECT_PROFILE))


def wifi_devices():
    """(interface, driver) for every Wi-Fi radio the kernel knows about."""
    devices = []
    with contextlib.suppress(OSError):
        for entry in sorted(SYS_NET.iterdir()):
            if (entry / "wireless").exists() or (entry / "phy80211").exists():
                driver = os.path.basename(os.path.realpath(entry / "device" / "driver"))
                devices.append((entry.name, driver))
    return devices


def choose_radio(policy, devices):
    """Which radio carries the Wi-Fi, and which NetworkManager must leave alone."""
    usb = [dev for dev in devices if dev[1] not in ONBOARD_DRIVERS]
    onboard = [dev for dev in devices if dev[1] in ONBOARD_DRIVERS]
    hands_off_onboard = ",".join("driver:" + driver for driver in ONBOARD_DRIVERS)
    hands_off_usb = "type:wifi," + ",".join("except:driver:" + driver for driver in ONBOARD_DRIVERS)
    if policy == "usb":
        return (usb[0] if usb else None), hands_off_onboard
    if policy == "onboard":
        return (onboard[0] if onboard else None), hands_off_usb
    if usb:
        return usb[0], hands_off_onboard
    return (onboard[0] if onboard else None), None


def nm_conf(unmanaged):
    lines = ["# Written by CampMixer at every start-up. Changes made here are overwritten.", ""]
    if unmanaged:
        lines += ["[device-campmixer-spare-radio]", "match-device=" + unmanaged, "managed=0", ""]
    return "\n".join(lines)


def apply():
    settings = load_settings()
    for problem in settings.problems:
        log("settings: " + problem)
    if not TESTING:
        # Let a USB Wi-Fi adaptor finish appearing before deciding which radio to use.
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(["udevadm", "settle", "--timeout=5"], check=False, timeout=10)
    devices = wifi_devices()
    chosen, unmanaged = choose_radio(settings.radio, devices)
    found = ", ".join("%s (%s)" % dev for dev in devices) or "none"
    log("radios found: %s; using %s" % (found, "%s (%s)" % chosen if chosen else "none"))

    wanted = {AP_PROFILE: ap_profile(settings), ETH_PROFILE: eth_profile(), ETH_DIRECT_PROFILE: eth_direct_profile()}
    for slot, name, password in settings.joins:
        wanted["%s%d" % (JOIN_PREFIX, slot)] = join_profile(slot, name, password)
    NM_PROFILES.mkdir(parents=True, exist_ok=True)
    for stale in NM_PROFILES.glob("campmixer-*.nmconnection"):
        if stale.name[: -len(".nmconnection")] not in wanted:
            stale.unlink()
    for profile_id, text in wanted.items():
        write_if_changed(NM_PROFILES / (profile_id + ".nmconnection"), text, 0o600)
    write_if_changed(NM_CONF, nm_conf(unmanaged), 0o644)

    # The mixer's whole job on this radio is Wi-Fi, so never start with it switched off.
    with contextlib.suppress(OSError):
        state = NM_STATE.read_text()
        if "WirelessEnabled=false" in state:
            write_if_changed(NM_STATE, state.replace("WirelessEnabled=false", "WirelessEnabled=true"), 0o644)
    return 0


# ---------------------------------------------------------------- the radio's job


def nmcli(*args, timeout=40):
    try:
        done = subprocess.run(
            [NMCLI, *args], capture_output=True, text=True, timeout=timeout, env=dict(os.environ, LC_ALL="C")
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return done.returncode, done.stdout + done.stderr


def split_terse(line):
    """Fields of one line of 'nmcli -t' output: ':' separates them and a backslash escapes."""
    fields, current, escaped = [], [], False
    for char in line:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ":":
            fields.append("".join(current))
            current = []
        else:
            current.append(char)
    fields.append("".join(current))
    return fields


def wifi_state():
    """(device, state, profile) for the Wi-Fi radio NetworkManager is running, or None."""
    code, out = nmcli("-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status")
    if code != 0:
        return None
    for line in out.splitlines():
        fields = split_terse(line)
        if len(fields) >= 4 and fields[1] == "wifi" and fields[2] not in ("unmanaged", "unavailable"):
            return fields[0], fields[2], fields[3]
    return None


def joined(state):
    return bool(state) and state[2].startswith(JOIN_PREFIX) and state[1] == "connected"


def start_own(device):
    state = wifi_state()
    if state and state[2] == AP_PROFILE and state[1] == "connected":
        return True
    code, out = nmcli("--wait", "30", "connection", "up", "id", AP_PROFILE, "ifname", device, timeout=45)
    if code != 0:
        log("could not start the mixer's own Wi-Fi: " + out.strip())
    else:
        log("running the mixer's own Wi-Fi")
    return code == 0


def follow_venue(device, joins):
    # NetworkManager may already be joining one of the listed networks by itself.
    state = None
    for _ in range(10):
        state = wifi_state()
        if state and state[1] == "connected":
            break
        pause(1)
    if not joined(state):
        if state and state[2] == AP_PROFILE:
            nmcli("connection", "down", "id", AP_PROFILE)  # a radio that is an access point cannot look around
            pause(1)
        _code, out = nmcli("-t", "-f", "SSID", "device", "wifi", "list", "ifname", device, "--rescan", "yes", timeout=30)
        visible = {split_terse(line)[0] for line in out.splitlines() if line}
        target = next(((profile, name) for profile, name in joins if name in visible), None)
        connected = False
        if target:
            log("found %s; joining it" % target[1])
            code, out = nmcli("--wait", "40", "connection", "up", "id", target[0], "ifname", device, timeout=55)
            connected = code == 0
            if not connected:
                log("could not join %s: %s" % (target[1], out.strip()))
        else:
            log("none of the listed Wi-Fi networks is in range")
        if not connected:
            start_own(device)
            return 0

    log("on a listed Wi-Fi network; watching the connection")
    lost = 0
    while True:
        pause(5)
        state = wifi_state()
        if joined(state):
            lost = 0
            continue
        if state and state[2] == AP_PROFILE and state[1] == "connected":
            log("NetworkManager has switched to the mixer's own Wi-Fi")
            return 0
        lost += 5
        if lost >= 30:
            log("the Wi-Fi network has been gone for 30 s; starting the mixer's own")
            start_own(device)
            return 0


def radio():
    settings = load_settings()
    state = None
    for _ in range(30):
        state = wifi_state()
        if state:
            break
        pause(1)
    if not state:
        log("no Wi-Fi radio is available; Ethernet only")
        return 0
    device = state[0]
    joins = [("%s%d" % (JOIN_PREFIX, slot), name) for slot, name, _password in settings.joins]
    if not joins:
        # NetworkManager starts the mixer's own network by itself. Step in only if it has not.
        for _ in range(20):
            state = wifi_state()
            if state and state[1] == "connected":
                return 0
            pause(1)
        start_own(device)
        return 0
    try:
        return follow_venue(device, joins)
    except Exception as exc:  # whatever goes wrong, do not leave the mixer without Wi-Fi
        log("unexpected error (%s); starting the mixer's own Wi-Fi" % exc)
        start_own(device)
        return 1


# ---------------------------------------------------------------- commands


def show(with_passwords):
    settings = load_settings()

    def secret(value):
        return value if with_passwords else ("(none)" if not value else "*" * 8)

    print("Own Wi-Fi name:     " + settings.name)
    print("Own Wi-Fi password: " + secret(settings.password))
    print("Band and channel:   %s GHz, channel %d" % (settings.band, settings.channel))
    print("Radio:              " + settings.radio)
    if settings.joins:
        for slot, name, password in settings.joins:
            print("Joins when in range: %s (slot %d, password %s)" % (name, slot, secret(password)))
    else:
        print("Joins when in range: nothing listed")
    for problem in settings.problems:
        print("PROBLEM: " + problem)
    return 1 if settings.problems else 0


def set_values(pairs):
    changes = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        key = key.strip().upper()
        if not sep or key not in KNOWN_KEYS:
            print("Not a setting: %s. Settings are: %s" % (pair, ", ".join(KNOWN_KEYS)), file=sys.stderr)
            return 2
        changes[key] = value
    update_file(changes)
    return finish_change()


def join(name, password):
    if not valid_name(name) or not valid_join_password(password):
        print("The name must be 1 to 32 characters and the password 8 to 63, or blank for an open network.", file=sys.stderr)
        return 2
    try:
        raw = read_raw()
    except OSError:
        raw = {}
    slots = range(1, JOIN_SLOTS + 1)
    slot = next((n for n in slots if raw.get("JOIN_WIFI_%d" % n, "") == name), None)
    if slot is None:
        slot = next((n for n in slots if not raw.get("JOIN_WIFI_%d" % n, "")), None)
    if slot is None:
        print("All %d slots are in use. Forget one first." % JOIN_SLOTS, file=sys.stderr)
        return 2
    update_file({"JOIN_WIFI_%d" % slot: name, "JOIN_WIFI_%d_PASSWORD" % slot: password})
    return finish_change()


def forget(name):
    try:
        raw = read_raw()
    except OSError:
        raw = {}
    changes = {}
    for slot in range(1, JOIN_SLOTS + 1):
        if raw.get("JOIN_WIFI_%d" % slot, "") == name:
            changes["JOIN_WIFI_%d" % slot] = ""
            changes["JOIN_WIFI_%d_PASSWORD" % slot] = ""
    if not changes:
        print("No listed network is called %s." % name, file=sys.stderr)
        return 2
    update_file(changes)
    return finish_change()


def finish_change():
    code = apply()
    for problem in load_settings().problems:
        print("PROBLEM: " + problem)
    return code


def main(argv):
    command = argv[1] if len(argv) > 1 else ""
    rest = argv[2:]
    if command == "apply" and not rest:
        return apply()
    if command == "radio" and not rest:
        return radio()
    if command == "show" and rest in ([], ["--passwords"]):
        return show(bool(rest))
    if command == "set" and rest:
        return set_values(rest)
    if command == "join" and len(rest) in (1, 2):
        return join(rest[0], rest[1] if len(rest) == 2 else "")
    if command == "forget" and len(rest) == 1:
        return forget(rest[0])
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
