#!/usr/bin/env bash
# Show the state of the gateway: services, listening ports, hotspot.
set -uo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "=== services ==="
for s in caddy microscope-dashboard; do
  printf '%-22s %s\n' "$s" "$(systemctl is-active "$s" 2>/dev/null)"
done

echo; echo "=== listening ports ==="
PORTS=$(python3 - "$DIR/config/microscopes.json" <<'PY'
import json, sys
c = json.load(open(sys.argv[1]))
ports = [c["gateway"]["dashboard_port"]] + [m["proxy_port"] for m in c["microscopes"]]
print("|".join(f":{p} " for p in ports))
PY
)
ss -tlnp 2>/dev/null | grep -E "$PORTS" || echo "(no configured ports listening)"

echo; echo "=== hotspot ==="
IFACE=$(python3 - "$DIR/config/microscopes.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))["gateway"]["hotspot"]["wifi_iface"])
PY
)
nmcli -t -f GENERAL.STATE,IP4.ADDRESS device show "$IFACE" 2>/dev/null | sed 's/^/  /'
nmcli -t -f NAME,DEVICE connection show --active | grep -i hotspot && echo "  hotspot: ACTIVE" || echo "  hotspot: inactive"
