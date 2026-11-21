#!/bin/bash
# Checks the two start-up pieces that follow the lock:
#   campmixer-bootro  (systemd generator)  boot partition for root only, and read-only while locked
#   campmixer-nofsck  (initramfs script)   no file system check, and so no write, while locked
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GENERATOR="$HERE/../rootfs/etc/systemd/system-generators/campmixer-bootro"
NOFSCK="$HERE/../rootfs/etc/initramfs-tools/scripts/init-premount/campmixer-nofsck"
WORK="$(mktemp -d)"
DROPIN="boot-firmware.mount.d/50-campmixer.conf"

fail() {
	echo "FAIL: $*" >&2
	exit 1
}

generate() { # $1 = name, $2 = what is mounted, $3 = the boot partition's fstab line; prints the output folder
	local out="$WORK/$1"
	printf '%s\n' "$2" >"$WORK/mounts"
	printf 'proc            /proc           proc    defaults          0       0\n%s\nPARTUUID=3a1867d2-02  /               ext4    defaults,noatime  0       1\n' "$3" >"$WORK/fstab"
	CAMPMIXER_MOUNTS="$WORK/mounts" CAMPMIXER_FSTAB="$WORK/fstab" sh "$GENERATOR" "$out" "$out.early" "$out.late" ||
		fail "$1: the generator failed"
	echo "$out"
}

STOCK_LINE='PARTUUID=3a1867d2-01  /boot/firmware  vfat    defaults          0       2'
# /proc/mounts as the emulated Pi showed it, unlocked and locked.
UNLOCKED='sysfs /sys sysfs rw,nosuid,nodev,noexec,relatime 0 0
proc /proc proc rw,relatime 0 0
/dev/mmcblk0p2 / ext4 rw,noatime 0 0
tmpfs /run tmpfs rw,nosuid,nodev,size=187768k,nr_inodes=819200,mode=755 0 0'
LOCKED='sysfs /sys sysfs rw,nosuid,nodev,noexec,relatime 0 0
proc /proc proc rw,relatime 0 0
/dev/mmcblk0p2 /media/root-ro ext4 ro,relatime 0 0
tmpfs-root /media/root-rw tmpfs rw,relatime 0 0
overlayroot / overlay rw,relatime,lowerdir=/media/root-ro,upperdir=/media/root-rw/overlay,workdir=/media/root-rw/overlay-workdir/_,uuid=on 0 0
tmpfs /run tmpfs rw,nosuid,nodev,size=187768k,nr_inodes=819200,mode=755 0 0'

# Unlocked: writable, but for root only, because the settings file on it holds the passwords and the PIN.
out="$(generate unlocked "$UNLOCKED" "$STOCK_LINE")"
grep -qx 'Options=defaults,umask=0077' "$out/$DROPIN" || fail "unlocked: $(cat "$out/$DROPIN" 2>&1)"
grep -qx '\[Mount\]' "$out/$DROPIN" || fail "unlocked: no [Mount] section"

# Locked: read-only as well. This is the line as overlayroot leaves it.
out="$(generate locked "$LOCKED" 'PARTUUID=3a1867d2-01 /boot/firmware vfat defaults 0 2 # overlayroot:fs-unsupported')"
grep -qx 'Options=defaults,umask=0077,ro' "$out/$DROPIN" || fail "locked: $(cat "$out/$DROPIN" 2>&1)"

# Other options are kept, and a choice fstab already makes about who may read it is left alone.
out="$(generate options "$LOCKED" 'PARTUUID=3a1867d2-01  /boot/firmware  vfat    defaults,flush,umask=0027  0  2')"
grep -qx 'Options=defaults,flush,umask=0027,ro' "$out/$DROPIN" || fail "options: $(cat "$out/$DROPIN" 2>&1)"
out="$(generate chosen "$UNLOCKED" 'PARTUUID=3a1867d2-01  /boot/firmware  vfat    defaults,fmask=0137,dmask=0027  0  2')"
[ ! -e "$out" ] || fail "fstab's own choice: something was written"

# Already read-only in fstab (a card locked by raspi-config): only the root-only part is added.
out="$(generate already "$LOCKED" 'PARTUUID=3a1867d2-01  /boot/firmware  vfat    defaults,ro          0       2')"
grep -qx 'Options=defaults,ro,umask=0077' "$out/$DROPIN" || fail "already read-only: $(cat "$out/$DROPIN" 2>&1)"

