# Immutable microscope fleet — Kinoite + gateway registry

**In one sentence:** each microscope box runs an immutable Fedora Kinoite desktop
plus a seafront container, both pulled from the gateway; the gateway is the only
machine that ever needs the internet, and only when *you* choose to give it.

## Three roles

- **Gateway `squidway`** (Fedora Kinoite, on-demand internet) — the fleet's offline
  mirror, running the **same OS as the boxes**. Runs a local OCI **registry** on the
  backbone (`192.168.50.1:5000`) holding two images: the **Kinoite OS image** and the
  **seafront app image**. Its own services are podman quadlets (registry, Caddy) plus a
  `uv` dashboard host service — no packages layered on the immutable base. It builds and
  serves the images; it doesn't need the box image itself (no microscope hardware).
- **Boxes `squid1–4`** (Kinoite, no internet) — immutable desktop appliances.
  Track the gateway's OS image via **bootc**; run seafront as a **podman**
  container from the gateway registry, serving `localhost:8000`. Local KDE desktop
  + Firefox is the break-glass console.
- **Backbone `192.168.50.0/24`** (isolated, no internet) — how boxes reach the
  gateway registry.

```
 internet ──(only when you connect it)──▶ ┌──────────────────────────────┐
                                          │  GATEWAY  squidway           │
                                          │  registry :5000              │
                                          │    • kinoite:<ver>  (whole OS)│
                                          │    • seafront:<ver> (app)     │
                                          │  dashboard (orchestrates)     │
                                          └──────────────┬───────────────┘
                              backbone 192.168.50.0/24 (NO internet)
                    ┌───────────────┬─────────┴────────┬───────────────┐
                 squid1          squid2             squid3          squid4
              Kinoite (bootc) tracks kinoite image  ·  podman runs seafront image
              localhost:8000 + Firefox  ◀── break-glass, zero network dependency
```

## The two things that ship

### 1. The OS — rare, on your command
- **Refresh (the ONLY internet moment in the system):** connect the gateway to the
  internet → rebuild/pull the customized Kinoite image into the local registry →
  disconnect.
- **Roll out to a box:** `bootc upgrade` over the backbone → new OS staged in the
  spare slot → reboot → atomic switch. A bad boot **auto-rolls-back**. The
  dashboard triggers this per *idle* box, so a running acquisition is untouched.
- The Kinoite image is pre-baked from **one Containerfile** with everything the
  hardware needs: camera drivers (gxipy/toupcam), udev rules, the `usbfs_memory_mb`
  kernel arg, the KDE desktop, a Firefox-kiosk autostart to `localhost:8000`, the
  seafront systemd/podman unit, and trust for the gateway registry.

### 2. The app — often, cheap
- Build the seafront image (where there's internet, or on the gateway) → push to
  the gateway registry.
- Per box: `podman pull` + restart the unit. Seconds; only changed layers cross the
  backbone. **No reboot, no package installs.**

## Why it's "flash OS, done — push image, done"
- **Flash a new box (once):** install Kinoite from the pre-baked image, point bootc
  at the gateway, first boot pulls seafront. No `dnf`/`apt` install lists, no
  per-package drift, no long update process.
- **Update software:** push a container image. No pip/uv/wheels, no Python-version
  matching, no dependency resolution on the box — the exact failure class
  (opencv / uv-cache) that motivated all of this.
- Everything a box needs comes from the gateway over the backbone; boxes never see
  the internet.

## The only per-box mutable state
Everything above (OS + app) is immutable and **identical fleet-wide**. Only two
small things differ per box, and both live on the writable partition that survives
OS upgrades:
- **`config.json`** — that box's camera + microcontroller USB IDs, plus the shared
  `mocroscope` profile. Pushed from the gateway (as `push-config.sh` does today) and
  mounted into the container.
- **acquired image data** — on a writable data path.

## Break-glass guarantee
Even with the gateway, backbone, and SSH all dead: walk up to the box → KDE desktop
→ Firefox → `localhost:8000` → the local seafront container → full microscope
control. The local path has **zero** network dependency; remote access is only ever
a convenience layer on top.

## Continuity with today's tooling
This maps onto the dashboard's existing **stage → flush** split: *stage* becomes
"refresh an image in the gateway registry," *flush* becomes "tell a box to pull it"
(`podman pull` for the app, `bootc upgrade` for the OS). Same idle-safe, per-box
rollout you already have.
