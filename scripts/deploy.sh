#!/usr/bin/env bash
# Copy this project to the gateway PC and run the installer there.
# Run this FROM your dev machine (workstation). Usage:
#   scripts/deploy.sh [user@host]      (default: pharmbio@lab3.local)
#
# Uses one multiplexed SSH connection (ControlMaster) for the rsync AND the
# installer, so you are asked for the SSH password at most ONCE. The gateway's
# sudo password is then asked once more inside install.sh (install.sh runs
# `sudo -v` up front and caches), unless the box has passwordless sudo.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${1:-pharmbio@lab3.local}"
DEST="microscope-gateway"

# --- one shared, authenticated SSH connection ---------------------------------
CTLDIR="$(mktemp -d "${TMPDIR:-/tmp}/gw-deploy.XXXXXX")"
CTL="$CTLDIR/cm"
SSH_OPTS=(-o ControlMaster=auto -o ControlPath="$CTL" -o ControlPersist=120)
RSH="ssh -o ControlMaster=auto -o ControlPath=$CTL -o ControlPersist=120"
trap 'ssh -o ControlPath="$CTL" -O exit "$TARGET" 2>/dev/null || true; rm -rf "$CTLDIR"' EXIT

echo "==> opening connection to $TARGET (authenticate once)"
ssh "${SSH_OPTS[@]}" "$TARGET" true   # <- the single SSH auth prompt

echo "==> syncing to $TARGET:~/$DEST"
# rsync if available (skips venv/git), else tar over ssh — both over the shared link
if command -v rsync >/dev/null 2>&1; then
  rsync -az --delete \
    --exclude '.git' --exclude '.venv' --exclude '__pycache__' \
    -e "$RSH" "$DIR/" "$TARGET:$DEST/"
else
  tar -C "$DIR" --exclude='.git' --exclude='.venv' --exclude='__pycache__' -cz . \
    | ssh "${SSH_OPTS[@]}" "$TARGET" "mkdir -p $DEST && tar -C $DEST -xz"
fi

echo "==> running installer on $TARGET (sudo will prompt for the gateway password)"
ssh -t "${SSH_OPTS[@]}" "$TARGET" "cd $DEST && bash scripts/install.sh"
