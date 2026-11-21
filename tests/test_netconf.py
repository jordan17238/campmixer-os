#!/usr/bin/env python3
"""Checks netconf.py against a pretend system: settings file in, NetworkManager profiles out.

If nmcli is installed, every generated profile is also read back by NetworkManager's own parser
and the name and password escaping is compared with what nmcli itself writes.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
NETCONF = os.path.join(HERE, "..", "rootfs", "opt", "campmixer", "netconf.py")
FAKE_NMCLI = os.path.join(HERE, "fake_nmcli.py")
os.chmod(FAKE_NMCLI, 0o755)
NMCLI = shutil.which("nmcli")
BASE = "WIFI_NAME=CampMixer\nWIFI_PASSWORD=abcd-efgh-jkmn\n"
ONBOARD, USB = ("wlan0", "brcmfmac"), ("wlan1", "mt76x2u")
MADE_UP = r"[a-z2-9]{4}-[a-z2-9]{4}-[a-z2-9]{4}"


def make_root(settings, radios):
    root = tempfile.mkdtemp(prefix="campmixer-test-")
    os.makedirs(root + "/boot/firmware")
    if settings is not None:
        with open(root + "/boot/firmware/campmixer.txt", "w") as handle:
            handle.write(settings)
    os.makedirs(root + "/var/lib/NetworkManager")
    for name, driver in [("eth0", None)] + list(radios):
        add_interface(root, name, driver)
    return root


def add_interface(root, name, driver):
    os.makedirs("%s/sys/class/net/%s.new/device" % (root, name))
    if driver:
        os.makedirs("%s/sys/class/net/%s.new/wireless" % (root, name))
        os.makedirs("%s/sys/bus/drivers/%s" % (root, driver), exist_ok=True)
        os.symlink("%s/sys/bus/drivers/%s" % (root, driver), "%s/sys/class/net/%s.new/device/driver" % (root, name))
    os.rename("%s/sys/class/net/%s.new" % (root, name), "%s/sys/class/net/%s" % (root, name))  # appears complete


def plug_into_usb(root, port, device_class):
    os.makedirs("%s/sys/bus/usb/devices/%s" % (root, port))
    with open("%s/sys/bus/usb/devices/%s/bDeviceClass" % (root, port), "w") as handle:
        handle.write(device_class + "\n")


def settings_file(root):
    with open(root + "/boot/firmware/campmixer.txt") as handle:
        return handle.read()


def run(root, *args, **extra_env):
    env = dict(os.environ, CAMPMIXER_ROOT=root, CAMPMIXER_SYS_NET=root + "/sys/class/net", **extra_env)
    return subprocess.run([sys.executable, NETCONF, *args], env=env, capture_output=True, text=True)


def profile_path(root, name):
    return "%s/etc/NetworkManager/system-connections/%s.nmconnection" % (root, name)


def profile(root, name):
    with open(profile_path(root, name)) as handle:
        text = handle.read()
    sections, current = {}, None
    for line in text.splitlines():
        if line.startswith("["):
            current = sections.setdefault(line.strip("[]"), {})
        elif "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            current[key] = value
    return sections


def profiles(root):
    return sorted(name[:-13] for name in os.listdir(root + "/etc/NetworkManager/system-connections"))


def conf(root):
    with open(root + "/etc/NetworkManager/conf.d/90-campmixer.conf") as handle:
        return handle.read()


def nm_reads(path):
    """True if NetworkManager's own parser accepts the profile."""
    with open(path) as handle:
        done = subprocess.run([NMCLI, "--offline", "connection", "modify", "connection.zone", ""],
                              stdin=handle, capture_output=True, text=True)
    return done.returncode == 0, done.stdout + done.stderr


