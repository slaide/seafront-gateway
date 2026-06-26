#!/usr/bin/env bash
# Start (or restart) the gateway services. Does not touch the hotspot.
set -euo pipefail
sudo systemctl restart caddy microscope-dashboard
echo "==> caddy + dashboard started"
