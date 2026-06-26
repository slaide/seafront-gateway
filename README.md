# Microscope gateway

Central access point for the microscope LAN. One PC (the **gateway**) hosts a
Wi-Fi hotspot; you join it from a laptop/phone, open a single dashboard, and
reach every microscope through it. The microscope PCs are never contacted
directly by clients.

```
   Wi-Fi hotspot ── clients join here
        │
   ┌────┴─────┐   :8000  dashboard (FastAPI)   ← this project
   │ Gateway  │   :8001 → squid1:8000  ┐
   │   PC     │   :8002 → squid2:8000  │  Caddy reverse proxy
   └────┬─────┘   :8003 → squid3:8000  │  (root→root, WebSockets included)
     switch       :8004 → squid4:8000  ┘
   ┌──┬──┬──┬──┐
  PC1 …        each runs:  seafront --host :: --port 8000
```

Two programs run in parallel on the gateway:
- **Caddy** — reverse-proxies `gateway:800N` → `squidN:8000`. Root→root, so
  seafront's HTML/API/WebSocket work with no rewriting.
- **dashboard/** — a FastAPI app on `:8000` serving the landing page; it
  health-checks each microscope on demand and links to its proxy port.

`config/microscopes.json` is the **single source of truth** (microscope list +
hotspot settings). The Caddyfile is generated from it.

## Bring it up

### First time, from your dev machine

```bash
scripts/deploy.sh                 # rsync to pharmbio@lab3.local + run installer
# or target another host:  scripts/deploy.sh user@host
```

### Or directly on the gateway PC

```bash
git clone <this repo>  ~/microscope-gateway   # (or scp it over)
cd ~/microscope-gateway
bash scripts/install.sh
```

`install.sh` installs `uv` + Caddy, builds the dashboard venv, generates the
Caddyfile, and installs + enables both `systemd` services. It is idempotent.

Then start the hotspot when you actually want clients to connect:

```bash
scripts/hotspot-up.sh     # ⚠ puts Wi-Fi into AP mode → no Wi-Fi internet while up
```

Open the dashboard at `http://<gateway>:8000` (e.g. `http://lab3.local:8000`
over the wired link, or the hotspot IP printed by `hotspot-up.sh`).

## Day-to-day

| Task | Command |
|---|---|
| Add/change a microscope | edit `config/microscopes.json`, then `scripts/apply-config.sh` |
| Start / stop services | `scripts/start.sh` / `scripts/stop.sh` |
| Start / stop hotspot | `scripts/hotspot-up.sh` / `scripts/hotspot-down.sh` |
| Check what's running | `scripts/status.sh` |
| Logs | `journalctl -u caddy -f` · `journalctl -u microscope-dashboard -f` |

Everything auto-starts on boot (`systemd`); the hotspot reconnects on boot once
you've run `hotspot-up.sh` at least once.

## Adding a microscope PC

1. Give the new microscope PC a static IP on the wired backbone (see
   `MICROSCOPE_NETWORK.md` in the seafront repo) and run seafront with
   `--host ::`.
2. Add an entry to `config/microscopes.json` (name, host, `proxy_port`).
3. `scripts/apply-config.sh`.

## Notes

- **Backbone addressing:** the proxy upstreams in `config/microscopes.json` are
  static IPs (e.g. `192.168.50.11`). Static is recommended over link-local/mDNS
  here because Caddy's resolver may not use mDNS, and static addresses don't
  change across reboots. Adjust the IPs to match your wired setup.
- **Dashboard bind:** `0.0.0.0` (IPv4) — hotspot clients get IPv4 addresses.
- **Caddy** listens dual-stack on each `:800N` automatically.
