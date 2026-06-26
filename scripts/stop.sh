#!/usr/bin/env bash
# Stop the gateway services. Does not touch the hotspot.
set -euo pipefail
sudo systemctl stop caddy microscope-dashboard
echo "==> caddy + dashboard stopped"