# --- 1. plain settings, built-in radio only
root = make_root(BASE, [ONBOARD])
done = run(root, "apply")
assert done.returncode == 0, done.stderr
assert "using wlan0 (brcmfmac)" in done.stderr
assert profiles(root) == ["campmixer-ap", "campmixer-eth", "campmixer-eth-direct"]
ap = profile(root, "campmixer-ap")
assert ap["wifi"] == {"band": "bg", "channel": "6", "mode": "ap", "powersave": "2", "ssid": "CampMixer"}
assert ap["wifi-security"]["psk"] == "abcd-efgh-jkmn" and ap["wifi-security"]["key-mgmt"] == "wpa-psk"
assert ap["ipv4"] == {"address1": "10.77.7.1/24", "method": "shared"}
assert ap["connection"]["autoconnect-priority"] == "100"
assert profile(root, "campmixer-eth")["ipv4"] == {"dhcp-timeout": "30", "may-fail": "false", "method": "auto"}
assert profile(root, "campmixer-eth-direct")["ipv4"] == {"method": "link-local"}
assert "match-device" not in conf(root)
assert oct(os.stat(profile_path(root, "campmixer-ap")).st_mode & 0o777) == "0o600"

# a second start-up with nothing changed must not write to the card again
stamp = os.stat(profile_path(root, "campmixer-ap")).st_mtime_ns
os.utime(profile_path(root, "campmixer-ap"), ns=(1, 1))
run(root, "apply")
assert os.stat(profile_path(root, "campmixer-ap")).st_mtime_ns == 1, stamp

# --- 2. which radio
both = make_root(BASE, [ONBOARD, USB])
assert "using wlan1 (mt76x2u)" in run(both, "apply").stderr
assert "match-device=driver:brcmfmac\nmanaged=0" in conf(both)
both = make_root(BASE + "WIFI_RADIO=onboard\n", [ONBOARD, USB])
assert "using wlan0 (brcmfmac)" in run(both, "apply").stderr
assert "match-device=type:wifi,except:driver:brcmfmac" in conf(both)
only = make_root(BASE + "WIFI_RADIO=usb\n", [ONBOARD])
assert "using none" in run(only, "apply").stderr and "match-device=driver:brcmfmac" in conf(only)
none = make_root(BASE, [])
assert "radios found: none" in run(none, "apply").stderr and profiles(none)[0] == "campmixer-ap"

# A USB adaptor that is plugged in but slow to start is waited for, so the built-in radio is not picked by mistake.
hubs = make_root(BASE, [ONBOARD])
plug_into_usb(hubs, "usb1", "09")
plug_into_usb(hubs, "1-1", "09")
os.makedirs(hubs + "/sys/bus/usb/devices/1-1:1.0")  # an interface entry: no bDeviceClass in it
done = run(hubs, "apply")
assert "using wlan0 (brcmfmac)" in done.stderr and "plugged into USB" not in done.stderr, done.stderr
slow = make_root(BASE, [ONBOARD])
plug_into_usb(slow, "1-1", "09")
plug_into_usb(slow, "1-1.2", "ff")
waiting = subprocess.Popen([sys.executable, NETCONF, "apply"], stderr=subprocess.PIPE, text=True,
                           env=dict(os.environ, CAMPMIXER_ROOT=slow, CAMPMIXER_SYS_NET=slow + "/sys/class/net"))
assert "plugged into USB" in waiting.stderr.readline()  # it has started to wait
add_interface(slow, *USB)
rest = waiting.stderr.read()
assert waiting.wait() == 0 and "using wlan1 (mt76x2u)" in rest, rest
assert "match-device=driver:brcmfmac\nmanaged=0" in conf(slow)
never = make_root(BASE, [ONBOARD])
plug_into_usb(never, "1-1.3", "08")  # a USB stick, say
done = run(never, "apply", CAMPMIXER_TIME_SCALE="0.001")
assert "plugged into USB" in done.stderr and "using wlan0 (brcmfmac)" in done.stderr and "match-device" not in conf(never)
fixed = make_root(BASE + "WIFI_RADIO=onboard\n", [ONBOARD])
plug_into_usb(fixed, "1-1.3", "08")
assert "plugged into USB" not in run(fixed, "apply").stderr  # told which radio to use: nothing to wait for
edit_time = make_root(BASE + "JOIN_WIFI_1=\n", [ONBOARD])
plug_into_usb(edit_time, "1-1.3", "08")
assert "plugged into USB" not in run(edit_time, "join", "Somewhere").stderr  # only at start-up, not on every change

