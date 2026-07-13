#!/usr/bin/env bash
# Control the gateway's single Wi-Fi radio. Run ON THE GATEWAY.
#
#   scripts/wifi-mode.sh ap                     # run a hotspot laptops connect to (no internet)
#   scripts/wifi-mode.sh client [ssid] [pass]   # join an external Wi-Fi for internet
#   scripts/wifi-mode.sh status                 # current mode / ssid / ip / internet
#   scripts/wifi-mode.sh ap --foreground        # do it inline instead of detached (watch it)
#
# ONE radio, so AP and client are MUTUALLY EXCLUSIVE — switching to one tears the
# other down. AP is the day-to-day mode (the deployment gateway has no internet);
# flip to client only to pull updates (image rebuild / git), then back to AP.
#
# Why the switch is detached by default: you may be toggling *over the very Wi-Fi you
# are tearing down* (a laptop on the AP switching to client, or vice-versa). A detached
# systemd unit does the switch so it completes even though your request's connection
# drops mid-way; it then VERIFIES the new mode and AUTO-REVERTS if it failed to come up,
# so a bad SSID can't strand the gateway. The wired backbone (192.168.50.1) and
# squidway.local are never touched — that path is always your way back in.
#
# Mode persists across reboots via NetworkManager autoconnect (not the JSON): whichever
# mode you last applied is what NM brings up at boot. `status` reports the LIVE state.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FC="python3 $DIR/scripts/fleet_config.py"
HOTSPOT_CON="fleet-hotspot"
SWITCH_UNIT="fleet-wifi-switch"
VERIFY_WAIT="${WIFI_VERIFY_WAIT:-20}"   # seconds to let the new mode settle before verifying

