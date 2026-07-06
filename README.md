# Seafront microscope fleet

One **gateway** and N **microscope boxes**, all running the **same OS — Fedora Kinoite**
(immutable KDE). The gateway (occasional internet) serves two OCI images from a local
registry to the boxes (no internet) over an isolated wired backbone. Each box runs
seafront in a podman container and a local KDE desktop as a break-glass console.
Provisioning is "flash the OS, push an image" — no per-package installs on any machine.
Architecture + rationale: [`docs/immutable-fleet.md`](docs/immutable-fleet.md).

- **Inventory / addressing:** `config/microscopes.json`. Backbone `192.168.50.0/24`,
  **no DHCP**: gateway `.1`, boxes `.11–.14`. Credentials `pharmbio` fleet-wide.
- **Two images:** `seafront` (the app) and `seafront-os` (the whole Kinoite OS).
  Both live in the gateway registry at `192.168.50.1:5000`.

## 1 · Gateway (once)

1. Install **Fedora Kinoite** on the gateway PC; create user `pharmbio`. Give it internet
   (Wi-Fi). (Same OS as the boxes — the gateway is just Kinoite + a few containers.)
   Pin its backbone NIC to `192.168.50.1` (the registry binds this address; find the
   wired NIC with `nmcli device status`):
   ```bash
   sudo nmcli connection modify "<wired-con>" ipv4.method manual ipv4.addresses 192.168.50.1/24
   sudo nmcli connection up "<wired-con>"
   ```
2. Clone this repo and run setup. Kinoite is immutable, so nothing is installed into the
   base: the registry and Caddy come up as **podman quadlets**, the dashboard as a `uv`
   host service, and the fleet SSH key is generated:
   ```bash
   git clone <repo-url> ~/seafront-gateway && cd ~/seafront-gateway
   bash scripts/gateway-setup.sh          # registry + Caddy + dashboard now running
   ```
3. Build + push both images (**the one step that needs internet**):
   ```bash
   bash scripts/build-images.sh           # seafront + seafront-os -> :5000
   ```
   Re-run `build-images.sh` whenever you want to refresh: reconnect the gateway to the
   internet → build → done. Nothing else in the system ever touches the internet.
   The app image builds `--frozen` from a **pinned seafront commit** (`SEAFRONT_REF` in
   `build-images.sh`) + its committed `uv.lock`, so a given gateway commit reproduces the
   exact same image.
4. Turn the OS image into a bootable USB installer (also needs internet):
   ```bash
   bash scripts/build-installer.sh        # -> out/bootiso/install.iso
   sudo dd if=out/bootiso/install.iso of=/dev/sdX bs=4M status=progress oflag=sync
   ```

## 2 · Each microscope box (once, at the keyboard)

Prep first (once, on the gateway): create `configs/squid<n>/config.json` for each box —
its real profile (placeholder USB IDs) **plus** the `mocroscope` object from
`configs/mocroscope.profile.json`. seafront won't start without this file.

1. **Boot the box from the installer USB** (`install.iso` from §1.4) and install to the
   internal disk. The box comes up already *being* the fleet image — no `bootc switch`,
   no per-package setup. SSH trust, registry trust, USB rules, the seafront service, and
   the kiosk autostart are all baked in; `pharmbio` is created by the installer.
2. First boot, **at the keyboard**, set identity + backbone IP (the switch has no DHCP,
   so the box is invisible on the wire until this runs):
   ```bash
   sudo box-postinstall <n>      # n = 1..4  ->  hostname squid<n>, IP 192.168.50.1<n>
   ```
3. Back on the gateway, push the box's config:
   ```bash
   bash scripts/push-config.sh squid<n>
   ```
   The box now serves seafront on `:8000` (mock profile) and auto-opens it fullscreen
   once someone logs into KDE (autologin is off by default — see caveats).

## 3 · Updates (from the gateway, idle boxes only)

| What | How |
|---|---|
| **App** (seafront) | bump `SEAFRONT_REF` in `build-images.sh` → `build-images.sh --seafront` → per box `sudo systemctl restart seafront` (quadlet `ExecStartPre` re-pulls `:stable`) |
| **OS** (Kinoite) | `build-images.sh --os`, then per box: `sudo bootc upgrade` + reboot (staged in the spare slot, auto-rollback on bad boot) |
| **Config** | edit `configs/squid<n>/config.json` → `push-config.sh squid<n>` |

A running acquisition is never disturbed — update the idle boxes, flush the busy one later.

## 4 · Remote access (convenience layer)

Caddy on the gateway reverse-proxies `gateway:800<n>` → `squid<n>:8000`; the dashboard
(`http://<gateway>:8000`) shows per-box health + links + logs. Edit
`config/microscopes.json` then `bash scripts/apply-config.sh` to change the mapping.
This is *only* convenience — the boxes are fully operable locally with it all down.

## Layout

```
config/microscopes.json   fleet inventory (source of truth)
configs/<box>/config.json per-box seafront config (pushed; gitignored)
configs/mocroscope.profile.json  shared mock profile to merge into each box config
images/seafront/          app image Containerfile
images/kinoite/           box OS image Containerfile + baked files/ tree
images/gateway/           gateway service quadlets (registry, Caddy)
scripts/                  gateway-setup, build-images, build-installer, push-config, apply-config, start/stop/status
dashboard/  Caddyfile      remote-access layer (dashboard = uv host service; Caddy = quadlet)
docs/immutable-fleet.md   architecture
```

## Pre-flash checklist / caveats

Unproven — verify on the gateway + one box before flashing the fleet:

- **`bootc-image-builder` ISO** (`build-installer.sh`) — untested; finicky about rootful
  storage + registry trust on the gateway.
- **One box installs, boots, and the gateway can reach it** — exercises sshd + fleet key +
  firewall + `box-postinstall` networking together.
- **USB passthrough** (needs hardware): `…/udev/rules.d/90-seafront-usb.rules` and the
  quadlet device section carry **placeholder vendor IDs** (fill from `lsusb`); SELinux is
  enforcing, so you'll likely need `setsebool -P container_use_devices on` (or
  `SecurityLabelDisable=true` in the quadlet).

Current limitations:
- **Kiosk needs a logged-in KDE session** (autologin off) — the UI appears after login.
  Enable SDDM autologin for `pharmbio` to have it come up unattended.
- **Dashboard stage/flush buttons aren't wired to the image/bootc flow** — drive updates
  from the CLI (§3); status/logs/restart work.
- **Fleet credential `pharmbio`** is baked into `images/kinoite/installer.toml`.