# --- 3. networks to join, and cleaning up after they are removed
joins = make_root(
    BASE + "JOIN_WIFI_1=Church Hall\nJOIN_WIFI_1_PASSWORD=hall-password\nJOIN_WIFI_2=Open Net\n"
    "JOIN_WIFI_3=Bad\nJOIN_WIFI_3_PASSWORD=short\nJOIN_WIFI_4=CampMixer\n", [USB])
done = run(joins, "apply")
assert profiles(joins) == ["campmixer-ap", "campmixer-eth", "campmixer-eth-direct", "campmixer-join-1", "campmixer-join-2"]
hall = profile(joins, "campmixer-join-1")
assert hall["wifi"] == {"mode": "infrastructure", "ssid": "Church Hall"} and hall["wifi-security"]["psk"] == "hall-password"
assert hall["connection"]["autoconnect-priority"] == "199" and hall["ipv4"] == {"method": "auto"}
assert "wifi-security" not in profile(joins, "campmixer-join-2")
assert "JOIN_WIFI_3_PASSWORD" in done.stderr and "JOIN_WIFI_4 is the mixer's own" in done.stderr
assert run(joins, "forget", "Church Hall").returncode == 0
assert "campmixer-join-1" not in profiles(joins) and "campmixer-join-2" in profiles(joins)

# --- 4. mistakes in the file fall back to something that still works
bad = make_root("# mine\nWIFI_NAME=%s\nWIFI_PASSWORD=short\nWIFI_BAND=5\nWIFI_CHANNEL=6\nWIFI_RADIO=both\n" % ("x" * 40), [USB])
done = run(bad, "apply")
ap = profile(bad, "campmixer-ap")
assert ap["wifi"]["ssid"] == "CampMixer" and ap["wifi"]["band"] == "a" and ap["wifi"]["channel"] == "36"
assert done.stderr.count("settings:") == 3, done.stderr

# There is no built-in Wi-Fi password. An unusable or blank one is replaced by a made-up one, saved in the file.
made_up = re.search(r"^WIFI_PASSWORD=(%s)$" % MADE_UP, settings_file(bad), re.M)
assert made_up and ap["wifi-security"]["psk"] == made_up.group(1), settings_file(bad)
assert "WIFI_PASSWORD was not usable" in done.stderr and settings_file(bad).startswith("# mine\nWIFI_NAME=xxx")
assert "setup" not in ap["wifi-security"]["psk"] and "short" not in settings_file(bad)
again = run(bad, "apply")
assert "made up" not in again.stderr and made_up.group(0) in settings_file(bad)  # and it stays the same after that
blank = make_root("WIFI_NAME=CampMixer\nWIFI_PASSWORD=\n", [USB])
assert "WIFI_PASSWORD was blank" in run(blank, "apply").stderr
assert re.search(r"^psk=%s$" % MADE_UP, open(profile_path(blank, "campmixer-ap")).read(), re.M)
missing = make_root(None, [USB])
done = run(missing, "apply")
assert done.returncode == 0, done.stderr
made_up = re.search(r"^WIFI_PASSWORD=(%s)$" % MADE_UP, settings_file(missing), re.M)
assert made_up and profile(missing, "campmixer-ap")["wifi-security"]["psk"] == made_up.group(1)

