# Microscope gateway

Central access point for the microscope LAN: one PC hosts a Wi-Fi hotspot, a
dashboard, and a Caddy reverse proxy that forwards one port per microscope to its
seafront server. `config/microscopes.json` is the single source of truth — edit
it, then run `scripts/apply-config.sh`.

| Component | Port | Purpose |
|---|---|---|
| Dashboard (FastAPI) | `dashboard_port` (8000; 8080 on lab3) | landing page + per-microscope status |
| Caddy reverse proxy | 8001…800N | `gateway:800N` → `squidN:8000` (HTTP + WebSocket) |
| Wi-Fi hotspot | — | clients join here to reach the gateway |

| Task | Command (run in the project dir) |
|---|---|
| Install / bring up | `scripts/install.sh` (on gateway) · `scripts/deploy.sh` (from dev machine) |
| Add / change a microscope | edit `config/microscopes.json` → `scripts/apply-config.sh` |
| Start / stop services | `scripts/start.sh` · `scripts/stop.sh` |
| Start / stop hotspot | `scripts/hotspot-up.sh` · `scripts/hotspot-down.sh` |
| Status / logs | `scripts/status.sh` · `journalctl -u caddy -u microscope-dashboard -f` |

## How it works

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
- **dashboard/** — a FastAPI app serving the landing page; it health-checks each
  microscope on demand and links to its proxy port.

The Caddyfile and the dashboard are both generated/driven from
`config/microscopes.json`.

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
- **Dashboard port** is `gateway.dashboard_port` in the config (the systemd unit
  is generated from it). On a dedicated gateway PC use `8000`. On the current
  `lab3` test box it's `8080`, because `lab3` already runs its own seafront on
  `:8000` (and `squid1` points at that local seafront so the proxy chain is
  demonstrable on one machine).
- **Dashboard bind:** `0.0.0.0` (IPv4) — hotspot clients get IPv4 addresses.
- **Caddy** listens dual-stack on each `:800N` automatically.
