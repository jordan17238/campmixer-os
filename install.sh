#!/bin/bash
# CampMixer OS installer: lays the CampMixer layer over Raspberry Pi OS Lite (64-bit).
#
# It runs in two places:
#   * inside the image build (image/build-image.sh sets CAMPMIXER_IMAGE_BUILD=1). This is how
#     CampMixerOS.img is made, and the normal way to get CampMixer OS.
#   * on a running Pi with stock Raspberry Pi OS Lite, Bookworm or Trixie:
#       sudo bash /boot/firmware/campmixer-os/install.sh
#     Safe to run again after changing anything in this folder.
#
# Settings:
#   CAMPMIXER_COUNTRY=NZ      Wi-Fi country, if not Australia
#   CAMPMIXER_HOSTNAME=name   instead of "mixer"
#   CAMPMIXER_NO_LOCK=1       on a running Pi, leave the card writable at the end
#   CAMPMIXER_ROOT=/some/dir  write into a copy of the system files instead of the real ones (tests)
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${CAMPMIXER_ROOT:-}"
BOOT="$ROOT/boot/firmware"
HOST_NAME="${CAMPMIXER_HOSTNAME:-mixer}"
COUNTRY="${CAMPMIXER_COUNTRY:-}"
VERSION="$(cat "$SRC/VERSION")"

IMAGE=0
if [ "${CAMPMIXER_IMAGE_BUILD:-0}" = 1 ]; then
	IMAGE=1
fi
REAL=1 # 0 when writing into a pretend system for the tests: no system commands are run
if [ -n "$ROOT" ]; then
	REAL=0
fi
LIVE=0 # a running Pi
if [ "$REAL" = 1 ] && [ "$IMAGE" = 0 ]; then
	LIVE=1
fi

say() {
	printf '\n== %s\n' "$*"
}

die() {
	printf '\nERROR: %s\n' "$*" >&2
	exit 1
}

have_package() {
	dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed"
}

put() {
	install -D -m "$1" "$SRC/rootfs/$2" "$ROOT/$2"
}

netconf() {
	if [ "$REAL" = 1 ]; then
		python3 /opt/campmixer/netconf.py "$@"
	else
		CAMPMIXER_ROOT="$ROOT" CAMPMIXER_SYS_NET="${CAMPMIXER_SYS_NET:-$ROOT/sys/class/net}" \
			python3 "$ROOT/opt/campmixer/netconf.py" "$@"
	fi
}

# ------------------------------------------------------------------ checks

[ "$REAL" = 0 ] || [ "$(id -u)" -eq 0 ] || die "Run this with sudo: sudo bash $0"
[ -f "$BOOT/config.txt" ] && [ -f "$BOOT/cmdline.txt" ] ||
	die "$BOOT/config.txt not found. This needs Raspberry Pi OS Bookworm or newer."
[ -f "$SRC/rootfs/opt/campmixer/bridge.py" ] && [ -f "$SRC/campmixer.txt" ] ||
	die "The kit is incomplete. Copy the whole campmixer-os folder again."
if [ "$LIVE" = 1 ]; then
	if grep -q "overlayroot=tmpfs" /proc/cmdline; then
		die "The card is locked. Run 'sudo campmixer unlock', restart, then run this again."
	fi
	command -v python3 >/dev/null || die "python3 is missing, so this is not Raspberry Pi OS Lite."
	# shellcheck disable=SC1091
	CODENAME="$(. /etc/os-release && echo "${VERSION_CODENAME:-unknown}")"
	case "$CODENAME" in
	bookworm | trixie) ;;
	*) echo "Note: written for Raspberry Pi OS Bookworm and Trixie; this is $CODENAME." ;;
	esac
fi

echo "CampMixer OS $VERSION installer"

# ------------------------------------------------------------------ packages

if [ "$LIVE" = 1 ]; then
	# Raspberry Pi OS sets the user up on first start; let that finish before changing things.
	if command -v cloud-init >/dev/null; then
		timeout 180 cloud-init status --wait >/dev/null 2>&1 || true
	fi
	need=()
	have_package dnsmasq-base || need+=(dnsmasq-base)
	command -v nft >/dev/null || command -v iptables >/dev/null || need+=(nftables)
	command -v iw >/dev/null || need+=(iw)
	have_package overlayroot || need+=(overlayroot)
	if [ "${#need[@]}" -gt 0 ]; then
		say "Installing ${need[*]}"
		apt-get update || die "Could not reach the package servers. Connect the Pi to the internet and try again."
		DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${need[@]}" ||
			die "Could not install ${need[*]}."
	fi