# If the made-up password cannot be saved, nobody could ever learn it, so the own network stays off instead.
stuck = make_root(BASE, [USB])
run(stuck, "apply")
assert "campmixer-ap" in profiles(stuck)  # from an earlier start-up, when all was well
os.remove(stuck + "/boot/firmware/campmixer.txt")
os.makedirs(stuck + "/boot/firmware/campmixer.txt")  # a folder where the file should be: it cannot be written
done = run(stuck, "apply")
assert done.returncode == 0 and "could not save a new Wi-Fi password" in done.stderr, done.stderr
assert "campmixer-ap" not in profiles(stuck) and "campmixer-eth" in profiles(stuck)
assert os.listdir(stuck + "/boot/firmware") == ["campmixer.txt"]  # no half-written file left lying about
shown = run(stuck, "show")
assert shown.returncode == 1 and "own Wi-Fi is off" in shown.stdout, shown.stdout
windows = make_root("﻿WIFI_NAME = \"Hall Mixer\"\r\nWIFI_PASSWORD='with space 1'\r\n# note\r\n", [USB])
run(windows, "apply")
assert profile(windows, "campmixer-ap")["wifi"]["ssid"] == "Hall Mixer"
assert profile(windows, "campmixer-ap")["wifi-security"]["psk"] == "with space 1"

# --- 5. Wi-Fi switched off in NetworkManager's saved state is switched back on
off = make_root(BASE, [USB])
with open(off + "/var/lib/NetworkManager/NetworkManager.state", "w") as handle:
    handle.write("[main]\nNetworkingEnabled=true\nWirelessEnabled=false\n")
run(off, "apply")
with open(off + "/var/lib/NetworkManager/NetworkManager.state") as handle:
    assert "WirelessEnabled=true" in handle.read()

# --- 6. changing the file keeps the notes in it
edit = make_root("# my notes\n" + BASE + "JOIN_WIFI_1=\nJOIN_WIFI_1_PASSWORD=\n", [USB])
assert run(edit, "join", "Camp Site", "site-password").returncode == 0
assert run(edit, "join", "Open Cafe").returncode == 0
assert run(edit, "set", "WIFI_NAME=Mixer Two", "WIFI_CHANNEL=11").returncode == 0
with open(edit + "/boot/firmware/campmixer.txt") as handle:
    text = handle.read()
assert text.startswith("# my notes\nWIFI_NAME=Mixer Two\n") and "JOIN_WIFI_1=Camp Site\nJOIN_WIFI_1_PASSWORD=site-password\n" in text
assert "JOIN_WIFI_2=Open Cafe\n" in text and "WIFI_CHANNEL=11\n" in text
assert profile(edit, "campmixer-ap")["wifi"]["ssid"] == "Mixer Two" and profile(edit, "campmixer-ap")["wifi"]["channel"] == "11"
assert run(edit, "join", "Camp Site", "new-password").returncode == 0  # same name: same slot
assert profile(edit, "campmixer-join-1")["wifi-security"]["psk"] == "new-password"
for extra in ("Three", "Four"):
    assert run(edit, "join", extra).returncode == 0
assert run(edit, "join", "Five").returncode == 2
assert run(edit, "join", "x", "short").returncode == 2
assert run(edit, "set", "NOT_A_SETTING=1").returncode == 2
assert run(edit, "forget", "Nobody").returncode == 2

