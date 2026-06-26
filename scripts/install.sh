#!/usr/bin/env bash
# Install the microscope gateway on this machine (run ON the gateway PC).
# Installs uv + Caddy, builds the dashboard venv, generates the Caddyfile, and
# installs/enables both systemd services. Idempotent: safe to re-run.
#
# The Wi-Fi hotspot is NOT started here (it would cut this machine's Wi-Fi
# internet). Start it deliberately with scripts/hotspot-up.sh.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

echo "==> caching sudo credentials"
sudo -v

# --- uv (user-level installer) -------------------------------------------------
UV="$HOME/.local/bin/uv"
if ! [ -x "$UV" ] && ! command -v uv >/dev/null 2>&1; then
  echo "==> installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
[ -x "$UV" ] || UV="$(command -v uv)"

# --- Caddy (official apt repo) -------------------------------------------------
if ! command -v caddy >/dev/null 2>&1; then
  echo "==> installing Caddy"
  sudo apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gnupg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | sudo gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
  sudo apt-get update
  sudo apt-get install -y caddy
fi

# --- dashboard venv ------------------------------------------------------------
echo "==> building dashboard venv"
( cd "$DIR/dashboard" && "$UV" sync )

# --- generate + apply config ---------------------------------------------------
echo "==> generating Caddyfile"
python3 "$DIR/scripts/gen-caddyfile.py"
sudo install -m 644 "$DIR/Caddyfile" /etc/caddy/Caddyfile

# --- systemd services ----------------------------------------------------------
echo "==> installing systemd service"
sudo install -m 644 "$DIR/systemd/microscope-dashboard.service" \
  /etc/systemd/system/microscope-dashboard.service
sudo systemctl daemon-reload
sudo systemctl enable --now caddy
sudo systemctl restart caddy
sudo systemctl enable --now microscope-dashboard

echo
echo "==> done. dashboard: http://$(hostname).local:8000  (and http://<this-host-ip>:8000)"
echo "    proxies: $(python3 -c "import json;print(', '.join(str(m['proxy_port']) for m in json.load(open('$DIR/config/microscopes.json'))['microscopes']))")"
echo "    start the Wi-Fi hotspot when ready:  scripts/hotspot-up.sh"
