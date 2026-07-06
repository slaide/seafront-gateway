#!/usr/bin/env bash
# Run ON the gateway WITH INTERNET, after build-images.sh (--os). Turns the
# seafront-os OCI image into a BOOTABLE INSTALLER you write to a USB stick and boot
# each box from. The box then installs the fleet image directly — so on first boot it
# ALREADY is the fleet OS (box-postinstall present, registry trust + seafront quadlet
# baked). No "stock Kinoite then bootc switch" dance, which can't work offline anyway.
#
# Uses bootc-image-builder. Output: $OUT/bootiso/install.iso  (dd it to a USB stick).
#
#   scripts/build-installer.sh                 # anaconda-iso (interactive USB installer)
#   TYPE=raw scripts/build-installer.sh        # raw disk image to dd straight onto a disk
#
# VALIDATE THIS ON THE REAL GATEWAY FIRST: bootc-image-builder is picky about rootful
# container storage + registry trust. It pulls the image from the gateway registry, so
# gateway-setup.sh must have installed the insecure-registry trust (it does).
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REG="${REGISTRY:-192.168.50.1:5000}"
OUT="${OUT:-$DIR/out}"
TYPE="${TYPE:-anaconda-iso}"     # anaconda-iso | raw | qcow2
CONFIG="$DIR/images/kinoite/installer.toml"   # optional: bakes the pharmbio user

mkdir -p "$OUT"
ARGS=(--type "$TYPE" --tls-verify=false)
[ -f "$CONFIG" ] && ARGS+=(--config /config.toml)

sudo podman run --rm --privileged --pull=newer \
    --security-opt label=type:unconfined_t \
    -v "$OUT":/output \
    -v /var/lib/containers/storage:/var/lib/containers/storage \
    ${CONFIG:+-v "$CONFIG":/config.toml:ro} \
    quay.io/centos-bootc/bootc-image-builder:latest \
    "${ARGS[@]}" "$REG/seafront-os:stable"

echo "==> installer under $OUT — write it to USB, e.g.:"
echo "    sudo dd if=$OUT/bootiso/install.iso of=/dev/sdX bs=4M status=progress oflag=sync"
