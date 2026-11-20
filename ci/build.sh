#!/bin/bash
# Builds the image on a GitHub machine.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

# The build needs about 10 GB. Clear out tools these machines carry that it never uses.
sudo rm -rf /usr/share/dotnet /usr/local/lib/android /opt/ghc /opt/hostedtoolcache/CodeQL /usr/share/swift 2>/dev/null || true
df -h / | tail -n1

bash image/build-image.sh
