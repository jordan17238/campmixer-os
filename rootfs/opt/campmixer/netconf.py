#!/usr/bin/env python3
"""CampMixer network settings.

Turns /boot/firmware/campmixer.txt into NetworkManager profiles and looks after the Wi-Fi radio.
It also hands the web app's PIN to the bridge, as a hash (how and why is in campmixer_auth.py).

  netconf.py apply                  write the profiles (runs at every start-up, before NetworkManager)
  netconf.py radio                  join a listed Wi-Fi network if one is in range, otherwise run the
                                    mixer's own network (runs after NetworkManager)
  netconf.py show [--passwords]     print the settings in use and any problems in the file
  netconf.py set KEY=VALUE ...      change lines in campmixer.txt
  netconf.py join NAME [PASSWORD]   add a Wi-Fi network to join
  netconf.py forget NAME            remove one
  netconf.py pin                    print the web app's PIN
  netconf.py pin NEW | --new        change it, to NEW or to a made-up one; every phone has to sign in again

Python standard library only.
"""
import contextlib
import grp
import json
import os
import re
import secrets
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import campmixer_auth as auth  # noqa: E402

ROOT = Path(os.environ.get("CAMPMIXER_ROOT", "/"))
TESTING = "CAMPMIXER_ROOT" in os.environ
BOOT = ROOT / "boot/firmware"
SETTINGS_FILE = BOOT / "campmixer.txt"
NM_PROFILES = ROOT / "etc/NetworkManager/system-connections"
NM_CONF = ROOT / "etc/NetworkManager/conf.d/90-campmixer.conf"
NM_STATE = ROOT / "var/lib/NetworkManager/NetworkManager.state"
WEB_KEY_FILE = ROOT / "var/lib/campmixer/web-key"  # root only; made on this Pi, never part of an image
WEB_AUTH_FILE = ROOT / "run/campmixer/web-auth.json"  # root writes it, the bridge's group may read it
BRIDGE_GROUP = os.environ.get("CAMPMIXER_BRIDGE_GROUP", "campmixer")
SYS_NET = Path(os.environ.get("CAMPMIXER_SYS_NET", "/sys/class/net"))
SYS_USB = Path(os.environ.get("CAMPMIXER_SYS_USB", str(ROOT / "sys/bus/usb/devices")))
NMCLI = os.environ.get("CAMPMIXER_NMCLI", "nmcli")
IW = os.environ.get("CAMPMIXER_IW", "iw")
TIME_SCALE = float(os.environ.get("CAMPMIXER_TIME_SCALE", "1"))

AP_ADDRESS = "10.77.7.1/24"
AP_PROFILE = "campmixer-ap"
ETH_PROFILE = "campmixer-eth"
ETH_DIRECT_PROFILE = "campmixer-eth-direct"
JOIN_PREFIX = "campmixer-join-"
JOIN_SLOTS = 4
DEFAULT_NAME = "CampMixer"
PASSWORD_LETTERS = "abcdefghjkmnpqrstuvwxyz23456789"  # none that look alike
ONBOARD_DRIVERS = ("brcmfmac",)
CHANNELS = {"2.4": tuple(range(1, 14)), "5": (36, 40, 44, 48, 149, 153, 157, 161, 165)}
DEFAULT_CHANNEL = {"2.4": 6, "5": 36}
POLL_S = 5
# With networks to join listed: how long the mixer's own network must have had nobody on it before the
# radio leaves it for a few seconds to look for one of them. Longer each time a look finds nothing.
LOOK_AFTER_IDLE_S = (60, 300, 600, 1200, 2400, 3600)
RETRY_REFUSED_S = 1800  # a listed network that would not let the mixer in is left alone this long
LOOK_NOW_FLAG = ROOT / "run/campmixer-look-now"
KNOWN_KEYS = ["WIFI_NAME", "WIFI_PASSWORD", "WIFI_BAND", "WIFI_CHANNEL", "WIFI_RADIO", "LOGIN_PASSWORD", "WEB_PIN"] + [
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


def decode_settings(data, keep_bytes=False):
    """The settings file's text, and whether all of it could be read.

    The file is edited by hand on whatever computer is nearby, and Notepad on older Windows saves in a local
    encoding rather than UTF-8. That must never stop the mixer: what cannot be read becomes a replacement
    character (or, with keep_bytes, a stand-in that turns back into the same byte when the file is rewritten).
    """
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):  # Notepad's "Unicode": UTF-16 with its marker
        with contextlib.suppress(UnicodeDecodeError):
            return data.decode("utf-16"), True
    try:
        return data.decode("utf-8-sig"), True
    except UnicodeDecodeError:
        return data.decode("utf-8-sig", errors="surrogateescape" if keep_bytes else "replace"), False


