#!/usr/bin/env bash
# Apply the fleet's optional "lab" (second-LAN) addressing from config/microscopes.json.
# Run ON THE GATEWAY.
#
#   scripts/apply-lab-ips.sh            # apply gateway.lab.gateway_ip + every scope's lab_host
#
# Each address is added as a SECONDARY static IP on the machine's backbone NIC (the one that
# already holds a 192.168.50.x address), KEEPING the backbone as the primary path. The backbone
# stays the fleet's internal control + container-registry route (192.168.50.1:5000 is baked into
# the OS image), so we never move off it — we only make the machines ALSO reachable on the lab LAN
# once its switch is bridged in. No default gateway is set on the lab side (inbound reach only).
#
# Idempotent: a machine already carrying its lab IP (live + in its NM profile) is left untouched.
# Uses only `nmcli connection modify` + `nmcli device reapply`, which the boxes' fleet sudoers
# allows passwordless and which apply the new address live WITHOUT bouncing the link (so SSH and
# any running acquisition are undisturbed). Re-run after (re)provisioning a box, like push-config.sh.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FC="python3 $DIR/scripts/fleet_config.py"
SSH_USER="${FLEET_SSH_USER:-pharmbio}"
KEY="${FLEET_SSH_KEY:-$HOME/.ssh/fleet}"
SSH=(ssh -i "$KEY" -o BatchMode=yes -o StrictHostKeyChecking=accept-new
     -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=6)

$FC validate >/dev/null

LAB_SUBNET="$($FC get gateway.lab.subnet 2>/dev/null || true)"
GW_LAB="$($FC get gateway.lab.gateway_ip 2>/dev/null || true)"
if [ -z "$LAB_SUBNET" ]; then
  echo "no gateway.lab in inventory — nothing to do"; exit 0
fi
PREFIX="${LAB_SUBNET##*/}"

# Per-machine applier snippet. Runs as pharmbio; escalates only when a change is needed
# (nmcli is passwordless on the boxes via the fleet sudoers). $1=lab IP, $2=prefix.
read -r -d '' APPLY <<'EOS' || true
set -e
NEWIP="$1"; PREFIX="$2"; CIDR="$NEWIP/$PREFIX"
DEV=$(ip -o -4 addr show | awk '/inet 192\.168\.50\./{print $2; exit}')
[ -n "$DEV" ] || { echo "$(hostname): no backbone (192.168.50.x) interface — skipped"; exit 1; }
live=no; ip -o -4 addr show dev "$DEV" | grep -qw "$NEWIP" && live=yes
CON=$(nmcli -t -f NAME,DEVICE connection show --active | awk -F: -v d="$DEV" '$2==d{print $1; exit}')
inprofile=no
if [ -n "$CON" ]; then
  nmcli -g ipv4.addresses connection show "$CON" | tr ', ' '\n\n' | grep -qxF "$CIDR" && inprofile=yes
fi
if [ "$live" = yes ] && [ "$inprofile" = yes ]; then
  echo "$(hostname): $CIDR already live + persisted on $DEV — ok"; exit 0
fi
[ -n "$CON" ] || { echo "$(hostname): no active NM connection on $DEV — skipped"; exit 1; }
[ "$inprofile" = yes ] || sudo nmcli connection modify "$CON" +ipv4.addresses "$CIDR"
sudo nmcli device reapply "$DEV"
echo "$(hostname): $DEV now = $(ip -o -4 addr show dev "$DEV" | awk '{print $4}' | tr '\n' ' ')"
EOS

# gateway itself (localhost)
if [ -n "$GW_LAB" ]; then
  echo "== gateway -> $GW_LAB/$PREFIX =="
  bash -c "$APPLY" _ "$GW_LAB" "$PREFIX"
fi

# each scope with a lab_host
for name in $($FC names); do
  labh="$($FC lab-host "$name")"
  host="$($FC host "$name")"
  if [ -z "$labh" ]; then echo "== $name: no lab_host — skipped =="; continue; fi
  echo "== $name ($host) -> $labh/$PREFIX =="
  if ! "${SSH[@]}" "$SSH_USER@$host" "bash -s '$labh' '$PREFIX'" <<EOS
$APPLY
EOS
  then echo "   !! $name unreachable or failed"; fi
done
echo "done."
