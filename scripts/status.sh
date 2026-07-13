#!/usr/bin/env bash
# Show the state of the gateway: services, listening ports, Wi-Fi.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FC="python3 $DIR/scripts/fleet_config.py"

echo "=== services ==="
for s in caddy microscope-dashboard; do
  printf '%-22s %s\n' "$s" "$(systemctl is-active "$s" 2>/dev/null)"
done

echo; echo "=== listening ports ==="
PORTS=$($FC all-ports | sed 's/^/:/;s/$/ /' | paste -sd'|')
ss -tlnp 2>/dev/null | grep -E "$PORTS" || echo "(no configured ports listening)"

echo; echo "=== wifi ==="
# Delegate to wifi-mode.sh (single source of Wi-Fi state) if present; otherwise a
# minimal fallback so status still works before Track B is deployed.
if [ -x "$DIR/scripts/wifi-mode.sh" ]; then
  bash "$DIR/scripts/wifi-mode.sh" status | sed 's/^/  /'
else
  IFACE=$($FC get gateway.wifi.iface)
  echo "  desired mode: $($FC get gateway.wifi.mode)"
  nmcli -t -f GENERAL.STATE,IP4.ADDRESS device show "$IFACE" 2>/dev/null | sed 's/^/  /'
fi
