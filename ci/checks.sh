#!/bin/bash
# Every check that needs no Pi. Runs before each image build.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

python3 -m venv .venv
.venv/bin/pip install --quiet websockets
.venv/bin/python tests/test_link.py
.venv/bin/python tests/test_auth.py
.venv/bin/python tests/test_bridge.py
.venv/bin/python tests/test_mixer.py
.venv/bin/python tests/test_netconf.py
.venv/bin/python tests/test_firststart.py
bash tests/test_install.sh
bash tests/test_lock.sh
# The web app's own message code, run outside a browser.
if command -v node >/dev/null; then
	node tests/test_mixer_link.js
	for script in rootfs/opt/campmixer/www/*.js; do node --check "$script"; done
else
	echo "NOTE: node is not installed, so the web app's message code was not checked here"
fi
if command -v shellcheck >/dev/null; then
	shellcheck -S warning install.sh rootfs/usr/local/bin/campmixer rootfs/etc/systemd/system-generators/campmixer-bootro \
		rootfs/etc/initramfs-tools/scripts/init-premount/campmixer-nofsck image/build-image.sh ci/*.sh tests/*.sh
fi