fi
# In an image build the same packages come from image/stage-campmixer/00-campmixer/00-packages.

# ------------------------------------------------------------------ files

say "Copying the CampMixer files"
put 755 opt/campmixer/bridge.py
put 755 opt/campmixer/netconf.py
put 755 opt/campmixer/firststart.py
put 644 opt/campmixer/campmixer_link.py
put 644 opt/campmixer/www/index.html
put 644 opt/campmixer/www/manifest.webmanifest
put 644 opt/campmixer/www/icon.svg
put 755 usr/local/sbin/campmixer
put 644 etc/systemd/system/campmixer-netconf.service
put 644 etc/systemd/system/campmixer-radio.service
put 644 etc/systemd/system/campmixer-bridge.service
put 644 etc/systemd/system/campmixer-firststart.service
put 644 etc/systemd/system.conf.d/10-campmixer-watchdog.conf
put 644 etc/systemd/journald.conf.d/10-campmixer.conf
put 644 etc/NetworkManager/dnsmasq-shared.d/campmixer.conf
put 644 etc/rpi/swap.conf.d/90-campmixer.conf
put 644 etc/motd
install -D -m 644 "$SRC/VERSION" "$ROOT/opt/campmixer/VERSION"

if [ "$REAL" = 1 ] && ! id campmixer >/dev/null 2>&1; then
	useradd --system --home-dir /opt/campmixer --no-create-home --shell /usr/sbin/nologin --groups dialout campmixer
fi

# ------------------------------------------------------------------ name

say "Naming the Pi $HOST_NAME (http://$HOST_NAME.local)"
mkdir -p "$ROOT/etc"
echo "$HOST_NAME" >"$ROOT/etc/hostname"
if grep -qE '^[[:space:]]*127\.0\.1\.1[[:space:]]' "$ROOT/etc/hosts" 2>/dev/null; then
	sed -i -E "s/^[[:space:]]*127\.0\.1\.1[[:space:]].*/127.0.1.1\t$HOST_NAME/" "$ROOT/etc/hosts"
else
	printf '127.0.1.1\t%s\n' "$HOST_NAME" >>"$ROOT/etc/hosts"
fi
if [ "$LIVE" = 1 ]; then
	hostnamectl hostname "$HOST_NAME" 2>/dev/null || true
fi
# CampMixer does its own first-start set-up. Left on, cloud-init would put the old name back at every start.
if [ -d "$ROOT/etc/cloud" ]; then
	touch "$ROOT/etc/cloud/cloud-init.disabled"
fi

# ------------------------------------------------------------------ boot settings

say "Setting up the serial port, Bluetooth and watchdog in config.txt"
CONFIG="$BOOT/config.txt"
sed -i '/^# >>> CampMixer >>>$/,/^# <<< CampMixer <<<$/d' "$CONFIG"
[ -z "$(tail -c1 "$CONFIG")" ] || echo >>"$CONFIG"
cat >>"$CONFIG" <<'EOF'
# >>> CampMixer >>>
# Managed by the CampMixer installer. Lines between the markers are rewritten when it runs.
[all]
# The serial port on header pins 8 and 10 is the link to the mixer core.
enable_uart=1
# Bluetooth off. This also gives the link the Pi's better serial port.
dtoverlay=disable-bt
# Hardware watchdog: restarts the Pi if it ever hangs.
dtparam=watchdog=on
# <<< CampMixer <<<
EOF

CMDLINE="$BOOT/cmdline.txt"
# The serial port carries mixer data, so the system console must stay off it.
sed -i -E 's/(^| )console=(serial0|ttyAMA0|ttyS0),[0-9]+//g; s/ +/ /g; s/^ //; s/ $//' "$CMDLINE"
if [ -n "$COUNTRY" ]; then
	sed -i -E 's/ ?cfg80211\.ieee80211_regdom=[^ ]*//g' "$CMDLINE"
fi
if ! grep -q 'cfg80211\.ieee80211_regdom=' "$CMDLINE"; then
	sed -i -E "1 s/\$/ cfg80211.ieee80211_regdom=${COUNTRY:-AU}/" "$CMDLINE"
