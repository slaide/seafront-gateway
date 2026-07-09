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
2. Clone this repo into **`/opt/seafront-gateway`** (NOT your home directory) and run
   setup. Kinoite is immutable, so nothing is installed into the base: the registry and
   Caddy come up as **podman quadlets**, the dashboard as a `uv` host service, and the
   fleet SSH key is generated:
   ```bash
   sudo git clone <repo-url> /opt/seafront-gateway
   sudo chown -R "$USER:$(id -gn)" /opt/seafront-gateway
   cd /opt/seafront-gateway
   bash scripts/gateway-setup.sh          # registry + Caddy + dashboard now running
   ```
   > **Why `/opt`, not `$HOME`:** SELinux is enforcing on Kinoite and labels everything
   > under `/home` (`== /var/home`) as `user_home_t`. A systemd service's confined domain
   > is not allowed to *execute* `user_home_t` files, so the dashboard's venv `uvicorn`
   > fails with "Permission denied" — even though the same command runs fine by hand (a
   > login shell runs in an unconfined domain). `/opt` gets a system label the service may
   > execute. `gateway-setup.sh` refuses to install from a home path and runs `restorecon`.
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
   > **Write the ISO RAW to the whole device (`dd`), and do NOT use Ventoy or other
   > multiboot USB loaders.** `dd` gives the stick a standard EFI System Partition, which
   > every firmware lists. The box hardware (confirmed on the **ASUS PN52**) does *not*
   > enumerate a Ventoy stick as a boot device — its two-partition exfat+`VTOYEFI` layout
   > never appears in the boot menu or boot order, even with Secure Boot and Fast Boot off
   > (the OS still mounts it fine, which makes this look like a media problem when it is
   > really a firmware/Ventoy incompatibility). `dd of=/dev/sdX` targets the **whole disk**,
   > not a partition — double-check the device with `lsblk` first.

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
The per-box step is also a button in the dashboard (§4): it shows each box's OS + app image
version against the registry, with independent **Update OS** (`bootc upgrade`) and **Update
seafront** (`podman pull`) actions, so you can roll one route without touching the other.

## 4 · Remote access (convenience layer)

Caddy on the gateway reverse-proxies `gateway:800<n>` → `squid<n>:8000`; the dashboard
(`http://<gateway>:8000`) shows per-box health, the OS + seafront image version each box runs
vs the latest in the registry, per-box **Update OS** / **Update seafront** buttons, and
links + logs. Edit `config/microscopes.json` then `bash scripts/apply-config.sh` to change
the mapping.
This is *only* convenience — the boxes are fully operable locally with it all down.

## Layout

```
config/microscopes.json   fleet inventory (source of truth)
configs/<box>/config.json per-box seafront config (pushed; gitignored)
configs/mocroscope.profile.json  shared mock profile to merge into each box config
images/seafront/          app image Containerfile
images/kinoite/           box OS image Containerfile + baked files/ tree
images/gateway/           gateway service quadlets (registry, Caddy)
scripts/                  gateway-setup, build-images, build-installer, push-config, apply-config, set-static-ip, start/stop/status
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
- **Dashboard image-version reads + Update buttons need a box already on an image built
  after this feature landed** — they call `sudo bootc status` / `sudo podman` via the
  extended fleet sudoers rule (`images/kinoite/files/etc/sudoers.d/seafront-fleet`). Boxes
  flashed from an earlier image lack that rule, so the dashboard shows their versions as
  *unknown* until the first `bootc upgrade` rolls the new OS (with its sudoers) onto them.
  `bootc upgrade` / `reboot` / `restart seafront` work regardless (already in the old rule).
- **Fleet credential `pharmbio`** is baked into `images/kinoite/installer.toml`.
