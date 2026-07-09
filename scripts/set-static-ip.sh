#!/usr/bin/env bash
# Assign a STATIC IPv4 address to this computer's wired NetworkManager profile
# (default "Wired connection 1"). Run ON THE COMPUTER you want to configure — the
# laptop you plug into the backbone, or a box during bring-up.
#
# The backbone (192.168.50.0/24) is ISOLATED: no DHCP, no internet, no gateway.
# So by default this sets ONLY an address (never a default route) — otherwise the
# wired link would steal the default route from WiFi and kill your internet. The
# profile is persisted, autoconnects, and reactivates the moment a cable link
# appears (a fresh box / unplugged cable shows NO-CARRIER until then).
#
#   scripts/set-static-ip.sh 192.168.50.100          # /24 assumed; no gateway
#   scripts/set-static-ip.sh 192.168.50.100/24
#   scripts/set-static-ip.sh 192.168.50.100 --con "Wired connection 1"
#   scripts/set-static-ip.sh 10.0.0.5/24 --gateway 10.0.0.1 --dns 10.0.0.1
#   scripts/set-static-ip.sh --dhcp                  # revert the profile to DHCP
set -euo pipefail

CON="Wired connection 1"
ADDR=""; PREFIX=""; GW=""; DNS=""; MODE="static"

usage() { sed -n '2,16p' "$0"; exit "${1:-0}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --con)     CON="$2"; shift 2 ;;
    --gateway) GW="$2"; shift 2 ;;
    --dns)     DNS="$2"; shift 2 ;;
    --dhcp)    MODE="dhcp"; shift ;;
    -h|--help) usage 0 ;;
    -*) echo "unknown option: $1" >&2; usage 1 ;;
    *)
      [ -n "$ADDR" ] && { echo "unexpected extra argument: $1" >&2; usage 1; }
      ADDR="$1"; shift ;;
  esac
done

# The profile must already exist (NetworkManager creates "Wired connection 1"
# automatically for an unconfigured wired NIC). Bail loudly if it doesn't.
if ! nmcli -t -f NAME connection show | grep -Fxq "$CON"; then
  echo "!! No NetworkManager connection named: $CON" >&2
  echo "!! Existing connections:" >&2
  nmcli -t -f NAME,TYPE connection show | sed 's/^/!!   /' >&2
  echo "!! Pass the right one with --con \"<name>\"." >&2
  exit 1
fi

if [ "$MODE" = "dhcp" ]; then
  echo "==> reverting '$CON' to DHCP"
  sudo nmcli connection modify "$CON" \
    ipv4.method auto \
    ipv4.addresses "" ipv4.gateway "" ipv4.dns "" \
    ipv4.never-default no
else
  [ -n "$ADDR" ] || { echo "!! missing IP address" >&2; usage 1; }
  # split IP/prefix; default to /24 when no prefix is given
  if [[ "$ADDR" == */* ]]; then PREFIX="${ADDR#*/}"; ADDR="${ADDR%/*}"; else PREFIX="24"; fi

  # No gateway (isolated backbone) => never-default, so this link never becomes the
  # system default route and WiFi keeps carrying internet. A gateway => allow it.
  if [ -n "$GW" ]; then NEVER_DEFAULT="no"; else NEVER_DEFAULT="yes"; fi

  echo "==> setting '$CON' to static ${ADDR}/${PREFIX}${GW:+  gw $GW}${DNS:+  dns $DNS}"
  [ -z "$GW" ] && echo "    (no gateway: never-default => WiFi keeps the default route / internet)"
  sudo nmcli connection modify "$CON" \
    ipv4.method manual \
    ipv4.addresses "${ADDR}/${PREFIX}" \
    ipv4.gateway "${GW}" \
    ipv4.dns "${DNS}" \
    ipv4.never-default "$NEVER_DEFAULT" \
    connection.autoconnect yes
fi

# Reactivate so the change takes effect now. Fails harmlessly if the port has no
# carrier yet — the profile is saved and will autoconnect on cable insert.
echo "==> activating '$CON'"
if ! sudo nmcli connection up "$CON" >/dev/null 2>&1; then
  echo "    (not active yet — likely NO-CARRIER; plug the cable in and it autoconnects)"
fi

DEV="$(nmcli -g connection.interface-name connection show "$CON")"
[ -n "$DEV" ] || DEV="$(nmcli -g GENERAL.DEVICES connection show "$CON")"
echo "==> result:"
nmcli -g ipv4.method,ipv4.addresses,ipv4.gateway,ipv4.never-default connection show "$CON" \
  | paste -d' ' <(printf 'method:\naddresses:\ngateway:\nnever-default:\n') -
[ -n "$DEV" ] && ip -br addr show "$DEV" 2>/dev/null || true