fi
COUNTRY="$(grep -oE 'cfg80211\.ieee80211_regdom=[A-Za-z0-9]{2}' "$CMDLINE" | head -n1 | cut -d= -f2 || true)"
COUNTRY="${COUNTRY:-AU}"
if [ "$LIVE" = 1 ] && command -v raspi-config >/dev/null; then
	# Also clears the "Wi-Fi is blocked until a country is set" state of a fresh install.
	raspi-config nonint do_wifi_country "$COUNTRY" >/dev/null 2>&1 || true
fi

# ------------------------------------------------------------------ settings file

SETTINGS="$BOOT/campmixer.txt"
if [ ! -f "$SETTINGS" ]; then
	say "Creating $SETTINGS"
	cp "$SRC/campmixer.txt" "$SETTINGS"
fi
# An image must not carry a password, so a blank one stays blank until the image's first start.
if [ "$IMAGE" = 0 ] && ! grep -qE '^WIFI_PASSWORD=.{8,}' "$SETTINGS"; then
	PASSWORD="$(python3 -c '
import secrets
letters = "abcdefghjkmnpqrstuvwxyz23456789"
print("-".join("".join(secrets.choice(letters) for _ in range(4)) for _ in range(3)))
')"
	if grep -q '^WIFI_PASSWORD=' "$SETTINGS"; then
		sed -i "s/^WIFI_PASSWORD=.*/WIFI_PASSWORD=$PASSWORD/" "$SETTINGS"
	else
		echo "WIFI_PASSWORD=$PASSWORD" >>"$SETTINGS"
	fi
fi

# ------------------------------------------------------------------ services

if [ "$IMAGE" = 1 ]; then
	# Tells campmixer-firststart.service that this card has not been started yet.
	mkdir -p "$ROOT/var/lib/campmixer"
	touch "$ROOT/var/lib/campmixer/first-start"
fi
if [ "$REAL" = 1 ]; then
	say "Switching the services on"
	if [ "$LIVE" = 1 ]; then
		systemctl daemon-reload
	fi
	systemctl enable campmixer-netconf.service campmixer-radio.service campmixer-bridge.service
	systemctl enable ssh.service || echo "Note: could not switch SSH on. Is openssh-server installed?"
	if [ "$IMAGE" = 1 ]; then
		systemctl enable campmixer-firststart.service
	else
		ls /etc/ssh/ssh_host_*_key >/dev/null 2>&1 || ssh-keygen -A
		systemctl start ssh.service || true
	fi
	for unit in serial-getty@ttyAMA0.service serial-getty@ttyS0.service serial-getty@serial0.service; do
		systemctl mask "$unit" >/dev/null 2>&1 || true
	done
	# Not needed on the mixer. They slow the start-up down or write to the card on a schedule.
	for unit in NetworkManager-wait-online.service bluetooth.service hciuart.service ModemManager.service \
		apt-daily.timer apt-daily-upgrade.timer man-db.timer dpkg-db-backup.timer logrotate.timer \
		e2scrub_all.timer dphys-swapfile.service; do
		systemctl disable "$unit" >/dev/null 2>&1 || true
	done
fi

# ------------------------------------------------------------------ done

say "CampMixer OS $VERSION is installed"
if [ "$IMAGE" = 1 ]; then
	echo "Passwords are set on the image's first start, from campmixer.txt on its boot partition."
	exit 0
fi

netconf apply
netconf show --passwords || true
cat <<EOF

Web app:  http://$HOST_NAME.local   (from the next start)
SSH:      ssh ${SUDO_USER:-your-user}@$HOST_NAME.local
Settings: $SETTINGS   (plain text, also editable with the card in a computer)
EOF

if [ "$LIVE" = 1 ]; then
	if [ "${CAMPMIXER_NO_LOCK:-0}" = 1 ]; then
		echo
		echo "The card is left writable because CAMPMIXER_NO_LOCK=1. Lock it later with: sudo campmixer lock"
	else
		say "Locking the card"
		if /usr/local/sbin/campmixer lock; then
			echo "From the next start nothing is written to the card, and the power can be pulled at any time."
			echo "To change or update things later: sudo campmixer unlock, restart, make the change, sudo campmixer lock."
		else
			echo "Could not lock the card. Everything else is installed; try 'sudo campmixer lock' after the restart."
		fi
	fi
	echo
	echo "Restart now:  sudo reboot"
fi