def read_settings_text():
    return decode_settings(SETTINGS_FILE.read_bytes())


def read_raw():
    return parse_file(read_settings_text()[0])


def valid_name(name):
    return 0 < len(name.encode("utf-8")) <= 32 and all(ord(ch) >= 32 and ord(ch) != 127 for ch in name)


def valid_passphrase(password):
    return 8 <= len(password) <= 63 and all(32 <= ord(ch) <= 126 for ch in password)


def valid_join_password(password):
    return password == "" or valid_passphrase(password) or bool(re.fullmatch(r"[0-9A-Fa-f]{64}", password))


def make_password():
    """A password that is easy to read off a screen and type on a phone, such as kq7m-x2np-84hd."""
    return "-".join("".join(secrets.choice(PASSWORD_LETTERS) for _ in range(4)) for _ in range(3))


class Settings:
    def __init__(self):
        self.name = DEFAULT_NAME
        self.password = None  # no usable password: the mixer's own Wi-Fi stays off
        self.band = "2.4"
        self.channel = DEFAULT_CHANNEL["2.4"]
        self.radio = "auto"
        self.pin = None  # no usable PIN: nobody can sign in to the web app
        self.joins = []  # (slot, name, password)
        self.issues = []  # (setting, what is wrong and what happens instead)

    @property
    def problems(self):
        return [text for _key, text in self.issues]


