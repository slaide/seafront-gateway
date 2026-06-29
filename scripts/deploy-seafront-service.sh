#!/usr/bin/env bash
# Fan out install-seafront-service.sh to every microscope over SSH, so seafront
# runs as a boot service on each box (survives logout + reboot, restarts on crash).
# Idempotent: re-run any time to update the unit across the whole fleet.
#
# Targets: hosts given as args (name@host or host), else every entry in
# config/microscopes.json (skipping 127.0.0.1/localhost).
#
#   deploy-seafront-service.sh [--profile squid] [--dir DIR] [--port P] [--user U] [--no-enable] [host ...]
#
# Auth (boxes need ssh + the per-PC setup done first):
#   - SSH keys + passwordless sudo  -> just run it.
#   - no keys yet                   -> MICROLAN_PASS=<password> deploy-seafront-service.sh
#                                      (one password for ssh login AND remote sudo, whole fleet)
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PROFILE=squid
SEAFRONT_DIR=''     # empty => each box auto-detects its own checkout (paths differ per box)
PORT=8000
USER_NAME="${MICROLAN_USER:-pharmbio}"
ENABLE_FLAG=""
HOSTS_IN=()
while [ $# -gt 0 ]; do
  case "$1" in
    --profile) PROFILE="$2"; shift 2;;
    --dir)     SEAFRONT_DIR="$2"; shift 2;;
    --port)    PORT="$2"; shift 2;;
    --user)    USER_NAME="$2"; shift 2;;
    --no-enable) ENABLE_FLAG="--no-enable"; shift;;
    *) HOSTS_IN+=("$1"); shift;;
  esac
done

# Build the target list ("label host" per line).
declare -a LABELS HOSTS
if [ ${#HOSTS_IN[@]} -gt 0 ]; then
  for h in "${HOSTS_IN[@]}"; do LABELS+=("${h%@*}"); HOSTS+=("${h##*@}"); done
else
  while IFS=$'\t' read -r n h; do LABELS+=("$n"); HOSTS+=("$h"); done < <(
    python3 -c "import json;[print(m['name']+chr(9)+m['host']) for m in json.load(open('$REPO/config/microscopes.json'))['microscopes'] if m['host'] not in ('127.0.0.1','localhost')]"
  )
fi
[ ${#HOSTS[@]} -gt 0 ] || { echo "no remote hosts (config has only local entries; pass them as args)"; exit 1; }

# SSH/SCP runners + remote-sudo prefix, depending on auth mode.
OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=8)
if [ -n "${MICROLAN_PASS:-}" ]; then
  AP=$(mktemp); printf '#!/bin/sh\necho "%s"\n' "$MICROLAN_PASS" >"$AP"; chmod +x "$AP"
  trap 'rm -f "$AP"' EXIT
  export SSH_ASKPASS="$AP" SSH_ASKPASS_REQUIRE=force DISPLAY="${DISPLAY:-:0}"
  PW=(-o PreferredAuthentications=password -o PubkeyAuthentication=no)
  SSH() { setsid -w ssh "${OPTS[@]}" "${PW[@]}" "$@"; }
  SCP() { setsid -w scp "${OPTS[@]}" "${PW[@]}" "$@"; }
  # remote sudo reads the password from the piped echo (script is a file arg, not stdin)
  SUDO="echo '$MICROLAN_PASS' | sudo -S -p ''"
else
  SSH() { ssh "${OPTS[@]}" "$@"; }
  SCP() { scp "${OPTS[@]}" "$@"; }
  SUDO="sudo -n"
fi

QUIET='Warning|post-quantum|store now|may need|openssh.com'
fail=0
for i in "${!HOSTS[@]}"; do
  label="${LABELS[$i]}"; host="${HOSTS[$i]}"; tgt="$USER_NAME@$host"
  echo "=== $label ($host) ==="
  if ! SCP "$REPO/scripts/install-seafront-service.sh" "$tgt:/tmp/install-seafront-service.sh" >/dev/null 2>&1; then
    echo "  !! scp failed — host unreachable or ssh auth wrong (run setup-microscope-pc.sh there first?)"; fail=1; continue
  fi
  DIRARG=""; [ -n "$SEAFRONT_DIR" ] && DIRARG="--dir $SEAFRONT_DIR"
  out=$(SSH "$tgt" "$SUDO bash /tmp/install-seafront-service.sh $PROFILE $DIRARG --port $PORT $ENABLE_FLAG" 2>&1) && rc=0 || rc=$?
  echo "$out" | grep -vE "$QUIET" | sed 's/^/  /'
  if [ "$rc" = 0 ]; then echo "  ok: $label"; else echo "  !! FAILED: $label (rc=$rc)"; fail=1; fi
done
[ "$fail" = 0 ] && echo "==> all hosts done." || echo "==> finished with errors (see above)."
exit $fail
