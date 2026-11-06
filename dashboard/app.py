"""Microscope gateway dashboard + fleet control.

A small FastAPI app that serves a landing page listing every microscope on the
LAN, health-checks each one, reports the OS and seafront-app IMAGE versions each
box runs versus the latest built in the gateway registry, and offers per-scope
maintenance actions.

Everything the fleet ships is an OCI image pulled from the gateway registry
(10.10.0.69:5000): the whole box OS (Fedora Kinoite, tracked with `bootc`) and
the seafront app (a podman container). So "which version is a box on?" is a
digest comparison, and updating is either `bootc upgrade` (OS) or `podman pull`
(app) — two INDEPENDENT routes with their own buttons.

Three status signals are reported per scope:
  - host_up     : TCP connect to <host>:22 succeeds          (the computer is on)
  - service_up  : HTTP reply from <host>:<seafront_port>      (seafront is serving)
  - image info  : OS + app image digest/version on the box vs the registry
The 5s polling loop uses only the two cheap TCP/HTTP probes (no SSH) so it stays
fast; image info is gathered over SSH on a slower (30s) cadence.

SSH is used for on-demand actions and image queries, via a dedicated key and a
NARROW sudoers rule on each box (see images/kinoite/files/etc/sudoers.d/
seafront-fleet): systemctl {start,stop,restart,status,is-active} seafront,
bootc {status,upgrade,switch}, podman {image inspect,pull} of the gateway
registry, and reboot — and nothing else.

Config comes from ../config/microscopes.json (shared with the Caddyfile
generator), so there is one source of truth.
"""
import asyncio
import copy
import hashlib
import ipaddress
import json
import os
import pathlib
import re
import shlex
import sys
import time

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
import fleet_config  # noqa: E402  single source of truth for the inventory (pure stdlib)


# The inventory is read FRESH on every request (no import-time snapshot), so add /
# remove / renumber — from the CLI scripts OR the dashboard's own write endpoints below
# — take effect immediately with no dashboard restart.
def scopes() -> list[dict]:
    return fleet_config.load()["microscopes"]


def scope_or_404(name: str) -> dict:
    m = fleet_config.by_name(fleet_config.load(), name)
    if m is None:
        raise HTTPException(404, f"unknown microscope: {name}")
    return m

# SSH identity the dashboard uses to reach the scopes (baked into the box image as
# the gateway's fleet key). Overridable via env for testing.
SSH_KEY = os.environ.get("FLEET_SSH_KEY", os.path.expanduser("~/.ssh/fleet"))
SSH_USER = os.environ.get("FLEET_SSH_USER", "pharmbio")
SEAFRONT_CONFIG_PATH = "~/seafront/config.json"  # on the scope
# Multiplexed-connection socket, one per remote user@host:port. Reusing a single warm
# master connection per box (see _ssh_base) is what keeps continuous polling from
# opening a fresh SSH each time — the connection flood that trips the boxes' per-source
# SSH penalty. %r/%h/%p are expanded by ssh, not here.
CONTROL_PATH = os.environ.get("FLEET_SSH_CONTROL_PATH", os.path.expanduser("~/.ssh/cm-%r@%h:%p"))

# The gateway registry and the two images it serves. Boxes reference these exact
# refs (bootc spec.image and the seafront quadlet Image=), so the same string is
# both what we query in the registry and what we compare against on the box.
REGISTRY = os.environ.get("FLEET_REGISTRY", "10.10.0.69:5000")
OS_REPO = "seafront-os"
APP_REPO = "seafront"
APP_IMAGE = f"{REGISTRY}/{APP_REPO}:stable"

SYSTEMCTL = "/usr/bin/systemctl"


def _browse_url(u: str) -> str:
    """Normalize a git remote (https or git@) to a browsable https URL."""
    u = (u or "").strip()
    if u.startswith("git@"):
        u = u.replace(":", "/", 1).replace("git@", "https://", 1)
    return u[:-4] if u.endswith(".git") else u


def _seafront_repo_url() -> str | None:
    """The seafront source repo the app image is built from (single source of
    truth: the SEAFRONT_REPO arg in images/seafront/Containerfile)."""
    cf = ROOT / "images" / "seafront" / "Containerfile"
    try:
        for line in cf.read_text().splitlines():
            if line.strip().startswith("ARG SEAFRONT_REPO="):
                return _browse_url(line.split("=", 1)[1])
    except Exception:
        pass
    return None


SEAFRONT_REPO_URL = _seafront_repo_url()

app = FastAPI(title="Microscope Gateway")


