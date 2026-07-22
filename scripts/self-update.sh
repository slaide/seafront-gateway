#!/usr/bin/env bash
# "Update gateway": pull the gateway checkout and apply it — regenerate the dashboard unit,
# reload Caddy + firewall, restart the dashboard (which loads new dashboard code). This is
# the gateway's *own software*; it does NOT rebuild the seafront/OS images (that is
# build-images.sh). Exposed as the dashboard's "Update gateway" button via a NOPASSWD
# sudoers rule, so updating the gateway needs no SSH. Needs internet (git pull from GitHub).
#
# It runs itself in a transient systemd unit: apply-config.sh restarts microscope-dashboard,
# and systemd kills a service's whole cgroup on stop — so a child of the dashboard would be
# killed mid-restart. Re-exec into our own unit (detached from the dashboard) to survive it.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ "${GATEWAY_SELFUPDATE_DETACHED:-}" != 1 ]; then
    # First entry (child of the dashboard). Relaunch detached and return immediately.
    exec systemd-run --collect --quiet --description="gateway self-update" \
        --setenv=GATEWAY_SELFUPDATE_DETACHED=1 -- "$0" "$@"
fi

# --- now running as our own transient unit, safe to restart the dashboard ---
OWNER="$(stat -c %U "$DIR")"
# git as the checkout owner so pulled files are not left root-owned (which breaks later pulls).
runuser -u "$OWNER" -- git -C "$DIR" fetch --all --prune
runuser -u "$OWNER" -- git -C "$DIR" pull --ff-only
# apply: regenerate the dashboard unit, reload Caddy/firewall, restart the dashboard.
"$DIR/scripts/apply-config.sh"