def load_settings(raw=None):
    """The settings in use. raw is the file's content if the caller has it already (or wants to try a change)."""
    settings = Settings()
    if raw is None:
        try:
            text, all_utf8 = read_settings_text()
            raw = parse_file(text)
            if not all_utf8:
                settings.issues.append(
                    ("FILE", "campmixer.txt is not saved as UTF-8, so an accent or a curly quote in a name will not match; "
                     "save it as UTF-8 or type plain characters")
                )
        except OSError as exc:
            settings.issues.append(("FILE", "cannot read %s (%s); using the built-in settings" % (SETTINGS_FILE, exc.strerror)))
            raw = {}

    name = raw.get("WIFI_NAME", "")
    if valid_name(name):
        settings.name = name
    elif name:
        settings.issues.append(("WIFI_NAME", "WIFI_NAME must be 1 to 32 characters; using %s" % DEFAULT_NAME))

    password = raw.get("WIFI_PASSWORD", "")
    if valid_passphrase(password):
        settings.password = password
    else:
        settings.issues.append(
            ("WIFI_PASSWORD", "WIFI_PASSWORD must be 8 to 63 plain characters; the mixer's own Wi-Fi is off until it is")
        )

    band = raw.get("WIFI_BAND", "2.4").lower().replace("ghz", "").strip()
    if band in ("2.4", "2", "bg"):
        settings.band = "2.4"
    elif band in ("5", "a"):
        settings.band = "5"
    else:
        settings.issues.append(("WIFI_BAND", "WIFI_BAND must be 2.4 or 5; using 2.4"))
    settings.channel = DEFAULT_CHANNEL[settings.band]
    channel = raw.get("WIFI_CHANNEL", "")
    if channel:
        if channel.isdigit() and int(channel) in CHANNELS[settings.band]:
            settings.channel = int(channel)
        else:
            settings.issues.append(
                ("WIFI_CHANNEL", "WIFI_CHANNEL %s is not a %s GHz channel; using %d" % (channel, settings.band, settings.channel))
            )

    radio = raw.get("WIFI_RADIO", "auto").lower() or "auto"
    if radio in ("auto", "usb", "onboard"):
        settings.radio = radio
    else:
        settings.issues.append(("WIFI_RADIO", "WIFI_RADIO must be auto, usb or onboard; using auto"))

    pin = auth.tidy_pin(raw.get("WEB_PIN", ""))
    pin_problem = auth.pin_problem(pin)
    if pin_problem is None:
        settings.pin = pin
    else:
        settings.issues.append(("WEB_PIN", "WEB_PIN %s; nobody can sign in to the web app until it is changed" % pin_problem))

    for slot in range(1, JOIN_SLOTS + 1):
        join_name = raw.get("JOIN_WIFI_%d" % slot, "")
        join_password = raw.get("JOIN_WIFI_%d_PASSWORD" % slot, "")
        if not join_name:
            continue
        key = "JOIN_WIFI_%d" % slot
        if not valid_name(join_name):
            settings.issues.append((key, "%s is not a usable network name; skipped" % key))
        elif join_name == settings.name:
            settings.issues.append((key, "%s is the mixer's own network name; skipped" % key))
        elif not valid_join_password(join_password):
            settings.issues.append((key + "_PASSWORD", "%s_PASSWORD must be 8 to 63 characters, or blank; skipped" % key))
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
            for line in Path("/proc/mounts").read_text(errors="replace").splitlines():
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
    data = text.encode("utf-8", errors="surrogateescape")  # bytes that were not UTF-8 go back out as they came in
    with contextlib.suppress(OSError):
        if path.read_bytes() == data and (mode is None or (path.stat().st_mode & 0o777) == mode):
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if mode is None else mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temp, mode)
        os.replace(temp, path)
    except OSError:
        with contextlib.suppress(OSError):
            temp.unlink()
        raise
    return True


def update_file(changes):
    """Change KEY=VALUE lines in campmixer.txt and keep every other line as it was."""
    try:
        # Lines that are not being changed are written back as they were, whatever encoding they are in.
        lines = decode_settings(SETTINGS_FILE.read_bytes(), keep_bytes=True)[0].splitlines()
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


def ensure_made_up():
    """Never run on a password or a PIN that is written down somewhere public, or that anyone would guess.

    A blank or unusable WIFI_PASSWORD or WEB_PIN is replaced by a made-up one, saved in campmixer.txt for the
    mixer's owner to read. Nothing of the kind is built into the image.
    """
    try:
        raw = read_raw()
    except OSError:
        raw = {}
    changes, notes = {}, []
    password = raw.get("WIFI_PASSWORD", "")
    if not valid_passphrase(password):
        changes["WIFI_PASSWORD"] = make_password()
        notes.append("WIFI_PASSWORD was %s" % ("not usable" if password else "blank"))
    pin = auth.tidy_pin(raw.get("WEB_PIN", ""))
    if auth.pin_problem(pin):
        changes["WEB_PIN"] = auth.make_pin()
        notes.append("WEB_PIN was %s" % ("not usable or too easy to guess" if pin else "blank"))
    if not changes:
        return
    try:
        update_file(changes)
    except (OSError, subprocess.SubprocessError) as exc:
        log("could not save a new %s in %s (%s)" % (" and ".join(changes), SETTINGS_FILE, exc))
        return
    if len(notes) == 1:
        log("%s, so a new one was made up. It is in campmixer.txt" % notes[0])
    else:
        log("%s, so new ones were made up. They are in campmixer.txt" % " and ".join(notes))


# ---------------------------------------------------------------- the web app's PIN


