#!/usr/bin/env bash
# Regenerate the Caddyfile + firewall from config/microscopes.json and reload services.
# Run after any inventory change (add/remove/renumber a scope, or a hand edit).
#
#   scripts/apply-config.sh                  # full: caddy + firewall + dashboard unit
#   scripts/apply-config.sh --no-dashboard   # caddy + firewall only; leave the dashboard
#                                            # running (it reads the inventory live, so it
#                                            # needs no restart). This is what the dashboard
#                                            # itself calls, so it never kills its own request.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Needs root (write /etc/caddy, restart caddy, firewalld). Self-elevate so every caller
# — an operator on the console, add/remove-scope, set-box-ip, or the non-tty dashboard —
# runs it the same way: prompt on a terminal, passwordless (fleet sudoers) for the
# dashboard. gateway-setup.sh grants pharmbio NOPASSWD for this exact script.
if [ "$(id -u)" != 0 ]; then
  if [ -t 0 ]; then exec sudo "$0" "$@"; else exec sudo -n "$0" "$@"; fi
fi

FC="python3 $DIR/scripts/fleet_config.py"
# apply-config manages this proxy-port window in the firewall; ports removed from the
# inventory inside it get closed. Keep in sync with PROXY_PORT_RANGE in fleet_config.py.
PROXY_LO=8001 PROXY_HI=8099

RESTART_DASHBOARD=1
for a in "$@"; do
  case "$a" in
    --no-dashboard) RESTART_DASHBOARD=0 ;;
    -*) echo "unknown option: $a" >&2; exit 1 ;;
  esac
done

# Fail fast on a bad inventory rather than half-applying it.
$FC validate

# --- Caddy: regenerate + reload -----------------------------------------------
python3 "$DIR/scripts/gen-caddyfile.py"
install -m 644 "$DIR/Caddyfile" /etc/caddy/Caddyfile
systemctl restart caddy   # Caddy is a podman quadlet container — restart, not reload

# --- Firewall: open configured ports, close stale proxy-range ports -----------
# gateway-setup.sh opens ports at first install; do it here too so add/remove take
# effect without re-running setup. Idempotent.
if command -v firewall-cmd >/dev/null; then
  CONFIGURED="$($FC all-ports)"   # dashboard_port + every proxy_port, one per line
  firewall-cmd --permanent --add-service=ssh >/dev/null 2>&1 || true
  for p in $CONFIGURED 5000; do
    firewall-cmd --permanent --add-port="$p/tcp" >/dev/null 2>&1 || true
  done
  # Prune proxy-range ports that are open but no longer in the inventory (a removed scope).
  OPEN="$(firewall-cmd --permanent --list-ports 2>/dev/null | tr ' ' '\n' | sed 's#/tcp##')"
  for p in $OPEN; do
    [[ "$p" =~ ^[0-9]+$ ]] || continue
    if [ "$p" -ge "$PROXY_LO" ] && [ "$p" -le "$PROXY_HI" ] && ! grep -qx "$p" <<<"$CONFIGURED"; then
      firewall-cmd --permanent --remove-port="$p/tcp" >/dev/null 2>&1 || true
      echo "   firewall: closed stale :$p"
    fi
  done
  firewall-cmd --reload >/dev/null
fi

# --- Dashboard unit (only when the dashboard port may have changed) -----------
if [ "$RESTART_DASHBOARD" = 1 ]; then
  DASH_PORT="$($FC get gateway.dashboard_port)"
  sed "s/__DASHBOARD_PORT__/$DASH_PORT/; s#/opt/seafront-gateway#$DIR#g" \
    "$DIR/systemd/microscope-dashboard.service" \
    > /etc/systemd/system/microscope-dashboard.service
  systemctl daemon-reload
  systemctl restart microscope-dashboard
  echo "==> config applied (Caddy + firewall reloaded, dashboard restarted on port $DASH_PORT)"
else
  echo "==> config applied (Caddy + firewall reloaded; dashboard left running)"
fi

# We ran as root, so files regenerated in the checkout (Caddyfile) are now root-owned
# and would later break pharmbio's git pull / gen steps. Hand them back to the checkout owner.
chown "$(stat -c '%U:%G' "$DIR")" "$DIR/Caddyfile" 2>/dev/null || true