# Not FAT: it would refuse the option and not mount at all, so only read-only is added, and only when locked.
out="$(generate ext4 "$UNLOCKED" 'PARTUUID=3a1867d2-01  /boot/firmware  ext4    defaults  0  2')"
[ ! -e "$out" ] || fail "ext4, unlocked: something was written"
out="$(generate ext4locked "$LOCKED" 'PARTUUID=3a1867d2-01  /boot/firmware  ext4    defaults  0  2')"
grep -qx 'Options=defaults,ro' "$out/$DROPIN" || fail "ext4, locked: $(cat "$out/$DROPIN" 2>&1)"

# No boot partition line, or only a commented one: nothing to do, and no error.
out="$(generate missing "$LOCKED" '')"
[ ! -e "$out" ] || fail "no fstab line: something was written"
out="$(generate commented "$LOCKED" '#PARTUUID=3a1867d2-01  /boot/firmware  vfat    defaults          0       2')"
[ ! -e "$out" ] || fail "commented line: something was written"

# An overlay somewhere else (a container, say) does not count as locked.
out="$(generate elsewhere "$UNLOCKED
overlay /var/lib/thing/merged overlay rw,relatime,lowerdir=/a,upperdir=/b,workdir=/c 0 0" "$STOCK_LINE")"
grep -qx 'Options=defaults,umask=0077' "$out/$DROPIN" || fail "overlay elsewhere: $(cat "$out/$DROPIN" 2>&1)"

# Neither a missing fstab nor a missing mount table may stop the start-up.
printf '%s\n' "$LOCKED" >"$WORK/mounts"
CAMPMIXER_MOUNTS="$WORK/mounts" CAMPMIXER_FSTAB="$WORK/none" sh "$GENERATOR" "$WORK/nofstab" || fail "no fstab: the generator failed"
[ ! -e "$WORK/nofstab" ] || fail "no fstab: something was written"
CAMPMIXER_MOUNTS="$WORK/none" CAMPMIXER_FSTAB="$WORK/fstab" sh "$GENERATOR" "$WORK/nomounts" || fail "no mount table: the generator failed"
grep -qx 'Options=defaults,umask=0077' "$WORK/nomounts/$DROPIN" || fail "no mount table: $(cat "$WORK/nomounts/$DROPIN" 2>&1)"

# ---- the file system check

nofsck() { # $1 = kernel command line; prints what the script adds to the start-up image's settings
	printf '%s\n' "$1" >"$WORK/cmdline"
	: >"$WORK/param.conf"
	CAMPMIXER_CMDLINE="$WORK/cmdline" CAMPMIXER_PARAM_CONF="$WORK/param.conf" sh "$NOFSCK" || fail "nofsck failed for: $1"
	cat "$WORK/param.conf"
}

CMD='console=tty1 root=PARTUUID=3a1867d2-02 rootfstype=ext4 fsck.repair=yes rootwait cfg80211.ieee80211_regdom=AU'
[ "$(nofsck "overlayroot=tmpfs $CMD")" = "fastboot=y" ] || fail "locked: the check was not switched off"
[ "$(nofsck "console=tty1 overlayroot=tmpfs:recurse=0 rootwait")" = "fastboot=y" ] || fail "locked, word in the middle"
[ -z "$(nofsck "$CMD")" ] || fail "unlocked: the stock check was switched off"
[ -z "$(nofsck "nooverlayroot=tmpfs $CMD")" ] || fail "a similar word switched the check off"
[ -z "$(nofsck "overlayroot=disabled $CMD")" ] || fail "overlayroot=disabled switched the check off"
# The start-up image asks every script what it depends on. That must not touch anything.
: >"$WORK/param.conf"
[ -z "$(CAMPMIXER_CMDLINE="$WORK/cmdline" CAMPMIXER_PARAM_CONF="$WORK/param.conf" sh "$NOFSCK" prereqs)" ] || fail "prereqs printed something"
[ ! -s "$WORK/param.conf" ] || fail "prereqs wrote to the settings"
# A missing command line must not stop the start-up.
CAMPMIXER_CMDLINE="$WORK/none" CAMPMIXER_PARAM_CONF="$WORK/param.conf" sh "$NOFSCK" || fail "no command line: the script failed"

echo "lock (boot partition and file system check): all checks passed"
