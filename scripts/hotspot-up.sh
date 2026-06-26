#!/usr/bin/env bash
# Start the Wi-Fi hotspot clients connect to.
# WARNING: this takes the Wi-Fi adapter into AP mode, DISCONNECTING it from its
# current Wi-Fi network (i.e. this machine loses Wi-Fi internet while the hotspot
# is up). The wired link to the microscope switch is unaffected.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

read -r SSID PASS IFACE < <(python3 - "$DIR/config/microscopes.json" <<'PY'
import json, sys
h = json.load(open(sys.argv[1]))["gateway"]["hotspot"]
print(h["ssid"], h["password"], h["wifi_iface"])
PY
)

echo "==> bringing up hotspot '$SSID' on $IFACE (disconnects $IFACE from Wi-Fi internet)"
sudo nmcli device wifi hotspot ifname "$IFACE" ssid "$SSID" password "$PASS"
# make the hotspot reconnect automatically on boot
sudo nmcli connection modify Hotspot connection.autoconnect yes 2>/dev/null || true

echo "==> hotspot up. Connect a client to SSID '$SSID' (password: $PASS)"
echo "    then open the dashboard at:"
nmcli -t -f IP4.ADDRESS device show "$IFACE" | sed 's#IP4.ADDRESS\[1\]:#    http://#; s#/.*#:8000#'
