#!/bin/bash -e
# Put the CampMixer kit inside the image. build-image.sh copies the kit into files/ first.

install -d "${ROOTFS_DIR}/usr/local/src/campmixer-os"
cp -r files/. "${ROOTFS_DIR}/usr/local/src/campmixer-os/"
