#!/usr/bin/env python3
"""Generate the Caddyfile from config/microscopes.json (single source of truth).

One reverse-proxy block per microscope: gateway:<proxy_port> -> <host>:<seafront_port>.
Root->root proxying means seafront's HTML, API, and WebSocket all work unchanged.
Caddy forwards WebSocket upgrades automatically.
"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
cfg = json.loads((ROOT / "config" / "microscopes.json").read_text())

lines = [
    "# AUTO-GENERATED from config/microscopes.json by scripts/gen-caddyfile.py.",
    "# Do not edit by hand: edit the JSON and run scripts/apply-config.sh.",
    "",
    "# The dashboard (FastAPI) runs separately on the dashboard port; it is not proxied here.",
    "",
]
for m in cfg["microscopes"]:
    lines += [
        f"# {m['name']}",
        f":{m['proxy_port']} {{",
        f"\treverse_proxy {m['host']}:{m['seafront_port']}",
        "}",
        "",
    ]

out = ROOT / "Caddyfile"
out.write_text("\n".join(lines))
print(f"wrote {out} ({len(cfg['microscopes'])} microscopes -> ports "
      f"{', '.join(str(m['proxy_port']) for m in cfg['microscopes'])})")
