#!/usr/bin/env bash
# Remove a microscope from the gateway (inventory + proxy + firewall). Run ON THE
# GATEWAY. This only de-registers it here; it does not touch the box itself.
#
#   scripts/remove-scope.sh <name>                  # drop from inventory + proxy
#   scripts/remove-scope.sh <name> --purge-config   # also delete its central config store
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PURGE=0; NAME=""
for a in "$@"; do
  case "$a" in
    --purge-config) PURGE=1 ;;
    -*) echo "unknown option: $a" >&2; exit 1 ;;
    *) NAME="$a" ;;
  esac
done
[ -n "$NAME" ] || { sed -n '2,7p' "$0"; exit 1; }

python3 "$DIR/scripts/fleet_config.py" remove "$NAME"
"$DIR/scripts/apply-config.sh"

if [ "$PURGE" = 1 ] && [ -d "$DIR/configs/$NAME" ]; then
  rm -rf "$DIR/configs/$NAME"
  echo "==> purged central config store configs/$NAME"
fi
