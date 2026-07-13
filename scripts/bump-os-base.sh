#!/usr/bin/env bash
# Re-pin the Fedora Kinoite base of the OS image (images/kinoite/Containerfile) to a
# specific digest. The base is digest-pinned so ordinary rebuilds reuse the cached base
# instead of re-downloading it; this is the deliberate way to move it forward. Needs
# internet (it resolves the digest from the registry) — run it where the gateway has a link.
#
#   scripts/bump-os-base.sh          # re-pin to the CURRENT digest of the tag already pinned
#   scripts/bump-os-base.sh 45       # move to Fedora 45 and pin its current digest
#
# Then: commit images/kinoite/Containerfile, and on the gateway run
#   scripts/build-images.sh --os     # pulls the new base ONCE, boxes then upgrade offline
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CF="$DIR/images/kinoite/Containerfile"
REPO="quay.io/fedora-ostree-desktops/kinoite"

# Tag: explicit arg, else the one currently pinned in the Containerfile's FROM line.
TAG="${1:-$(grep -oE "kinoite:[0-9]+" "$CF" | head -1 | cut -d: -f2)}"
[ -n "$TAG" ] || { echo "!! could not determine a tag; pass one, e.g. $0 44" >&2; exit 1; }

echo "==> resolving current digest of $REPO:$TAG"
if command -v skopeo >/dev/null; then
  # Manifest only — cheap; the actual base download happens later at build time.
  DIGEST="$(skopeo inspect --format '{{.Digest}}' "docker://$REPO:$TAG")"
else
  podman pull "$REPO:$TAG" >/dev/null
  DIGEST="$(podman image inspect "$REPO:$TAG" --format '{{.Digest}}')"
fi
[ -n "$DIGEST" ] || { echo "!! failed to resolve digest" >&2; exit 1; }
echo "==> $REPO:$TAG -> $DIGEST"

# Rewrite the single FROM line (dots in the repo escaped for the regex).
sed -i -E "s#^FROM ${REPO//./\\.}.*#FROM ${REPO}:${TAG}@${DIGEST}#" "$CF"
grep -n "^FROM " "$CF" | sed 's/^/   /'
echo "==> updated $CF — commit it, then (with internet): scripts/build-images.sh --os"
