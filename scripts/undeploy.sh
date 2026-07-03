#!/usr/bin/env bash
# Remove the gateway from a host (reverse of install.sh). Run FROM your dev machine.
#   scripts/undeploy.sh [user@host] [--purge]      (default host: pharmbio@lab3.local)
#
# Stops + disables the `caddy` and `microscope-dashboard` services and removes the
# generated dashboard unit + the Caddyfile. It does NOT touch seafront, so this is
# safe on lab3 (which runs its own seafront on :8000 alongside the test gateway).
#   --purge   additionally apt-removes Caddy and deletes the ~/microscope-gateway
#             checkout. Omit it to keep both around for re-deploying later.
#
# Like deploy.sh, uses one multiplexed SSH connection so you authenticate once
# (sudo on the target prompts once more, unless it has passwordless sudo).
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

TARGET=""; PURGE=0
for a in "$@"; do
  case "$a" in
    --purge) PURGE=1 ;;
    -*) echo "unknown option: $a" >&2; exit 1 ;;
    *) TARGET="$a" ;;
  esac
done
TARGET="${TARGET:-pharmbio@lab3.local}"
DEST="microscope-gateway"

# --- one shared, authenticated SSH connection ---------------------------------
CTLDIR="$(mktemp -d "${TMPDIR:-/tmp}/gw-undeploy.XXXXXX")"
CTL="$CTLDIR/cm"
SSH_OPTS=(-o ControlMaster=auto -o ControlPath="$CTL" -o ControlPersist=120)
trap 'ssh -o ControlPath="$CTL" -O exit "$TARGET" 2>/dev/null || true; rm -rf "$CTLDIR"' EXIT

echo "==> opening connection to $TARGET (authenticate once)"
ssh "${SSH_OPTS[@]}" "$TARGET" true

echo "==> tearing down gateway on $TARGET (purge=$PURGE; sudo will prompt)"
ssh -t "${SSH_OPTS[@]}" "$TARGET" "PURGE=$PURGE DEST=$DEST bash -s" <<'REMOTE'
set -uo pipefail
echo "==> caching sudo credentials"; sudo -v

echo "==> stopping + disabling gateway services"
sudo systemctl disable --now microscope-dashboard 2>/dev/null || true
sudo systemctl disable --now caddy               2>/dev/null || true

echo "==> removing generated dashboard unit + Caddyfile"
sudo rm -f /etc/systemd/system/microscope-dashboard.service
sudo rm -f /etc/caddy/Caddyfile   # our generated config; blank so a stray start won't proxy stale hosts
sudo systemctl daemon-reload

if [ "${PURGE:-0}" = 1 ]; then
  echo "==> purging Caddy + checkout (~/$DEST)"
  sudo apt-get purge -y caddy 2>/dev/null || true
  sudo apt-get autoremove -y   2>/dev/null || true
  rm -rf "$HOME/$DEST"
fi

echo "==> seafront left untouched: $(systemctl is-active seafront 2>/dev/null || echo 'no seafront service')"
echo "==> undeploy done."
REMOTE