# --- status probes (no SSH — fast enough for the polling loop) -----------------
async def _tcp_up(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        fut = asyncio.open_connection(host, port)
        reader, writer = await asyncio.wait_for(fut, timeout=timeout)
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return True
    except Exception:
        return False


async def _service_up(client: httpx.AsyncClient, host: str, port: int) -> bool:
    try:
        r = await client.get(f"http://{host}:{port}/", timeout=1.5)
        return r.status_code < 500
    except Exception:
        return False


async def _check(client: httpx.AsyncClient, m: dict) -> dict:
    host_up, service_up = await asyncio.gather(
        _tcp_up(m["host"], 22),
        _service_up(client, m["host"], m["seafront_port"]),
    )
    return {
        "name": m["name"],
        "host": m["host"],
        "proxy_port": m["proxy_port"],
        "seafront_port": m["seafront_port"],
        "host_up": host_up,
        "service_up": service_up,
    }


# --- SSH plumbing for on-demand actions + image queries ------------------------
def _ssh_base(name: str) -> list[str]:
    m = scope_or_404(name)
    return [
        "ssh", "-i", SSH_KEY,
        "-o", "BatchMode=yes",                    # never hang on a password prompt
        # The boxes sit on an isolated, trusted backbone and are RE-IMAGED regularly,
        # so their SSH host keys rotate every install. Pinning keys (accept-new) then
        # hard-fails with "REMOTE HOST IDENTIFICATION HAS CHANGED" after a reflash —
        # which broke Update-OS on a reinstalled box. There is no meaningful MITM
        # surface on this switch, so don't pin or persist host keys.
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",                   # drop the "added to known hosts" warning
        "-o", "ConnectTimeout=5",
        # REUSE ONE CONNECTION PER BOX. The dashboard polls every box continuously; a
        # fresh SSH per read is a flood of short-lived connections from one source IP,
        # which trips the boxes' OpenSSH PerSourcePenalties (9.8+, default on): they then
        # RESET our connections for a cooldown ("kex_exchange_identification: Connection
        # reset by peer"), surfacing as flaky / "OS: unknown" reads that come and go. With
        # a multiplexed master (auto + persist), reads after the first share one warm
        # connection — no per-read handshake, no connection flood, no penalty. The box
        # image also exempts the backbone (sshd_config.d/10-fleet.conf) as belt-and-braces.
        "-o", "ControlMaster=auto",
        "-o", f"ControlPath={CONTROL_PATH}",
        "-o", "ControlPersist=60",
        # A reboot can kill the box end of the shared connection without a clean close; the
        # master then sits on a dead TCP link and every multiplexed read hangs until our
        # timeout, so the dashboard keeps serving pre-reboot values. Keepalives make the
        # master notice within ~10s and exit, so the next read opens a fresh connection.
        "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=2",
        # No KDC/DNS on the isolated backbone, so GSSAPI only adds a per-connection
        # name-lookup cost and buys nothing (we authenticate by key).
        "-o", "GSSAPIAuthentication=no",
        f"{SSH_USER}@{m['host']}", "--",
    ]


async def _close_ssh_master(name: str) -> None:
    """Best-effort `ssh -O exit` on the box's multiplexed master connection."""
    base = _ssh_base(name)
    target = base[base.index("--") - 1]
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-o", f"ControlPath={CONTROL_PATH}", "-O", "exit", target,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.wait(), timeout=5.0)
    except Exception:
        pass


async def _ssh(name: str, argv: list[str], timeout: float = 20.0,
               input: str | None = None) -> tuple[int, str, str]:
    """Run a command on a scope over SSH with the fleet key. `argv` is appended
    after `--`; scope name/host come only from our config, never client input.
    `input`, if given, is fed to the remote command's stdin (e.g. a config piped to
    `cat > file`)."""
    cmd = _ssh_base(name) + argv
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if input is not None else None,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(
            proc.communicate(input.encode() if input is not None else None), timeout=timeout)
    except asyncio.TimeoutError:
        raise HTTPException(504, f"ssh to {name} timed out")
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


async def _wait_service_up(name: str, timeout: float = 15.0) -> bool:
    """Poll the box's seafront HTTP port until it answers, up to `timeout`s — used to confirm
    a config change actually booted (a crash-looping seafront never serves)."""
    m = scope_or_404(name)
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient() as client:
        while time.monotonic() < deadline:
            if await _service_up(client, m["host"], m["seafront_port"]):
                return True
            await asyncio.sleep(2)
    return False


# --- image versions: gateway registry vs each box -----------------------------
_MANIFEST_ACCEPT = ", ".join([
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
])


async def _registry_image(client: httpx.AsyncClient, repo: str, tag: str = "stable") -> dict:
    """Latest built image in the gateway registry: manifest digest (the value
    bootc/podman compare against) + created time + version label, via the plain
    registry v2 HTTP API (no skopeo dependency)."""
    base = f"http://{REGISTRY}/v2/{repo}"
    try:
        r = await client.get(f"{base}/manifests/{tag}",
                             headers={"Accept": _MANIFEST_ACCEPT}, timeout=4.0)
        if r.status_code != 200:
            return {"error": f"registry HTTP {r.status_code}"}
        digest = r.headers.get("Docker-Content-Digest")
        man = r.json()
        # If the tag is a multi-arch index, follow to the first child manifest.
        if not man.get("config") and man.get("manifests"):
            child = man["manifests"][0].get("digest")
            r = await client.get(f"{base}/manifests/{child}",
                                 headers={"Accept": _MANIFEST_ACCEPT}, timeout=4.0)
            man = r.json()
        cfg_digest = man.get("config", {}).get("digest")
        version = revision = created = None
        if cfg_digest:
            rc = await client.get(f"{base}/blobs/{cfg_digest}", timeout=4.0)
            if rc.status_code == 200:
                cfg = rc.json()
                created = cfg.get("created")
                labels = cfg.get("config", {}).get("Labels") or {}
                version = labels.get("org.opencontainers.image.version")
                revision = labels.get("org.opencontainers.image.revision")
        return {"digest": digest, "version": version, "revision": revision, "created": created}
    except Exception as e:
        return {"error": str(e)}


def _parse_bootc(text: str) -> dict:
    """Parse `bootc status --format=json` into booted + staged image descriptors."""
    text = text.strip()
    if not text:
        return {"available": False}
    try:
        d = json.loads(text)
    except Exception:
        return {"available": False}
    st = d.get("status", {})
    booted = (st.get("booted") or {}).get("image", {})
    staged = st.get("staged")
    staged_img = (staged or {}).get("image", {}) if staged else {}
    return {
        "available": True,
        "version": booted.get("version"),
        "digest": booted.get("imageDigest"),
        "timestamp": booted.get("timestamp"),
        "staged_version": staged_img.get("version"),
        "staged_digest": staged_img.get("imageDigest"),
    }


def _parse_app(text: str) -> dict:
    """Parse `podman image inspect <ref>` (the image PRESENT in box storage, even
    if the container is not running)."""
    text = text.strip()
    if not text:
        return {"present": False}
    try:
        arr = json.loads(text)
        d = arr[0] if isinstance(arr, list) and arr else {}
    except Exception:
        return {"present": False}
    labels = d.get("Labels") or (d.get("Config") or {}).get("Labels") or {}
    return {"present": True, "digest": d.get("Digest"), "created": d.get("Created"),
            "revision": labels.get("org.opencontainers.image.revision")}


def _parse_drift(text: str) -> list[str] | None:
    """`seafront-etc-sync --check` output -> drifted /etc paths, or None when the check
    could not run (box image predates it, or sudo refused)."""
    lines = [l.strip() for l in text.strip().splitlines() if l.strip()]
    if not lines or lines[-1] != "rc=0":
        return None
    return lines[:-1]


def _parse_clock(chrony_out: str, box_date: str, returned_at: float) -> dict:
    """Box clock vs the gateway's. `offset_s` = box wall clock minus gateway wall clock,
    measured directly (box `date` vs the time the SSH call returned), so it is correct even
    when chrony is misconfigured; it overestimates by the SSH return latency (~10-100 ms).
    `server`/`synced` are chrony's own view: which NTP source it follows and whether it
    is locked to it (refid 0 / "Not synchronised" means free-running)."""
    clock: dict = {"offset_s": None, "server": None, "synced": False}
    try:
        clock["offset_s"] = round(float(box_date.strip()) - returned_at, 3)
    except ValueError:
        pass
    fields = {k.strip(): v.strip() for k, _, v in
              (l.partition(":") for l in chrony_out.splitlines()) if v}
    # "Reference ID    : 0A0A0045 (10.10.0.69)"; "Leap status     : Normal"
    refid, _, rest = fields.get("Reference ID", "").partition(" ")
    clock["server"] = rest.strip("() ") or None
    if refid == "7F7F0101":   # chrony's `local` reference: serving its own clock
        clock["server"] = "local clock"
    clock["synced"] = refid not in ("", "00000000") and fields.get("Leap status") == "Normal"
    return clock


# --- diagnostics ----------------------------------------------------------------
# Runs ON the box as pharmbio (stdlib python3 of the OS image), prints one JSON object.
# `summary` is the cheap part folded into every images poll (card uptime + the amber
# Diagnostics button); `full` adds the container's own view of the serial ports, which
# needs `podman exec` (sudoers) and is only run when the Diagnostics dialog is opened.
_DIAG_PY = r"""
import glob, json, os, subprocess, sys, time
full = sys.argv[1:] == ["full"]
H = os.path.expanduser("~/seafront")
def rd(p, d=None):
    try:
        with open(p) as f:
            return f.read().strip()
    except OSError:
        return d
def run(argv, t=10):
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=t)
        return r.returncode, r.stdout + r.stderr
    except Exception as e:
        return -1, str(e)
o = {"uptime_s": float((rd("/proc/uptime") or "0").split()[0])}
o["selinux"] = {"1": "enforcing", "0": "permissive"}.get(rd("/sys/fs/selinux/enforce"), "disabled")
b = rd("/sys/fs/selinux/booleans/container_use_devices")
o["container_use_devices"] = None if b is None else b.split()[0] == "1"
st = os.statvfs("/var")
o["disk"] = {"path": "/var", "total": st.f_blocks * st.f_frsize,
             "used": (st.f_blocks - st.f_bfree) * st.f_frsize, "free": st.f_bavail * st.f_frsize}
_, out = run(["systemctl", "show", "seafront", "-p", "ActiveState", "-p", "SubState",
              "-p", "NRestarts", "-p", "ActiveEnterTimestamp", "-p", "ActiveEnterTimestampMonotonic"])
kv = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
mono = int(kv.get("ActiveEnterTimestampMonotonic") or 0)
o["service"] = {"state": kv.get("ActiveState"), "sub": kv.get("SubState"),
                "restarts": int(kv.get("NRestarts") or 0), "since": kv.get("ActiveEnterTimestamp") or None,
                # systemd's monotonic clock is CLOCK_MONOTONIC, same as time.monotonic()
                "uptime_s": time.monotonic() - mono / 1e6 if kv.get("ActiveState") == "active" and mono else None}
prof = rd(H + "/active_microscope")
o["profile_source"] = "override" if prof else "image default"
if not prof:
    for l in (rd("/etc/containers/systemd/seafront.container") or "").splitlines():
        if l.startswith("Environment=SEAFRONT_MICROSCOPE="):
            prof = l.split("=", 2)[2].strip()
o["profile"] = prof
m = None
try:
    with open(H + "/config.json") as f:
        m = next((x for x in json.load(f).get("microscopes", []) if x.get("system.microscope_name") == prof), None)
except Exception:
    pass
o["profile_found"] = m is not None
o["mock"] = bool(m) and m.get("system.microscope_type") == "mock"
o["devices"] = [{"key": k, "id": m[k], "driver": m.get(k[:-3] + ".driver")}
                for k in sorted(m or {}) if k.endswith(".id") and isinstance(m[k], str)]
usb = []
for d in sorted(glob.glob("/sys/bus/usb/devices/*")):
    if not rd(d + "/idVendor") or rd(d + "/bDeviceClass") == "09":   # no interfaces, no hubs
        continue
    usb.append({"bus": os.path.basename(d), "vid": rd(d + "/idVendor"), "pid": rd(d + "/idProduct"),
                "manufacturer": rd(d + "/manufacturer"), "product": rd(d + "/product"),
                "serial": rd(d + "/serial"), "tty": []})
bybus = {u["bus"]: u for u in usb}
for t in sorted(glob.glob("/sys/class/tty/ttyACM*") + glob.glob("/sys/class/tty/ttyUSB*")):
    p = os.path.realpath(t + "/device")
    for _ in range(4):
        if os.path.basename(p) in bybus:
            bybus[os.path.basename(p)]["tty"].append("/dev/" + os.path.basename(t))
            break
        p = os.path.dirname(p)
o["usb"] = usb
if full:
    rc, out = run(["sudo", "-n", "/usr/bin/podman", "exec", "seafront",
                   "/app/.venv/bin/python", "-m", "serial.tools.list_ports", "-v"], 20)
    o["container_ports"] = {"rc": rc, "out": out}
    o["chrony"] = run(["chronyc", "-n", "tracking"])[1]
o["now"] = time.time()   # last, so it is as close as possible to the ssh return
print(json.dumps(o))
"""


def _placeholder_id(i: str) -> bool:
    return not i.strip() or "PLACEHOLDER" in i.upper()


def _diag_issues(d: dict | None, clock: dict | None) -> list[str]:
    """Things worth a look, short enough for a tooltip. Drives the amber Diagnostics button."""
    if not d:
        return []
    out: list[str] = []
    if clock and clock.get("offset_s") is not None and abs(clock["offset_s"]) >= 1:
        out.append(f"clock off by {clock['offset_s']:+.1f} s")
    if clock and not clock.get("synced"):
        out.append("clock not synced")
    if d.get("selinux") != "enforcing":
        out.append(f"SELinux {d.get('selinux')}")
    if d.get("container_use_devices") is False:
        out.append("container_use_devices off: the container cannot open USB devices")
    disk = d.get("disk") or {}
    if disk.get("total") and disk["free"] / disk["total"] < 0.10:
        out.append(f"{disk['path']} {100 - 100 * disk['free'] // disk['total']}% full")
    svc = d.get("service") or {}
    if svc.get("state") != "active":
        out.append(f"seafront service {svc.get('state')} ({svc.get('sub')})")
    elif svc.get("restarts") and (svc.get("uptime_s") or 0) < 600:
        out.append(f"seafront restarted {svc['restarts']}x, up only {int(svc['uptime_s'] or 0)} s")
    if not d.get("profile_found"):
        out.append(f"profile {d.get('profile')!r} not in config.json")
    elif not d.get("mock"):
        serials = {u.get("serial") for u in d.get("usb") or []}
        for dev in d.get("devices") or []:
            if _placeholder_id(dev["id"]):
                out.append(f"{dev['key']} is a placeholder")
            elif dev["id"] not in serials:
                out.append(f"{dev['key']} {dev['id']} not on USB")
    cp = d.get("container_ports")
    if cp and cp["rc"] == 0 and not d.get("mock"):
        for dev in d.get("devices") or []:
            if dev["key"] == "microcontroller.id" and not _placeholder_id(dev["id"]) \
                    and f"SER={dev['id']}" not in cp["out"]:
                out.append(f"microcontroller {dev['id']} not visible inside the container")
    return out


def _parse_diag(text: str) -> dict | None:
    try:
        d = json.loads(text.strip().splitlines()[-1])
        return d if isinstance(d, dict) else None
    except Exception:
        return None


# List the profile names in the box's config.json (no sudo — pharmbio owns it). Single
# argv element / single-quoted python so ssh's space-join + remote re-parse keep it intact.
_LIST_PROFILES_CMD = (
    "python3 -c 'import json,os;"
    "print(chr(10).join(m.get(\"system.microscope_name\",\"\") "
    "for m in json.load(open(os.path.expanduser(\"~/seafront/config.json\"))).get(\"microscopes\",[])))'"
    " 2>/dev/null"
)

# One SSH round-trip per box: OS image (bootc) + app image (podman) + the seafront profiles
# in config.json + the active-profile override. bootc/podman need root (fleet sudoers grants
# exactly those reads); the config reads run as pharmbio.
_BOX_IMAGES_CMD = (
    "sudo -n /usr/bin/bootc status --format=json 2>/dev/null"
    "; echo '<<<SPLIT>>>'; "
    f"sudo -n /usr/bin/podman image inspect {APP_IMAGE} 2>/dev/null"
    "; echo '<<<SPLIT>>>'; "
    f"{_LIST_PROFILES_CMD}"
    "; echo '<<<SPLIT>>>'; "
    "cat ~/seafront/active_microscope 2>/dev/null"
    "; echo '<<<SPLIT>>>'; "
    # image-owned /etc files that differ from the image (bootc keeps local edits across
    # upgrades); rc line distinguishes "no drift" from "check unavailable" (older image)
    "sudo -n /usr/bin/seafront-etc-sync --check 2>/dev/null; echo \"rc=$?\""
    "; echo '<<<SPLIT>>>'; "
    # uptime, service state, SELinux, disk, USB vs the active profile (see _DIAG_PY)
    f"python3 -c {shlex.quote(_DIAG_PY)} summary 2>/dev/null"
    "; echo '<<<SPLIT>>>'; "
    # clock: chrony's view (which server, synced?) + the box's wall clock, read LAST so it
    # is taken as close as possible to when the SSH call returns (see _parse_clock)
    # `| cat`: with its stdout directly on the ssh channel (or uvicorn's subprocess pipe, see
    # _gateway_time), chronyc 4.9 prints nothing and exits 0; that is also why `-c` once
    # looked broken
    "chronyc -n tracking 2>/dev/null | cat"
    "; echo '<<<SPLIT>>>'; "
    "date +%s.%N"
)


# Last successful per-box read, so a transient SSH failure serves stale-but-real data
# instead of blanking the box to "unknown". Keyed by name -> (result, monotonic time).
_LAST_BOX_IMAGES: dict[str, tuple[dict, float]] = {}


async def _box_images(name: str) -> dict:
    # Single argv element: ssh space-joins argv and the remote shell re-parses, so
    # the `;`/redirection are interpreted on the box (not locally).
    out = ""
    try:
        _rc, out, _err = await _ssh(name, [_BOX_IMAGES_CMD], timeout=15.0)
    except HTTPException:
        out = ""
    returned_at = time.time()   # gateway wall clock, compared with the box's `date`
    # The command echoes '<<<SPLIT>>>' between sections unconditionally, so its presence
    # proves the SSH actually ran the command — vs. a reset/timeout that returns nothing.
    # On a real failure, serve the last good read (flagged with its age) rather than
    # collapsing a healthy box to "unknown" on one transient miss (the flaky-read symptom).
    if "<<<SPLIT>>>" not in out:
        cached = _LAST_BOX_IMAGES.get(name)
        if cached:
            result, ts = cached
            served = copy.deepcopy(result)
            served["stale_seconds"] = int(time.monotonic() - ts)
            return served
        return {"name": name, "os": {"available": False}, "seafront": {"present": False},
                "microscopes": [], "active": None, "stale_seconds": None}
    parts = out.split("<<<SPLIT>>>")
    os_part = parts[0] if len(parts) > 0 else ""
    app_part = parts[1] if len(parts) > 1 else ""
    prof_part = parts[2] if len(parts) > 2 else ""
    active_part = parts[3] if len(parts) > 3 else ""
    drift_part = parts[4] if len(parts) > 4 else ""
    diag_part = parts[5] if len(parts) > 5 else ""
    chrony_part = parts[6] if len(parts) > 6 else ""
    date_part = parts[7] if len(parts) > 7 else ""
    profiles = [p.strip() for p in prof_part.splitlines() if p.strip()]
    os_info = _parse_bootc(os_part)
    os_info["etc_drift"] = _parse_drift(drift_part)
    clock = _parse_clock(chrony_part, date_part, returned_at)
    diag = _parse_diag(diag_part)
    result = {"name": name, "os": os_info, "seafront": _parse_app(app_part),
              "microscopes": profiles, "active": active_part.strip() or None,
              "clock": clock, "uptime_s": (diag or {}).get("uptime_s"),
              "issues": _diag_issues(diag, clock)}
    _LAST_BOX_IMAGES[name] = (copy.deepcopy(result), time.monotonic())
    return {**result, "stale_seconds": 0}


@app.get("/api/images")
async def images() -> JSONResponse:
    """OS + seafront image versions: latest in the gateway registry, and what each
    box currently has, annotated with whether the box is up to date."""
    async with httpx.AsyncClient() as client:
        (reg_os, reg_app), boxes = await asyncio.gather(
            asyncio.gather(_registry_image(client, OS_REPO), _registry_image(client, APP_REPO)),
            asyncio.gather(*[_box_images(m["name"]) for m in scopes()]),
        )
    reg_os["vs_checkout"] = await _os_image_vs_checkout(reg_os.get("revision"))
    for b in boxes:
        osd, appd = b["os"], b["seafront"]
        rd = reg_os.get("digest")
        # "Up to date" means the box runs the image's config too: an image-owned /etc file
        # with local edits is kept by bootc across upgrades, so a digest match alone lies.
        osd["up_to_date"] = bool(osd.get("digest") and rd and osd["digest"] == rd
                                 and osd.get("etc_drift") == [])
        osd["staged_latest"] = bool(osd.get("staged_digest") and rd and osd["staged_digest"] == rd)
        ad = reg_app.get("digest")
        appd["up_to_date"] = bool(appd.get("digest") and ad and appd["digest"] == ad)
    return JSONResponse({"registry": {"os": reg_os, "seafront": reg_app}, "boxes": list(boxes)})


@app.get("/api/microscopes")
async def microscopes() -> JSONResponse:
    return JSONResponse(scopes())


@app.get("/api/status")
async def status() -> JSONResponse:
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*[_check(client, m) for m in scopes()])
    return JSONResponse(list(results))


# --- quick actions (return as soon as the command completes) -------------------
@app.post("/api/scope/{name}/restart-service")
async def restart_service(name: str) -> JSONResponse:
    # Applies a freshly-pulled app image: the quadlet's ExecStartPre pulls :stable,
    # then the container restarts. Give it headroom in case a pull happens here.
    rc, out, err = await _ssh(name, ["sudo", SYSTEMCTL, "restart", "seafront"], timeout=120.0)
    if rc != 0:
        raise HTTPException(502, f"restart failed: {err or out}".strip())
    return JSONResponse({"ok": True, "action": "restart-service"})


@app.post("/api/scope/{name}/reboot")
async def reboot(name: str) -> JSONResponse:
    # Use /usr/sbin/reboot, NOT `systemctl reboot`: the box fleet sudoers grants
    # /usr/sbin/reboot passwordless, while `systemctl reboot` matches only the
    # password-required %wheel rule — so over our tty-less SSH `sudo systemctl reboot`
    # silently fails and the box never reboots (a staged bootc OS is never applied).
    # A real reboot tears down the SSH channel (nonzero rc, connection-drop message) =
    # success; a sudo/permission refusal returns fast with a recognizable error — surface
    # that as a failure instead of a false "ok".
    rc, out, err = await _ssh(name, ["sudo", "-n", "/usr/sbin/reboot"], timeout=10.0)
    msg = (err or out).strip()
    if rc != 0 and any(s in msg for s in
                       ("sudo:", "a password is required", "a terminal is required", "not allowed")):
        raise HTTPException(502, f"reboot not permitted: {msg}")
    # The box is going away: drop its pre-reboot image read (else a failed read during the
    # reboot serves it as "staged — reboot to apply") and close the shared SSH connection so
    # the first read after boot starts fresh instead of reusing a dead one.
    _LAST_BOX_IMAGES.pop(name, None)
    await _close_ssh_master(name)
    return JSONResponse({"ok": True, "action": "reboot", "note": msg})


@app.get("/api/scope/{name}/config", response_class=PlainTextResponse)
async def get_config(name: str) -> str:
    rc, out, err = await _ssh(name, ["cat", SEAFRONT_CONFIG_PATH])
    if rc != 0:
        raise HTTPException(502, f"could not read config: {err or out}".strip())
    return out