def root_is_overlay():
    """True when the card is locked: whatever is written to the system now is gone at the next start."""
    locked = False
    if not TESTING:
        with contextlib.suppress(OSError):
            for line in Path("/proc/self/mounts").read_text(errors="replace").splitlines():
                fields = line.split()
                if len(fields) >= 3 and fields[1] == "/":
                    locked = fields[2] == "overlay"
    return locked


def web_key(create=True):
    """This Pi's own secret for the web app: 32 random bytes, readable by root only.

    Sign-in cookies are signed with a key made from it and the PIN. It is made up here the first time it is
    needed, which on a fresh image is the first start, while the card can still be written. It is never part
    of an image: a secret that everyone can download is no secret.
    """
    with contextlib.suppress(OSError, ValueError):
        key = bytes.fromhex(WEB_KEY_FILE.read_text(encoding="ascii").strip())
        if len(key) == 32:
            return key
    if not create:
        return None
    key = secrets.token_bytes(32)
    try:
        write_if_changed(WEB_KEY_FILE, key.hex() + "\n", 0o600)
    except OSError as exc:
        # Still worth running: the PIN works, phones just have to sign in again more often than they should.
        log("could not keep this Pi's key for the web app (%s); phones will have to sign in again before long" % exc)
        return key
    if root_is_overlay():
        log("the card is locked and had no key for the web app yet, so phones will have to sign in again after every "
            "restart. To fix that once: sudo campmixer unlock, restart, sudo campmixer lock, restart")
    return key


def write_web_auth(pin):
    """Give the bridge what it needs to check the PIN and recognise its own cookies, and nothing more.

    Without a usable PIN the file is taken away, and the bridge then lets nobody sign in.
    """
    if pin is None:
        with contextlib.suppress(FileNotFoundError):
            WEB_AUTH_FILE.unlink()
        return
    data = json.dumps(auth.derive(web_key(), pin), indent=1).encode("ascii") + b"\n"
    try:
        gid = grp.getgrnam(BRIDGE_GROUP).gr_gid
    except KeyError:
        gid = None
        log("there is no group called %s, so the bridge cannot be given the PIN's hash and nobody can sign in" % BRIDGE_GROUP)
    folder = WEB_AUTH_FILE.parent
    folder.mkdir(parents=True, exist_ok=True)
    shared = gid is not None and os.geteuid() == 0
    if shared:
        os.chown(folder, 0, gid)
    os.chmod(folder, 0o750)
    temp = WEB_AUTH_FILE.with_name(WEB_AUTH_FILE.name + ".tmp")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(fd, 0o600)
            if shared:
                os.fchown(fd, 0, gid)
                os.fchmod(fd, 0o640)
            handle.write(data)
        os.replace(temp, WEB_AUTH_FILE)
    except OSError:
        with contextlib.suppress(OSError):
            temp.unlink()
        raise


def apply_pin(settings):
    try:
        write_web_auth(settings.pin)
    except (OSError, ValueError) as exc:
        log("could not set up the web app's PIN (%s); nobody can sign in until that is fixed" % exc)
        with contextlib.suppress(OSError):
            WEB_AUTH_FILE.unlink()
        return False
    return settings.pin is not None


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


def nm_version():
    """NetworkManager's version as (major, minor), or None if it cannot be asked (as in the tests)."""
    text = os.environ.get("CAMPMIXER_NM_VERSION", "")
    if not text and not TESTING:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            text = subprocess.run(["NetworkManager", "--version"], capture_output=True, text=True, timeout=10).stdout
    match = re.match(r"\s*(\d+)\.(\d+)", text)
    return (int(match.group(1)), int(match.group(2))) if match else None


