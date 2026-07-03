#!/usr/bin/env bash
# Bootstrap fleet control: let the gateway drive each scope's seafront service
# over SSH without passwords. Run this ON THE GATEWAY (it needs backbone access).
#
# For every microscope in config/microscopes.json it:
#   - ensures a dedicated gateway key exists (~/.ssh/fleet)
#   - installs that key in the scope's authorized_keys
#   - drops a NARROW sudoers rule so the login user may run, passwordless:
#       systemctl {start,stop,restart,status,is-active} seafront   and   systemctl reboot
#     (nothing else — no blanket sudo)
#
# This is the one privileged bootstrap that still needs each box's password, so
# it prompts once per host. After it, the dashboard's buttons work unattended.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSH_USER="${FLEET_SSH_USER:-pharmbio}"
KEY="$HOME/.ssh/fleet"

# --- gateway key ---------------------------------------------------------------
if [ ! -f "$KEY" ]; then
  echo "==> generating gateway fleet key: $KEY"
  ssh-keygen -t ed25519 -N "" -f "$KEY" -C "fleet@$(hostname)"
fi
PUB="$(cat "$KEY.pub")"

# --- the narrow sudoers rule ---------------------------------------------------
# %s is the login user. systemctl path is /usr/bin/systemctl on Ubuntu/Arch.
read -r -d '' SUDOERS <<EOF || true
# Installed by seafront-gateway scripts/setup-fleet-control.sh — fleet control only.
$SSH_USER ALL=(root) NOPASSWD: /usr/bin/systemctl start seafront, /usr/bin/systemctl stop seafront, /usr/bin/systemctl restart seafront, /usr/bin/systemctl status seafront, /usr/bin/systemctl is-active seafront, /usr/bin/systemctl reboot
EOF

HOSTS=$(python3 -c "import json;print('\n'.join(m['host'] for m in json.load(open('$DIR/config/microscopes.json'))['microscopes']))")

for host in $HOSTS; do
  echo
  echo "==> $host : installing fleet key + sudoers (password for $SSH_USER@$host)"
  CTLDIR="$(mktemp -d "${TMPDIR:-/tmp}/fleet.XXXXXX")"; CTL="$CTLDIR/cm"
  OPTS=(-o ControlMaster=auto -o ControlPath="$CTL" -o ControlPersist=60 -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8)
  # authorize the key (idempotent), then install sudoers via one -t sudo session
  if ssh "${OPTS[@]}" "$SSH_USER@$host" \
       "install -d -m700 ~/.ssh && touch ~/.ssh/authorized_keys && grep -qxF '$PUB' ~/.ssh/authorized_keys || echo '$PUB' >> ~/.ssh/authorized_keys"; then
    ssh -t "${OPTS[@]}" "$SSH_USER@$host" \
       "printf '%s\n' \"$SUDOERS\" | sudo tee /etc/sudoers.d/seafront-fleet >/dev/null && sudo chmod 440 /etc/sudoers.d/seafront-fleet && sudo visudo -cf /etc/sudoers.d/seafront-fleet && echo '   sudoers OK'"
    echo "==> $host : done"
  else
    echo "!! $host : could not connect — skipped"
  fi
  ssh -o ControlPath="$CTL" -O exit "$SSH_USER@$host" 2>/dev/null || true
  rm -rf "$CTLDIR"
done

echo
echo "==> verifying passwordless access:"
for host in $HOSTS; do
  printf '   %s: ' "$host"
  ssh -i "$KEY" -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new \
    "$SSH_USER@$host" "sudo -n /usr/bin/systemctl is-active seafront" 2>/dev/null \
    | head -1 || echo "unreachable / not set up"
done
