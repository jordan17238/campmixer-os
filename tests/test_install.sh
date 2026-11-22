#!/bin/bash
# Runs install.sh against copies of the stock Raspberry Pi OS boot files and checks every edit it makes.
# The fixtures are config.txt and cmdline.txt as shipped by pi-gen (arm64 branch, November 2026).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIT="$(dirname "$HERE")"

fail() {
	echo "FAIL: $*" >&2
	exit 1
}

make_root() { # $1 = contents of cmdline.txt
	local root
	root="$(mktemp -d)"
	mkdir -p "$root/boot/firmware" "$root/etc/cloud" "$root/sys/class/net/eth0/device" "$root/var/lib/NetworkManager"
	cp "$HERE/fixtures/config.txt" "$root/boot/firmware/config.txt"
	printf '%s\n' "$1" >"$root/boot/firmware/cmdline.txt"
	printf '127.0.0.1\tlocalhost\n::1\t\tlocalhost ip6-localhost ip6-loopback\n127.0.1.1\traspberrypi\n' >"$root/etc/hosts"
	echo raspberrypi >"$root/etc/hostname"
	echo "$root"
}

install_into() { # $1 = root, rest = extra environment
	local root="$1"
	shift
	env CAMPMIXER_ROOT="$root" "$@" bash "$KIT/install.sh" >"$root/install.log" 2>&1 || {
		cat "$root/install.log"
		fail "installer failed"
	}
}

STOCK="$(cat "$HERE/fixtures/cmdline.txt")"

# ---- a fresh card
root="$(make_root "$STOCK")"
install_into "$root"
cmdline="$(cat "$root/boot/firmware/cmdline.txt")"
[ "$(wc -l <"$root/boot/firmware/cmdline.txt")" -eq 1 ] || fail "cmdline.txt is no longer one line"
[ "$cmdline" = "console=tty1 root=ROOTDEV rootfstype=ext4 fsck.repair=yes rootwait resize cfg80211.ieee80211_regdom=AU" ] ||
	fail "unexpected cmdline: $cmdline"

config="$root/boot/firmware/config.txt"
[ "$(grep -c '^# >>> CampMixer >>>$' "$config")" -eq 1 ] || fail "managed block missing"
for line in '[all]' 'enable_uart=1' 'dtoverlay=disable-bt' 'dtparam=watchdog=on'; do
	sed -n '/^# >>> CampMixer >>>$/,/^# <<< CampMixer <<<$/p' "$config" | grep -qxF "$line" || fail "config.txt lacks $line"
done
head -n "$(wc -l <"$HERE/fixtures/config.txt")" "$config" | cmp -s - "$HERE/fixtures/config.txt" ||
	fail "the stock part of config.txt was changed"

[ "$(cat "$root/etc/hostname")" = mixer ] || fail "hostname not set"
grep -qP '^127\.0\.1\.1\tmixer$' "$root/etc/hosts" || fail "/etc/hosts not updated"
[ "$(grep -c '127.0.1.1' "$root/etc/hosts")" -eq 1 ] || fail "/etc/hosts has two 127.0.1.1 lines"
[ -e "$root/etc/cloud/cloud-init.disabled" ] || fail "cloud-init left on"

while read -r mode path; do
	[ "$(stat -c %a "$root/$path")" = "$mode" ] || fail "$path has mode $(stat -c %a "$root/$path"), wanted $mode"
