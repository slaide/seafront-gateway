#!/usr/bin/env bash
# Run ON the gateway (Fedora Kinoite — the SAME OS as the boxes), once, with internet.
# Kinoite is immutable: nothing is installed into the base. The gateway's services run
# as podman quadlets (registry, Caddy) — the same substrate the boxes use — and the
# dashboard runs as a host service off a uv venv in $HOME (no base changes).
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> uv (dashboard runtime; installs into ~/.local, base untouched)"
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

echo "==> fleet SSH key (gateway -> boxes; .pub is baked into the box image)"
[ -f "$HOME/.ssh/fleet" ] || ssh-keygen -t ed25519 -N '' -f "$HOME/.ssh/fleet" -C fleet@squidway

echo "==> dashboard venv"
( cd "$DIR/dashboard" && uv sync )

echo "==> trust the gateway's own registry (so bootc-image-builder / podman can pull it)"
sudo install -d /etc/containers/registries.conf.d
sudo install -m644 "$DIR/images/kinoite/files/etc/containers/registries.conf.d/10-gateway.conf" \
    /etc/containers/registries.conf.d/10-gateway.conf

echo "==> registry + Caddy quadlets"
sudo install -d /var/lib/seafront-registry /etc/caddy /etc/containers/systemd
python3 "$DIR/scripts/gen-caddyfile.py"
sudo install -m644 "$DIR/Caddyfile" /etc/caddy/Caddyfile
sudo install -m644 "$DIR/images/gateway/registry.container" /etc/containers/systemd/registry.container
sudo install -m644 "$DIR/images/gateway/caddy.container"    /etc/containers/systemd/caddy.container
sudo systemctl daemon-reload      # quadlet generator creates registry.service + caddy.service
sudo systemctl start registry caddy

echo "==> dashboard host service"
DASH_PORT=$(python3 -c "import json;print(json.load(open('$DIR/config/microscopes.json'))['gateway']['dashboard_port'])")
sed "s/__DASHBOARD_PORT__/$DASH_PORT/; s#/home/pharmbio/microscope-gateway#$DIR#g" \
    "$DIR/systemd/microscope-dashboard.service" \
    | sudo tee /etc/systemd/system/microscope-dashboard.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now microscope-dashboard

echo "==> firewall: open registry + dashboard + proxy ports (Kinoite runs firewalld)"
if command -v firewall-cmd >/dev/null; then
    PORTS=$(python3 -c "import json;d=json.load(open('$DIR/config/microscopes.json'));print(5000);print(d['gateway']['dashboard_port']);[print(m['proxy_port']) for m in d['microscopes']]")
    for p in $PORTS; do sudo firewall-cmd --permanent --add-port="$p/tcp" >/dev/null; done
    sudo firewall-cmd --reload
fi

echo "==> gateway ready. next (with internet):  scripts/build-images.sh"
