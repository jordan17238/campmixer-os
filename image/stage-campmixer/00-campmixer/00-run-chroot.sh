#!/bin/bash -e
# Runs inside the image. The same installer as on a live Pi, told that it is building an image.

CAMPMIXER_IMAGE_BUILD=1 bash /usr/local/src/campmixer-os/install.sh