done <<'EOF'
755 opt/campmixer/bridge.py
755 opt/campmixer/netconf.py
755 opt/campmixer/firststart.py
644 opt/campmixer/campmixer_link.py
644 opt/campmixer/campmixer_auth.py
644 opt/campmixer/VERSION
644 opt/campmixer/www/index.html
644 opt/campmixer/www/app.js
644 opt/campmixer/www/mixer-link.js
644 opt/campmixer/www/app.css
644 opt/campmixer/www/manifest.webmanifest
644 opt/campmixer/www/icon.svg
644 etc/ssh/sshd_config.d/10-campmixer.conf
600 var/lib/campmixer/web-key
755 usr/local/bin/campmixer
755 etc/systemd/system-generators/campmixer-bootro
755 etc/initramfs-tools/scripts/init-premount/campmixer-nofsck
644 etc/systemd/system/systemd-remount-fs.service.d/10-campmixer-overlay.conf
644 etc/systemd/system/campmixer-bridge.service
644 etc/systemd/system/campmixer-netconf.service
644 etc/systemd/system/campmixer-radio.service
644 etc/systemd/system/campmixer-firststart.service
644 etc/motd
644 etc/systemd/system.conf.d/10-campmixer-watchdog.conf
644 etc/systemd/journald.conf.d/10-campmixer.conf
644 etc/rpi/swap.conf.d/90-campmixer.conf
644 etc/NetworkManager/dnsmasq-shared.d/campmixer.conf
600 etc/NetworkManager/system-connections/campmixer-ap.nmconnection
600 etc/NetworkManager/system-connections/campmixer-eth.nmconnection
EOF

settings="$root/boot/firmware/campmixer.txt"
# The Wi-Fi password is the standard one that every CampMixer starts with, as written in the settings file that
# ships. Nothing is made up in its place.
standard="$(sed -n 's/^WIFI_PASSWORD=//p' "$HERE/../campmixer.txt")"
[ "${#standard}" -ge 8 ] && [ "${#standard}" -le 63 ] || fail "the settings file that ships has no usable standard Wi-Fi password: '$standard'"
grep -qxF "WIFI_PASSWORD=$standard" "$settings" || fail "the standard Wi-Fi password was not kept"
password="$(sed -n 's/^WIFI_PASSWORD=//p' "$settings")"
grep -qxF "psk=$password" "$root/etc/NetworkManager/system-connections/campmixer-ap.nmconnection" ||
	fail "the access point does not use the password in campmixer.txt"
grep -q "Own Wi-Fi password: $password" "$root/install.log" || fail "the installer did not show the password"

# The web app's PIN: made up, shown, and handed to the bridge as a hash only.
pin="$(sed -n 's/^WEB_PIN=//p' "$settings")"
[[ "$pin" =~ ^[0-9]{8}$ ]] || fail "no PIN was made up: '$pin'"
grep -q "Web app PIN:        $pin" "$root/install.log" || fail "the installer did not show the PIN"
[ -s "$root/run/campmixer/web-auth.json" ] || fail "the bridge was given nothing to check the PIN with"
! grep -q "$pin" "$root/run/campmixer/web-auth.json" || fail "the PIN itself is in the bridge's file"
! grep -q "$(cat "$root/var/lib/campmixer/web-key")" "$root/run/campmixer/web-auth.json" || fail "the Pi's key is in the bridge's file"
# Every file the page asks for is one the installer puts there.
for asked in $(grep -oE '(src|href)="/[^"]+"' "$root/opt/campmixer/www/index.html" | cut -d'"' -f2); do
	[ -f "$root/opt/campmixer/www$asked" ] || fail "the page asks for $asked, which is not installed"
done

# ---- running it again changes nothing, and clears out the command's old place
mkdir -p "$root/usr/local/sbin"
echo old >"$root/usr/local/sbin/campmixer"
before="$(cat "$config" "$root/boot/firmware/cmdline.txt" "$settings" "$root/etc/hosts" "$root/var/lib/campmixer/web-key" | sha256sum)"
install_into "$root"
after="$(cat "$config" "$root/boot/firmware/cmdline.txt" "$settings" "$root/etc/hosts" "$root/var/lib/campmixer/web-key" | sha256sum)"
[ "$before" = "$after" ] || fail "a second run changed the boot files"
[ ! -e "$root/usr/local/sbin/campmixer" ] || fail "the old copy of the command was left behind"
grep -q 'ExecStart=/usr/local/bin/campmixer firststart' "$root/etc/systemd/system/campmixer-firststart.service" ||
	fail "the first-start unit does not point at the command"

