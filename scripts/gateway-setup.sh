#!/usr/bin/env bash
# Run ON the gateway (Fedora Kinoite — the SAME OS as the boxes), once, with internet.
# Kinoite is immutable: nothing is installed into the base. The gateway's services run
# as podman quadlets (registry, Caddy) — the same substrate the boxes use — and the
# dashboard runs as a host service off a uv venv inside this checkout (no base changes).
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# The checkout must NOT live under a home directory. SELinux (enforcing on Kinoite) labels
# everything under /home (== /var/home) and /root 'user_home_t'/'admin_home_t', and the
# dashboard's systemd service domain is forbidden from exec'ing those files — the venv's
# uvicorn dies with "Permission denied" even though you can run it by hand. Fail loudly with
# the exact fix instead of installing a service that can never start.
case "$DIR" in
    /home/*|/var/home/*|/root/*|/var/roothome/*)
        cat >&2 <<EOF
!! Refusing to install from a home directory:
!!     $DIR
!! SELinux labels this 'user_home_t'; a systemd service cannot exec the venv here
!! (audit: avc denied { execute } ... user_home_t). Move the checkout under /opt:
!!
!!     sudo mv "$DIR" /opt/seafront-gateway
!!     sudo chown -R "\$USER:\$(id -gn)" /opt/seafront-gateway
!!     cd /opt/seafront-gateway && bash scripts/gateway-setup.sh
EOF
        exit 1 ;;
esac

echo "==> appliance power: never auto-suspend"
# This gateway is a Kinoite DESKTOP; left alone it suspends after idle and takes the
# registry, dashboard, Caddy and ssh down with it (the whole fleet control plane goes
# dark). Mask the sleep targets: DE-independent, permanent, cannot be re-armed by KDE.
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target

echo "==> uv (dashboard runtime; installs into ~/.local, base untouched)"
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

echo "==> fleet SSH key (gateway -> boxes; .pub is baked into the box image)"
[ -f "$HOME/.ssh/fleet" ] || ssh-keygen -t ed25519 -N '' -f "$HOME/.ssh/fleet" -C fleet@squidway

echo "==> dashboard venv"
# Rebuild from scratch: a stale .venv (e.g. left by a repo move/rename) makes `uv sync`
# a near-no-op that leaves console scripts with dead shebangs -> systemd 203/EXEC.
rm -rf "$DIR/dashboard/.venv"
( cd "$DIR/dashboard" && uv sync )
# Label the freshly built tree with its on-disk defaults (e.g. usr_t under /opt) so the
# service domain may exec the venv. Without this, files created here can retain a context
# the service can't run. No-op on systems without SELinux tooling.
command -v restorecon >/dev/null && sudo restorecon -RF "$DIR"

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
sed "s/__DASHBOARD_PORT__/$DASH_PORT/; s#/opt/seafront-gateway#$DIR#g" \
    "$DIR/systemd/microscope-dashboard.service" \
    | sudo tee /etc/systemd/system/microscope-dashboard.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now microscope-dashboard

echo "==> dashboard sudoers (reboot + the self-elevating fleet scripts)"
# The dashboard runs as pharmbio. Its git pull + rootless-podman image rebuilds need
# no sudo (pharmbio owns $DIR and its own podman storage). What DOES need root:
# rebooting the host, applying config (write /etc/caddy, restart caddy, firewalld), and
# toggling Wi-Fi. apply-config.sh and wifi-mode.sh self-elevate; grant pharmbio those
# two scripts (bare + with args) passwordless so the non-tty dashboard can run them —
# and nothing else. Args are matched with a trailing '*'; avoid ':' in any argument
# (SSIDs/passwords), which sudoers parses as a host/command separator.
sudo install -m 0440 /dev/stdin /etc/sudoers.d/seafront-gateway <<EOF
pharmbio ALL=(root) NOPASSWD: /usr/sbin/reboot, /usr/bin/systemctl reboot, $DIR/scripts/apply-config.sh, $DIR/scripts/apply-config.sh *, $DIR/scripts/wifi-mode.sh, $DIR/scripts/wifi-mode.sh *
EOF
sudo visudo -cf /etc/sudoers.d/seafront-gateway

echo "==> remote management: enable sshd so the gateway is reachable over ssh"
# Kinoite ships sshd OFF by default (images/kinoite/Containerfile enables it on the boxes
# for the same reason). The gateway needs it too, or there is no way in over the network.
sudo systemctl enable --now sshd

echo "==> firewall: open ssh + registry + dashboard + proxy ports (Kinoite runs firewalld)"
if command -v firewall-cmd >/dev/null; then
    sudo firewall-cmd --permanent --add-service=ssh >/dev/null
    PORTS=$(python3 -c "import json;d=json.load(open('$DIR/config/microscopes.json'));print(5000);print(d['gateway']['dashboard_port']);[print(m['proxy_port']) for m in d['microscopes']]")
    for p in $PORTS; do sudo firewall-cmd --permanent --add-port="$p/tcp" >/dev/null; done
    sudo firewall-cmd --reload
fi

echo "==> gateway ready. next (with internet):  scripts/build-images.sh"
# Wi-Fi is intentionally left in whatever mode it is in now — during setup and the
# build-images step below the gateway needs its client-mode internet. Once images are
# built, run `scripts/wifi-mode.sh ap` to become the hotspot laptops connect to; that
# choice then persists across reboots (NetworkManager autoconnect). Flip back with
# `scripts/wifi-mode.sh client` whenever you need internet to rebuild images.
echo "==> Wi-Fi: left as-is (client/internet). After build-images: scripts/wifi-mode.sh ap"
