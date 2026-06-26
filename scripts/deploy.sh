#!/usr/bin/env bash
# Copy this project to the gateway PC and run the installer there.
# Run this FROM your dev machine (workstation). Usage:
#   scripts/deploy.sh [user@host]      (default: pharmbio@lab3.local)
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="${1:-pharmbio@lab3.local}"
DEST="microscope-gateway"

echo "==> syncing to $TARGET:~/$DEST"
# rsync if available (skips venv/git), else tar over ssh
if command -v rsync >/dev/null 2>&1; then
  rsync -az --delete \
    --exclude '.git' --exclude '.venv' --exclude '__pycache__' \
    "$DIR/" "$TARGET:$DEST/"
else
  tar -C "$DIR" --exclude='.git' --exclude='.venv' --exclude='__pycache__' -cz . \
    | ssh "$TARGET" "mkdir -p $DEST && tar -C $DEST -xz"
fi

echo "==> running installer on $TARGET (sudo will prompt for the gateway password)"
ssh -t "$TARGET" "cd $DEST && bash scripts/install.sh"
