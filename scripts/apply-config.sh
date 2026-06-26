#!/usr/bin/env bash
# Regenerate the Caddyfile from config/microscopes.json and reload services.
# Run this after editing config/microscopes.json (e.g. adding a microscope).
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

python3 "$DIR/scripts/gen-caddyfile.py"
sudo install -m 644 "$DIR/Caddyfile" /etc/caddy/Caddyfile
sudo systemctl reload caddy
sudo systemctl restart microscope-dashboard
echo "==> config applied (Caddy reloaded, dashboard restarted)"
