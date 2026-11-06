#!/usr/bin/env bash
# Set the GATEWAY's clock by hand. Run ON THE GATEWAY (the dashboard calls it).
#
#   scripts/set-time.sh <unix-epoch-seconds>     # e.g. scripts/set-time.sh "$(date +%s)"
#
# For when the gateway has no internet: its chrony then serves its own clock to the boxes
# (`local stratum 10`, see gateway-setup.sh), so the whole fleet is only as right as this
# clock. Self-elevates; the dashboard (pharmbio) is granted this script in sudoers.
# The boxes are stepped to the new time by the dashboard afterwards (chronyc makestep).
set -euo pipefail

E="${1:-}"
# whole or fractional seconds; sanity window 2025-01-01 .. 2100-01-01
if ! [[ "$E" =~ ^[0-9]{10}(\.[0-9]+)?$ ]] || [ "${E%.*}" -lt 1735689600 ] || [ "${E%.*}" -gt 4102444800 ]; then
  echo "usage: $0 <unix-epoch-seconds between 2025 and 2100>, got '$E'" >&2
  exit 2
fi

if [ "$(id -u)" -ne 0 ]; then
  if [ -t 0 ]; then exec sudo "$0" "$@"; else exec sudo -n "$0" "$@"; fi
fi

before="$(date --iso-8601=seconds)"
date -s "@$E" >/dev/null
# Persist to the RTC now: chrony's rtcsync only copies the system clock every 11 min,
# and a reboot before that would bring the old time back.
hwclock --systohc 2>/dev/null || true
echo "gateway clock: $before -> $(date --iso-8601=seconds)"
