#!/bin/bash
# Builds CampMixer OS: image_<date>-CampMixerOS.img.xz, Raspberry Pi OS Lite (64-bit) with the
# CampMixer layer built in. Write it to a card with Raspberry Pi Imager and the Pi starts as the mixer.
#
# GitHub runs this for you (.github/workflows/build-image.yml and ci/build.sh). To run it yourself you need a
# Linux machine with Docker and git, about 10 GB of disk and an hour or two:
#   ./build-image.sh
# It downloads Raspberry Pi's own image builder (pi-gen), adds the CampMixer stage and builds.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KIT="$(dirname "$HERE")"
WORK="${CAMPMIXER_BUILD_DIR:-$HERE/build}"
PIGEN_BRANCH="${PIGEN_BRANCH:-arm64}"

die() {
	echo "ERROR: $*" >&2
	exit 1
}

command -v git >/dev/null || die "git is needed: sudo apt install git"
command -v docker >/dev/null || die "Docker is needed: sudo apt install docker.io"

CONFIG="$HERE/config"
[ -f "$CONFIG" ] || CONFIG="$HERE/config.example"

mkdir -p "$WORK"
if [ ! -d "$WORK/pi-gen/.git" ]; then
	git clone --depth 1 --branch "$PIGEN_BRANCH" https://github.com/RPi-Distro/pi-gen.git "$WORK/pi-gen"
fi
cd "$WORK/pi-gen"
echo "pi-gen is at $(git log -1 --format='%h, %cd' --date=short)"

rm -rf stage-campmixer
cp -r "$HERE/stage-campmixer" stage-campmixer
mkdir -p stage-campmixer/00-campmixer/files
cp -r "$KIT/install.sh" "$KIT/campmixer.txt" "$KIT/VERSION" "$KIT/rootfs" stage-campmixer/00-campmixer/files/
# pi-gen skips scripts that are not executable, and a zip or a FAT card loses that flag.
chmod +x stage-campmixer/prerun.sh stage-campmixer/00-campmixer/00-run.sh
# Only the CampMixer image is wanted, not the plain Lite one as well.
touch stage2/SKIP_IMAGES

cp "$CONFIG" config
# The image carries no usable password: this one is random and thrown away. The real login
# password is set on the card's first start (rootfs/opt/campmixer/firststart.py).
THROWAWAY="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
sed -i "s/^FIRST_USER_PASS=.*/FIRST_USER_PASS=$THROWAWAY/" config

if [ "${CONTINUE:-0}" != 1 ]; then
	docker rm -v pigen_work >/dev/null 2>&1 || true
fi
./build-docker.sh -c config

echo
echo "Done. Write this to the card with Raspberry Pi Imager (Use custom), with no customisation:"
ls -1 "$WORK/pi-gen/deploy/"*.img.xz 2>/dev/null || echo "  (look in $WORK/pi-gen/deploy)"
