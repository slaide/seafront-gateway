#!/usr/bin/env bash
# ONE-SHOT bring-up: name a PC and put it on the network. Run this once at the
# keyboard and you can SSH into the box afterwards. It:
#   - sets the hostname (+ /etc/hosts)            <- "rename the PC"
#   - installs & enables ssh + avahi (mDNS)       <- remote access by name
#   - pins a static backbone IP on the wired NIC  <- cures the DHCP "setting network address" hang
#   - opens the firewall on that link (ssh/mdns/seafront)
# Idempotent — safe to re-run to change a box's name or IP.
#
# Distro: Ubuntu/Debian (apt) or Arch (pacman). NetworkManager required
# (netplan/systemd-networkd boxes: see seafront/MICROSCOPE_NETWORK.md §5b var B).
# Does NOT install seafront itself — clone the repo + `uv sync` separately, then
# run it with `--host :: --port <port>` (MICROSCOPE_NETWORK.md §11).
#
# After this, register the PC on the gateway:  scripts/register-microscope.sh
set -euo pipefail

usage() {
  echo "usage: $0 <hostname> <static-ip[/cidr]> [seafront-port]"
  echo "  e.g. $0 squid5 192.168.50.15 8000   (cidr defaults to /24, port to 8000)"
  echo "  override NIC detection with:  IFACE=enp2s0 $0 ..."
  exit 1
}
[ $# -ge 2 ] || usage
HOSTNAME_NEW="$1"
IP_CIDR="$2"; [[ "$IP_CIDR" == */* ]] || IP_CIDR="$IP_CIDR/24"
SEAFRONT_PORT="${3:-8000}"

# --- detect wired interface (override with IFACE=...) --------------------------
IFACE="${IFACE:-$(ip -o link show | awk -F': ' '{print $2}' \
  | grep -E '^(en|eth)' | grep -vE 'docker|veth|br-' | head -1)}"
[ -n "$IFACE" ] || { echo "no wired interface found; set IFACE=<name> and re-run"; exit 1; }
echo "==> wired interface: $IFACE"

echo "==> caching sudo credentials"; sudo -v

# --- hostname ------------------------------------------------------------------
echo "==> setting hostname: $HOSTNAME_NEW"
sudo hostnamectl set-hostname "$HOSTNAME_NEW"
# keep /etc/hosts in sync (Debian/Ubuntu map the hostname to 127.0.1.1); otherwise
# sudo/avahi emit "unable to resolve host" warnings after a rename.
if grep -qE '^127\.0\.1\.1[[:space:]]' /etc/hosts; then
  sudo sed -i -E "s/^(127\.0\.1\.1[[:space:]]+).*/\1$HOSTNAME_NEW/" /etc/hosts
else
  echo "127.0.1.1 $HOSTNAME_NEW" | sudo tee -a /etc/hosts >/dev/null
fi

# --- packages: ssh + avahi (mDNS) ----------------------------------------------
if command -v apt-get >/dev/null 2>&1; then
  echo "==> installing openssh-server avahi-daemon libnss-mdns (apt)"
  sudo apt-get update
  sudo apt-get install -y openssh-server avahi-daemon libnss-mdns
  sudo systemctl enable --now ssh avahi-daemon       # Ubuntu unit is 'ssh'
elif command -v pacman >/dev/null 2>&1; then
  echo "==> installing openssh avahi nss-mdns (pacman)"
  sudo pacman -S --needed --noconfirm openssh avahi nss-mdns
  sudo systemctl enable --now sshd avahi-daemon
  # Arch does NOT wire nss-mdns into nsswitch automatically
  grep -q mdns /etc/nsswitch.conf || sudo sed -i \
    's/^hosts: mymachines /hosts: mymachines mdns_minimal [NOTFOUND=return] /' \
    /etc/nsswitch.conf
else
  echo "unsupported distro (need apt or pacman); install ssh+avahi manually"; exit 1
fi

# Re-publish mDNS under the (possibly just-changed) hostname. `enable --now` does
# NOT restart an already-running avahi, so without this it keeps advertising the
# old <name>.local and the box is unreachable by its new name.
echo "==> restarting avahi to publish $HOSTNAME_NEW.local"
sudo systemctl restart avahi-daemon

# --- static IP on the wired NIC -----------------------------------------------
# Two backends in the wild: NetworkManager (Ubuntu Desktop / Arch) and
# netplan + systemd-networkd (Ubuntu Server — there NM shows the NIC as
# "unmanaged" and has no "Wired connection 1" to modify). Detect and branch.
echo "==> pinning $IP_CIDR on $IFACE"
NM_STATE=""
command -v nmcli >/dev/null 2>&1 && \
  NM_STATE=$(nmcli -t -f DEVICE,STATE device 2>/dev/null | awk -F: -v i="$IFACE" '$1==i{print $2}')

if [ -n "$NM_STATE" ] && [ "$NM_STATE" != unmanaged ] && [ "$NM_STATE" != unavailable ]; then
  echo "    backend: NetworkManager ($IFACE state=$NM_STATE)"
  CON=$(nmcli -t -f NAME,DEVICE connection show | awk -F: -v i="$IFACE" '$2==i{print $1; exit}')
  CON=${CON:-"Wired connection 1"}
  sudo nmcli connection modify "$CON" \
       ipv4.method manual ipv4.addresses "$IP_CIDR" \
       ipv6.method link-local connection.autoconnect yes
  sudo nmcli connection up "$CON"
elif command -v netplan >/dev/null 2>&1; then
  echo "    backend: netplan + systemd-networkd ($IFACE not managed by NM)"
  NP=/etc/netplan/60-microscope-lan.yaml
  sudo tee "$NP" >/dev/null <<EOF
network:
  version: 2
  ethernets:
    $IFACE:
      renderer: networkd
      dhcp4: false
      dhcp6: false
      addresses: [$IP_CIDR]
      link-local: [ipv6]
      optional: true
EOF
  sudo chmod 600 "$NP"
  sudo netplan apply
else
  echo "!! $IFACE is unmanaged by NetworkManager and netplan is absent — cannot set a"
  echo "   static IP automatically. Configure $IP_CIDR (no DHCP) on $IFACE by hand."
  exit 1
fi

# --- ensure sshd is reachable on the wired backbone ----------------------------
# Some boxes ship with sshd pinned to a foreign ListenAddress (e.g. a management
# LAN), so it never listens on this link and is unreachable here. If such a pin
# exists and doesn't already include our IP, ADD our wired IP: sshd then listens
# on the union (existing access preserved, no exposure on unrelated interfaces).
# A box with no ListenAddress already listens on all interfaces — leave it alone.
WIRED_IP="${IP_CIDR%/*}"
SSHD_FILES=(/etc/ssh/sshd_config)
[ -d /etc/ssh/sshd_config.d ] && SSHD_FILES+=(/etc/ssh/sshd_config.d/*.conf)
if grep -rqiE '^[[:space:]]*ListenAddress' "${SSHD_FILES[@]}" 2>/dev/null \
   && ! grep -rqiE "^[[:space:]]*ListenAddress[[:space:]]+${WIRED_IP//./\\.}([[:space:]]|$)" "${SSHD_FILES[@]}" 2>/dev/null; then
  echo "==> sshd is pinned to a specific address; also binding $WIRED_IP"
  if grep -qiE '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\.d' /etc/ssh/sshd_config; then
    echo "ListenAddress $WIRED_IP" | sudo tee /etc/ssh/sshd_config.d/60-microscope-lan.conf >/dev/null
  else
    printf '\n# microscope LAN backbone\nListenAddress %s\n' "$WIRED_IP" | sudo tee -a /etc/ssh/sshd_config >/dev/null
  fi
  if sudo sshd -t; then
    sudo systemctl restart ssh 2>/dev/null || sudo systemctl restart sshd
  else
    echo "!! sshd -t failed after adding ListenAddress — NOT restarting; check /etc/ssh/sshd_config"
  fi
fi

# --- firewall: open the wired link only (if ufw is active) ---------------------
if systemctl is-active --quiet ufw; then
  echo "==> opening firewall on $IFACE (ssh, mdns, seafront:$SEAFRONT_PORT)"
  sudo ufw allow in on "$IFACE" to any port 22 proto tcp comment 'ssh microlan'
  sudo ufw allow in on "$IFACE" to any port 5353 proto udp comment 'mdns microlan'
  sudo ufw allow in on "$IFACE" to any port "$SEAFRONT_PORT" proto tcp comment 'seafront'
fi

echo
echo "==> done. this PC: $HOSTNAME_NEW.local  @  ${IP_CIDR%/*}  on $IFACE"
echo "    next: install seafront as a service ->  scripts/install-seafront-service.sh <microscope-profile> --dir <seafront-checkout> --port $SEAFRONT_PORT"
echo "    then on the gateway                 ->  scripts/register-microscope.sh $HOSTNAME_NEW ${IP_CIDR%/*} '' $SEAFRONT_PORT"
