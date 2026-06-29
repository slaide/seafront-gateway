# Microscope gateway

One gateway PC hosts a Wi-Fi hotspot + dashboard + Caddy reverse proxy; clients
join the hotspot and reach every microscope through it. `config/microscopes.json`
is the single source of truth.

## Install

**Gateway PC** (once):
```bash
bash scripts/install.sh        # uv + Caddy + dashboard, as boot services
bash scripts/hotspot-up.sh     # start Wi-Fi AP (⚠ kills this PC's Wi-Fi internet)
```

**Each microscope PC** — ⚠ **run the first step at the keyboard, not over the wire.**
A fresh box defaults to DHCP on the wired NIC and hangs at "setting network address"
on our DHCP-less switch (see *Wired NIC hangs* below), so it's invisible on the wire
until the wired NIC is pinned to a static IP. Sitting at the box (`<profile>` is
seafront's hardware profile, e.g. `squid` — same on every box; `<name>`/`<ip>` are this
box's identity, e.g. `lab1 192.168.50.11`. The seafront checkout is auto-detected under
the user's home; pass `--dir <path>` only if a box has more than one):
```bash
bash scripts/setup-microscope-pc.sh      <name> <ip>   # net: hostname, mDNS, static IP, firewall
git -C <seafront-checkout> pull                        # seafront MUST support --host (old checkouts don't)
bash scripts/install-seafront-service.sh <profile>     # seafront as a boot service (auto-finds the checkout)
```
The `git pull` matters: a checkout too old for the `--host` flag makes the service
crash-loop. `install-seafront-service.sh` refuses to install against such a checkout
and tells you to pull first.
then back on the gateway:
```bash
bash scripts/register-microscope.sh <name> <ip>                  # add to proxy + dashboard
```

Once a box is network-reachable, the seafront-service step can be re-run for the **whole
fleet at once** from the gateway instead of per box (no keys yet → pass the shared password):
```bash
MICROLAN_PASS=<password> bash scripts/deploy-seafront-service.sh --profile squid
```

Open the dashboard at `http://<gateway>:8000`. Done.

## Addressing

Static `192.168.50.0/24` on the wired backbone (no DHCP — addresses are fixed, not
link-local). These are the `<name> <ip>` you pass to `setup-microscope-pc.sh`:

| Name | IPv4 | Role |
|---|---|---|
| `squidway` | `192.168.50.1` | gateway (dedicated PC) — *future* |
| `lab1` | `192.168.50.11` | microscope PC |
| `lab2` | `192.168.50.12` | microscope PC |
| `lab3` | `192.168.50.13` | microscope PC (also temp gateway until `squidway` exists) |
| `lab4` | `192.168.50.14` | microscope PC |

(A dev/admin laptop can take e.g. `192.168.50.50` to reach the backbone directly.)

## Day-to-day

| Task | Command |
|---|---|
| Start / stop services | `scripts/start.sh` · `scripts/stop.sh` |
| Start / stop hotspot | `scripts/hotspot-up.sh` · `scripts/hotspot-down.sh` |
| Status | `scripts/status.sh` |
| Logs | `journalctl -u caddy -u microscope-dashboard -f` (gateway) · `journalctl -u seafront -f` (a scope) |
| Change config by hand | edit `config/microscopes.json` → `scripts/apply-config.sh` |

---

## How it works

```
   Wi-Fi hotspot ── clients join here
        │
   ┌────┴─────┐   :8000  dashboard (FastAPI)   ← this project
   │ Gateway  │   :8001 → lab1:8000  ┐
   │ squidway │   :8002 → lab2:8000  │  Caddy reverse proxy
   └────┬─────┘   :8003 → lab3:8000  │  (root→root, WebSockets included)
     switch       :8004 → lab4:8000  ┘
   ┌──┬──┬──┬──┐
  lab1 …       each runs:  seafront --host :: --port 8000  (systemd)
```

Two programs run on the gateway, both driven from `config/microscopes.json`:
- **Caddy** — reverse-proxies `gateway:800N` → `labN:8000`, root→root, so
  seafront's HTML/API/WebSocket work with no rewriting.
- **dashboard/** — FastAPI landing page; health-checks each microscope on demand
  and links to its proxy port.

## The scripts

| Script | Runs on | Does |
|---|---|---|
| `install.sh` | gateway | uv + Caddy + dashboard venv + both systemd services. Idempotent. |
| `deploy.sh [user@host]` | dev machine | rsync repo to the gateway + run `install.sh` there. |
| `hotspot-up.sh` / `hotspot-down.sh` | gateway | start / stop the Wi-Fi AP. |
| `setup-microscope-pc.sh <name> <ip>` | microscope PC (keyboard) | one-shot bring-up: hostname, ssh+avahi (mDNS), static IP (cures the DHCP hang), firewall. Idempotent. |
| `install-seafront-service.sh <profile> [--dir D] [--port P] [--no-enable]` | microscope PC | seafront as a systemd service (survives logout + reboot, restarts on crash). |
| `deploy-seafront-service.sh [--profile P] [--dir D] [host ...]` | gateway | fan out `install-seafront-service.sh` to the whole fleet over SSH. |
| `register-microscope.sh <name> <ip> [proxy_port] [seafront_port]` | gateway | add to `microscopes.json` (auto-picks proxy port) + reload proxy/dashboard. |
| `apply-config.sh` | gateway | regenerate Caddyfile from config + reload services. |

Notes on the per-PC scripts:
- `setup-microscope-pc.sh` handles both network backends automatically: **NetworkManager**
  (Ubuntu Desktop / Arch) and **netplan + systemd-networkd** (Ubuntu Server, where the NIC
  shows as "unmanaged" and there is no "Wired connection 1" — it writes
  `/etc/netplan/60-microscope-lan.yaml` instead). Either way it pins a **static** backbone
  IP — Caddy's resolver may bypass mDNS, so `.local` names aren't reliable as proxy
  upstreams. `--dir` exists because the seafront checkout path differs per machine.
- Both per-PC scripts need `sudo` (a password unless the PC has passwordless sudo),
  so run them in an interactive session on each PC, or set up passwordless sudo to
  loop over hosts non-interactively.

## Wired NIC hangs at "setting network address" (every fresh box)

The switch has **no DHCP server** (deliberate — it's an isolated wired island). But a
stock install defaults the wired NIC to DHCP (`ipv4.method = auto`), so on this switch
it retries forever, sits in `connecting (getting IP configuration)` / "setting network
address", and eventually NetworkManager **drops the interface** — the box then vanishes
from the wire and (if it blocks boot) may hang on startup. Its Wi-Fi internet is
unaffected.

The cure is to take the wired NIC off DHCP — which `setup-microscope-pc.sh` does (it pins
a static IP). The catch: until it runs, the box is unreachable over the wire, so **the
first run must be at the keyboard** — that one run also installs ssh, so everything after
is remote.

(netplan/systemd-networkd boxes: the equivalent is `link-local: [ipv4, ipv6]` +
`optional: true` — see `MICROSCOPE_NETWORK.md` §5b in the seafront repo.)

## Notes

- **Backbone addressing:** proxy upstreams in `config/microscopes.json` are static
  IPs (e.g. `192.168.50.11`–`.14`). Adjust to match your wiring.
- **Dashboard port** is `gateway.dashboard_port` in the config; the systemd unit is
  generated from it. Use `8000` on a dedicated gateway.
- **lab3 test box:** `lab3` doubles as a microscope PC (its own seafront on `:8000`),
  so its dashboard runs on `:8080` and `squid1` points at `127.0.0.1`. On a dedicated
  gateway, set `dashboard_port` back to `8000` and use real backbone IPs.
- **Binds:** dashboard on `0.0.0.0` (hotspot clients get IPv4); Caddy dual-stack on
  each `:800N` automatically.
