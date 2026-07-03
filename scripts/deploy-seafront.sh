#!/usr/bin/env bash
# Distribute the latest seafront to the microscope boxes FROM THE GATEWAY.
# The boxes have no internet (deliberate), so the gateway is the software hub:
# it assembles a canonical checkout and pushes code + the uv package cache over
# the backbone so each box builds its venv fully offline.
#
# Two ways to give the gateway the code:
#   git  (default) — gateway pulls from GitHub (needs the gateway's internet):
#       scripts/deploy-seafront.sh [--ref <sha/tag>] [scope ...]
#   zip  — feed a GitHub "Download ZIP" archive (no git/credentials needed;
#          this is what the dashboard upload button uses):
#       scripts/deploy-seafront.sh --zip seafront-main.zip [scope ...]
#
# Other flags:
#   --code-only   skip the uv cache sync + uv sync (faster; only if deps unchanged)
#   --all         every scope in microscopes.json (default when none named)
#
# Each box ends up with ~/seafront-app (the code) + a venv built offline from the
# pushed uv cache. It does NOT install/start the service — do that per box when
# its microscope is connected:  install-seafront-service.sh <profile> --dir ~/seafront-app
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSH_USER="${FLEET_SSH_USER:-pharmbio}"
KEY="$HOME/.ssh/fleet"
REPO_URL="${SEAFRONT_REPO:-https://github.com/slaide/seafront.git}"
SRC="$HOME/seafront-dist"          # canonical checkout on the gateway
DEST="seafront-app"                # standard path on each box (relative to ~)
UV="$HOME/.local/bin/uv"; command -v uv >/dev/null 2>&1 && UV="$(command -v uv)"
SSH=(ssh -i "$KEY" -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new)
RSH="ssh -i $KEY -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new"

REF="origin/main"; ZIP=""; URL=""; BUNDLE=""; BUNDLED=""; CODE_ONLY=0; STAGE=1; PUSH=1; SCOPES=(); expect=""
for a in "$@"; do
  case "$expect" in
    ref) REF="$a"; expect=""; continue ;;
    zip) ZIP="$a"; expect=""; continue ;;
    url) URL="$a"; expect=""; continue ;;
    bundle) BUNDLE="$a"; expect=""; continue ;;
  esac
  case "$a" in
    --ref) expect=ref ;;
    --zip) expect=zip ;;
    --url) expect=url ;;
    --bundle) expect=bundle ;;
    --code-only) CODE_ONLY=1 ;;
    --stage-only) PUSH=0 ;;   # assemble the version onto the gateway; touch no boxes
    --push-only) STAGE=0 ;;   # push the already-staged gateway version to the named boxes
    --all) SCOPES=() ;;
    -*) echo "unknown option: $a" >&2; exit 1 ;;
    *) SCOPES+=("$a") ;;
  esac
