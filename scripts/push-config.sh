#!/usr/bin/env bash
# Push a scope's config from the gateway's central store to the box, then
# restart seafront. Run ON THE GATEWAY. Central source of truth:
#   configs/<scope>/config.json   ->   <scope>:~/seafront/config.json
#
# Boxes keep a LOCAL copy (this pushes, it does not network-mount), so a scope
# still boots with its last-good config if the gateway/network is down. The
# previous config is backed up on the box as config.json.bak before overwrite.
#
#   scripts/push-config.sh <scope> [<scope> ...]     # push named scopes
#   scripts/push-config.sh --all                     # push every scope
#   scripts/push-config.sh <scope> --no-restart      # push but don't restart
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSH_USER="${FLEET_SSH_USER:-pharmbio}"
KEY="$HOME/.ssh/fleet"
SSH=(ssh -i "$KEY" -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new)

RESTART=1; SCOPES=()
for a in "$@"; do
  case "$a" in
    --all) SCOPES=($(python3 -c "import json;print(' '.join(m['name'] for m in json.load(open('$DIR/config/microscopes.json'))['microscopes']))")) ;;
    --no-restart) RESTART=0 ;;
    -*) echo "unknown option: $a" >&2; exit 1 ;;
    *) SCOPES+=("$a") ;;
  esac
done
[ ${#SCOPES[@]} -gt 0 ] || { echo "usage: $0 <scope>... | --all [--no-restart]"; exit 1; }

host_of() { python3 -c "import json,sys; d=json.load(open('$DIR/config/microscopes.json')); print(next((m['host'] for m in d['microscopes'] if m['name']==sys.argv[1]), ''))" "$1"; }

for scope in "${SCOPES[@]}"; do
  src="$DIR/configs/$scope/config.json"
  host="$(host_of "$scope")"
  [ -n "$host" ]   || { echo "!! $scope: not in microscopes.json — skipped"; continue; }
  [ -f "$src" ]    || { echo "!! $scope: no central config at $src — skipped"; continue; }
  echo "==> $scope ($host): backup + push $src"
  "${SSH[@]}" "$SSH_USER@$host" \
    "cp -f ~/seafront/config.json ~/seafront/config.json.bak 2>/dev/null || true"
  scp -i "$KEY" -o BatchMode=yes -o StrictHostKeyChecking=accept-new \
    "$src" "$SSH_USER@$host:seafront/config.json"
  if [ "$RESTART" = 1 ]; then
    echo "   restarting seafront"
    "${SSH[@]}" "$SSH_USER@$host" "sudo -n /usr/bin/systemctl restart seafront"
  fi
  echo "==> $scope: done"
done