@app.post("/api/scope/{name}/config")
async def set_config(name: str, request: Request, restart: bool = False) -> JSONResponse:
    """Replace a box's ~/seafront/config.json, optionally restarting seafront (?restart=1).

    The body is validated as JSON and written atomically (config.json is root-owned by the
    container, but its dir is pharmbio-owned, so temp-file + mv works). The box then reports
    the sha256 of the file it now has, which must match what we sent, so a save that did not
    land is an error, never a silent success. The gateway's central store
    (configs/<name>/config.json) is updated too, so push-config / reflash keep the change.

    There is no auto-revert. A config that seafront rejects (placeholder device IDs, hardware
    not attached yet) is a normal state for a box being set up. With ?restart=1 we wait for
    seafront to serve; if it doesn't, the journal from THIS restart is returned so the reason
    is visible."""
    m = scope_or_404(name)
    raw = (await request.body()).decode("utf-8", "replace")
    try:
        cfg = json.loads(raw)
    except Exception as e:
        raise HTTPException(400, f"not valid JSON: {e}")
    if not isinstance(cfg, dict) or not isinstance(cfg.get("microscopes"), list):
        raise HTTPException(400, "config must be a JSON object with a 'microscopes' array")
    text = json.dumps(cfg, indent=4) + "\n"
    want = hashlib.sha256(text.encode()).hexdigest()

    # One round-trip: write, then report what is on disk now plus the profile override and
    # the box clock (the latter bounds the journal query if the restart fails).
    push = ("cat > ~/seafront/config.json.new"
            " && mv -f ~/seafront/config.json.new ~/seafront/config.json"
            " && echo \"SHA=$(sha256sum < ~/seafront/config.json | cut -d' ' -f1)\""
            " && echo \"ACTIVE=$(cat ~/seafront/active_microscope 2>/dev/null)\""
            " && echo \"NOW=$(date +%s)\"")
    rc, out, err = await _ssh(name, [push], input=text, timeout=60.0)
    kv = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    if rc != 0 or "SHA" not in kv:
        raise HTTPException(502, f"could not write config: {(err or out).strip()}")
    if kv["SHA"] != want:
        raise HTTPException(502, f"config on {name} does not match what was sent "
                                 f"(box sha256 {kv['SHA'][:12]}, sent {want[:12]})")

    central = ROOT / "configs" / name / "config.json"
    central.parent.mkdir(parents=True, exist_ok=True)
    central.write_text(text)

    # Which profile seafront will run: the per-box override, else the image default
    # (SEAFRONT_MICROSCOPE=mocroscope). Editing any other profile has no effect on startup.
    active = kv.get("ACTIVE", "").strip() or "mocroscope"
    profiles = [p.get("system.microscope_name", "") for p in cfg["microscopes"] if isinstance(p, dict)]
    warning = None
    if active not in profiles:
        warning = (f"seafront runs profile {active!r}, which is not in this config "
                   f"(has: {', '.join(profiles) or 'none'}); it will refuse to start")
    result = {"ok": True, "synced": True, "sha256": want, "active_profile": active,
              "warning": warning, "restarted": False}
    if not restart:
        return JSONResponse(result)

    async def journal_since_save() -> str:
        since = kv.get("NOW", "").strip()
        jargs = ["journalctl", "-u", "seafront", "--no-pager", "-n", "40"]
        if since.isdigit():
            jargs += ["--since", f"@{since}"]
        _rc, log, _err = await _ssh(name, jargs, timeout=20.0)
        return log.strip()

    rc, out, err = await _ssh(name, ["sudo -n /usr/bin/systemctl restart seafront"], timeout=150.0)
    if rc != 0:
        raise HTTPException(502, f"config saved (sha256 verified), but restart failed: "
                                 f"{(err or out).strip()}\n\n{await journal_since_save()}")
    result["restarted"] = True
    # The restart covers stop (hardware close), the image pull in ExecStartPre, container
    # start and python imports; give it room before calling it failed.
    if await _wait_service_up(name, timeout=90.0):
        result["serving"] = True
        return JSONResponse(result)
    raise HTTPException(422, f"config saved (sha256 verified) and seafront restarted with "
                             f"profile {active!r}, but it is not serving on port "
                             f"{m['seafront_port']} after 90s. Journal since the save:\n\n"
                             f"{await journal_since_save()}")


@app.get("/api/scope/{name}/logs", response_class=PlainTextResponse)
async def get_logs(name: str, lines: int = 200) -> str:
    lines = max(1, min(lines, 2000))
    rc, out, err = await _ssh(
        name, ["journalctl", "-u", "seafront", "-n", str(lines), "--no-pager"]
    )
    if rc != 0:
        raise HTTPException(502, f"could not read logs: {err or out}".strip())
    return out


@app.get("/api/scope/{name}/diagnostics")
async def get_diagnostics(name: str) -> JSONResponse:
    """Everything the Diagnostics dialog shows except the log (fetched separately)."""
    rc, out, err = await _ssh(name, ["python3", "-", "full"], timeout=40.0, input=_DIAG_PY)
    returned_at = time.time()
    d = _parse_diag(out)
    if d is None:
        raise HTTPException(502, f"diagnostics failed (exit {rc}): {(err or out).strip()[-2000:]}")
    clock = _parse_clock(d.get("chrony") or "", str(d.get("now", "")), returned_at)
    return JSONResponse({**d, "clock": clock, "issues": _diag_issues(d, clock)})


# --- long-running image updates (streamed as per-box background jobs) ----------
# bootc upgrade / podman pull download image data and can take minutes, so they
# run detached and stream output; the UI polls /api/scope/{name}/job. One job per
# box at a time (different boxes run concurrently).
JOBS: dict[str, dict] = {}


async def _run_job(key: str, cmd: list[str]) -> None:
    job = JOBS[key]
    buf: list[str] = []
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        job["proc"] = proc

        async def pump() -> None:
            assert proc.stdout is not None
            while True:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    break
                buf.append(chunk.decode(errors="replace"))
                job["log"] = "".join(buf)
            await proc.wait()

        await asyncio.wait_for(pump(), timeout=1800.0)   # image builds can be minutes
        job["rc"] = proc.returncode
    except asyncio.TimeoutError:
        buf.append("\n[timed out after 1800s]\n")
        job["rc"] = 124
        if job.get("proc"):
            try:
                job["proc"].kill()
            except Exception:
                pass
    except Exception as e:
        buf.append(f"\n[error: {e}]\n")
        job["rc"] = 1
    finally:
        job["log"] = "".join(buf)
        job["running"] = False
        job["proc"] = None


def _start_job(key: str, kind: str, cmd: list[str]) -> JSONResponse:
    """Launch a streamed background job under `key` (a scope name, or GW_JOB for the
    gateway). `cmd` is the full argv to exec — an SSH invocation for a box, or a
    local command for the gateway. One job per key at a time."""
    j = JOBS.get(key)
    if j and j.get("running"):
        raise HTTPException(409, f"a job is already running ({j.get('kind')})")
    JOBS[key] = {"kind": kind, "running": True, "rc": None, "log": "", "proc": None}
    asyncio.create_task(_run_job(key, cmd))
    return JSONResponse({"ok": True, "kind": kind})


def _job_status(key: str) -> dict:
    j = JOBS.get(key) or {}
    return {"running": j.get("running", False), "rc": j.get("rc"),
            "kind": j.get("kind"), "log": j.get("log", "")}


@app.post("/api/scope/{name}/update-os")
async def update_os(name: str) -> JSONResponse:
    """Download + stage the latest OS image (`bootc upgrade`). Does NOT reboot —
    the box keeps running the current OS until it is rebooted (auto-rollback on a
    bad boot), so this is safe to run and reboot the box later."""
    scope_or_404(name)
    return _start_job(name, "update-os",
                      _ssh_base(name) + ["sudo", "-n", "/usr/bin/bootc", "upgrade"])


@app.post("/api/scope/{name}/update-seafront")
async def update_seafront(name: str) -> JSONResponse:
    """Pull the latest seafront app image (`podman pull`). Does NOT restart the
    container — a running acquisition keeps its current image until you restart
    the service, which then comes up on the freshly-pulled image."""
    scope_or_404(name)
    return _start_job(name, "update-seafront",
                      _ssh_base(name) + ["sudo", "-n", "/usr/bin/podman", "pull", APP_IMAGE])


@app.get("/api/scope/{name}/job")
async def scope_job(name: str) -> JSONResponse:
    scope_or_404(name)
    return JSONResponse(_job_status(name))


# --- gateway self-management (this host: git state, image rebuild, reboot) -----
# The dashboard runs on the gateway as pharmbio, so these are LOCAL commands. git
# pull + rootless-podman rebuilds need no privilege (pharmbio owns the checkout and
# its own podman storage); only the host reboot does (NOPASSWD rule from
# gateway-setup.sh). NB: an image rebuild does not restart THIS dashboard, so a
# pulled dashboard-code change only takes effect on the next service restart.
GW_HOST = os.uname().nodename
GW_JOB = "__gateway__"   # reserved JOBS key; cannot collide with a scope name


