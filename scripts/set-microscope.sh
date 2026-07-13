#!/usr/bin/env bash
# Switch which microscope profile a box's seafront runs — e.g. its real profile vs the
# shared "mocroscope" mock — for debugging/testing. Run ON THE GATEWAY.
#
#   scripts/set-microscope.sh <box> --list        # show available profiles + current
#   scripts/set-microscope.sh <box> <profile>     # switch to <profile> + restart seafront
#
# How it works: a box's ~/seafront/config.json can hold several profiles (each keyed by
# system.microscope_name). This writes the chosen name to the box's per-box override file
# ~/seafront/active_microscope and restarts seafront, which then runs `--microscope <that>`.
# Both steps are within the fleet's existing permissions (pharmbio owns the file; restart
# seafront is NOPASSWD). Needs a box on a seafront app image built after override support
# landed; older images ignore the file — "Update seafront" first.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FC="python3 $DIR/scripts/fleet_config.py"
SSH_USER="${FLEET_SSH_USER:-pharmbio}"
KEY="${FLEET_SSH_KEY:-$HOME/.ssh/fleet}"
SSH=(ssh -i "$KEY" -o BatchMode=yes -o StrictHostKeyChecking=no
     -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=6)

BOX="${1:?usage: set-microscope.sh <box> <profile|--list>}"
PROFILE="${2:?usage: set-microscope.sh <box> <profile|--list>}"
HOST="$($FC host "$BOX")"
[ -n "$HOST" ] || { echo "!! $BOX is not in the inventory" >&2; exit 1; }

# Profile names from the box's config.json (single-quoted python; double quotes inside).
LIST="python3 -c 'import json,os;print(chr(10).join(m.get(\"system.microscope_name\",\"\") for m in json.load(open(os.path.expanduser(\"~/seafront/config.json\"))).get(\"microscopes\",[])))' 2>/dev/null"
avail="$("${SSH[@]}" "$SSH_USER@$HOST" "$LIST" 2>/dev/null)"
active="$("${SSH[@]}" "$SSH_USER@$HOST" "cat ~/seafront/active_microscope 2>/dev/null" || true)"

if [ "$PROFILE" = "--list" ]; then
  echo "$BOX profiles in config.json:"; echo "$avail" | sed 's/^/  /'
  echo "active override: ${active:-<none> — uses image default (SEAFRONT_MICROSCOPE)}"
  exit 0
fi

grep -Fxq "$PROFILE" <<<"$avail" \
  || { echo "!! '$PROFILE' is not a profile in $BOX config.json (have: $(echo "$avail" | tr '\n' ' '))" >&2; exit 1; }

echo "==> $BOX: switch seafront profile -> $PROFILE (+ restart)"
"${SSH[@]}" "$SSH_USER@$HOST" "printf '%s\n' '$PROFILE' > ~/seafront/active_microscope"
"${SSH[@]}" "$SSH_USER@$HOST" "sudo -n /usr/bin/systemctl restart seafront"
echo "==> done. If nothing changes, the box is on an older app image that ignores the override — Update seafront, then retry."
