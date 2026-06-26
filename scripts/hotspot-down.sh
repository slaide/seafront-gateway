#!/usr/bin/env bash
# Stop the Wi-Fi hotspot and return the adapter to normal Wi-Fi (restores internet).
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IFACE=$(python3 - "$DIR/config/microscopes.json" <<'PY'
import json, sys
print(json.load(open(sys.argv[1]))["gateway"]["hotspot"]["wifi_iface"])
PY
)

echo "==> stopping hotspot on $IFACE"
sudo nmcli connection modify Hotspot connection.autoconnect no 2>/dev/null || true
sudo nmcli connection down Hotspot 2>/dev/null || true
sudo nmcli device disconnect "$IFACE" 2>/dev/null || true
sudo nmcli device connect "$IFACE" 2>/dev/null || true
echo "==> $IFACE returning to normal Wi-Fi"