def eth_profiles():
    """The wired profile or profiles, as {name: text}.

    NetworkManager 1.52 and later, which is what the image has: one profile. DHCP never gives up, and for as
    long as it has no address the port has a link-local one (169.254.x.x). A cable straight to a laptop works
    within seconds, and a DHCP server that turns up later is still picked up, without touching the cable.
    (Tried on 1.52.1: link-local address 2 s after plugging in, DHCP address 20 s after a server appeared.)

    Older versions cannot do that in one profile, so they get two: one try at DHCP, then a link-local profile
    takes over and stays until the cable is next unplugged.
    """
    version = nm_version()
    if version is None or version >= (1, 52):
        return {
            ETH_PROFILE: HEADER + (
                "[connection]\nid={id}\nuuid={uuid}\ntype=ethernet\nautoconnect=true\nautoconnect-priority=50\n"
                "multi-connect=3\n\n"  # every wired port may use it at once, should a second one be added
                "[ipv4]\ndhcp-timeout=2147483647\nlink-local=4\nmay-fail=true\nmethod=auto\n\n"
                "[ipv6]\nmethod=link-local\n"
            ).format(id=ETH_PROFILE, uuid=profile_uuid(ETH_PROFILE))
        }
    return {
        ETH_PROFILE: HEADER + (
            "[connection]\nid={id}\nuuid={uuid}\ntype=ethernet\nautoconnect=true\nautoconnect-priority=50\n"
            "autoconnect-retries=1\n\n"
            "[ipv4]\ndhcp-timeout=30\nmay-fail=false\nmethod=auto\n\n"
            "[ipv6]\nmethod=auto\n"
        ).format(id=ETH_PROFILE, uuid=profile_uuid(ETH_PROFILE)),
        ETH_DIRECT_PROFILE: HEADER + (
            "[connection]\nid={id}\nuuid={uuid}\ntype=ethernet\nautoconnect=true\nautoconnect-priority=40\n\n"
            "[ipv4]\nmethod=link-local\n\n"
            "[ipv6]\nmethod=link-local\n"
        ).format(id=ETH_DIRECT_PROFILE, uuid=profile_uuid(ETH_DIRECT_PROFILE)),
    }


def wifi_devices():
    """(interface, driver) for every Wi-Fi radio the kernel knows about."""
    devices = []
    with contextlib.suppress(OSError):
        for entry in sorted(SYS_NET.iterdir()):
            if (entry / "wireless").exists() or (entry / "phy80211").exists():
                driver = os.path.basename(os.path.realpath(entry / "device" / "driver"))
                devices.append((entry.name, driver))
    return devices


def is_usb_radio(device):
    return device[1] not in ONBOARD_DRIVERS


def usb_device_plugged_in():
    """True when anything but a hub is on the USB ports. It might be a Wi-Fi adaptor that is still starting."""
    with contextlib.suppress(OSError):
        for entry in SYS_USB.iterdir():
            with contextlib.suppress(OSError):
                if (entry / "bDeviceClass").read_text().strip() != "09":
                    return True
    return False


def choose_radio(policy, devices):
    """Which radio carries the Wi-Fi, and which NetworkManager must leave alone."""
    usb = [dev for dev in devices if is_usb_radio(dev)]
    onboard = [dev for dev in devices if not is_usb_radio(dev)]
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


def apply(starting_up=False):
    ensure_made_up()
    settings = load_settings()
    for problem in settings.problems:
        log("settings: " + problem)
    apply_pin(settings)
    if starting_up and not TESTING:
        # Let a USB Wi-Fi adaptor finish appearing before deciding which radio to use.
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(["udevadm", "settle", "--timeout=5"], check=False, timeout=10)
    devices = wifi_devices()
    if starting_up and settings.radio == "auto" and not any(map(is_usb_radio, devices)) and usb_device_plugged_in():
        # Something is plugged in but no USB radio has shown up yet. An adaptor can take a few seconds
        # longer than the built-in radio, and picking the built-in one by mistake would cost the range.
        log("something is plugged into USB; giving a Wi-Fi adaptor up to 6 s to appear")
        for _ in range(12):
            pause(0.5)
            devices = wifi_devices()
            if any(map(is_usb_radio, devices)):
                break
    chosen, unmanaged = choose_radio(settings.radio, devices)
    found = ", ".join("%s (%s)" % dev for dev in devices) or "none"
    log("radios found: %s; using %s" % (found, "%s (%s)" % chosen if chosen else "none"))

    wanted = eth_profiles()
    if settings.password:
        wanted[AP_PROFILE] = ap_profile(settings)
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
        state = NM_STATE.read_text(errors="replace")
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


