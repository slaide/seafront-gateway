#!/usr/bin/env bash
# Regenerate the Caddyfile from config/microscopes.json and reload services.
# Run this after editing config/microscopes.json (e.g. adding a microscope).
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

python3 "$DIR/scripts/gen-caddyfile.py"
sudo install -m 644 "$DIR/Caddyfile" /etc/caddy/Caddyfile
sudo systemctl reload caddy

# regenerate the dashboard unit (the port may have changed in config)
DASH_PORT=$(python3 -c "import json;print(json.load(open('$DIR/config/microscopes.json'))['gateway']['dashboard_port'])")
sed "s/__DASHBOARD_PORT__/$DASH_PORT/" "$DIR/systemd/microscope-dashboard.service" \
  | sudo tee /etc/systemd/system/microscope-dashboard.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl restart microscope-dashboard
echo "==> config applied (Caddy reloaded, dashboard restarted on port $DASH_PORT)"