# A value the mixer could not use is refused, and nothing is changed.
before = settings_file(edit)
for refused, reason in (
    (["WIFI_PASSWORD=short"], "WIFI_PASSWORD must be 8 to 63 plain characters."),
    (["WIFI_PASSWORD="], "WIFI_PASSWORD must be 8 to 63"),
    (["WIFI_NAME=Fine Name", "WIFI_PASSWORD=pässword-long"], "WIFI_PASSWORD must be"),
    (["WIFI_NAME=" + "x" * 33], "WIFI_NAME must be 1 to 32 characters."),
    (["WIFI_BAND=6"], "WIFI_BAND must be 2.4 or 5."),
    (["WIFI_BAND=5"], "WIFI_CHANNEL 11 is not a 5 GHz channel."),
    (["WIFI_CHANNEL=36"], "WIFI_CHANNEL 36 is not a 2.4 GHz channel."),
    (["WIFI_RADIO=both"], "WIFI_RADIO must be auto, usb or onboard."),
    (["JOIN_WIFI_1_PASSWORD=short"], "JOIN_WIFI_1_PASSWORD must be 8 to 63 characters, or blank."),
):
    done = run(edit, "set", *refused)
    assert done.returncode == 2 and "Not changed: " + reason in done.stderr, (refused, done.stderr)
    assert settings_file(edit) == before, refused
assert run(edit, "set", "WIFI_BAND=5", "WIFI_CHANNEL=").returncode == 0  # a new band with that band's usual channel
assert profile(edit, "campmixer-ap")["wifi"]["band"] == "a" and profile(edit, "campmixer-ap")["wifi"]["channel"] == "36"
assert run(edit, "set", "WIFI_PASSWORD=a new long password").returncode == 0
assert profile(edit, "campmixer-ap")["wifi-security"]["psk"] == "a new long password"
assert run(edit, "set", "LOGIN_PASSWORD=").returncode == 0
shown = run(edit, "show")
assert "site-password" not in shown.stdout and "Camp Site" in shown.stdout
assert "new-password" in run(edit, "show", "--passwords").stdout

# --- 7. NetworkManager's own tools agree with what was written
if NMCLI:
    tricky_name, tricky_password = "Camp; Mixer #1 \\ x", " pass;word\\x #="
    tricky = make_root('WIFI_NAME="%s"\nWIFI_PASSWORD="%s"\nJOIN_WIFI_1=Café ☕\nJOIN_WIFI_2=16;17;\n'
                       'JOIN_WIFI_2_PASSWORD=%s\n' % (tricky_name, tricky_password, "a" * 64), [USB])
    assert run(tricky, "apply").returncode == 0
    for root_dir in (root, joins, tricky, bad):
        for name in profiles(root_dir):
            ok, output = nm_reads(profile_path(root_dir, name))
            assert ok, (name, output)

    def nmcli_writes(*settings):
        done = subprocess.run([NMCLI, "--offline", "connection", "add", "type", "wifi", "con-name", "x", *settings],
                              capture_output=True, text=True, check=True)
        return dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)

    theirs = nmcli_writes("wifi.ssid", tricky_name, "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", tricky_password)
    ours = profile(tricky, "campmixer-ap")
    assert ours["wifi"]["ssid"] == theirs["ssid"], (ours["wifi"]["ssid"], theirs["ssid"])
    assert ours["wifi-security"]["psk"] == theirs["psk"], (ours["wifi-security"]["psk"], theirs["psk"])
    for slot, name in ((1, "Café ☕"), (2, "16;17;")):
        assert profile(tricky, "campmixer-join-%d" % slot)["wifi"]["ssid"] == nmcli_writes("wifi.ssid", name)["ssid"], name
    theirs = nmcli_writes("wifi.ssid", "CampMixer", "wifi.mode", "ap", "wifi.band", "bg", "wifi.channel", "6",
                          "wifi.powersave", "2", "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", "abcd-efgh-jkmn",
                          "wifi-sec.proto", "rsn", "wifi-sec.pairwise", "ccmp",
                          "wifi-sec.group", "ccmp", "ipv4.method", "shared", "ipv4.addresses", "10.77.7.1/24")
    ours = profile(root, "campmixer-ap")
    for section in ("wifi", "wifi-security", "ipv4"):
        for key, value in ours[section].items():
            if key not in ("ssid", "psk", "method"):
                assert theirs.get(key) == value, (section, key, value, theirs.get(key))
    nm_daemon = shutil.which("NetworkManager") or "/usr/sbin/NetworkManager"
    if os.path.exists(nm_daemon):
        for root_dir in (root, both):
            open(root_dir + "/empty.conf", "w").close()
            done = subprocess.run([nm_daemon, "--print-config", "--config", root_dir + "/empty.conf", "--config-dir",
                                   root_dir + "/etc/NetworkManager/conf.d"], capture_output=True, text=True)
            assert done.returncode == 0, done.stderr
        assert "[device-campmixer-spare-radio]" in done.stdout and "managed=0" in done.stdout
    print("netconf: profiles accepted by NetworkManager %s" % subprocess.run(
        [NMCLI, "--version"], capture_output=True, text=True).stdout.strip().split()[-1])
