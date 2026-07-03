#!/usr/bin/env bash
# Build a SELF-CONTAINED OFFLINE seafront bundle on a machine WITH internet.
# The bundle carries everything the fleet needs so the gateway can update the
# boxes with NO internet anywhere (the deployment reality):
#   bundle/
#     seafront/        the code at <ref> (no .venv)
#     uvcache/         every wheel for the lock (populated by `uv sync`)
#     uv               the uv binary used (so gateway+boxes match its cache layout)
#     VERSION          what this is
#
# Build it on a Linux x86_64 machine matching the microscopes (Ubuntu) so the
# wheels are ABI-compatible — the gateway itself while it still has internet is
# ideal. Then feed the bundle to the offline gateway via the dashboard upload or
#   deploy-seafront.sh --bundle <bundle.tar.zst>
#
#   build-bundle.sh --ref <sha/tag> [-o out.tar.zst]      # from git (needs internet)
#   build-bundle.sh --url <zip-url>  [-o out.tar.zst]      # from a GitHub zip URL
#   build-bundle.sh --zip <file>     [-o out.tar.zst]      # from a local zip
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_URL="${SEAFRONT_REPO:-https://github.com/slaide/seafront.git}"
UV="$(command -v uv || echo "$HOME/.local/bin/uv")"
[ -x "$UV" ] || { echo "!! uv not found"; exit 1; }

REF=""; URL=""; ZIP=""; OUT=""; expect=""
for a in "$@"; do
  case "$expect" in ref) REF="$a"; expect="";continue;; url) URL="$a";expect="";continue;; zip) ZIP="$a";expect="";continue;; out) OUT="$a";expect="";continue;; esac
  case "$a" in
    --ref) expect=ref ;; --url) expect=url ;; --zip) expect=zip ;; -o|--out) expect=out ;;
    *) echo "unknown arg: $a" >&2; exit 1 ;;
  esac
done
[ -n "$REF$URL$ZIP" ] || { echo "usage: $0 --ref <ref> | --url <zipurl> | --zip <file> [-o out.tar.zst]"; exit 1; }

WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
CODE="$WORK/bundle/seafront"; mkdir -p "$CODE" "$WORK/bundle"

echo "==> assembling code"
if [ -n "$ZIP" ] || [ -n "$URL" ]; then
  z="$ZIP"
  if [ -n "$URL" ]; then z="$WORK/src.zip"; echo "   downloading $URL"; curl -fSL --retry 2 -o "$z" "$URL"; fi
  tmp="$(mktemp -d)"; unzip -q "$z" -d "$tmp"
  inner="$(find "$tmp" -mindepth 1 -maxdepth 1)"
  if [ "$(printf '%s\n' "$inner" | wc -l)" = 1 ] && [ -d "$inner" ]; then ( shopt -s dotglob; mv "$inner"/* "$CODE"/ ); else ( shopt -s dotglob; mv "$tmp"/* "$CODE"/ ); fi
  rm -rf "$tmp"
  VERSION="${URL:+url:$(basename "$URL")}"; VERSION="${VERSION:-zip:$(basename "$ZIP")}"
else
  echo "   cloning $REPO_URL @ $REF"
  git clone -q "$REPO_URL" "$CODE"
  git -C "$CODE" checkout -q "$REF" 2>/dev/null || git -C "$CODE" reset --hard "$REF"
  VERSION="git:$(git -C "$CODE" rev-parse --short HEAD)"
fi

echo "==> populating wheel cache (uv sync — this is the step that needs internet)"
export UV_CACHE_DIR="$WORK/bundle/uvcache"
( cd "$CODE" && "$UV" sync )
rm -rf "$CODE/.venv"                       # keep code + wheels, drop the local venv
cp "$UV" "$WORK/bundle/uv"                 # ship the exact uv so cache layout matches
"$UV" --version > "$WORK/bundle/VERSION"
echo "$VERSION" >> "$WORK/bundle/VERSION"

OUT="${OUT:-$DIR/seafront-bundle.tar.zst}"
echo "==> packing $OUT"
tar -C "$WORK/bundle" -caf "$OUT" .
echo "==> done: $OUT ($(du -h "$OUT" | cut -f1)), version: $VERSION"