async def _git(*args: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(ROOT), *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return out.decode(errors="replace").strip()


async def _run_local(argv: list[str], timeout: float = 30.0) -> tuple[int, str, str]:
    """Run a local gateway command (not SSH): apply-config, wifi-mode, nmcli. The
    privileged scripts self-elevate via the fleet sudoers rule, so no sudo here."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        raise HTTPException(504, f"command timed out: {' '.join(argv)}")
    except FileNotFoundError:
        raise HTTPException(500, f"command not found: {argv[0]}")
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


async def _internet_ok() -> bool:
    """True when the gateway has real upstream connectivity (needed to rebuild images).
    Wi-Fi in AP/hotspot mode has none."""
    try:
        _rc, out, _ = await _run_local(
            ["nmcli", "-t", "-f", "CONNECTIVITY", "general", "status"], timeout=6.0)
    except HTTPException:
        return False
    return out.strip() == "full"


@app.get("/api/gateway")
async def gateway_info() -> JSONResponse:
    head, subject, dirty, behind, origin, incoming = await asyncio.gather(
        _git("rev-parse", "--short", "HEAD"),
        _git("log", "-1", "--pretty=%s"),
        _git("status", "--porcelain"),
        _git("rev-list", "--count", "HEAD..@{u}"),   # as of last fetch (no network here)
        _git("remote", "get-url", "origin"),
        _git("log", "-n", "10", "--format=%h %s", "HEAD..@{u}"),
    )
    try:
        fetched_at = (ROOT / ".git" / "FETCH_HEAD").stat().st_mtime
    except OSError:
        fetched_at = None
    return JSONResponse({
        "time": await _gateway_time(),
        "host": GW_HOST,
        "git": {
            "head": head,
            "subject": subject,
            "dirty": bool(dirty.strip()),
            "behind": int(behind) if behind.isdigit() else None,
            "incoming": [l for l in incoming.splitlines() if l.strip()] if behind.isdigit() else [],
            "fetched_at": fetched_at,
        },
        "seafront": {"pinned": _pinned_seafront_ref(), "remote": SEAFRONT_REMOTE or None},
        "repos": {
            "gateway": _browse_url(origin) if origin.startswith(("http", "git@")) else None,
            "seafront": SEAFRONT_REPO_URL,
        },
        "rebuild": _job_status(GW_JOB),
    })


async def _gateway_time() -> dict:
    """Gateway wall clock + chrony's view of it. Offline, chrony serves this clock to the
    whole fleet (`local stratum 10`), so this is the time every box ends up with."""
    try:
        # `| cat`: inside the uvicorn service chronyc prints nothing (exit 0) when its stdout
        # is the event loop's pipe; through a shell pipe it works (same as on the boxes)
        _rc, tracking, _ = await _run_local(["sh", "-c", "chronyc -n tracking | cat"], timeout=5.0)
    except HTTPException:
        tracking = ""
    try:
        _rc, tz, _ = await _run_local(["timedatectl", "show", "-p", "Timezone", "--value"],
                                      timeout=5.0)
    except HTTPException:
        tz = ""
    c = _parse_clock(tracking, "", 0.0)
    return {"epoch": time.time(), "tz": tz.strip() or None,
            "server": c["server"], "synced": c["synced"]}


class TimeIn(BaseModel):
    epoch: float   # unix seconds, from the browser's clock or a typed-in time


@app.post("/api/gateway/time")
async def gateway_set_time(t: TimeIn) -> JSONResponse:
    """Set the gateway clock (scripts/set-time.sh, self-elevating via the gateway sudoers
    rule), then step every box to it. Without the step, chrony on the boxes would slew
    toward the new time at <=83 ms/s (makestep only applies at chronyd start), so a
    10-minute correction would take ~2 h."""
    rc, out, err = await _run_local(
        [str(ROOT / "scripts" / "set-time.sh"), f"{t.epoch:.3f}"], timeout=15.0)
    if rc:
        raise HTTPException(502, f"set-time failed: {(err or out).strip()}")

    async def step(name: str) -> tuple[str, str]:
        # makestep only steps when the box has a measurement of the gateway's new time;
        # `burst` gets one immediately (no sudo needed for it), then step.
        try:
            rc, o, e = await _ssh(
                name, ["chronyc burst 4/4 >/dev/null 2>&1; sleep 6; "
                       "sudo -n /usr/bin/chronyc makestep"], timeout=20.0)
        except HTTPException as ex:
            return name, f"unreachable ({ex.detail})"
        if rc:
            return name, "not allowed yet (box OS predates the chronyc sudoers rule; it will slew instead)"
        return name, "stepped"

    boxes = dict(await asyncio.gather(*[step(m["name"]) for m in scopes()]))
    return JSONResponse({"ok": True, "gateway": out.strip(), "boxes": boxes})


_NO_NET = ("gateway has no internet (Wi-Fi in hotspot/AP mode?). "
           "Switch Wi-Fi to client mode first.")


@app.post("/api/gateway/build")
async def gateway_build(target: str, seafront_ref: str = "") -> JSONResponse:
    """Build ONE image from the current checkout and push it to the registry. Streamed job;
    no git pull (that is /api/gateway/self-update) and no box is touched.

    target=seafront: the app image at `seafront_ref` (commit, branch, tag, or "latest"),
    blank = the commit pinned in build-images.sh. target=os: the OS image."""
    # Both builds pull from the internet (seafront clone, base/dnf for the OS). Fail fast
    # with the fix instead of hanging when the Wi-Fi radio is in AP mode.
    if not await _internet_ok():
        raise HTTPException(409, _NO_NET)
    if target == "seafront":
        ref = seafront_ref.strip()
        env = f"SEAFRONT_REF={shlex.quote(ref)} " if ref else ""
        build = f"{env}bash scripts/build-images.sh --seafront"
    elif target == "os":
        build = "bash scripts/build-images.sh --os"
    else:
        raise HTTPException(400, "target must be 'seafront' or 'os'")
    return _start_job(GW_JOB, f"build {target}", ["bash", "-lc", f"cd {ROOT} && {build}"])


@app.post("/api/gateway/fetch")
async def gateway_fetch() -> JSONResponse:
    """`git fetch` the gateway checkout: shows what a pull would bring, changes nothing."""
    if not await _internet_ok():
        raise HTTPException(409, _NO_NET)
    try:
        out = await asyncio.wait_for(_git("fetch", "--all", "--prune"), timeout=60.0)
    except asyncio.TimeoutError:
        raise HTTPException(504, "git fetch timed out")
    return JSONResponse({"ok": True, "log": out})


def _pinned_seafront_ref() -> str | None:
    """The seafront commit build-images.sh builds by default (its REF= line)."""
    try:
        m = re.search(r'REF="\$\{SEAFRONT_REF:-([0-9a-fA-F]+)\}"',
                      (ROOT / "scripts" / "build-images.sh").read_text())
    except OSError:
        return None
    return m.group(1) if m else None


# Last `git ls-remote` of the seafront repo's default branch: {"sha", "at"}. In memory
# only; the gateway has no seafront checkout (the image build clones it).
SEAFRONT_REMOTE: dict = {}


@app.post("/api/seafront/fetch")
async def seafront_fetch() -> JSONResponse:
    """Look up the seafront repo's current default-branch commit (no clone, no build)."""
    if not await _internet_ok():
        raise HTTPException(409, _NO_NET)
    if not SEAFRONT_REPO_URL:
        raise HTTPException(500, "seafront repo URL not found in images/seafront/Containerfile")
    rc, out, err = await _run_local(["git", "ls-remote", SEAFRONT_REPO_URL + ".git", "HEAD"],
                                    timeout=30.0)
    sha = out.split()[0] if rc == 0 and out.strip() else None
    if not sha:
        raise HTTPException(502, f"git ls-remote failed: {(err or out).strip()[-500:]}")
    SEAFRONT_REMOTE.update(sha=sha, at=time.time())
    return JSONResponse(SEAFRONT_REMOTE)


async def _os_image_vs_checkout(rev: str | None) -> str:
    """Has images/kinoite changed between the commit the registry OS image was built from
    and the checkout? 'same' | 'changed' | 'unknown' (no label, -dirty build, gone commit)."""
    if not rev or not re.fullmatch(r"[0-9a-f]{7,40}", rev):
        return "unknown"
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(ROOT), "diff", "--quiet", rev, "HEAD", "--", "images/kinoite",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
    rc = await proc.wait()
    return {0: "same", 1: "changed"}.get(rc, "unknown")


@app.get("/api/gateway/job")
async def gateway_job() -> JSONResponse:
    return JSONResponse(_job_status(GW_JOB))


@app.post("/api/gateway/self-update")
async def gateway_self_update() -> JSONResponse:
    """Update the gateway's own software: git pull the checkout + apply it (regenerate the
    dashboard unit, reload Caddy/firewall, restart the dashboard). NOT an image rebuild.

    Runs detached in a transient unit (self-update.sh) because applying restarts THIS
    dashboard, so this returns immediately and the client should poll until the dashboard
    is back, then reload. Needs internet (git pull)."""
    if not await _internet_ok():
        raise HTTPException(409, "gateway has no internet (Wi-Fi in hotspot/AP mode?). "
                                 "Switch Wi-Fi to client mode, then update.")
    proc = await asyncio.create_subprocess_exec(
        "sudo", "-n", f"{ROOT}/scripts/self-update.sh",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=15.0)
    except asyncio.TimeoutError:
        return JSONResponse({"ok": True, "note": "update started; dashboard restarting"})
    if proc.returncode:
        raise HTTPException(500, f"self-update failed to start: {out.decode()[-500:]}")
    return JSONResponse({"ok": True, "note": "update started; dashboard restarting"})


@app.post("/api/gateway/reboot")
async def gateway_reboot() -> JSONResponse:
    # Reboots THIS host; the reply may not flush as the system goes down, so a
    # dropped connection on the client side is expected/success.
    try:
        proc = await asyncio.create_subprocess_exec(
            "sudo", "-n", "/usr/sbin/reboot",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
    except asyncio.TimeoutError:
        return JSONResponse({"ok": True, "note": "reboot issued (connection dropping)"})
    if proc.returncode:
        raise HTTPException(502, f"reboot failed: {out.decode(errors='replace').strip()}")
    return JSONResponse({"ok": True})


# --- fleet inventory writes (add / remove / renumber) --------------------------
# The dashboard is unauthenticated and admin-level by design: anyone who can reach it
# can change the fleet. These edit config/microscopes.json (pharmbio owns the checkout),
# then run apply-config.sh --no-dashboard, which self-elevates via the fleet sudoers rule
# to reload Caddy + the firewall WITHOUT restarting this dashboard (it reads live).
class ScopeIn(BaseModel):
    name: str
    host: str
    proxy_port: int | None = None
    seafront_port: int = 8000
    type: str = "squid"


class IpIn(BaseModel):
    ip: str


class WifiIn(BaseModel):
    mode: str                     # "ap" | "client"
    ssid: str | None = None
    password: str | None = None


async def _apply_config() -> None:
    rc, out, err = await _run_local(
        [str(SCRIPTS / "apply-config.sh"), "--no-dashboard"], timeout=120.0)
    if rc != 0:
        raise HTTPException(500, f"apply-config failed: {(err or out).strip()}")


@app.post("/api/fleet/scope")
async def fleet_add_scope(s: ScopeIn) -> JSONResponse:
    cfg = fleet_config.load()
    try:
        entry = fleet_config.add_scope(cfg, s.name, s.host, s.proxy_port, s.seafront_port, s.type)
        fleet_config.save(cfg)
    except ValueError as e:
        raise HTTPException(400, str(e))
    await _apply_config()
    return JSONResponse({"ok": True, "scope": entry})


@app.delete("/api/fleet/scope/{name}")
async def fleet_remove_scope(name: str) -> JSONResponse:
    cfg = fleet_config.load()
    try:
        fleet_config.remove_scope(cfg, name)
        fleet_config.save(cfg)
    except ValueError as e:
        raise HTTPException(404, str(e))
    await _apply_config()
    return JSONResponse({"ok": True, "removed": name})


@app.post("/api/scope/{name}/set-ip")
async def scope_set_ip(name: str, body: IpIn) -> JSONResponse:
    """Renumber a box's backbone IP over SSH (set-box-ip.sh: add-new / verify / drop-old,
    then update the inventory + reload the proxy). Streamed as a per-box job."""
    scope_or_404(name)
    try:
        ipaddress.ip_address(body.ip.split("/")[0])
    except ValueError:
        raise HTTPException(400, f"invalid ip: {body.ip}")
    return _start_job(name, "set-ip", [str(SCRIPTS / "set-box-ip.sh"), name, body.ip])


# --- Wi-Fi control -------------------------------------------------------------
def _parse_kv(text: str) -> dict:
    d: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            d[k.strip()] = v.strip()
    return d


@app.get("/api/wifi")
async def wifi_status() -> JSONResponse:
    _rc, out, _err = await _run_local([str(SCRIPTS / "wifi-mode.sh"), "status"], timeout=15.0)
    return JSONResponse(_parse_kv(out))


@app.post("/api/wifi/mode")
async def wifi_set_mode(w: WifiIn) -> JSONResponse:
    if w.mode not in ("ap", "client"):
        raise HTTPException(400, "mode must be 'ap' or 'client'")
    argv = [str(SCRIPTS / "wifi-mode.sh"), w.mode]
    if w.mode == "client" and w.ssid:
        argv.append(w.ssid)
        if w.password:
            argv.append(w.password)
    # wifi-mode.sh self-elevates and detaches the actual switch (systemd-run), so this
    # returns promptly even though the radio flip may drop the caller's own connection.
    rc, out, err = await _run_local(argv, timeout=30.0)
    if rc != 0:
        raise HTTPException(500, f"wifi switch failed: {(err or out).strip()}")
    return JSONResponse({"ok": True, "scheduled": w.mode, "note": out.strip()})


# --- microscope profile switch (real vs mock, etc.) ----------------------------
class MicroscopeIn(BaseModel):
    profile: str


@app.post("/api/scope/{name}/microscope")
async def set_microscope(name: str, body: MicroscopeIn) -> JSONResponse:
    """Switch which profile the box's seafront runs: validate the name against the box's
    config.json, write the per-box override file (~/seafront/active_microscope), and
    restart seafront. Needs a box on an app image built after override support landed
    (older images ignore the file)."""
    scope_or_404(name)
    prof = body.profile.strip()
    _rc, out, _err = await _ssh(name, [_LIST_PROFILES_CMD])
    avail = [p.strip() for p in out.splitlines() if p.strip()]
    if prof not in avail:
        raise HTTPException(400, f"profile {prof!r} not in {name} config (have: {', '.join(avail) or 'none'})")
    cmd = (f"printf '%s\\n' {shlex.quote(prof)} > ~/seafront/active_microscope"
           " && sudo -n /usr/bin/systemctl restart seafront")
    rc, out, err = await _ssh(name, [cmd], timeout=120.0)
    if rc != 0:
        raise HTTPException(502, f"switch failed: {(err or out).strip()}")
    return JSONResponse({"ok": True, "profile": prof})


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return HTML


HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Microscope Gateway</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 16px/1.5 system-ui, sans-serif;
         background: #0e1116; color: #e6edf3; }
  header { padding: 28px 24px 8px; }
  h1 { margin: 0; font-size: 1.4rem; letter-spacing: .02em; }
  .sub { color: #8b949e; font-size: .9rem; margin-top: 4px; }
  .gateway { margin-top: 14px; padding: 14px 16px; background: #161b22; border: 1px solid #30363d;
             border-radius: 12px; display: flex; flex-direction: column; gap: 12px; }
  .gwrow { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .gwsecs { display: grid; gap: 12px; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); }
  .gwsec { background: #0e1116; border: 1px solid #21262d; border-radius: 10px; padding: 10px 12px;
           display: flex; flex-direction: column; gap: 6px; }
  .gwsec h2 { margin: 0 0 2px; font-size: .7rem; font-weight: 700; letter-spacing: .08em;
              text-transform: uppercase; color: #8b949e; }
  .gwsec .gwbtns { margin-top: auto; flex-wrap: wrap; }
  .gwname { font-size: 1rem; }
  .flag { font-size: .7rem; padding: 2px 9px; border-radius: 999px; letter-spacing: .05em;
          text-transform: uppercase; }
  .flag.idle { background: #21262d; color: #8b949e; border: 1px solid #30363d; }
  .flag.rebuilding { background: #3a2d09; color: #f0c674; border: 1px solid #bb8009; }
  .gwbtns { display: flex; align-items: center; gap: 8px; margin-top: 4px; }
  .gwbtns button { padding: 7px 12px; }
  .gwbtns input { font: inherit; font-size: .85rem; padding: 7px 10px; border-radius: 8px;
                  border: 1px solid #30363d; background: #0e1116; color: #e6edf3; }
  .hint { position: relative; display: inline-flex; align-items: center; justify-content: center;
          width: 20px; height: 20px; flex: none; border-radius: 50%; cursor: help;
          background: #21262d; border: 1px solid #30363d; color: #8b949e;
          font-size: .75rem; font-weight: 700; }
  .hint:hover, .hint:focus { color: #e6edf3; border-color: #58a6ff; outline: none; }
  .hint-pop { position: absolute; bottom: calc(100% + 8px); left: 50%; transform: translateX(-50%);
              width: 260px; padding: 10px 12px; z-index: 20; text-align: left;
              background: #161b22; border: 1px solid #30363d; border-radius: 8px;
              color: #c9d1d9; font-size: .8rem; font-weight: 400; line-height: 1.45;
              box-shadow: 0 6px 20px #00000066;
              opacity: 0; visibility: hidden; transition: opacity .12s; }
  .hint:hover .hint-pop, .hint:focus .hint-pop { opacity: 1; visibility: visible; }
  .hint-pop code { font-family: ui-monospace, monospace; background: #0e1116;
                   padding: 0 4px; border-radius: 4px; color: #e6edf3; }
  .repolink { color: #58a6ff; text-decoration: none; }
  .repolink:hover { text-decoration: underline; }
  .ok { color: #3fb950; }
  .stale { color: #d29922; }
  .staged { color: #58a6ff; }
  .muted { color: #6e7681; }
  button.hot { border-color: #bb8009; color: #f0c674; }
  .toolbar { padding: 0 24px; }
  .grid { display: grid; gap: 16px; padding: 16px 24px 24px;
          grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); align-items: start; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 12px;
          padding: 18px; display: flex; flex-direction: column; gap: 10px; }
  .cardhead { display: flex; align-items: center; gap: 8px; cursor: pointer; user-select: none; }
  .chev { color: #8b949e; margin-left: auto; transition: transform .12s; }
  .card.collapsed .chev { transform: rotate(90deg); }
  .card.collapsed .cardbody { display: none; }
  .cardbody { display: flex; flex-direction: column; gap: 10px; }
  .badge { font-size: .7rem; padding: 1px 8px; border-radius: 999px; border: 1px solid #bb8009;
           color: #f0c674; }
  .badge.job { border-color: #1f6feb; color: #58a6ff; }
  .name { font-size: 1.1rem; font-weight: 600; }
  .signals { display: flex; flex-wrap: wrap; gap: 4px 16px; font-size: .85rem; }
  .sig { display: flex; align-items: center; gap: 8px; }
  .dot { width: 10px; height: 10px; border-radius: 50%; background: #6e7681; flex: none; }
  .dot.up { background: #3fb950; box-shadow: 0 0 8px #3fb95088; }
  .dot.down { background: #f85149; }
  .meta { color: #8b949e; font-size: .8rem; font-family: ui-monospace, monospace; }
  .ver { font-size: .8rem; font-family: ui-monospace, monospace; color: #c9d1d9; }
  .btns { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; margin-top: auto; }
  button, a.open { font: inherit; font-size: .85rem; font-weight: 600; cursor: pointer;
           border: 1px solid #30363d; border-radius: 8px; padding: 8px; text-align: center;
           background: #21262d; color: #e6edf3; text-decoration: none; }
  a.open { background: #238636; border-color: #238636; color: #fff; }
  a.open.down { background: #30363d; border-color: #30363d; color: #8b949e; pointer-events: none; }
  button:hover { border-color: #6e7681; }
  button.danger:hover { border-color: #f85149; color: #f85149; }
  button:disabled { opacity: .5; cursor: default; }
  .jobrun { color: #58a6ff; }
  .linkbtn { background: none; border: none; padding: 0 2px; color: #58a6ff; font: inherit;
             font-size: .8rem; text-decoration: underline; cursor: pointer; }
  footer { color: #6e7681; font-size: .8rem; padding: 0 24px 24px; }
  #modal { position: fixed; inset: 0; background: #000a; display: none; padding: 32px;
           align-items: stretch; }
  #modal.show { display: flex; }
  #modalbox { background: #0d1117; border: 1px solid #30363d; border-radius: 12px;
              width: 100%; max-width: 1000px; margin: auto; max-height: 100%;
              display: flex; flex-direction: column; }
  #modalhead { display: flex; justify-content: space-between; align-items: center;
               padding: 14px 18px; border-bottom: 1px solid #30363d; }
  #modalbody { margin: 0; padding: 16px 18px; overflow: auto; white-space: pre-wrap;
               font: 13px/1.45 ui-monospace, monospace; color: #c9d1d9; }
  #modaltabs { display: none; gap: 6px; padding: 10px 18px 0; border-bottom: 1px solid #30363d; }
  #modaltabs button { border-radius: 8px 8px 0 0; border-bottom: none; padding: 6px 14px; }
  #modaltabs button.on { background: #0d1117; border-color: #6e7681; }
  #modaltabs .spacer { flex: 1; }
  #modalhtml { display: none; padding: 16px 18px; overflow: auto; font-size: .85rem; }
  #modalhtml h3 { font-size: .9rem; margin: 18px 0 8px; color: #e6edf3; }
  #modalhtml h3:first-child { margin-top: 0; }
  #modalhtml pre { white-space: pre-wrap; font: 12px/1.4 ui-monospace, monospace; color: #c9d1d9;
                   background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 10px; }
  .dtable { border-collapse: collapse; width: 100%; font-family: ui-monospace, monospace; font-size: .8rem; }
  .dtable th { text-align: left; color: #8b949e; font-weight: 600; }
  .dtable th, .dtable td { padding: 5px 8px; border-bottom: 1px solid #21262d; vertical-align: top; }
  .diskbar { height: 8px; background: #21262d; border-radius: 4px; overflow: hidden; max-width: 320px; margin-top: 4px; }
  .diskbar div { height: 100%; background: #3fb950; }
  .diskbar div.full { background: #d29922; }
  #toast { position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%);
           background: #161b22; border: 1px solid #30363d; border-radius: 8px;
           padding: 10px 16px; font-size: .85rem; display: none; }
  #addmodal, #micmodal { position: fixed; inset: 0; background: #000a; display: none;
              z-index: 10; padding: 32px; align-items: center; justify-content: center; }
  #addmodal.show, #micmodal.show { display: flex; }
  #addbox, #micbox { background: #0d1117; border: 1px solid #30363d; border-radius: 12px;
            width: 100%; max-width: 420px; }
  #addhead, #michead { display: flex; justify-content: space-between; align-items: center;
             padding: 14px 18px; border-bottom: 1px solid #30363d; }
  .addform { padding: 16px 18px; display: flex; flex-direction: column; gap: 12px; }
  .addform label { display: flex; flex-direction: column; gap: 4px;
                   font-size: .78rem; color: #8b949e; }
  .addform input, .addform select { font: inherit; font-size: .9rem; padding: 8px 10px;
                   border-radius: 8px; border: 1px solid #30363d; background: #0e1116; color: #e6edf3; }
  .addform input:focus, .addform select:focus { outline: none; border-color: #58a6ff; }
  .adderr { color: #f85149; font-size: .8rem; min-height: 1.1em; }
  #cfgerr { white-space: pre-wrap; max-height: 40vh; overflow: auto; font-family: monospace; }
  .addbtns { display: flex; justify-content: flex-end; gap: 8px; margin-top: 2px; }
  button.primary { background: #238636; border-color: #238636; color: #fff; }
  #cfgmodal { position: fixed; inset: 0; background: #000a; display: none; z-index: 10;
              padding: 32px; align-items: center; justify-content: center; }
  #cfgmodal.show { display: flex; }
  #cfgbox { background: #0d1117; border: 1px solid #30363d; border-radius: 12px;
            width: 100%; max-width: 820px; display: flex; flex-direction: column; max-height: 100%; }
  #cfghead { display: flex; justify-content: space-between; align-items: center;
             padding: 14px 18px; border-bottom: 1px solid #30363d; }
  .cfgform { padding: 16px 18px; display: flex; flex-direction: column; gap: 12px; min-height: 0; }
  #cfgtext { width: 100%; height: 55vh; resize: vertical; box-sizing: border-box;
             font: 13px/1.45 ui-monospace, monospace; padding: 10px; border-radius: 8px;
             border: 1px solid #30363d; background: #0e1116; color: #e6edf3; }
  #cfgtext:focus { outline: none; border-color: #58a6ff; }
</style>
</head>
<body>
<header>
  <h1>🔬 Microscope Gateway</h1>
  <div class="sub">Fleet status &amp; image updates. Auto-refreshing every 1s.</div>
  <div class="gateway">
    <div class="gwrow">
      <span class="gwname">🖥️ <b id="gwhost">gateway</b></span>
      <span class="flag idle" id="gwstate">idle</span>
      <span class="ver" id="gwgit">git …</span>
    </div>
    <div class="gwsecs">
      <section class="gwsec">
        <h2>Gateway software</h2>
        <div class="ver" id="gwsync">…</div>
        <div class="ver muted" id="gwincoming" style="white-space:pre-wrap"></div>
        <div class="ver">repos: <a id="repogw" class="repolink" target="_blank" rel="noopener">seafront-gateway</a> · <a id="reposf" class="repolink" target="_blank" rel="noopener">seafront</a></div>
        <div class="gwbtns">
          <button class="needsnet" onclick="gatewayFetch()">Fetch</button>
          <button class="needsnet" id="gwselfupdate" onclick="gatewaySelfUpdate()">Pull + apply</button>
          <span class="hint" tabindex="0" aria-label="update gateway help">?<span class="hint-pop"><b>Fetch</b> checks GitHub for new gateway commits and lists them. Nothing changes.<br><br><b>Pull + apply</b> pulls them and applies the gateway's own software (dashboard, Caddy, scripts, firewall). Restarts the dashboard; the page reloads when it is back. Does <b>not</b> build images.</span></span>
          <button class="danger" onclick="gatewayReboot()">Reboot gateway</button>
        </div>
      </section>
      <section class="gwsec">
        <h2>seafront image</h2>
        <div class="ver">registry: <b id="regapp">…</b></div>
        <div class="ver" id="sfsrc">pinned …</div>
        <div class="gwbtns">
          <button class="needsnet" onclick="seafrontFetch()">Fetch</button>
          <input id="gwref" type="text" placeholder="ref (blank = pinned)" style="flex:1;min-width:9em">
          <span class="hint" tabindex="0" aria-label="seafront ref help">?<span class="hint-pop"><b>Fetch</b> looks up the newest commit on seafront's default branch. Nothing is built.<br><br><b>Build</b> builds the seafront image and pushes it to the registry. The ref is the seafront commit to build: <b>blank</b> = the commit pinned in <code>build-images.sh</code>, or a SHA, a branch (<code>main</code>), a tag, or <code>latest</code>.<br><br>No box changes until you click <b>Update seafront</b> on it.</span></span>
        </div>
        <div class="gwbtns"><button class="needsnet buildbtn" onclick="gatewayBuild('seafront')">Build seafront image</button></div>
      </section>
      <section class="gwsec">
        <h2>OS image</h2>
        <div class="ver">registry: <b id="regos">…</b></div>
        <div class="ver" id="ossrc">…</div>
        <div class="gwbtns">
          <button class="needsnet buildbtn" onclick="gatewayBuild('os')">Build OS image</button>
          <span class="hint" tabindex="0" aria-label="OS build help">?<span class="hint-pop">Builds the box OS from <code>images/kinoite</code> in the gateway's <b>current checkout</b> (pull first under Gateway software to get new commits) and pushes it to the registry.<br><br>No box changes until you click <b>Update OS</b> on it.</span></span>
        </div>
      </section>
      <section class="gwsec">
        <h2>Clock</h2>
        <div class="ver" id="gwtime">time …</div>
        <div class="gwbtns">
          <input id="gwtimein" type="datetime-local" step="1" style="flex:1;min-width:12em">
          <span class="hint" tabindex="0" aria-label="gateway time help">?<span class="hint-pop">Without internet the gateway runs on its own clock and every box syncs to it, so the whole fleet is only as right as this clock.<br><br><b>Set from this browser</b> copies this device's clock (a phone or laptop usually has the right time). Or type a time (in this browser's time zone) and click <b>Set gateway time</b>.<br><br>The boxes are then stepped to the new time right away.</span></span>
        </div>
        <div class="gwbtns">
          <button onclick="setGatewayTime(false)">Set gateway time</button>
          <button onclick="setGatewayTime(true)">Set from this browser</button>
        </div>
      </section>
      <section class="gwsec">
        <h2>Network</h2>
        <div class="ver" id="gwwifi">wifi …</div>
        <div class="gwbtns">
          <button id="wifiap" onclick="setWifi('ap')">📶 Wi-Fi: Hotspot</button>
          <button id="wificlient" onclick="setWifi('client')">🌐 Wi-Fi: Client (internet)</button>
        </div>
      </section>
    </div>
  </div>
</header>
<div class="toolbar"><button onclick="addScope()">➕ Add microscope</button>
  <button onclick="collapseAll(true)">Collapse all</button>
  <button onclick="collapseAll(false)">Expand all</button></div>
<div class="grid" id="grid"></div>
<footer id="foot">loading…</footer>

<div id="modal"><div id="modalbox">
  <div id="modalhead"><strong id="modaltitle"></strong>
    <button onclick="closeModal()">close</button></div>
  <div id="modaltabs">
    <button data-tab="log" onclick="diagTab('log')">Log</button>
    <button data-tab="hardware" onclick="diagTab('hardware')">Hardware</button>
    <button data-tab="system" onclick="diagTab('system')">System</button>
    <span class="spacer"></span>
    <button onclick="diagReload()">↻ refresh</button>
  </div>
  <pre id="modalbody"></pre>
  <div id="modalhtml"></div>
</div></div>
<div id="toast"></div>

<div id="addmodal" onclick="if(event.target===this)closeAdd()">
  <div id="addbox">
    <div id="addhead"><strong>Add microscope</strong>
      <button onclick="closeAdd()">close</button></div>
    <div class="addform" onkeydown="if(event.key==='Enter')submitAdd(); if(event.key==='Escape')closeAdd();">
      <label>Name<input id="f-name" placeholder="e.g. squid5" autocomplete="off"></label>
      <label>Backbone IP<input id="f-host" placeholder="e.g. 10.10.0.15 (inside backbone subnet)" autocomplete="off"></label>
      <label>Proxy port<input id="f-pp" placeholder="blank = auto-assign next free" autocomplete="off"></label>
      <label>Type<input id="f-type" value="squid" autocomplete="off"></label>
      <label>seafront port<input id="f-sp" value="8000" autocomplete="off"></label>
      <div class="adderr" id="adderr"></div>
      <div class="addbtns">
        <button onclick="closeAdd()">Cancel</button>
        <button id="addsubmit" class="primary" onclick="submitAdd()">Add</button>
      </div>
    </div>
  </div>
</div>

<div id="micmodal" onclick="if(event.target===this)closeMic()">
  <div id="micbox">
    <div id="michead"><strong id="mictitle">Microscope profile</strong>
      <button onclick="closeMic()">close</button></div>
    <div class="addform">
      <label>Profile (from the box's config.json)<select id="micselect"></select></label>
      <div class="adderr" id="micerr"></div>
      <div class="addbtns">
        <button onclick="closeMic()">Cancel</button>
        <button id="micsubmit" class="primary" onclick="applyMic()">Apply + restart</button>
      </div>
    </div>
  </div>
</div>

<div id="cfgmodal" onclick="if(event.target===this)closeCfg()">
  <div id="cfgbox">
    <div id="cfghead"><strong id="cfgtitle">config</strong>
      <button onclick="closeCfg()">close</button></div>
    <div class="cfgform">
      <textarea id="cfgtext" spellcheck="false" autocomplete="off"></textarea>
      <div class="adderr" id="cfgerr"></div>
      <div class="addbtns">
        <button onclick="closeCfg()">Cancel</button>
        <button id="cfgsave" onclick="saveConfig(false)">Save</button>
        <button id="cfgsaverestart" class="primary" onclick="saveConfig(true)">Save + restart</button>
      </div>
    </div>
  </div>
</div>

<script>
function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.style.display = 'block';
  clearTimeout(t._h); t._h = setTimeout(() => t.style.display = 'none', 4000);
}
function short(d) { return d ? d.replace('sha256:', '').slice(0, 12) : '?'; }
// seafront git commit an image was built from (image label); empty for older unlabeled images.
function rev(r) { return r ? ' @' + r.slice(0, 8) : ''; }
function fdate(iso) { if (!iso) return ''; const t = new Date(iso); return isNaN(t) ? '' : t.toLocaleDateString(); }
// Full build timestamp as YYYYMMDD-HHMM (local time) — distinguishes multiple rebuilds/day.
function ftime(iso) {
  if (!iso) return '';
  const t = new Date(iso); if (isNaN(t)) return '';
  const p = n => String(n).padStart(2, '0');
  return `${t.getFullYear()}${p(t.getMonth()+1)}${p(t.getDate())}-${p(t.getHours())}${p(t.getMinutes())}`;
}

function osLine(o, reg) {
  if (!o || o.available === false) return '<span class="muted">OS: unknown (needs image-based box)</span>';
  const v = o.version || short(o.digest);
  const t = ftime(o.timestamp);
  let badge = '';
  if (o.staged_latest) badge = ' <span class="staged">staged ' + (ftime(reg && reg.created) || o.staged_version || '') + ' — reboot to apply</span>';
  else if (o.up_to_date) badge = ' <span class="ok">up to date</span>';
  else if (reg && reg.digest && o.digest === reg.digest) badge = ' <span class="stale">image current, config not</span>';
  else if (reg && reg.digest) badge = ' <span class="stale">update → ' + (ftime(reg.created) || reg.version || short(reg.digest)) + '</span>';
  let drift = '';
  if (o.etc_drift === null || o.etc_drift === undefined)
    drift = '<br><span class="stale">⚠ /etc drift: cannot check (OS image predates seafront-etc-sync)</span>';
  else if (o.etc_drift.length)
    drift = '<br><span class="stale">⚠ /etc differs from image (reboot or <code>sudo seafront-etc-sync</code>): ' + o.etc_drift.join(', ') + '</span>';
  return 'OS ' + v + (t ? ' · ' + t : '') + ' · ' + short(o.digest) + badge + drift;
}
// Box clock vs gateway clock. Warn past 1 s: SSH latency alone stays well below that.
function clockLine(c) {
  if (!c || c.offset_s == null) return '<span class="muted">unknown</span>';
  const off = c.offset_s, abs = Math.abs(off);
  const txt = abs < 1 ? (off >= 0 ? '+' : '') + (off * 1000).toFixed(0) + ' ms'
                      : (off >= 0 ? '+' : '') + off.toFixed(1) + ' s';
  const src = c.synced ? 'synced to ' + esc(c.server) : '<b>not synced</b>' + (c.server ? ' (' + esc(c.server) + ')' : '');
  const cls = (abs >= 1 || !c.synced) ? 'stale' : 'ok';
  return '<span class="' + cls + '">' + txt + ' vs gateway · ' + src + '</span>';
}
function esc(v) {
  return String(v == null ? '' : v).replace(/[&<>"']/g, ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));
}
function dur(sec) {
  if (sec == null) return '?';
  sec = Math.floor(sec);
  const d = Math.floor(sec / 86400), h = Math.floor(sec % 86400 / 3600), m = Math.floor(sec % 3600 / 60);
  if (d) return d + 'd ' + h + 'h';
  if (h) return h + 'h ' + m + 'm';
  if (m) return m + 'm';
  return sec + 's';
}
function gb(b) { return (b / 1e9).toFixed(1) + ' GB'; }

// Diagnostics dialog: Log (journal) / Hardware (configured USB IDs vs what the host and the
// container see) / System (uptime, service restarts, clock, SELinux, disk). Hardware and
// System share one fetch; Log is fetched on its own.
let DIAG = null;   // {name, data | null, error | null}
async function diagnostics(name) {
  MODAL_JOB = null;
  document.getElementById('modaltitle').textContent = name + ' — diagnostics';
  document.getElementById('modaltabs').style.display = 'flex';
  document.getElementById('modal').classList.add('show');
  DIAG = {name, data: null, error: null, tab: null};
  const issues = (IMAGES.boxes[name] || {}).issues || [];
  diagTab(issues.length ? 'system' : 'log');
  await diagLoad();
}
async function diagLoad() {
  const d = DIAG; if (!d) return;
  d.data = null; d.error = null;
  if (d.tab !== 'log') diagTab(d.tab);
  try {
    const r = await fetch('/api/scope/' + d.name + '/diagnostics');
    const j = await r.json().catch(() => ({}));
    if (r.ok) d.data = j; else d.error = j.detail || ('HTTP ' + r.status);
  } catch (e) { d.error = String(e); }
  if (DIAG === d && d.tab !== 'log') diagTab(d.tab);
}
function diagReload() {
  if (!DIAG) return;
  if (DIAG.tab === 'log') diagTab('log'); else diagLoad();
}
async function diagTab(tab) {
  const d = DIAG; if (!d) return;
  d.tab = tab;
  document.querySelectorAll('#modaltabs button[data-tab]').forEach(b => b.classList.toggle('on', b.dataset.tab === tab));
  const pre = document.getElementById('modalbody'), html = document.getElementById('modalhtml');
  pre.style.display = tab === 'log' ? '' : 'none';
  html.style.display = tab === 'log' ? 'none' : 'block';
  if (tab === 'log') {
    pre.textContent = 'loading…';
    try {
      const r = await fetch('/api/scope/' + d.name + '/logs?lines=500');
      const t = await r.text();
      if (DIAG === d && d.tab === 'log') { pre.textContent = t; pre.scrollTop = pre.scrollHeight; }
    } catch (e) { pre.textContent = 'error: ' + e; }
    return;
  }
  if (d.error) { html.innerHTML = '<span class="stale">' + esc(d.error) + '</span>'; return; }
  if (!d.data) { html.innerHTML = '<span class="muted">loading…</span>'; return; }
  html.innerHTML = tab === 'hardware' ? diagHardware(d.data) : diagSystem(d.data);
}
function issueList(x) {
  if (!x.issues.length) return '<div class="ok">no problems found</div>';
  return x.issues.map(i => '<div class="stale">⚠ ' + esc(i) + '</div>').join('');
}
function diagHardware(x) {
  const ph = id => !id.trim() || id.toUpperCase().includes('PLACEHOLDER');
  const cp = x.container_ports || {};
  let h = '<h3>Configured devices · profile ' + esc(x.profile) + ' (' + esc(x.profile_source) + ')</h3>';
  if (!x.profile_found) h += '<div class="stale">⚠ profile not found in config.json</div>';
  else if (x.mock) h += '<div class="muted">mock profile: no hardware expected</div>';
  if (x.devices.length) {
    h += '<table class="dtable"><tr><th>config key</th><th>id</th><th>driver</th><th>host USB</th><th>container</th></tr>';
    for (const dev of x.devices) {
      const u = x.usb.find(u => u.serial === dev.id);
      let host, cont = '<span class="muted">n/a</span>';
      if (ph(dev.id)) host = '<span class="muted">placeholder</span>';
      else if (u) host = '<span class="ok">✓ ' + esc(u.bus) + ' ' + esc(u.product || '') + (u.tty.length ? ' · ' + esc(u.tty.join(' ')) : '') + '</span>';
      else host = '<span class="stale">✗ not connected</span>';
      if (dev.key === 'microcontroller.id' && !ph(dev.id)) {
        if (cp.rc === 0) cont = cp.out.includes('SER=' + dev.id) ? '<span class="ok">✓ visible</span>' : '<span class="stale">✗ not visible</span>';
        else cont = '<span class="muted">unknown</span>';
      }
      h += '<tr><td>' + esc(dev.key) + '</td><td>' + esc(dev.id) + '</td><td>' + esc(dev.driver || '') + '</td><td>' + host + '</td><td>' + cont + '</td></tr>';
    }
    h += '</table>';
  }
  h += '<h3>USB devices on the host</h3><table class="dtable"><tr><th>bus</th><th>vid:pid</th><th>manufacturer</th><th>product</th><th>serial</th><th>tty</th></tr>';
  for (const u of x.usb)
    h += '<tr><td>' + esc(u.bus) + '</td><td>' + esc(u.vid) + ':' + esc(u.pid) + '</td><td>' + esc(u.manufacturer || '') + '</td><td>' + esc(u.product || '') + '</td><td>' + esc(u.serial || '') + '</td><td>' + esc(u.tty.join(' ')) + '</td></tr>';
  h += '</table><h3>Serial ports inside the seafront container (what seafront sees)</h3>';
  if (cp.rc === 0) h += '<pre>' + esc(cp.out.trim() || '(none)') + '</pre>';
  else h += '<div class="muted">unavailable: ' + esc((cp.out || '').trim()) + ' (needs an OS image with the list_ports sudoers rule, and a running container)</div>';
  return h;
}
function diagSystem(x) {
  const s = x.service || {}, dk = x.disk || {};
  const pct = dk.total ? Math.round(100 * (dk.total - dk.free) / dk.total) : 0;
  const row = (k, v) => '<tr><th>' + k + '</th><td>' + v + '</td></tr>';
  let h = '<h3>Problems</h3>' + issueList(x) + '<h3>Status</h3><table class="dtable">';
  h += row('computer uptime', dur(x.uptime_s));
  h += row('seafront service', '<span class="' + (s.state === 'active' ? 'ok' : 'stale') + '">' + esc(s.state) + ' (' + esc(s.sub) + ')</span>'
        + (s.uptime_s != null ? ' · up ' + dur(s.uptime_s) + ' · since ' + esc(s.since) : ''));
  h += row('automatic restarts', '<span class="' + (s.restarts ? 'stale' : '') + '">' + s.restarts + '</span>'
        + ' <span class="muted">(crash restarts since boot or the last manual restart)</span>');
  h += row('clock', clockLine(x.clock));
  h += row('SELinux', '<span class="' + (x.selinux === 'enforcing' ? 'ok' : 'stale') + '">' + esc(x.selinux) + '</span>'
        + ' · container_use_devices ' + (x.container_use_devices ? '<span class="ok">on</span>' : '<span class="stale">' + (x.container_use_devices === false ? 'off' : 'unknown') + '</span>'));
  h += row('disk ' + esc(dk.path), gb(dk.total - dk.free) + ' used of ' + gb(dk.total) + ' · ' + gb(dk.free) + ' free (' + pct + '%)'
        + '<div class="diskbar"><div class="' + (pct >= 90 ? 'full' : '') + '" style="width:' + pct + '%"></div></div>');
  h += '</table>';
  if (x.chrony) h += '<h3>chronyc tracking</h3><pre>' + esc(x.chrony.trim()) + '</pre>';
  return h;
}
function appLine(a, reg) {
  if (!a || a.present === false) return '<span class="muted">seafront image: not present</span>';
  const t = ftime(a.created);
  let s = 'seafront' + rev(a.revision) + (t ? ' ' + t : '') + ' · ' + short(a.digest);
  if (a.up_to_date) s += ' <span class="ok">up to date</span>';
  else if (reg && reg.digest) s += ' <span class="stale">update → ' + (ftime(reg.created) || short(reg.digest)) + '</span>';
  return s;
}

async function act(name, action, confirmMsg) {
  if (confirmMsg && !confirm(confirmMsg)) return;
  toast(`${name}: ${action}…`);
  try {
    const r = await fetch(`/api/scope/${name}/${action}`, {method: 'POST'});
    const j = await r.json().catch(() => ({}));
    toast(r.ok ? `${name}: ${action} ok` : `${name}: ${action} failed — ${j.detail || r.status}`);
  } catch (e) { toast(`${name}: ${action} failed — ${e}`); }
  setTimeout(() => { refresh(); fetchImages(); }, 1500);
}

// Edit a box's config.json: load it into a textarea, validate JSON on save, POST it. "Save"
// only writes the file (the box confirms by sha256); "Save + restart" also restarts seafront
// and, if it doesn't serve, shows the journal from that restart. No auto-revert.
function cfgButtons(on) {
  document.getElementById('cfgsave').disabled = !on;
  document.getElementById('cfgsaverestart').disabled = !on;
}
async function editConfig(name) {
  const ta = document.getElementById('cfgtext'), err = document.getElementById('cfgerr');
  document.getElementById('cfgtitle').textContent = name + ' — config.json';
  ta.value = 'loading…'; err.textContent = '';
  cfgButtons(false);
  const m = document.getElementById('cfgmodal'); m.dataset.box = name; m.classList.add('show');
  try {
    const r = await fetch(`/api/scope/${name}/config`);
    const t = await r.text();
    if (!r.ok) { ta.value = ''; err.textContent = 'could not load config: ' + t; return; }
    ta.value = t;
    cfgButtons(true);
  } catch (e) { ta.value = ''; err.textContent = 'load failed: ' + e; }
}
function closeCfg() { document.getElementById('cfgmodal').classList.remove('show'); }
async function saveConfig(restart) {
  const name = document.getElementById('cfgmodal').dataset.box;
  const ta = document.getElementById('cfgtext'), err = document.getElementById('cfgerr');
  let parsed;
  try { parsed = JSON.parse(ta.value); }
  catch (e) { err.textContent = 'invalid JSON: ' + e.message; return; }
  if (!parsed || typeof parsed !== 'object' || !Array.isArray(parsed.microscopes)) {
    err.textContent = "config must be a JSON object with a 'microscopes' array"; return;
  }
  if (restart && !confirm(`Save config to ${name} and restart seafront?`)) return;
  cfgButtons(false);
  err.textContent = restart ? 'saving + restarting + waiting for seafront (up to ~2 min)…' : 'saving…';
  try {
    const r = await fetch(`/api/scope/${name}/config?restart=${restart ? 1 : 0}`,
      {method: 'POST', headers: {'Content-Type': 'application/json'}, body: ta.value});
    const j = await r.json().catch(() => ({}));
    if (r.ok) {
      const what = restart ? 'config saved, seafront restarted and serving ✓' : 'config saved (not restarted) ✓';
      if (j.warning) { err.textContent = 'saved, but: ' + j.warning; cfgButtons(true); }
      else closeCfg();
      toast(`${name}: ${what} (profile ${j.active_profile})`);
      setTimeout(() => { refresh(); fetchImages(); }, 1000);
    } else { err.textContent = j.detail || ('failed (HTTP ' + r.status + ')'); cfgButtons(true); }
  } catch (e) { err.textContent = 'request failed: ' + e; cfgButtons(true); }
}

let IMAGES = {registry: {}, boxes: {}};
// Per-box background jobs (update-os / update-seafront / set-ip). They run server-side and
// can take minutes; tracked here so the grid stays fully interactive while they run — no
// blocking modal, just an inline per-card indicator. MODAL_JOB names the box whose live log
// the (optional, dismissible) modal is currently showing.
const BOXJOBS = {};
let MODAL_JOB = null;
function closeModal() {
  MODAL_JOB = null; DIAG = null;
  document.getElementById('modal').classList.remove('show');
  document.getElementById('modaltabs').style.display = 'none';
  document.getElementById('modalhtml').style.display = 'none';
  document.getElementById('modalbody').style.display = '';
}
async function fetchImages() {
  try {
    const d = await (await fetch('/api/images')).json();
    IMAGES.registry = d.registry || {}; IMAGES.boxes = {}; IMAGES.at = Date.now();
    (d.boxes || []).forEach(b => IMAGES.boxes[b.name] = b);
    const ro = IMAGES.registry.os || {}, ra = IMAGES.registry.seafront || {};
    document.getElementById('regos').textContent =
      (ro.error ? '(error)' : ((ro.version || short(ro.digest)) + (ftime(ro.created) ? ' · ' + ftime(ro.created) : '') + ' · ' + short(ro.digest)));
    document.getElementById('regapp').textContent =
      rev(ra.revision).trim() + ' ' + (ra.error ? '(error)' : ((ftime(ra.created) ? ftime(ra.created) + ' · ' : '') + short(ra.digest)));
    const vc = ro.vs_checkout, os_ = document.getElementById('ossrc');
    os_.textContent = vc === 'same' ? 'checkout has no OS changes since this build'
      : vc === 'changed' ? 'images/kinoite changed since this build: rebuild to ship it'
      : 'cannot compare with the checkout';
    os_.className = 'ver ' + (vc === 'changed' ? 'stale' : 'muted');
    GW_REGAPP_REV = ra.revision || null; renderSeafrontSrc();
  } catch (e) { document.getElementById('regos').textContent = '(unreachable)'; }
  refresh();
}

function openModal(title) {
  document.getElementById('modaltitle').textContent = title;
  document.getElementById('modalbody').textContent = 'starting…';
  document.getElementById('modal').classList.add('show');
}
// Fire a long-running per-box job WITHOUT blocking the UI: POST to start, then track it in
// BOXJOBS and poll in the background. The card shows an inline "⏳ …" with a dismissible
// "view log"; the grid keeps refreshing and every other control stays usable meanwhile.
async function startJob(name, action, kind, opts = {}) {
  if (opts.confirmMsg && !confirm(opts.confirmMsg)) return;
  if (BOXJOBS[name] && BOXJOBS[name].running) { toast(`${name}: a job is already running (${BOXJOBS[name].kind})`); return; }
  BOXJOBS[name] = {kind, running: true, rc: null, log: '(starting…)'};
  refresh();
  const init = {method: 'POST'};
  if (opts.body) { init.headers = {'Content-Type': 'application/json'}; init.body = JSON.stringify(opts.body); }
  try {
    const r = await fetch(`/api/scope/${name}/${action}`, init);
    if (!r.ok) {
      const j = await r.json().catch(() => ({}));
      BOXJOBS[name] = {kind, running: false, rc: 1, log: 'error: ' + (j.detail || r.status)};
      toast(`${name}: ${kind} failed — ${j.detail || r.status}`); refresh(); return;
    }
  } catch (e) {
    BOXJOBS[name] = {kind, running: false, rc: 1, log: 'request failed: ' + e};
    toast(`${name}: ${kind} — request failed`); refresh(); return;
  }
  toast(`${name}: ${kind} started (running in background)`);
  pollBoxJob(name);
}
async function pollBoxJob(name) {
  let j;
  try { j = await (await fetch(`/api/scope/${name}/job`)).json(); }
  catch (e) { setTimeout(() => pollBoxJob(name), 3000); return; }
  BOXJOBS[name] = j;
  if (MODAL_JOB === name && document.getElementById('modal').classList.contains('show')) {
    const box = document.getElementById('modalbody');
    box.textContent = j.log || '(starting…)'; box.scrollTop = box.scrollHeight;
  }
  if (j.running) { setTimeout(() => pollBoxJob(name), 1500); return; }
  toast(`${name}: ${j.kind || 'job'} ${j.rc === 0 ? 'finished ✓' : 'exited ' + j.rc}`);
  refresh(); fetchImages();
}
// Open the (dismissible) modal on a box's job log. Closing it does NOT stop the job.
function viewJob(name) {
  MODAL_JOB = name;
  const job = BOXJOBS[name] || {};
  document.getElementById('modaltitle').textContent = `${name} — ${job.kind || 'job'}`;
  document.getElementById('modalbody').textContent = job.log || '(no output yet)';
  document.getElementById('modal').classList.add('show');
}
// After a page (re)load, re-attach to any jobs still running server-side so their inline
// progress reappears instead of silently vanishing.
async function reattachJobs() {
  let scopes;
  try { scopes = await (await fetch('/api/microscopes')).json(); } catch (e) { return; }
  await Promise.all(scopes.map(async m => {
    try {
      const j = await (await fetch(`/api/scope/${m.name}/job`)).json();
      if (j && j.running) { BOXJOBS[m.name] = j; pollBoxJob(m.name); }
    } catch (e) {}
  }));
  refresh();
}

// Gateway clock = browser clock + GW_CLOCK.offset_ms (measured at each fetchGateway).
const GW_CLOCK = { offset_ms: null, tz: null, server: null, synced: false };
function gwTimeTick() {
  const el = document.getElementById('gwtime');
  if (GW_CLOCK.offset_ms == null) { el.textContent = 'time …'; return; }
  const now = new Date(Date.now() + GW_CLOCK.offset_ms);
  let shown;
  try {
    shown = now.toLocaleString('sv-SE', { timeZone: GW_CLOCK.tz || undefined }) + (GW_CLOCK.tz ? ' ' + GW_CLOCK.tz : '');
  } catch (e) { shown = now.toISOString(); }
  const d = GW_CLOCK.offset_ms / 1000, ad = Math.abs(d);
  const delta = (d >= 0 ? '+' : '') + (ad < 1 ? (d * 1000).toFixed(0) + ' ms' : d.toFixed(1) + ' s');
  const src = !GW_CLOCK.synced ? 'not synced'
            : GW_CLOCK.server === 'local clock' ? 'own clock (no internet time)'
            : 'synced to ' + GW_CLOCK.server;
  el.innerHTML = 'time ' + shown + ' · ' + src + ' · <span class="' + (ad > 2 ? 'stale' : 'ok') + '">' + delta + ' vs this browser</span>';
}
async function setGatewayTime(fromBrowser) {
  let epoch;
  if (fromBrowser) epoch = Date.now() / 1000;
  else {
    const v = document.getElementById('gwtimein').value;
    if (!v) { toast('enter a date + time first'); return; }
    epoch = new Date(v).getTime() / 1000;   // datetime-local is read in the browser's zone
    if (isNaN(epoch)) { toast('invalid time'); return; }
  }
  const target = new Date(epoch * 1000).toLocaleString('sv-SE');
  const warn = (GW_CLOCK.synced && GW_CLOCK.server && GW_CLOCK.server !== 'local clock')
    ? '\\n\\nNOTE: the gateway is currently synced to an internet time server (' + GW_CLOCK.server + '); that is normally more accurate than this.' : '';
  if (!confirm('Set the gateway clock to ' + target + ' (this browser\\'s time zone) and step every box to it?' + warn)) return;
  if (fromBrowser) epoch = Date.now() / 1000;   // re-read: the confirm dialog took time
  try {
    const r = await fetch('/api/gateway/time', {method: 'POST', headers: {'Content-Type': 'application/json'},
                                                body: JSON.stringify({epoch})});
    const j = await r.json();
    if (!r.ok) { alert('Setting the time failed:\\n' + (j.detail || r.status)); return; }
    const boxes = Object.entries(j.boxes || {}).map(([n, s]) => n + ': ' + s).join('\\n');
    alert(j.gateway + (boxes ? '\\n\\n' + boxes : ''));
  } catch (e) { alert('Setting the time failed: ' + e); }
  fetchGateway(); fetchImages();
}
async function fetchGateway() {
  try {
    const t0 = Date.now();
    const g = await (await fetch('/api/gateway')).json();
    const t1 = Date.now();
    if (g.time) {
      Object.assign(GW_CLOCK, { offset_ms: g.time.epoch * 1000 - (t0 + t1) / 2, tz: g.time.tz,
                                server: g.time.server, synced: g.time.synced });
      gwTimeTick();
    }
    document.getElementById('gwhost').textContent = g.host || 'gateway';
    const gi = g.git || {};
    let s = gi.head ? (gi.head + (gi.subject ? ' · ' + gi.subject : '')) : '?';
    if (gi.dirty) s += '  · ⚠ dirty';
    document.getElementById('gwgit').textContent = 'git ' + s;
    const rp = g.repos || {};
    if (rp.gateway) document.getElementById('repogw').href = rp.gateway;
    if (rp.seafront) document.getElementById('reposf').href = rp.seafront;
    const job = g.rebuild || {};
    GW_BUILDING = !!job.running;
    const flag = document.getElementById('gwstate');
    flag.textContent = GW_BUILDING ? (job.kind || 'building') + '…' : 'idle';
    flag.className = 'flag ' + (GW_BUILDING ? 'rebuilding' : 'idle');
    const fa = gi.fetched_at ? 'fetched ' + ago(gi.fetched_at) : 'never fetched';
    const sync = document.getElementById('gwsync');
    sync.textContent = 'checkout ' + (gi.head || '?') + ' · ' +
      (gi.behind ? gi.behind + ' commit(s) to pull' : 'up to date with origin') + ' (' + fa + ')';
    sync.className = 'ver' + (gi.behind ? ' stale' : '');
    document.getElementById('gwincoming').textContent = (gi.incoming || []).join('\\n');
    GW_SF = g.seafront || {}; renderSeafrontSrc();
    updateNetBtns();
  } catch (e) { /* leave last-known */ }
}
// Fetch/build/pull buttons need internet (blocked while Wi-Fi is in AP mode); build buttons
// are also blocked while a build runs. fetchWifi and fetchGateway each own one flag.
let GW_NONET = false, GW_BUILDING = false, GW_SF = {}, GW_REGAPP_REV = null;
function updateNetBtns() {
  document.querySelectorAll('button.needsnet').forEach(b => {
    b.disabled = GW_NONET || (GW_BUILDING && b.classList.contains('buildbtn'));
    b.title = GW_NONET ? 'Needs internet: switch Wi-Fi to Client mode first' : '';
  });
}
function ago(epoch) {
  const s = Date.now() / 1000 - epoch;
  return s < 90 ? 'just now' : dur(s) + ' ago';
}
// pinned commit vs registry image vs the remote branch head (after Fetch)
function renderSeafrontSrc() {
  const el = document.getElementById('sfsrc'); if (!el) return;
  const p = GW_SF.pinned, r = GW_SF.remote, img = GW_REGAPP_REV;
  const s8 = x => (x || '?').slice(0, 8);
  let t = 'pinned ' + s8(p);
  if (p && img) t += (s8(img) === s8(p)) ? ' (= registry)' : ' (registry is ' + s8(img) + ')';
  t += r ? ' · newest on GitHub ' + s8(r.sha) + ' (' + ago(r.at) + ')' : ' · GitHub: not fetched';
  el.textContent = t;
}
async function postJson(url) {
  const r = await fetch(url, {method: 'POST'});
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.detail || ('HTTP ' + r.status));
  return j;
}
async function gatewayFetch() {
  toast('gateway: git fetch…');
  try { await postJson('/api/gateway/fetch'); toast('gateway: fetched'); }
  catch (e) { toast('gateway fetch failed: ' + e.message); }
  fetchGateway();
}
async function seafrontFetch() {
  toast('seafront: looking up the newest commit…');
  try { const j = await postJson('/api/seafront/fetch'); GW_SF.remote = j; renderSeafrontSrc(); toast('seafront: newest is ' + j.sha.slice(0, 8)); }
  catch (e) { toast('seafront fetch failed: ' + e.message); }
}
async function gatewayBuild(target) {
  const ref = target === 'seafront' ? (document.getElementById('gwref').value || '').trim() : '';
  const what = target === 'os' ? 'the OS image from the current checkout'
    : 'the seafront image at ' + (ref ? '"' + ref + '"' : 'the pinned commit');
  if (!confirm('Build ' + what + ' and push it to the registry?\\n\\nNo box is touched. Takes a few minutes.')) return;
  openModal('Gateway: build ' + (target === 'os' ? 'OS image' : 'seafront image' + (ref ? ' @ ' + ref : '')));
  const url = '/api/gateway/build?target=' + target + (ref ? '&seafront_ref=' + encodeURIComponent(ref) : '');
  try { await postJson(url); }
  catch (e) { document.getElementById('modalbody').textContent = 'error: ' + e.message; return; }
  fetchGateway();
  pollGatewayJob();
}
async function fetchWifi() {
  try {
    const w = await (await fetch('/api/wifi')).json();
    const net = (w.internet || '').startsWith('yes');
    let s = 'Wi-Fi: ' + (w.mode || '?');
    if (w.ssid) s += ' · ' + w.ssid;
    if (w.mode === 'ap' && w['ap-clients']) s += ' · ' + w['ap-clients'] + ' client(s)';
    s += ' · internet ' + (net ? 'yes' : 'no');
    document.getElementById('gwwifi').textContent = s;
    document.getElementById('wifiap').disabled = (w.mode === 'ap');
    document.getElementById('wificlient').disabled = (w.mode === 'client' && net);
    GW_NONET = !net; updateNetBtns();
  } catch (e) { /* leave last-known */ }
}
async function setWifi(mode) {
  const body = { mode };
  if (mode === 'ap') {
    if (!confirm('Switch Wi-Fi to HOTSPOT (AP)?\\n\\nLaptops can join the "microscopes" network to reach this dashboard, but the gateway LOSES internet (no image rebuilds until you switch back). If you are connected over the gateway current Wi-Fi, that link drops — reconnect to the hotspot. The wired backbone and squidway.local stay reachable.')) return;
  } else {
    if (!confirm('Switch Wi-Fi to CLIENT (internet)?\\n\\nThe gateway rejoins an external Wi-Fi for internet (needed to rebuild images). The "microscopes" hotspot goes DOWN, so laptops on it lose the dashboard until you switch back. Reach the gateway over the wired backbone or squidway.local meanwhile.')) return;
    const ssid = prompt('Client Wi-Fi SSID to join (blank = use a network the gateway already knows):', '');
    if (ssid === null) return;
    if (ssid) { body.ssid = ssid; const pw = prompt('Password for "' + ssid + '" (blank if open / already saved):', ''); if (pw) body.password = pw; }
  }
  toast('Wi-Fi: switching to ' + mode + '… (may take up to ~2 min; your link may drop)');
  try {
    const r = await fetch('/api/wifi/mode', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    const j = await r.json().catch(() => ({}));
    toast(r.ok ? ('Wi-Fi: ' + mode + ' scheduled') : ('Wi-Fi failed — ' + (j.detail || r.status)));
  } catch (e) { toast('Wi-Fi request sent; link may have dropped — reconnect and check status'); }
  setTimeout(fetchWifi, 8000);
}
function addScope() {
  const v = (id, val) => document.getElementById(id).value = val;
  v('f-name', ''); v('f-host', ''); v('f-pp', ''); v('f-type', 'squid'); v('f-sp', '8000');
  document.getElementById('adderr').textContent = '';
  document.getElementById('addsubmit').disabled = false;
  document.getElementById('addmodal').classList.add('show');
  document.getElementById('f-name').focus();
}
function closeAdd() { document.getElementById('addmodal').classList.remove('show'); }
async function submitAdd() {
  const val = id => document.getElementById(id).value.trim();
  const err = document.getElementById('adderr');
  const name = val('f-name'), host = val('f-host'), pp = val('f-pp');
  const type = val('f-type') || 'squid', sp = val('f-sp');
  if (!name || !host) { err.textContent = 'Name and backbone IP are required.'; return; }
  const body = { name, host, type };
  if (pp) body.proxy_port = parseInt(pp, 10);
  if (sp) body.seafront_port = parseInt(sp, 10);
  const btn = document.getElementById('addsubmit');
  btn.disabled = true; err.textContent = 'adding…';
  try {
    const r = await fetch('/api/fleet/scope', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    const j = await r.json().catch(() => ({}));
    if (r.ok) {
      closeAdd();
      toast('added ' + name + ' → proxy :' + (j.scope && j.scope.proxy_port));
      setTimeout(() => { refresh(); fetchImages(); }, 800);
    } else {
      // Keep the dialog open with the entered values so the error can be fixed in place.
      err.textContent = j.detail || ('failed (HTTP ' + r.status + ')');
      btn.disabled = false;
    }
  } catch (e) {
    err.textContent = 'request failed: ' + e;
    btn.disabled = false;
  }
}
async function removeScope(name) {
  if (!confirm('Remove ' + name + ' from the fleet?\\n\\nDe-registers it from the proxy + dashboard. Does not touch the box itself.')) return;
  toast('removing ' + name + '…');
  try {
    const r = await fetch('/api/fleet/scope/' + name, {method: 'DELETE'});
    const j = await r.json().catch(() => ({}));
    toast(r.ok ? (name + ' removed') : ('remove failed — ' + (j.detail || r.status)));
  } catch (e) { toast('remove failed — ' + e); }
  setTimeout(() => { refresh(); fetchImages(); }, 1000);
}
function changeIp(name) {
  const ip = prompt('New backbone IP for ' + name + ' (inside the backbone subnet):');
  if (!ip) return;
  startJob(name, 'set-ip', 'set-ip', {body: {ip}, confirmMsg:
    'Renumber ' + name + ' to ' + ip + '?\\n\\nApplies the new IP over SSH (keeps the old address until the new one is confirmed, auto-reverts on failure), then re-points the proxy. Runs in the background (~30s).'});
}
async function pollGatewayJob() {
  const box = document.getElementById('modalbody');
  try {
    const j = await (await fetch('/api/gateway/job')).json();
    box.textContent = j.log || '(starting…)';
    box.scrollTop = box.scrollHeight;
    if (j.running) { setTimeout(pollGatewayJob, 1500); }
    else { box.textContent += `\\n\\n--- finished (exit ${j.rc}) ---`; box.scrollTop = box.scrollHeight; fetchGateway(); fetchImages(); }
  } catch (e) { box.textContent += '\\npoll error: ' + e; }
}
async function gatewaySelfUpdate() {
  if (!confirm('Pull the gateway repo and apply it (restarts the dashboard)?\\n\\nDoes not build images. The page reloads once the dashboard is back.')) return;
  openModal('Gateway — update software');
  const box = document.getElementById('modalbody');
  box.textContent = 'pulling + applying… the dashboard will restart.';
  let r;
  try { r = await fetch('/api/gateway/self-update', {method: 'POST'}); }
  catch (e) { box.textContent = 'request failed: ' + e; return; }
  if (!r.ok) {
    const j = await r.json().catch(() => ({}));
    box.textContent = 'error: ' + (j.detail || r.status);
    return;
  }
  box.textContent = 'dashboard restarting — reloading when it is back…';
  setTimeout(waitForDashboardThenReload, 3000);
}
async function waitForDashboardThenReload() {
  for (let i = 0; i < 30; i++) {
    try { const r = await fetch('/api/gateway', {cache: 'no-store'}); if (r.ok) { location.reload(); return; } }
    catch (e) { /* dashboard still down */ }
    await new Promise(res => setTimeout(res, 1500));
  }
  location.reload();
}
async function gatewayReboot() {
  if (!confirm('Reboot the GATEWAY (' + (document.getElementById('gwhost').textContent) + ')?\\n\\nDrops the whole control plane (dashboard, registry, proxy) for ~1 min. Boxes keep running locally.')) return;
  toast('gateway: rebooting…');
  try { await fetch('/api/gateway/reboot', {method: 'POST'}); } catch (e) { /* connection drops as it goes down */ }
}
function updateOs(name) {
  const reg = (IMAGES.registry.os || {});
  const to = ftime(reg.created) || reg.version || short(reg.digest);
  startJob(name, 'update-os', 'update-os', {confirmMsg:
    `Update OS on ${name} → ${to}?\\n\\nDownloads + stages the new OS image (bootc upgrade) in the BACKGROUND — you can keep using the dashboard. It does NOT reboot; click Reboot to activate (bad boots auto-roll-back). Do idle boxes only.`});
}
function updateApp(name) {
  const reg = (IMAGES.registry.seafront || {});
  const to = ftime(reg.created) || short(reg.digest);
  startJob(name, 'update-seafront', 'update-seafront', {confirmMsg:
    `Update seafront on ${name} → ${to}?\\n\\nPulls the latest app image (podman pull) in the BACKGROUND. It does NOT restart the container — click Restart service to apply.`});
}

function microscopeDialog(name) {
  const b = IMAGES.boxes[name] || {};
  const profs = b.microscopes || [];
  const active = b.active;                     // override-file content, or null
  const sel = document.getElementById('micselect');
  sel.innerHTML = '';
  profs.forEach(p => {
    const o = document.createElement('option');
    o.value = p; o.textContent = p + (p === active ? '  (active)' : '');
    if (p === active) o.selected = true;
    sel.appendChild(o);
  });
  if (!profs.length) { const o = document.createElement('option'); o.textContent = '(no profiles in config.json)'; sel.appendChild(o); }
  document.getElementById('mictitle').textContent = name + ' — microscope profile';
  document.getElementById('micerr').textContent = (!active && profs.length) ? 'No override set — box runs the image default until you pick one.' : '';
  document.getElementById('micsubmit').disabled = !profs.length;
  const m = document.getElementById('micmodal');
  m.dataset.box = name; m.classList.add('show');
}
function closeMic() { document.getElementById('micmodal').classList.remove('show'); }
async function applyMic() {
  const name = document.getElementById('micmodal').dataset.box;
  const profile = document.getElementById('micselect').value;
  const err = document.getElementById('micerr'); const btn = document.getElementById('micsubmit');
  btn.disabled = true; err.textContent = 'switching + restarting seafront…';
  try {
    const r = await fetch('/api/scope/' + name + '/microscope', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({profile})});
    const j = await r.json().catch(() => ({}));
    if (r.ok) { closeMic(); toast(name + ': profile → ' + profile + ' (restarting)'); setTimeout(() => { refresh(); fetchImages(); }, 1500); }
    else { err.textContent = j.detail || ('failed (HTTP ' + r.status + ')'); btn.disabled = false; }
  } catch (e) { err.textContent = 'request failed: ' + e; btn.disabled = false; }
}

// Last seen host_up per box. When a box comes back (reboot finished), re-read its image
// versions right away instead of waiting for the 30s poll, which otherwise leaves the
// pre-reboot "staged — reboot to apply" on screen. Two reads: sshd answers before bootc
// and podman are fully settled.
const HOST_UP = {};
function boxCameBack(name) {
  setTimeout(fetchImages, 3000);
  setTimeout(fetchImages, 15000);
}
// Collapsed cards, remembered per browser. New boxes start expanded.
function collapsedSet() {
  try { return new Set(JSON.parse(localStorage.getItem('collapsed') || '[]')); } catch (e) { return new Set(); }
}
function saveCollapsed(set) {
  try { localStorage.setItem('collapsed', JSON.stringify([...set])); } catch (e) {}
}
let COLLAPSED = collapsedSet();
function isCollapsed(name) { return COLLAPSED.has(name); }
function toggleCard(name) {
  if (COLLAPSED.has(name)) COLLAPSED.delete(name); else COLLAPSED.add(name);
  saveCollapsed(COLLAPSED); refresh();
}
function collapseAll(on) {
  COLLAPSED = on ? new Set(Object.keys(HOST_UP)) : new Set();
  saveCollapsed(COLLAPSED); refresh();
}
async function refresh() {
  let data;
  try { data = await (await fetch('/api/status')).json(); }
  catch (e) { document.getElementById('foot').textContent = 'gateway unreachable'; return; }
  for (const m of data) {
    if (HOST_UP[m.name] === false && m.host_up) boxCameBack(m.name);
    HOST_UP[m.name] = m.host_up;
  }
  const grid = document.getElementById('grid');
  grid.innerHTML = '';
  for (const m of data) {
    const url = `http://${location.hostname}:${m.proxy_port}/`;
    const card = document.createElement('div');
    const collapsed = isCollapsed(m.name);
    card.className = 'card' + (collapsed ? ' collapsed' : '');
    const hs = m.host_up ? 'up' : 'down', ss = m.service_up ? 'up' : 'down';
    const img = IMAGES.boxes[m.name] || {};
    const osd = img.os || {}, appd = img.seafront || {};
    const regOs = (IMAGES.registry.os || {}).digest;
    const osStale = osd.available && regOs && osd.digest !== regOs && !osd.staged_latest;
    const appStale = appd.present && !appd.up_to_date && (IMAGES.registry.seafront || {}).digest;
    const issues = img.issues || [];
    const job = BOXJOBS[m.name];
    const jobRunning = !!(job && job.running);
    const jr = jobRunning ? 'disabled' : '';
    let jobLine = '';
    if (jobRunning) jobLine = `<div class="ver jobrun">⏳ ${job.kind}… <button class="linkbtn" onclick="viewJob('${m.name}')">view log</button></div>`;
    else if (job && job.rc != null && job.rc !== 0) jobLine = `<div class="ver stale">⚠ ${job.kind} failed (exit ${job.rc}) <button class="linkbtn" onclick="viewJob('${m.name}')">log</button></div>`;
    // uptime ticks locally between the 30 s image polls; a cached read is stale_seconds older
    const up = img.uptime_s == null ? null
      : img.uptime_s + (img.stale_seconds || 0) + (Date.now() - (IMAGES.at || Date.now())) / 1000;
    let badges = '';
    if (collapsed) {
      if (jobRunning) badges += `<span class="badge job">⏳ ${job.kind}</span>`;
      if (issues.length) badges += `<span class="badge" title="${esc(issues.join(' · '))}">⚠ ${issues.length}</span>`;
      if (osStale || appStale || (osd.etc_drift && osd.etc_drift.length)) badges += '<span class="badge">update</span>';
    }
    card.innerHTML = `
      <div class="cardhead" onclick="toggleCard('${m.name}')">
        <span class="name">${m.name}</span>${badges}<span class="chev">▾</span>
      </div>
      <div class="signals">
        <div class="sig"><span class="dot ${hs}"></span>Computer: ${m.host_up ? 'online' : 'offline'}${m.host_up && up != null ? ' <span class="muted">· up ' + dur(up) + '</span>' : ''}</div>
        <div class="sig"><span class="dot ${ss}"></span>seafront: ${m.service_up ? 'running' : 'down'}</div>
      </div>
      <div class="meta"><a class="repolink" href="http://${m.host}:${m.seafront_port}/">${m.host}:${m.seafront_port}</a> · <a class="repolink" href="${url}">via gateway :${m.proxy_port}</a></div>
      <div class="cardbody">
      <div class="ver">${osLine(osd, IMAGES.registry.os)}</div>
      <div class="ver">${appLine(appd, IMAGES.registry.seafront)}</div>
      ${img.stale_seconds ? `<div class="ver stale">⏳ last read failed — showing values from ${img.stale_seconds}s ago</div>` : ''}
      ${jobLine}
      <div class="btns">
        <a class="open ${m.service_up ? '' : 'down'}" href="${url}">${m.service_up ? 'Open' : 'Offline'}</a>
        <button onclick="act('${m.name}','restart-service')">Restart service</button>
        <button onclick="editConfig('${m.name}')">Config</button>
        <button class="${issues.length ? 'hot' : ''}" title="${esc(issues.join(' · '))}" onclick="diagnostics('${m.name}')">Diagnostics${issues.length ? ' ⚠ ' + issues.length : ''}</button>
        <button class="${osStale ? 'hot' : ''}" ${jr} onclick="updateOs('${m.name}')">Update OS</button>
        <button class="${appStale ? 'hot' : ''}" ${jr} onclick="updateApp('${m.name}')">Update seafront</button>
        <button ${jr} onclick="changeIp('${m.name}')">Change IP</button>
        <button class="danger" onclick="removeScope('${m.name}')">Remove</button>
        <button style="grid-column:1/3" onclick="microscopeDialog('${m.name}')">🔬 Profile: ${(IMAGES.boxes[m.name]||{}).active || 'default'}</button>
        <button class="danger" style="grid-column:1/3" ${jr}
          onclick="act('${m.name}','reboot','Reboot ${m.name} (${m.host})? This interrupts anything running on it.')">
          Reboot computer</button>
      </div>
      </div>`;
    grid.appendChild(card);
  }
  const hostUp = data.filter(m => m.host_up).length;
  const svcUp = data.filter(m => m.service_up).length;
  document.getElementById('foot').textContent =
    `${hostUp}/${data.length} computers up · ${svcUp}/${data.length} services running · updated ${new Date().toLocaleTimeString()}`;
}
fetchImages();
fetchGateway();
fetchWifi();
reattachJobs();
setInterval(refresh, 1000);
setInterval(fetchImages, 30000);   // image info needs SSH per box — poll gently
setInterval(fetchGateway, 15000);  // gateway git/rebuild state (local, cheap)
setInterval(gwTimeTick, 1000);     // gateway clock display ticks locally between fetches
setInterval(fetchWifi, 15000);     // gateway Wi-Fi mode + internet reachability
</script>
</body>
</html>"""