else:
    print("netconf: nmcli not installed, skipped the NetworkManager cross-checks")


# --- 8. the radio's decisions, played against a pretend nmcli
def radio(settings, **state):
    root_dir = make_root(settings, [USB])
    state_file = root_dir + "/nm.json"
    start = {"dev": "wlan1", "state": "disconnected", "conn": "", "visible": [], "fail": [], "calls": [], "polls": 0}
    start.update(state)
    with open(state_file, "w") as handle:
        json.dump(start, handle)
    done = run(root_dir, "radio", CAMPMIXER_NMCLI=FAKE_NMCLI, FAKE_NM_STATE=state_file, CAMPMIXER_TIME_SCALE="0.001")
    with open(state_file) as handle:
        end = json.load(handle)
    actions = []
    for call in end["calls"]:
        if "up" in call:
            actions.append("up " + call[call.index("id") + 1])
        elif "down" in call:
            actions.append("down")
        elif "list" in call:
            actions.append("scan")
    return done, actions, end


JOIN = BASE + "JOIN_WIFI_1=Church Hall\nJOIN_WIFI_1_PASSWORD=hall-password\nJOIN_WIFI_2=Camp: Site\n"

done, actions, end = radio(BASE)
assert done.returncode == 0 and actions == ["up campmixer-ap"], actions
done, actions, end = radio(BASE, state="connected", conn="campmixer-ap")
assert actions == [], actions  # already running its own network: leave it alone

# its own network came up first, a listed one is in range: switch to it, then fall back when it drops
done, actions, end = radio(JOIN, state="connected", conn="campmixer-ap", visible=["Other", "Church Hall"], drop_at=6)
assert done.returncode == 0 and actions == ["down", "scan", "up campmixer-join-1", "up campmixer-ap"], actions
assert "gone for 30 s" in done.stderr and end["conn"] == "campmixer-ap"

done, actions, end = radio(JOIN, state="connected", conn="campmixer-ap", visible=["Camp: Site"], drop_at=6)
assert actions[:3] == ["down", "scan", "up campmixer-join-2"], actions  # a colon in the name survives nmcli's format

done, actions, end = radio(JOIN, state="connected", conn="campmixer-ap", visible=["Somewhere Else"])
assert actions == ["down", "scan", "up campmixer-ap"] and "in range" in done.stderr, actions

done, actions, end = radio(JOIN, state="connected", conn="campmixer-join-1", drop_at=5)
assert actions == ["up campmixer-ap"], actions  # NetworkManager had already joined: only the fallback is ours

done, actions, end = radio(JOIN, visible=["Church Hall"], fail=["campmixer-join-1"])
assert actions == ["scan", "up campmixer-join-1", "up campmixer-ap"] and "could not join" in done.stderr, actions

done, actions, end = radio(JOIN, dev="")
assert done.returncode == 0 and actions == [] and "Ethernet only" in done.stderr

# no usable password and no way to save one: the own network is not started, and that is said plainly
done, actions, end = radio("WIFI_PASSWORD=short\n")
assert done.returncode == 0 and actions == [] and "own Wi-Fi is off" in done.stderr, (actions, done.stderr)

print("netconf: all checks passed")
