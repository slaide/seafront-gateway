#!/usr/bin/env bash
# Run ON the gateway WHILE IT HAS INTERNET. Build the app and/or OS image and push
# them to the gateway registry. THIS is the one and only step that needs internet;
# after it, the whole fleet updates offline from the registry.
#
#   build-images.sh                    # both images
#   build-images.sh --seafront         # app only (the frequent one)
#   build-images.sh --os               # OS only (rare)
#   SEAFRONT_REF=<sha|tag> build-images.sh --seafront
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REG="${REGISTRY:-192.168.50.1:5000}"
REF="${SEAFRONT_REF:-main}"

DO_APP=0; DO_OS=0
[ $# -eq 0 ] && { DO_APP=1; DO_OS=1; }
for a in "$@"; do case "$a" in
    --seafront) DO_APP=1 ;;
    --os)       DO_OS=1 ;;
    *) echo "unknown arg: $a" >&2; exit 1 ;;
esac; done

if [ "$DO_APP" = 1 ]; then
    echo "==> build seafront:$REF"
    podman build --pull -t "$REG/seafront:stable" \
        --build-arg SEAFRONT_REF="$REF" "$DIR/images/seafront"
    podman push --tls-verify=false "$REG/seafront:stable"
fi

if [ "$DO_OS" = 1 ]; then
    echo "==> bake gateway fleet key into the OS image context"
    cp "$HOME/.ssh/fleet.pub" "$DIR/images/kinoite/fleet.pub"
    echo "==> build seafront-os"
    podman build --pull -t "$REG/seafront-os:stable" "$DIR/images/kinoite"
    podman push --tls-verify=false "$REG/seafront-os:stable"
fi

echo "==> pushed to $REG"
