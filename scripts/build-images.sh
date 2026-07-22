#!/usr/bin/env bash
# Run ON the gateway WHILE IT HAS INTERNET. Build the app and/or OS image and push
# them to the gateway registry. THIS is the one and only step that needs internet;
# after it, the whole fleet updates offline from the registry.
#
#   build-images.sh                    # both images
#   build-images.sh --seafront         # app only (the frequent one)
#   build-images.sh --os               # OS only (rare)
#   SEAFRONT_REF=<sha|branch|tag|latest> build-images.sh --seafront   # test any ref
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REG="${REGISTRY:-10.10.0.69:5000}"
SEAFRONT_REPO="${SEAFRONT_REPO:-https://github.com/slaide/seafront.git}"
# Pinned seafront commit (source of truth) — the reproducible-release default: a gateway
# commit + this SHA + seafront's committed uv.lock fully determine the app image. Keep it
# on a hardware-verified commit; bump it to roll the fleet forward. For testing, override
# SEAFRONT_REF=<sha|branch|tag|latest> (the dashboard exposes a field) — no gateway commit.
REF="${SEAFRONT_REF:-e13a34fe7e6cceface687a14523268f1276b011b}"

# Resolve a symbolic ref (branch / tag / "latest" / "HEAD") to a concrete commit SHA; a
# SHA (full or abbreviated) is used as-is. This keeps the build reproducible AND makes the
# podman git-checkout layer cache-bust correctly — a bare branch name as the build-arg
# keeps the same value as the branch advances, so podman would reuse the stale checkout.
if ! printf '%s' "$REF" | grep -qiE '^[0-9a-f]{7,40}$'; then
    case "$REF" in latest|LATEST|HEAD|"") LOOKUP=HEAD ;; *) LOOKUP="$REF" ;; esac
    RESOLVED="$(git ls-remote "$SEAFRONT_REPO" "$LOOKUP" | awk 'NR==1{print $1}')"
    [ -n "$RESOLVED" ] || { echo "error: cannot resolve SEAFRONT_REF='$REF' at $SEAFRONT_REPO" >&2; exit 1; }
    echo "==> resolved SEAFRONT_REF='$REF' -> $RESOLVED"
    REF="$RESOLVED"
fi

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
        --build-arg SEAFRONT_REPO="$SEAFRONT_REPO" \
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
