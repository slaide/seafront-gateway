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
# Make exactly one wifi profile ($1) the autoconnect winner and turn autoconnect OFF on
# every other wifi profile. This keeps the radio's boot/fallback network unambiguous and
# stops one mode from silently reclaiming the radio from the other (an AP left with
# autoconnect=yes hijacks client mode; a stray saved network steals the radio from the AP).
_set_exclusive_autoconnect() {
  local winner="$1" c
  nmcli -t -f NAME,TYPE con show | awk -F: '$2=="802-11-wireless"{print $1}' | while read -r c; do
    if [ "$c" = "$winner" ]; then
      nmcli con modify "$c" connection.autoconnect yes 2>/dev/null || true
    else
      nmcli con modify "$c" connection.autoconnect no 2>/dev/null || true
    fi
  done
}

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
  # AP is the sole autoconnect winner while in AP mode; a later `client` switch re-enables
  # the network it joins. This is what stops the AP from hijacking client mode later.
  _set_exclusive_autoconnect "$HOTSPOT_CON"
  nmcli con up "$HOTSPOT_CON"
}

apply_client() {
  local ifc ss pw active; ifc="$(iface)"; ss="${1:-}"; pw="${2:-}"
  echo "==> switching $ifc to client mode (station)"
  # A radio beaconing as an AP can't scan/associate; drop the hotspot and stop it
  # auto-reclaiming the radio (the hijack we hit).
  nmcli con modify "$HOTSPOT_CON" connection.autoconnect no 2>/dev/null || true
  nmcli device disconnect "$ifc" 2>/dev/null || true

  if [ -z "$ss" ]; then
    # No SSID: reconnect the best KNOWN in-range network. Temporarily allow all saved
    # client profiles as candidates so NM can pick one, then pin whatever came up as the
    # SOLE autoconnect — so only it (not the AP, not some other saved net) is the fallback.
    nmcli -t -f NAME,TYPE con show | awk -F: '$2=="802-11-wireless"{print $1}' \
      | while read -r c; do [ "$c" = "$HOTSPOT_CON" ] || nmcli con modify "$c" connection.autoconnect yes 2>/dev/null || true; done
    nmcli device wifi rescan ifname "$ifc" 2>/dev/null || true
    sleep 5
    nmcli --wait 30 device connect "$ifc" || true
    active="$(active_wifi_con)"
    [ -n "$active" ] && _set_exclusive_autoconnect "$active"
    return
  fi

  # Explicit SSID: drive it through a connection PROFILE + `con up`. `con up` scans and
  # associates as part of activation, so — unlike `device wifi connect` — it does NOT need
  # the SSID to already be in the scan list, which is exactly what fails right after the
  # radio leaves AP mode. Reuse a saved profile (matched by name == SSID, how NM names
  # them) or create one; only set a PSK when a password was supplied.
  if nmcli -t -f NAME,TYPE con show | awk -F: '$2=="802-11-wireless"{print $1}' | grep -Fxq "$ss"; then
    [ -n "$pw" ] && nmcli con modify "$ss" wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$pw"
  elif [ -n "$pw" ]; then
    nmcli con add type wifi ifname "$ifc" con-name "$ss" ssid "$ss" \
      wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$pw"
  else
    nmcli con add type wifi ifname "$ifc" con-name "$ss" ssid "$ss"
  fi
  # Only the joined network auto-rejoins; the hotspot and every other saved profile are
  # turned off, so the last explicit mode is exactly what the radio does next / at boot.
  _set_exclusive_autoconnect "$ss"
  nmcli --wait 30 con up "$ss"
}

verify_mode() {
  case "$1" in
    ap)     [ "$(current_mode)" = ap ] ;;
    client) # "full" = real internet; "portal" = associated behind a captive portal. NOT
            # "limited"/"none" — those mean connected to something with no upstream, which
            # is exactly the failure we are guarding against (client mode exists for internet).
            local c; c="$(nmcli -t -f CONNECTIVITY general status 2>/dev/null || true)"
            [ "$c" = full ] || [ "$c" = portal ] ;;
  esac
}

# Detached worker: switch, wait, verify, revert-to-previous on failure.
do_apply() {
  local target="$1" ss="${2:-}" pw="${3:-}" prev
  prev="$(current_mode)"
  echo "==> switching Wi-Fi: $prev -> $target"
  # `|| true`: a failed switch (e.g. bad password, network out of range) must NOT abort the
  # worker under `set -e` — it has to fall through to verify + revert so we never strand it.
  if [ "$target" = ap ]; then apply_ap || true; else apply_client "$ss" "$pw" || true; fi
  echo "==> waiting ${VERIFY_WAIT}s for $target to settle…"
  sleep "$VERIFY_WAIT"
  if verify_mode "$target"; then
    echo "==> Wi-Fi is now in $target mode"
  else
    echo "!! $target did not come up; reverting to $prev" >&2
    if [ "$prev" = ap ]; then apply_ap || true; else apply_client || true; fi
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
