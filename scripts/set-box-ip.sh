#!/usr/bin/env bash
# Change a box's backbone IP centrally and re-point the proxy/dashboard at it.
# Run ON THE GATEWAY. Use this to resolve an IP conflict on the larger network, or
# to renumber the fleet.
#
#   scripts/set-box-ip.sh <name> <new-ip>[/prefix]      # /24 assumed if no prefix
#   scripts/set-box-ip.sh squid3 192.168.50.23
#
# The trap this avoids: the new address is applied over the very SSH session that
# applies it, so a naive "set new IP" cuts its own connection and can strand the box.
# Instead we keep the OLD address until the box is CONFIRMED reachable on the NEW one:
#
#   1. add NEW as a *second* address on the box's wired connection (OLD stays up)
#   2. reapply  -> the box answers on BOTH; our SSH (on OLD) is undisturbed
#   3. verify from the gateway that the box answers on NEW
#   4. ok      -> SSH via NEW, drop OLD (leaving only NEW); update inventory; apply
#      not ok  -> remove the NEW address we added (box still on OLD); abort untouched
#
# The new IP must be inside the backbone subnet (validated before anything is touched)
# so the gateway can route to it. Requires the box to run an OS image carrying the
# nmcli fleet-sudoers rule (older images: renumber at the keyboard with box-postinstall).
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FC="python3 $DIR/scripts/fleet_config.py"
SSH_USER="${FLEET_SSH_USER:-pharmbio}"
KEY="${FLEET_SSH_KEY:-$HOME/.ssh/fleet}"
# Boxes are re-imaged (host keys rotate), same as the dashboard: don't pin them.
SSH=(ssh -i "$KEY" -o BatchMode=yes -o StrictHostKeyChecking=no
     -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=5)

NAME="${1:?usage: set-box-ip.sh <name> <new-ip>[/prefix]}"
NEWADDR="${2:?usage: set-box-ip.sh <name> <new-ip>[/prefix]}"
if [[ "$NEWADDR" == */* ]]; then PFX="${NEWADDR#*/}"; NEWIP="${NEWADDR%/*}"; else PFX="24"; NEWIP="$NEWADDR"; fi

OLDIP="$($FC host "$NAME")"
[ -n "$OLDIP" ] || { echo "!! $NAME is not in the inventory" >&2; exit 1; }
if [ "$OLDIP" = "$NEWIP" ]; then echo "==> $NAME already at $NEWIP; nothing to do"; exit 0; fi

# Validate the prospective inventory BEFORE touching the box (in-subnet, no clashes).
python3 - "$DIR" "$NAME" "$NEWIP" <<'PY' || { echo "!! new IP rejected" >&2; exit 1; }
import sys; sys.path.insert(0, f"{sys.argv[1]}/scripts")
import fleet_config as fc
try:
    cfg = fc.load(); fc.set_host(cfg, sys.argv[2], sys.argv[3]); fc.validate(cfg)
except ValueError as e:
    sys.exit(str(e))
PY

# Remote snippet: resolve the box's first wired NIC + its active connection name.
NICCON='NIC=""; for d in /sys/class/net/*; do n=$(basename "$d"); [ "$n" = lo ] && continue; [ -d "$d/wireless" ] && continue; [ "$(cat "$d/type" 2>/dev/null)" = 1 ] && { NIC="$n"; break; }; done; CON=$(nmcli -g GENERAL.CONNECTION device show "$NIC")'

echo "==> $NAME: $OLDIP -> $NEWIP/$PFX"
echo "    [1/4] adding $NEWIP/$PFX as a second address (keeping $OLDIP)"
# reapply can briefly blip the link; tolerate a dropped SSH channel (the change is
# persisted and both addresses come back on reactivation). We confirm via NEW next.
"${SSH[@]}" "$SSH_USER@$OLDIP" \
  "$NICCON; sudo -n nmcli connection modify \"\$CON\" +ipv4.addresses \"$NEWIP/$PFX\" && sudo -n nmcli device reapply \"\$NIC\"" \
  || echo "    (ssh channel dropped during reapply — expected; verifying NEW)"

echo "    [2/4] verifying the box answers on $NEWIP"
OK=0
for i in $(seq 1 10); do
  if "${SSH[@]}" "$SSH_USER@$NEWIP" true 2>/dev/null; then OK=1; break; fi
  sleep 1
done

if [ "$OK" != 1 ]; then
  echo "!! $NAME did not come up on $NEWIP — reverting (box stays on $OLDIP)" >&2
  "${SSH[@]}" "$SSH_USER@$OLDIP" \
    "$NICCON; sudo -n nmcli connection modify \"\$CON\" -ipv4.addresses \"$NEWIP/$PFX\"; sudo -n nmcli device reapply \"\$NIC\"" \
    || echo "!! revert over SSH failed too; recover at the keyboard: box-postinstall $NAME $OLDIP" >&2
  exit 1
fi

echo "    [3/4] confirmed; dropping the old address $OLDIP"
"${SSH[@]}" "$SSH_USER@$NEWIP" \
  "$NICCON; sudo -n nmcli connection modify \"\$CON\" ipv4.addresses \"$NEWIP/$PFX\" && sudo -n nmcli device reapply \"\$NIC\"" \
  || echo "    (ssh channel dropped finalizing — fine; only $NEWIP remains)"

echo "    [4/4] updating inventory + reloading proxy/firewall"
$FC set-host "$NAME" "$NEWIP"
# apply-config self-elevates; run as the current user so the dashboard's non-tty call
# and an operator's console call both work (dashboard has NOPASSWD via fleet sudoers).
"$DIR/scripts/apply-config.sh" --no-dashboard
echo "==> $NAME is now $NEWIP. (Open it via the dashboard / gateway proxy as before.)"
