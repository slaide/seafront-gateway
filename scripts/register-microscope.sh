#!/usr/bin/env bash
# Run ON the gateway: add a microscope to config/microscopes.json and apply.
# Picks the next free proxy_port automatically unless you pass one.
# Equivalent to: edit config/microscopes.json by hand, then apply-config.sh.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
  echo "usage: $0 <name> <host-ip> [proxy_port] [seafront_port]"
  echo "  e.g. $0 squid5 192.168.50.15            (auto proxy_port, seafront 8000)"
  echo "       $0 squid5 192.168.50.15 8005 8000"
  exit 1
}
[ $# -ge 2 ] || usage

python3 - "$DIR/config/microscopes.json" "$1" "$2" "${3:-}" "${4:-8000}" <<'PY'
import json, sys
path, name, host, proxy, seaport = sys.argv[1:6]
cfg = json.load(open(path))
ms = cfg["microscopes"]
if any(m["name"] == name for m in ms):
    sys.exit(f"error: microscope '{name}' already in config")
proxy = int(proxy) if proxy else max((m["proxy_port"] for m in ms), default=8000) + 1
if any(m["proxy_port"] == proxy for m in ms):
    sys.exit(f"error: proxy_port {proxy} already in use")
ms.append({"name": name, "host": host, "seafront_port": int(seaport), "proxy_port": proxy})
json.dump(cfg, open(path, "w"), indent=2)
open(path, "a").write("\n")
print(f"==> added {name} -> {host}:{seaport}  (proxy port {proxy})")
PY

"$DIR/scripts/apply-config.sh"