# ---- a card that has been started once, set up for another country, with its own settings file
used="$(make_root "console=serial0,115200 console=tty1 root=PARTUUID=4d3ee428-02 rootfstype=ext4 fsck.repair=yes rootwait quiet cfg80211.ieee80211_regdom=GB")"
printf 'dtparam=audio=on\n[all]\nenable_uart=0' >"$used/boot/firmware/config.txt" # no newline at the end
printf 'WIFI_NAME=Hall Mixer\nWIFI_PASSWORD=my own password\n' >"$used/boot/firmware/campmixer.txt"
install_into "$used"
[ "$(cat "$used/boot/firmware/cmdline.txt")" = "console=tty1 root=PARTUUID=4d3ee428-02 rootfstype=ext4 fsck.repair=yes rootwait quiet cfg80211.ieee80211_regdom=GB" ] ||
	fail "existing country not kept: $(cat "$used/boot/firmware/cmdline.txt")"
grep -qx 'enable_uart=0' "$used/boot/firmware/config.txt" || fail "existing config line lost"
[ "$(grep -n '^enable_uart=' "$used/boot/firmware/config.txt" | tail -n1 | cut -d: -f2)" = "enable_uart=1" ] ||
	fail "enable_uart=1 is not the last word in config.txt"
grep -qxF 'WIFI_PASSWORD=my own password' "$used/boot/firmware/campmixer.txt" || fail "his password was replaced"
grep -qxF 'ssid=Hall Mixer' "$used/etc/NetworkManager/system-connections/campmixer-ap.nmconnection" || fail "his network name was not used"

install_into "$used" CAMPMIXER_COUNTRY=NZ CAMPMIXER_HOSTNAME=hallmixer
grep -q ' cfg80211.ieee80211_regdom=NZ$' "$used/boot/firmware/cmdline.txt" || fail "country override not applied"
[ "$(grep -o 'regdom' "$used/boot/firmware/cmdline.txt" | wc -l)" -eq 1 ] || fail "two country settings"
[ "$(cat "$used/etc/hostname")" = hallmixer ] || fail "hostname override not applied"

# ---- building an image: the standard Wi-Fi password goes in, the one every CampMixer starts with and that is
# public anyway. Nothing else does, and the card is marked as not yet started.
img="$(make_root "$STOCK")"
install_into "$img" CAMPMIXER_IMAGE_BUILD=1
grep -qx 'WIFI_NAME=CampMixer' "$img/boot/firmware/campmixer.txt" || fail "the image does not carry the standard Wi-Fi name"
grep -qxF "WIFI_PASSWORD=$standard" "$img/boot/firmware/campmixer.txt" || fail "the image does not carry the standard Wi-Fi password"
[ "$(grep -c '^[A-Z_0-9]*PASSWORD=.' "$img/boot/firmware/campmixer.txt")" -eq 1 ] || fail "an image must carry no password but the standard Wi-Fi one"
grep -qx 'LOGIN_PASSWORD=' "$img/boot/firmware/campmixer.txt" || fail "an image must not carry a login password"
grep -qx 'WEB_PIN=' "$img/boot/firmware/campmixer.txt" || fail "an image must not carry a PIN"
# Nor a key: one that everybody can download would let anybody sign their own cookies.
[ ! -e "$img/var/lib/campmixer/web-key" ] || fail "an image must not carry the key the web app's cookies are signed with"
[ ! -e "$img/run/campmixer" ] || fail "the bridge's sign-in file was written at build time"
[ -z "$(find "$img/var/lib/campmixer" -type f ! -name first-start)" ] || fail "the image has more than the first-start marker: $(ls "$img/var/lib/campmixer")"
[ -e "$img/var/lib/campmixer/first-start" ] || fail "first-start marker missing from the image"
[ ! -e "$img/etc/NetworkManager/system-connections" ] || fail "network profiles were written at build time"
[ "$(cat "$img/boot/firmware/cmdline.txt")" = "console=tty1 root=ROOTDEV rootfstype=ext4 fsck.repair=yes rootwait resize cfg80211.ieee80211_regdom=AU" ] ||
	fail "image cmdline: $(cat "$img/boot/firmware/cmdline.txt")"
[ ! -e "$root/var/lib/campmixer/first-start" ] || fail "first-start marker set on a normal install"

# ---- not a Raspberry Pi OS card
empty="$(mktemp -d)"
if CAMPMIXER_ROOT="$empty" bash "$KIT/install.sh" >/dev/null 2>&1; then
	fail "installer ran without boot files"
fi

echo "installer: all checks passed"
