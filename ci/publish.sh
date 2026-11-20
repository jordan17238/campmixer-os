#!/bin/bash
# Publishes the built image under the repository's Releases.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

version="$(cat VERSION)"
build="${RUN_NUMBER:-0}"
built="$(find image/build/pi-gen/deploy -maxdepth 1 -name '*.img.xz' | head -n1)"
[ -n "$built" ] || { echo "No image was built." >&2; exit 1; }
name="CampMixerOS-${version}-build${build}.img.xz"
sudo mv "$built" "$name"
sudo chown "$(id -u):$(id -g)" "$name"
sha256sum "$name" | tee "$name.sha256"
ls -l "$name"

flags=()
if [ "${GITHUB_REF_NAME:-main}" != main ]; then
	flags+=(--prerelease)
fi
gh release create "v${version}-build${build}" "$name" "$name.sha256" \
	--target "${GITHUB_SHA}" \
	--title "CampMixer OS ${version}, build ${build}" \
	--notes "Write the .img.xz file to a microSD card with Raspberry Pi Imager (choose Use custom, and skip customisation). The first-start steps are in README.md." \
	"${flags[@]}"