def on_own(state):
    return bool(state) and state[2] == AP_PROFILE and state[1] == "connected"


def start_own(device):
    if load_settings().password is None:
        log("the mixer's own Wi-Fi is off: WIFI_PASSWORD in campmixer.txt is not usable")
        return False
    if on_own(wifi_state()):
        return True
    code, out = nmcli("--wait", "30", "connection", "up", "id", AP_PROFILE, "ifname", device, timeout=45)
    if code != 0:
        log("could not start the mixer's own Wi-Fi: " + out.strip())
    else:
        log("running the mixer's own Wi-Fi")
    return code == 0


def stations(device):
    """How many phones are on the mixer's own network, or None if that cannot be told."""
    try:
        done = subprocess.run([IW, "dev", device, "station", "dump"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    return sum(1 for line in done.stdout.splitlines() if line.startswith("Station "))


def scan(device):
    """Names of the Wi-Fi networks in range. Empty if the radio would not look."""
    out = ""
    for _ in range(5):
        code, out = nmcli("-t", "-f", "SSID", "device", "wifi", "list", "ifname", device, "--rescan", "yes", timeout=30)
        if code == 0:
            return {split_terse(line)[0] for line in out.splitlines() if line}
        pause(2)  # NetworkManager refuses to scan while a connection is still coming up or going down
    log("the radio would not scan: " + out.strip())
    return set()


def settle(device):
    """Let NetworkManager finish what it does by itself at start-up.

    It joins a listed network if it can see one and starts the mixer's own if it cannot. Stepping in while
    it is still busy would cut a slow join short, so this only watches, and acts if nothing comes of it.
    """
    quiet = 0
    for _ in range(90):
        state = wifi_state()
        if state and state[1] == "connected":
            return
        if state and state[1].startswith("connecting"):
            quiet = 0
        else:
            quiet += 1
            if quiet >= 20:
                break
        pause(1)
    log("NetworkManager has not brought any Wi-Fi up by itself")
    start_own(device)


def look(device, candidates):
    """Look for a listed network, which a radio cannot do while it is an access point.

    Returns (profile, name, joined). With nothing joined, the mixer's own network is running again.
    """
    nmcli("connection", "down", "id", AP_PROFILE)
    visible = scan(device)
    target = next(((profile, name) for profile, name in candidates if name in visible), None)
    if not target:
        log("none of the listed Wi-Fi networks is in range")
        start_own(device)
        return None, None, False
    log("found %s; joining it" % target[1])
    code, out = nmcli("--wait", "45", "connection", "up", "id", target[0], "ifname", device, timeout=60)
    if code != 0:
        log("could not join %s: %s" % (target[1], out.strip()))
        start_own(device)
    return target[0], target[1], code == 0


def keep_wifi(device, joins, look_now=False):
    """The radio's job when there are networks to join. Never returns unless a test asks it to.

    On a listed network: stay, and start the mixer's own if it is lost for 30 s.
    On the mixer's own: once nobody has been connected for a while, take a look for a listed network.
    A look takes the own network away for some seconds, so it waits longer each time it finds nothing.
    """
    names = dict(joins)
    retry_at = {}  # profile -> when a network that refused the mixer may be tried again
    clock = idle = lost = looks = 0
    was = None
    rounds = int(os.environ.get("CAMPMIXER_RADIO_ROUNDS", "0"))  # tests only: stop after this many turns
    turn = 0
    while True:
        state = wifi_state()
        if state:
            device = state[0]  # a USB adaptor that was unplugged and put back can return under another name
        if joined(state):
            if was != state[2]:
                log("on the listed Wi-Fi network %s" % names.get(state[2], state[2]))
            was = state[2]
            idle = lost = looks = 0
        elif on_own(state) or look_now:
            if was != AP_PROFILE and not look_now:
                log("on the mixer's own Wi-Fi")
            was = AP_PROFILE
            lost = 0
            candidates = [(profile, name) for profile, name in joins if retry_at.get(profile, 0) <= clock]
            # Zero means nobody. None means it cannot be told, and then the network is left alone.
            idle = idle + POLL_S if stations(device) == 0 else 0
            due = idle >= LOOK_AFTER_IDLE_S[min(looks, len(LOOK_AFTER_IDLE_S) - 1)]
            if (due or look_now) and candidates:
                if look_now:
                    log("the list of networks has changed; looking for one of them")
                else:
                    log("nobody has been on the mixer's own Wi-Fi for %d s; looking for a listed network" % idle)
                    looks += 1
                profile, _name, ok = look(device, candidates)
                if profile and not ok:
                    retry_at[profile] = clock + RETRY_REFUSED_S
                idle = 0
                was = None
        else:
            # Neither. NetworkManager may be reconnecting by itself; give it 30 s before stepping in.
            lost += POLL_S
            if lost >= 30:
                log("no Wi-Fi connection for 30 s; starting the mixer's own")
                start_own(device)
                lost = 0
        look_now = False
        turn += 1
        if rounds and turn >= rounds:
            return 0
        pause(POLL_S)
        clock += POLL_S


def radio():
    settings = load_settings()
    look_now = False
    with contextlib.suppress(OSError):
        LOOK_NOW_FLAG.unlink()  # left by "campmixer wifi join": the list has just changed, so look straight away
        look_now = True
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
            if on_own(wifi_state()):
                return 0
            pause(1)
        start_own(device)
        return 0
    try:
        if not look_now:
            settle(device)
        return keep_wifi(device, joins, look_now)
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
    print("Own Wi-Fi password: " + (secret(settings.password) if settings.password else "(not set, so the own Wi-Fi is off)"))
    print("Band and channel:   %s GHz, channel %d" % (settings.band, settings.channel))
    print("Radio:              " + settings.radio)
    print("Web app PIN:        " + (secret(settings.pin) if settings.pin else "(not set, so nobody can sign in)"))
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
    try:
        raw = read_raw()
    except OSError:
        raw = {}
    # Refuse a value the mixer could not use, rather than save it and fall back to something else.
    touched = set(changes) | ({"WIFI_CHANNEL"} if "WIFI_BAND" in changes else set())
    refused = [text for key, text in load_settings({**raw, **changes}).issues if key in touched]
    if refused:
        for text in refused:
            print("Not changed: " + text.split(";")[0] + ".", file=sys.stderr)
        return 2
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


def pin_command(rest):
    if not rest:
        pin = load_settings().pin
        print(pin if pin else "No usable PIN is set, so nobody can sign in. Make one up with: sudo campmixer pin --new")
        return 0 if pin else 1
    new = auth.make_pin() if rest == ["--new"] else auth.tidy_pin(rest[0])
    problem = auth.pin_problem(new)
    if problem:
        print("Not changed: the PIN %s." % problem, file=sys.stderr)
        return 2
    update_file({"WEB_PIN": new})
    settings = load_settings()
    if settings.pin != new or not apply_pin(settings):
        print("PROBLEM: the new PIN is saved in campmixer.txt but could not be put to use. See: campmixer logs", file=sys.stderr)
        return 1
    print("The PIN is now %s. Every phone has to sign in again with it." % new)
    return 0


def finish_change():
    code = apply()
    for problem in load_settings().problems:
        print("PROBLEM: " + problem)
    return code


def main(argv):
    command = argv[1] if len(argv) > 1 else ""
    rest = argv[2:]
    if command == "apply" and not rest:
        return apply(starting_up=True)
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
    if command == "pin" and len(rest) <= 1:
        return pin_command(rest)
    if command == "web-key" and not rest:  # make this Pi's key for the web app now, while the card can be written
        web_key()
        return 0
    print(__doc__.strip(), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
