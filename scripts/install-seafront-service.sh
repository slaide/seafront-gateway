#!/usr/bin/env bash
# Run ON a microscope PC: install seafront as a systemd service so it survives
# SSH-session close AND reboot, and restarts on crash. Replaces the "ssh in and
# run it by hand in a shell that dies when you log out" workflow.
#
# Idempotent: re-running updates the unit and restarts the service.
#
#   install-seafront-service.sh <microscope-name> [--dir DIR] [--port PORT] [--no-enable]
#     <microscope-name>  passed to `uv run seafront --microscope <name>` (e.g. squid)
#     --dir   DIR   seafront code checkout (working dir). default: <user>/Desktop/seafront
#     --port  PORT  bind port. default: 8000
#     --no-enable   start now but do NOT auto-start on boot (use if the scope may be powered off)
set -euo pipefail

[ $# -ge 1 ] || { sed -n '2,11p' "$0"; exit 1; }
NAME="$1"; shift
PORT=8000
ENABLE=1
DIR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dir)  DIR="$2"; shift 2;;
    --port) PORT="$2"; shift 2;;
    --no-enable) ENABLE=0; shift;;
    *) echo "unknown arg: $1"; exit 1;;
  esac
done

# Service runs as the invoking user (not root), even when this script is sudo'd.
RUN_USER="${SUDO_USER:-$USER}"
RUN_HOME=$(getent passwd "$RUN_USER" | cut -d: -f6)
[ -n "$RUN_HOME" ] || { echo "cannot resolve home for $RUN_USER"; exit 1; }
DIR="${DIR:-$RUN_HOME/Desktop/seafront}"
# expand a leading ~ — systemd WorkingDirectory needs an absolute path.
case "$DIR" in "~/"*) DIR="$RUN_HOME/${DIR#\~/}";; "~") DIR="$RUN_HOME";; esac

# Locate uv (user-level install or on PATH).
UV="$RUN_HOME/.local/bin/uv"
[ -x "$UV" ] || UV="$(command -v uv || true)"
[ -n "$UV" ] && [ -x "$UV" ] || { echo "uv not found (looked in $RUN_HOME/.local/bin and PATH)"; exit 1; }

[ -d "$DIR" ] || { echo "seafront checkout not found at: $DIR  (pass --dir)"; exit 1; }

# The unit runs seafront with --host; an old checkout that lacks that flag would
# crash-loop on every start. Refuse up front with a clear fix instead.
if ! grep -q -- '"--host"' "$DIR/seafront/__main__.py" 2>/dev/null; then
  echo "!! seafront at $DIR is too old to support --host — the service would crash-loop."
  echo "   update it first:  git -C $DIR pull   (then re-run this)"
  exit 1
fi

echo "==> user=$RUN_USER  dir=$DIR  uv=$UV  microscope=$NAME  port=$PORT  enable=$ENABLE"
sudo -v

sudo tee /etc/systemd/system/seafront.service >/dev/null <<EOF
[Unit]
Description=seafront microscope server ($NAME)
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$DIR
ExecStart=$UV run seafront --microscope $NAME --host :: --port $PORT
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload

# --no-enable: install the unit only — do NOT start it (and don't touch hardware).
# This is the safe path when the microscope may be powered off.
if [ "$ENABLE" != 1 ]; then
  sudo systemctl disable seafront >/dev/null 2>&1 || true
  echo "==> unit installed, NOT started (manual mode)."
  echo "    start when ready:  sudo systemctl start seafront   (logs: journalctl -u seafront -f)"
  exit 0
fi

sudo systemctl enable --now seafront
echo "==> waiting for seafront to bind :$PORT ..."
for i in $(seq 1 20); do
  if (ss -ltn 2>/dev/null || netstat -ltn) | grep -q ":$PORT "; then
    echo "==> up. seafront listening on :$PORT (service 'seafront', logs: journalctl -u seafront -f)"
    exit 0
  fi
  sleep 2
done
echo "!! not listening after 40s — check: journalctl -u seafront -n 50 --no-pager"
exit 1