# The Wi-Fi radio. An explicit gateway.wifi.iface in the config wins (for a gateway with
# more than one radio); otherwise auto-detect the first wireless netdev. Avoids baking in
# a device name that differs per gateway (e.g. wlp3s0 vs wlp87s0f0).
iface() {
  local configured
  configured="$($FC get gateway.wifi.iface 2>/dev/null || true)"
  if [ -n "$configured" ]; then echo "$configured"; return; fi
  local d
  for d in /sys/class/net/*; do
    [ -d "$d/wireless" ] && { basename "$d"; return; }
  done
}
ssid()   { $FC get gateway.wifi.hotspot.ssid; }
psk()    { $FC get gateway.wifi.hotspot.password; }

# The wifi connection currently active on the radio (empty if none).
active_wifi_con() {
  nmcli -t -f NAME,TYPE con show --active 2>/dev/null \
    | awk -F: '$2=="802-11-wireless"{print $1; exit}'
}

current_mode() {
  local c; c="$(active_wifi_con)"
  if [ "$c" = "$HOTSPOT_CON" ]; then echo ap
  elif [ -n "$c" ];            then echo client
  else                              echo off; fi
}

# --- status (read-only; no root needed) ---------------------------------------
do_status() {
  local ifc mode
  ifc="$(iface)"; mode="$(current_mode)"
  echo "mode: $mode"
  echo "iface: $ifc"
  echo "ssid: $(nmcli -t -f ACTIVE,SSID dev wifi 2>/dev/null | awk -F: '$1=="yes"{print $2; exit}')"
  echo "ipv4: $(nmcli -g IP4.ADDRESS device show "$ifc" 2>/dev/null | paste -sd' ')"
  local conn; conn="$(nmcli -t -f CONNECTIVITY general status 2>/dev/null || echo unknown)"
  echo "internet: $([ "$conn" = full ] && echo yes || echo "no ($conn)")"
  if [ "$mode" = ap ]; then
    local leases="/var/lib/NetworkManager/dnsmasq-${ifc}.leases"
    [ -f "$leases" ] && echo "ap-clients: $(wc -l < "$leases")"
  fi
}

# --- the actual switches (root) -----------------------------------------------
apply_ap() {
  local ifc ss pw; ifc="$(iface)"; ss="$(ssid)"; pw="$(psk)"
  echo "==> bringing up hotspot '$ss' on $ifc (shared IPv4, no upstream needed)"
  if ! nmcli -t -f NAME con show 2>/dev/null | grep -Fxq "$HOTSPOT_CON"; then
    nmcli con add type wifi ifname "$ifc" con-name "$HOTSPOT_CON" autoconnect no ssid "$ss"
  fi
  # AP + WPA2 + shared (NM runs DHCP/NAT for connecting laptops). connection.zone=trusted
  # puts the AP interface in firewalld's trusted zone so laptops reach the dashboard +
  # proxy ports; a client profile (no zone) stays in the default zone.
  nmcli con modify "$HOTSPOT_CON" \
    connection.interface-name "$ifc" \
    802-11-wireless.mode ap 802-11-wireless.band bg 802-11-wireless.ssid "$ss" \
    ipv4.method shared \
    wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$pw" \
    connection.zone trusted \
    connection.autoconnect yes connection.autoconnect-priority 100
  # Demote every other wifi profile so it doesn't grab the radio at boot.
  nmcli -t -f NAME,TYPE con show | awk -F: '$2=="802-11-wireless"{print $1}' \
    | while read -r c; do [ "$c" = "$HOTSPOT_CON" ] || nmcli con modify "$c" connection.autoconnect no; done
  nmcli con up "$HOTSPOT_CON"
}

apply_client() {
  local ifc ss pw; ifc="$(iface)"; ss="${1:-}"; pw="${2:-}"
  echo "==> switching $ifc to client mode (station)"
  nmcli con modify "$HOTSPOT_CON" connection.autoconnect no 2>/dev/null || true
  nmcli con down "$HOTSPOT_CON" 2>/dev/null || true
  if [ -n "$ss" ]; then
    # Creates (or reuses) a saved profile, autoconnect yes by default.
    nmcli device wifi connect "$ss" ${pw:+password "$pw"} ifname "$ifc"
  else
    # No SSID given: activate the best known/autoconnect network on the radio.
    nmcli device connect "$ifc"
  fi
}

verify_mode() {
  case "$1" in
    ap)     [ "$(current_mode)" = ap ] ;;
    client) local c; c="$(nmcli -t -f CONNECTIVITY general status 2>/dev/null || true)"
            [ "$c" = full ] || [ "$c" = portal ] || [ "$c" = limited ] ;;
  esac
}

# Detached worker: switch, wait, verify, revert-to-previous on failure.
do_apply() {
  local target="$1" ss="${2:-}" pw="${3:-}" prev
  prev="$(current_mode)"
  echo "==> switching Wi-Fi: $prev -> $target"
  if [ "$target" = ap ]; then apply_ap; else apply_client "$ss" "$pw"; fi
  echo "==> waiting ${VERIFY_WAIT}s for $target to settle…"
  sleep "$VERIFY_WAIT"
  if verify_mode "$target"; then
    echo "==> Wi-Fi is now in $target mode"
  else
    echo "!! $target did not come up; reverting to $prev" >&2
    if [ "$prev" = ap ]; then apply_ap; else apply_client; fi
  fi
}

require_root() {
  [ "$(id -u)" = 0 ] && return 0
  # Prompt on a terminal; passwordless (fleet sudoers) for the non-tty dashboard.
  if [ -t 0 ]; then exec sudo "$0" "$@"; else exec sudo -n "$0" "$@"; fi
}

CMD="${1:-status}"; shift || true
case "$CMD" in
  status) do_status ;;
  __apply)                                  # internal entry point for the detached unit
    require_root __apply "$@"; do_apply "$@" ;;
  ap|client)
    require_root "$CMD" "$@"
    FG=0; ARGS=()
    for a in "$@"; do [ "$a" = --foreground ] && FG=1 || ARGS+=("$a"); done
    if [ "$FG" = 1 ]; then
      do_apply "$CMD" "${ARGS[@]:-}"
    else
      # Detach so the switch survives the caller's connection dropping mid-way.
      systemctl reset-failed "$SWITCH_UNIT" 2>/dev/null || true
      systemd-run --unit="$SWITCH_UNIT" --collect \
        /usr/bin/env bash "$0" __apply "$CMD" "${ARGS[@]:-}" >/dev/null
      echo "==> switch to $CMD scheduled (detached). Poll: scripts/wifi-mode.sh status"
      echo "    watch: journalctl -fu $SWITCH_UNIT"
    fi ;;
  *) sed -n '2,10p' "$0"; exit 1 ;;
esac
