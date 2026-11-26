# AGENTS.md

Fleet management for one gateway plus N microscope boxes, all on Fedora Kinoite (immutable).
There is no repo-wide build, test suite, lint config or CI. The repo ships:

- shell scripts in `scripts/`, run on the gateway,
- two OCI images, `images/seafront/` (the app) and `images/kinoite/` (the box OS), built on the
  gateway and pushed to its registry,
- the dashboard: one FastAPI file, `dashboard/app.py`, with all HTML/CSS/JS inline in `HTML`.

`README.md` is the operator runbook and `docs/immutable-fleet.md` the architecture write-up.
Both still show the pre-2026-07 backbone (`192.168.50.x`); for addresses, trust
`config/microscopes.json` and the code.

## Network and access

Backbone is the shared lab LAN `10.10.0.0/24` (moved off the isolated `192.168.50.0/24` in
2026-07). Gateway `10.10.0.69`: registry on `:5000`, dashboard on `:8000`. Box addresses are in
`config/microscopes.json` (squid1-4 at `.11`-`.14`, seafront on `:8000`).

People log into the gateway and the boxes over SSH with a **username and password**. No key is
set up for that. Do not try `~/.ssh/fleet`, `id_ed25519` or any other local key against them;
ask the user for the login.

The fleet key is for the gateway's own automation only: `~/.ssh/fleet` on the gateway, its
`.pub` baked into the OS image as `/etc/ssh/authorized_keys.d/pharmbio`. The scripts and the
dashboard use it to drive boxes as `pharmbio`.

## Where things run

Almost everything runs on the gateway; each `scripts/*.sh` header says where it runs and what
it needs. The exception is `sudo box-postinstall <name> <ip>` at a new box's own keyboard,
which sets hostname + static IP; until then the box has no backbone address.

Internet is needed only by `gateway-setup.sh`, `build-images.sh`, `build-installer.sh`,
`bump-os-base.sh` and `self-update.sh` (git pull). The gateway gets it by switching its single
Wi-Fi radio to client mode (`wifi-mode.sh client`), which takes the hotspot down.

## Commands

```bash
# dashboard, local dev (reads ../config/microscopes.json)
cd dashboard && uv sync && uv run uvicorn --app-dir . app:app --host 0.0.0.0 --port 8000

python3 scripts/fleet_config.py validate   # inventory check; module docstring lists every verb
scripts/apply-config.sh                    # after ANY inventory edit; self-elevates

scripts/build-images.sh                    # both images
scripts/build-images.sh --seafront         # app only; SEAFRONT_REF=<sha|branch|tag|latest> to test a ref
scripts/build-images.sh --os               # OS only
```

Validate a change with `fleet_config.py validate`, by running the dashboard locally, and by
running the script you touched.

## Architecture

**Inventory.** `config/microscopes.json`; schema, validation and atomic writes live in
`scripts/fleet_config.py`. That module is stdlib-only so the system `python3` and the dashboard
venv can both import it; keep it that way, and read the inventory through it in new code
(`gen-caddyfile.py`, `push-config.sh` and `gateway-setup.sh` still parse the JSON directly).
The dashboard loads the inventory per request, so inventory changes need no restart. Its
mutating endpoints call `apply-config.sh --no-dashboard` so they don't restart the process
serving the request.

**Two images, two independent update routes** (separate dashboard buttons):

- `seafront` (`images/seafront/Containerfile`): the app, plus the Daheng Galaxy SDK extracted
  from the gxipy fork's `install.run`. On a box it is a rootful quadlet system service
  (`/etc/containers/systemd/seafront.container` -> `seafront.service`, container `seafront`,
  port 8000) with host `/dev` bind-mounted for the camera and the Teensy. Updated by
  `podman pull` + `systemctl restart seafront`.
- `seafront-os` (`images/kinoite/Containerfile`): the whole box OS, tracked with `bootc`,
  updated by `bootc upgrade` + reboot. `images/kinoite/files/` is copied to `/` and holds the
  baked config: quadlet, sudoers, udev, sshd, kargs, kiosk autostart, `box-postinstall`,
  `seafront-run`.

A box's version is a manifest-digest comparison: registry v2 API vs `bootc status` /
`podman image inspect` over SSH.

**Box sudoers allowlist:** `images/kinoite/files/etc/sudoers.d/seafront-fleet`. A new
dashboard/script action that runs something privileged on a box must be added there, or it
fails silently over the tty-less SSH channel.

**Per-box mutable state** (survives OS upgrades): `/home/pharmbio/seafront/`, mounted into the
container as `/root/seafront`. It holds `config.json` (pushed by `push-config.sh`), the
`active_microscope` profile override and acquired images. Plus hostname and IP. Everything else
is identical across the fleet.

External storage: the container sees the host's `/mnt` (= `/var/mnt`) at both paths, with
`rslave` propagation and SELinux confinement off (`SecurityLabelDisable`), so a drive, sshfs or
NFS mounted there later is usable without a `context=` option. A T7 SSD (label `T7`)
auto-mounts at `/mnt/t7`: `var-mnt-t7.mount` when plugged in later, `t7-mount-at-boot.service`
when already plugged in at boot.

`sudo seafront-run [cmd]` on a box starts a throwaway container from the seafront image with
the same device passthrough, for hardware checks without touching the service.

**Dashboard** is unauthenticated and admin-level by design. Long operations (bootc upgrade,
podman pull, image builds, renumbering, gateway self-update) run as streamed background jobs
in the `JOBS` dict.

## Invariants

Each is explained in a comment at its site; read that before "fixing" one.

- `SEAFRONT_REF` default is pinned twice: the `REF=` line in `scripts/build-images.sh` (source
  of truth; the dashboard reads it too) and `ARG SEAFRONT_REF=` in
  `images/seafront/Containerfile`. Bump both.
- The OS base is digest-pinned (`FROM ...kinoite:44@sha256:...`). Move it only with
  `scripts/bump-os-base.sh`; a moved tag re-downloads the multi-GB base.
- The proxy-port window 8001-8099 is duplicated: `PROXY_PORT_RANGE` in `fleet_config.py` and
  `PROXY_LO`/`PROXY_HI` in `apply-config.sh`, which closes stale firewall ports inside it.
- Bind uvicorn/seafront to `0.0.0.0`, never `::`. uvicorn's IPv6 socket is V6ONLY and refuses
  IPv4.
- The gateway checkout lives under `/opt`, not `/home`: SELinux labels home files
  `user_home_t`, which the dashboard's systemd unit can't exec. `gateway-setup.sh` refuses a
  home path and runs `restorecon`.
- No `host:port` colon in a sudoers command spec; sudo then ignores the whole file. Match the
  registry with `10.10.0.69*`.
- Reboot a box with `/usr/sbin/reboot`; `systemctl reboot` isn't in the sudoers rule.
- Box SSH host keys aren't pinned (`StrictHostKeyChecking=no`, `UserKnownHostsFile=/dev/null`)
  because a reflashed box gets new keys. Exception: `push-config.sh` still uses `accept-new`
  against the default known_hosts.
- Gitignored, never hand-edit: `Caddyfile` (generated by `gen-caddyfile.py`),
  `configs/*/config.json`, `images/kinoite/fleet.pub`.
