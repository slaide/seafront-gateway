#!/usr/bin/env bash
# Register a new microscope with the gateway (inventory + proxy + firewall + dashboard).
# Run ON THE GATEWAY.
#
#   scripts/add-scope.sh <name> <ip> [proxy_port] [--type T] [--seafront-port P]
#   scripts/add-scope.sh squid5 192.168.50.15            # next free proxy_port auto-assigned
#   scripts/add-scope.sh microdisplay 192.168.50.30 --type display
#
# This is the gateway side only. A brand-new box still needs its first address set at
# the keyboard (`box-postinstall <name> <ip>`), because a box with no IP is invisible
# on the DHCP-less backbone. After that, and after creating configs/<name>/config.json,
# run `scripts/push-config.sh <name>`.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ $# -ge 2 ] || { sed -n '2,14p' "$0"; exit 1; }

# fleet_config validates (unique name/IP/port, IP in backbone subnet) and saves atomically.
python3 "$DIR/scripts/fleet_config.py" add "$@"
"$DIR/scripts/apply-config.sh"

NAME="$1"
echo "==> next: box-postinstall $NAME <ip> at the box, create configs/$NAME/config.json, then push-config.sh $NAME"