done
[ -z "$expect" ] || { echo "--$expect needs a value"; exit 1; }
if [ ${#SCOPES[@]} -eq 0 ]; then
  SCOPES=($(python3 -c "import json;print(' '.join(m['name'] for m in json.load(open('$DIR/config/microscopes.json'))['microscopes']))"))
fi
host_of() { python3 -c "import json,sys; d=json.load(open('$DIR/config/microscopes.json')); print(next((m['host'] for m in d['microscopes'] if m['name']==sys.argv[1]),''))" "$1"; }

# --- 1. assemble the canonical checkout on the gateway -------------------------
if [ "$STAGE" = 1 ]; then
  DL_ZIP=""
  if [ -n "$BUNDLE" ]; then
    # OFFLINE path: everything comes from the bundle; the gateway needs no internet.
    [ -f "$BUNDLE" ] || { echo "!! bundle not found: $BUNDLE"; exit 1; }
    echo "==> unpacking offline bundle $BUNDLE"
    btmp="$(mktemp -d)"; tar -C "$btmp" -xf "$BUNDLE"
    rm -rf "$SRC"; mkdir -p "$SRC"; ( shopt -s dotglob; mv "$btmp/seafront"/* "$SRC"/ )
    echo "==> installing bundled uv + seeding cache (no network)"
    install -D -m755 "$btmp/uv" "$HOME/.local/bin/uv"; UV="$HOME/.local/bin/uv"
    mkdir -p "$HOME/.cache/uv"; rsync -a "$btmp/uvcache/" "$HOME/.cache/uv/"
    VERSION="$(tail -1 "$btmp/VERSION" 2>/dev/null || echo bundle)"
    rm -rf "$btmp"
    BUNDLED=1   # skip the gateway's online uv sync; boxes still get cache + offline sync
  elif [ -n "$URL" ] || [ -n "$ZIP" ]; then
    if [ -n "$URL" ]; then
      command -v curl >/dev/null 2>&1 || { echo "!! need curl"; exit 1; }
      echo "==> downloading $URL (on the gateway — no CORS, uses the gateway's internet)"
      DL_ZIP="$(mktemp --suffix=.zip)"; curl -fSL --retry 2 -o "$DL_ZIP" "$URL"; ZIP="$DL_ZIP"
    fi
    [ -f "$ZIP" ] || { echo "!! zip not found: $ZIP"; exit 1; }
    command -v unzip >/dev/null 2>&1 || { echo "!! need 'unzip' (sudo apt-get install unzip)"; exit 1; }
    echo "==> extracting $ZIP -> $SRC"
    tmp="$(mktemp -d)"; unzip -q "$ZIP" -d "$tmp"
    rm -rf "$SRC"; mkdir -p "$SRC"
    # GitHub archives wrap everything in a single top-level dir (e.g. seafront-main/)
    inner="$(find "$tmp" -mindepth 1 -maxdepth 1)"
    if [ "$(printf '%s\n' "$inner" | wc -l)" = 1 ] && [ -d "$inner" ]; then
      ( shopt -s dotglob; mv "$inner"/* "$SRC"/ )
    else
      ( shopt -s dotglob; mv "$tmp"/* "$SRC"/ )
    fi
    rm -rf "$tmp"; [ -n "$DL_ZIP" ] && rm -f "$DL_ZIP"
    VERSION="${URL:+url:$(basename "$URL")}"; VERSION="${VERSION:-zip:$(basename "$ZIP")}"
  else
    [ -d "$SRC/.git" ] || { echo "==> cloning $REPO_URL -> $SRC"; git clone "$REPO_URL" "$SRC"; }
    echo "==> updating $SRC to $REF"
    git -C "$SRC" fetch --all --prune; git -C "$SRC" reset --hard "$REF"
    VERSION="git:$(git -C "$SRC" rev-parse --short HEAD)"
  fi
  # stamp the version so boxes (which may lack .git) can report what they run
  echo "$VERSION" > "$SRC/DEPLOYED_VERSION"
  echo "==> gateway staged $VERSION"
  if [ "$CODE_ONLY" = 0 ] && [ -z "$BUNDLED" ]; then
    echo "==> uv sync on the gateway (populates the uv cache with all wheels)"
    ( cd "$SRC" && "$UV" sync )
  fi
else
  VERSION="$(cat "$SRC/DEPLOYED_VERSION" 2>/dev/null || echo staged)"
fi

# --- 2. push (flush) the staged version to each named box over the backbone ----
if [ "$PUSH" = 1 ]; then
for scope in "${SCOPES[@]}"; do
  host="$(host_of "$scope")"
  [ -n "$host" ] || { echo "!! $scope: not in microscopes.json — skipped"; continue; }
  echo
  echo "========== $scope ($host) =========="
  if ! "${SSH[@]}" "$SSH_USER@$host" true 2>/dev/null; then
    echo "!! unreachable (fleet key/backbone) — skipped"; continue
  fi
  echo "-- code -> ~/$DEST"
  rsync -az --delete --exclude '.venv' --exclude '__pycache__' \
    -e "$RSH" "$SRC/" "$SSH_USER@$host:$DEST/"
  if [ "$CODE_ONLY" = 0 ]; then
    # Ship the gateway's uv binary first: the box's uv must match ours, else it
    # reads a different cache layout and can't find the wheels we push (the exact
    # cause of the earlier opencv "not found in cache" offline failure).
    echo "-- uv binary (align version to the gateway's)"
    rsync -az -e "$RSH" "$UV" "$SSH_USER@$host:.local/bin/uv"
    echo "-- uv cache (incremental)"
    rsync -az -e "$RSH" "$HOME/.cache/uv/" "$SSH_USER@$host:.cache/uv/"
    echo "-- uv sync --offline on the box"
    "${SSH[@]}" "$SSH_USER@$host" "cd ~/$DEST && ~/.local/bin/uv sync --offline"
  fi
  echo "==> $scope now at $("${SSH[@]}" "$SSH_USER@$host" "cat ~/$DEST/DEPLOYED_VERSION 2>/dev/null")"
done
fi

echo
if [ "$PUSH" = 1 ]; then
  echo "==> done ($VERSION)"
else
  echo "==> staged $VERSION on the gateway; no boxes touched (flush per box separately)"
fi
