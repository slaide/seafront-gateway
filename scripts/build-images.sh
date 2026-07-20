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
REG="${REGISTRY:-10.10.0.69:5000}"
# Pinned seafront commit (source of truth). A gateway commit + this SHA + seafront's
# committed uv.lock fully determine the app image. Bump this to roll the app forward;
# override with SEAFRONT_REF=<sha|tag> only for one-off testing.
REF="${SEAFRONT_REF:-dba35b3773dea851939f8964a30b9aa37ec3cd9d}"

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
    # --pull=missing: the base is digest-pinned in the Containerfile, so fetch it only when
    # that exact digest is not already in local storage (i.e. right after a deliberate bump
    # via bump-os-base.sh). Avoids re-downloading the multi-GB base on every build.
    podman build --pull=missing -t "$REG/seafront-os:stable" "$DIR/images/kinoite"
    podman push --tls-verify=false "$REG/seafront-os:stable"
fi

echo "==> pushed to $REG"
