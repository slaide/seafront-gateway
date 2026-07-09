"""Microscope gateway dashboard + fleet control.

A small FastAPI app that serves a landing page listing every microscope on the
LAN, health-checks each one, reports the OS and seafront-app IMAGE versions each
box runs versus the latest built in the gateway registry, and offers per-scope
maintenance actions.

Everything the fleet ships is an OCI image pulled from the gateway registry
(192.168.50.1:5000): the whole box OS (Fedora Kinoite, tracked with `bootc`) and
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
import json
import os
import pathlib

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config" / "microscopes.json").read_text())
MICROSCOPES = CONFIG["microscopes"]
BY_NAME = {m["name"]: m for m in MICROSCOPES}

# SSH identity the dashboard uses to reach the scopes (baked into the box image as
# the gateway's fleet key). Overridable via env for testing.
SSH_KEY = os.environ.get("FLEET_SSH_KEY", os.path.expanduser("~/.ssh/fleet"))
SSH_USER = os.environ.get("FLEET_SSH_USER", "pharmbio")
SEAFRONT_CONFIG_PATH = "~/seafront/config.json"  # on the scope

# The gateway registry and the two images it serves. Boxes reference these exact
# refs (bootc spec.image and the seafront quadlet Image=), so the same string is
# both what we query in the registry and what we compare against on the box.
REGISTRY = os.environ.get("FLEET_REGISTRY", "192.168.50.1:5000")
OS_REPO = "seafront-os"
APP_REPO = "seafront"
APP_IMAGE = f"{REGISTRY}/{APP_REPO}:stable"

SYSTEMCTL = "/usr/bin/systemctl"

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
        "host_up": host_up,
        "service_up": service_up,
    }


# --- SSH plumbing for on-demand actions + image queries ------------------------
def _ssh_base(name: str) -> list[str]:
    m = BY_NAME.get(name)
    if m is None:
        raise HTTPException(404, f"unknown microscope: {name}")
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
        f"{SSH_USER}@{m['host']}", "--",
    ]


async def _ssh(name: str, argv: list[str], timeout: float = 20.0) -> tuple[int, str, str]:
    """Run a command on a scope over SSH with the fleet key. `argv` is appended
    after `--`; scope name/host come only from our config, never client input."""
    cmd = _ssh_base(name) + argv
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        raise HTTPException(504, f"ssh to {name} timed out")
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


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
        version = created = None
        if cfg_digest:
            rc = await client.get(f"{base}/blobs/{cfg_digest}", timeout=4.0)
            if rc.status_code == 200:
                cfg = rc.json()
                created = cfg.get("created")
                labels = cfg.get("config", {}).get("Labels") or {}
                version = labels.get("org.opencontainers.image.version")
        return {"digest": digest, "version": version, "created": created}
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
    return {"present": True, "digest": d.get("Digest"), "created": d.get("Created")}


# One SSH round-trip per box: OS image (bootc) + app image present (podman). Both
# need root; the fleet sudoers rule grants exactly these two reads passwordless.
_BOX_IMAGES_CMD = (
    "sudo -n /usr/bin/bootc status --format=json 2>/dev/null"
    "; echo '<<<SPLIT>>>'; "
    f"sudo -n /usr/bin/podman image inspect {APP_IMAGE} 2>/dev/null"
)


async def _box_images(name: str) -> dict:
    # Single argv element: ssh space-joins argv and the remote shell re-parses, so
    # the `;`/redirection are interpreted on the box (not locally).
    try:
        _rc, out, _err = await _ssh(name, [_BOX_IMAGES_CMD], timeout=15.0)
    except HTTPException:
        return {"name": name, "os": {"available": False}, "seafront": {"present": False}}
    os_part, _, app_part = out.partition("<<<SPLIT>>>")
    return {"name": name, "os": _parse_bootc(os_part), "seafront": _parse_app(app_part)}


@app.get("/api/images")
async def images() -> JSONResponse:
    """OS + seafront image versions: latest in the gateway registry, and what each
    box currently has, annotated with whether the box is up to date."""
    async with httpx.AsyncClient() as client:
        (reg_os, reg_app), boxes = await asyncio.gather(
            asyncio.gather(_registry_image(client, OS_REPO), _registry_image(client, APP_REPO)),
            asyncio.gather(*[_box_images(m["name"]) for m in MICROSCOPES]),
        )
    for b in boxes:
        osd, appd = b["os"], b["seafront"]
        rd = reg_os.get("digest")
        osd["up_to_date"] = bool(osd.get("digest") and rd and osd["digest"] == rd)
        osd["staged_latest"] = bool(osd.get("staged_digest") and rd and osd["staged_digest"] == rd)
        ad = reg_app.get("digest")
        appd["up_to_date"] = bool(appd.get("digest") and ad and appd["digest"] == ad)
    return JSONResponse({"registry": {"os": reg_os, "seafront": reg_app}, "boxes": list(boxes)})


@app.get("/api/microscopes")
async def microscopes() -> JSONResponse:
    return JSONResponse(MICROSCOPES)


@app.get("/api/status")
async def status() -> JSONResponse:
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*[_check(client, m) for m in MICROSCOPES])
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
    # `systemctl reboot` tears down the connection as it succeeds; ssh returns
    # non-zero on that dropped channel, so treat a connection drop as success.
    rc, out, err = await _ssh(name, ["sudo", SYSTEMCTL, "reboot"], timeout=10.0)
    return JSONResponse({"ok": True, "action": "reboot", "note": (err or out).strip()})


@app.get("/api/scope/{name}/config", response_class=PlainTextResponse)
async def get_config(name: str) -> str:
    rc, out, err = await _ssh(name, ["cat", SEAFRONT_CONFIG_PATH])
    if rc != 0:
        raise HTTPException(502, f"could not read config: {err or out}".strip())
    return out


@app.get("/api/scope/{name}/logs", response_class=PlainTextResponse)
async def get_logs(name: str, lines: int = 200) -> str:
    lines = max(1, min(lines, 2000))
    rc, out, err = await _ssh(
        name, ["journalctl", "-u", "seafront", "-n", str(lines), "--no-pager"]
    )
    if rc != 0:
        raise HTTPException(502, f"could not read logs: {err or out}".strip())
    return out


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
    if name not in BY_NAME:
        raise HTTPException(404, f"unknown microscope: {name}")
    return _start_job(name, "update-os",
                      _ssh_base(name) + ["sudo", "-n", "/usr/bin/bootc", "upgrade"])


@app.post("/api/scope/{name}/update-seafront")
async def update_seafront(name: str) -> JSONResponse:
    """Pull the latest seafront app image (`podman pull`). Does NOT restart the
    container — a running acquisition keeps its current image until you restart
    the service, which then comes up on the freshly-pulled image."""
    if name not in BY_NAME:
        raise HTTPException(404, f"unknown microscope: {name}")
    return _start_job(name, "update-seafront",
                      _ssh_base(name) + ["sudo", "-n", "/usr/bin/podman", "pull", APP_IMAGE])


@app.get("/api/scope/{name}/job")
async def scope_job(name: str) -> JSONResponse:
    if name not in BY_NAME:
        raise HTTPException(404, f"unknown microscope: {name}")
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


@app.get("/api/gateway")
async def gateway_info() -> JSONResponse:
    head, subject, dirty, behind = await asyncio.gather(
        _git("rev-parse", "--short", "HEAD"),
        _git("log", "-1", "--pretty=%s"),
        _git("status", "--porcelain"),
        _git("rev-list", "--count", "HEAD..@{u}"),   # as of last fetch (no network here)
    )
    return JSONResponse({
        "host": GW_HOST,
        "git": {
            "head": head,
            "subject": subject,
            "dirty": bool(dirty.strip()),
            "behind": int(behind) if behind.isdigit() else None,
        },
        "rebuild": _job_status(GW_JOB),
    })


@app.post("/api/gateway/update")
async def gateway_update() -> JSONResponse:
    """Fetch latest git + rebuild BOTH images into the registry. Streamed job;
    updates the registry only — no box is touched until it is rolled."""
    cmd = ["bash", "-lc",
           f"cd {ROOT} && git fetch --all --prune && git pull --ff-only && "
           "bash scripts/build-images.sh --os --seafront"]
    return _start_job(GW_JOB, "gateway-update", cmd)


@app.get("/api/gateway/job")
async def gateway_job() -> JSONResponse:
    return JSONResponse(_job_status(GW_JOB))


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
             border-radius: 12px; display: inline-flex; flex-direction: column; gap: 7px;
             min-width: 360px; }
  .gwrow { display: flex; align-items: center; gap: 10px; }
  .gwname { font-size: 1rem; }
  .flag { font-size: .7rem; padding: 2px 9px; border-radius: 999px; letter-spacing: .05em;
          text-transform: uppercase; }
  .flag.idle { background: #21262d; color: #8b949e; border: 1px solid #30363d; }
  .flag.rebuilding { background: #3a2d09; color: #f0c674; border: 1px solid #bb8009; }
  .gwbtns { display: flex; gap: 8px; margin-top: 4px; }
  .gwbtns button { padding: 7px 12px; }
  .ok { color: #3fb950; }
  .stale { color: #d29922; }
  .staged { color: #58a6ff; }
  .muted { color: #6e7681; }
  button.hot { border-color: #bb8009; color: #f0c674; }
  .grid { display: grid; gap: 16px; padding: 24px;
          grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 12px;
          padding: 18px; display: flex; flex-direction: column; gap: 10px; }
  .name { font-size: 1.1rem; font-weight: 600; }
  .signals { display: flex; flex-direction: column; gap: 6px; font-size: .85rem; }
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
  #toast { position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%);
           background: #161b22; border: 1px solid #30363d; border-radius: 8px;
           padding: 10px 16px; font-size: .85rem; display: none; }
</style>
</head>
<body>
<header>
  <h1>🔬 Microscope Gateway</h1>
  <div class="sub">Fleet status &amp; image updates. Auto-refreshing every 5s.</div>
  <div class="gateway">
    <div class="gwrow">
      <span class="gwname">🖥️ <b id="gwhost">gateway</b></span>
      <span class="flag idle" id="gwstate">idle</span>
    </div>
    <div class="ver" id="gwgit">git …</div>
    <div class="ver">registry serves — OS <b id="regos">…</b> · seafront <b id="regapp">…</b></div>
    <div class="gwbtns">
      <button id="gwrebuild" onclick="gatewayRebuild()">Fetch git + rebuild images</button>
      <button class="danger" onclick="gatewayReboot()">Reboot gateway</button>
    </div>
  </div>
</header>
<div class="grid" id="grid"></div>
<footer id="foot">loading…</footer>

<div id="modal"><div id="modalbox">
  <div id="modalhead"><strong id="modaltitle"></strong>
    <button onclick="document.getElementById('modal').classList.remove('show')">close</button></div>
  <pre id="modalbody"></pre>
</div></div>
<div id="toast"></div>

<script>
function toast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg; t.style.display = 'block';
  clearTimeout(t._h); t._h = setTimeout(() => t.style.display = 'none', 4000);
}
function short(d) { return d ? d.replace('sha256:', '').slice(0, 12) : '?'; }
function fdate(iso) { if (!iso) return ''; const t = new Date(iso); return isNaN(t) ? '' : t.toLocaleDateString(); }

function osLine(o, reg) {
  if (!o || o.available === false) return '<span class="muted">OS: unknown (needs image-based box)</span>';
  const v = o.version || short(o.digest);
  let badge = '';
  if (o.staged_latest) badge = ' <span class="staged">staged ' + (o.staged_version || '') + ' — reboot to apply</span>';
  else if (o.up_to_date) badge = ' <span class="ok">up to date</span>';
  else if (reg && reg.digest) badge = ' <span class="stale">update → ' + (reg.version || short(reg.digest)) + '</span>';
  return 'OS ' + v + badge;
}
function appLine(a, reg) {
  if (!a || a.present === false) return '<span class="muted">seafront image: not present</span>';
  const d = fdate(a.created);
  let s = 'seafront ' + short(a.digest) + (d ? ' (' + d + ')' : '');
  if (a.up_to_date) s += ' <span class="ok">up to date</span>';
  else if (reg && reg.digest) s += ' <span class="stale">update → ' + short(reg.digest) + '</span>';
  return s;
}

async function view(name, kind) {
  const box = document.getElementById('modalbody');
  document.getElementById('modaltitle').textContent = `${name} — ${kind}`;
  box.textContent = 'loading…';
  document.getElementById('modal').classList.add('show');
  try {
    const r = await fetch(`/api/scope/${name}/${kind}`);
    box.textContent = await r.text();
  } catch (e) { box.textContent = 'error: ' + e; }
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

let IMAGES = {registry: {}, boxes: {}};
async function fetchImages() {
  try {
    const d = await (await fetch('/api/images')).json();
    IMAGES.registry = d.registry || {}; IMAGES.boxes = {};
    (d.boxes || []).forEach(b => IMAGES.boxes[b.name] = b);
    const ro = IMAGES.registry.os || {}, ra = IMAGES.registry.seafront || {};
    document.getElementById('regos').textContent =
      'OS ' + (ro.error ? '(error)' : (ro.version || short(ro.digest)));
    document.getElementById('regapp').textContent =
      'seafront ' + (ra.error ? '(error)' : short(ra.digest)) + (fdate(ra.created) ? ' (' + fdate(ra.created) + ')' : '');
  } catch (e) { document.getElementById('regos').textContent = 'OS (unreachable)'; }
  refresh();
}

function openModal(title) {
  document.getElementById('modaltitle').textContent = title;
  document.getElementById('modalbody').textContent = 'starting…';
  document.getElementById('modal').classList.add('show');
}
async function runJob(name, action, title) {
  openModal(title);
  let r;
  try { r = await fetch(`/api/scope/${name}/${action}`, {method: 'POST'}); }
  catch (e) { document.getElementById('modalbody').textContent = 'request failed: ' + e; return; }
  if (!r.ok) {
    const j = await r.json().catch(() => ({}));
    document.getElementById('modalbody').textContent = 'error: ' + (j.detail || r.status);
    return;
  }
  pollJob(name);
}
async function pollJob(name) {
  const box = document.getElementById('modalbody');
  try {
    const j = await (await fetch(`/api/scope/${name}/job`)).json();
    box.textContent = j.log || '(starting…)';
    box.scrollTop = box.scrollHeight;
    if (j.running) { setTimeout(() => pollJob(name), 1200); }
    else { box.textContent += `\\n\\n--- finished (exit ${j.rc}) ---`; box.scrollTop = box.scrollHeight; fetchImages(); }
  } catch (e) { box.textContent += '\\npoll error: ' + e; }
}

async function fetchGateway() {
  try {
    const g = await (await fetch('/api/gateway')).json();
    document.getElementById('gwhost').textContent = g.host || 'gateway';
    const gi = g.git || {};
    let s = gi.head ? (gi.head + (gi.subject ? ' · ' + gi.subject : '')) : '?';
    if (gi.behind) s += '  · ' + gi.behind + ' behind origin';
    if (gi.dirty) s += '  · ⚠ dirty';
    document.getElementById('gwgit').textContent = 'git ' + s;
    const rebuilding = !!(g.rebuild && g.rebuild.running);
    const flag = document.getElementById('gwstate');
    flag.textContent = rebuilding ? 'rebuilding' : 'idle';
    flag.className = 'flag ' + (rebuilding ? 'rebuilding' : 'idle');
    document.getElementById('gwrebuild').disabled = rebuilding;
  } catch (e) { /* leave last-known */ }
}
async function gatewayRebuild() {
  if (!confirm('Fetch latest git + rebuild BOTH images on the gateway?\\n\\nUpdates the registry only — no box is touched. Takes several minutes.')) return;
  openModal('Gateway — fetch git + rebuild images');
  let r;
  try { r = await fetch('/api/gateway/update', {method: 'POST'}); }
  catch (e) { document.getElementById('modalbody').textContent = 'request failed: ' + e; return; }
  if (!r.ok) {
    const j = await r.json().catch(() => ({}));
    document.getElementById('modalbody').textContent = 'error: ' + (j.detail || r.status);
    return;
  }
  fetchGateway();
  pollGatewayJob();
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
async function gatewayReboot() {
  if (!confirm('Reboot the GATEWAY (' + (document.getElementById('gwhost').textContent) + ')?\\n\\nDrops the whole control plane (dashboard, registry, proxy) for ~1 min. Boxes keep running locally.')) return;
  toast('gateway: rebooting…');
  try { await fetch('/api/gateway/reboot', {method: 'POST'}); } catch (e) { /* connection drops as it goes down */ }
}
function updateOs(name) {
  const reg = (IMAGES.registry.os || {});
  const to = reg.version || short(reg.digest);
  if (!confirm(`Update OS on ${name} → ${to}?\\n\\nDownloads + stages the new OS image (bootc upgrade). It does NOT reboot — the box keeps running until you click Reboot to activate it (bad boots auto-roll-back). Do idle boxes only.`)) return;
  runJob(name, 'update-os', `Update OS — ${name} → ${to}`);
}
function updateApp(name) {
  const reg = (IMAGES.registry.seafront || {});
  if (!confirm(`Update seafront image on ${name} → ${short(reg.digest)}?\\n\\nPulls the latest app image (podman pull). It does NOT restart the container — click Restart service to apply, so a running acquisition is undisturbed.`)) return;
  runJob(name, 'update-seafront', `Update seafront — ${name} → ${short(reg.digest)}`);
}

async function refresh() {
  let data;
  try { data = await (await fetch('/api/status')).json(); }
  catch (e) { document.getElementById('foot').textContent = 'gateway unreachable'; return; }
  const grid = document.getElementById('grid');
  grid.innerHTML = '';
  for (const m of data) {
    const url = `http://${location.hostname}:${m.proxy_port}/`;
    const card = document.createElement('div');
    card.className = 'card';
    const hs = m.host_up ? 'up' : 'down', ss = m.service_up ? 'up' : 'down';
    const img = IMAGES.boxes[m.name] || {};
    const osd = img.os || {}, appd = img.seafront || {};
    const osStale = osd.available && !osd.up_to_date && !osd.staged_latest && (IMAGES.registry.os || {}).digest;
    const appStale = appd.present && !appd.up_to_date && (IMAGES.registry.seafront || {}).digest;
    card.innerHTML = `
      <div class="name">${m.name}</div>
      <div class="signals">
        <div class="sig"><span class="dot ${hs}"></span>Computer: ${m.host_up ? 'online' : 'offline'}</div>
        <div class="sig"><span class="dot ${ss}"></span>seafront: ${m.service_up ? 'running' : 'down'}</div>
      </div>
      <div class="meta">${m.host}:${m.proxy_port}</div>
      <div class="ver">${osLine(osd, IMAGES.registry.os)}</div>
      <div class="ver">${appLine(appd, IMAGES.registry.seafront)}</div>
      <div class="btns">
        <a class="open ${m.service_up ? '' : 'down'}" href="${url}">${m.service_up ? 'Open' : 'Offline'}</a>
        <button onclick="act('${m.name}','restart-service')">Restart service</button>
        <button onclick="view('${m.name}','config')">View config</button>
        <button onclick="view('${m.name}','logs')">View logs</button>
        <button class="${osStale ? 'hot' : ''}" onclick="updateOs('${m.name}')">Update OS</button>
        <button class="${appStale ? 'hot' : ''}" onclick="updateApp('${m.name}')">Update seafront</button>
        <button class="danger" style="grid-column:1/3"
          onclick="act('${m.name}','reboot','Reboot ${m.name} (${m.host})? This interrupts anything running on it.')">
          Reboot computer</button>
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
setInterval(refresh, 5000);
setInterval(fetchImages, 30000);   // image info needs SSH per box — poll gently
setInterval(fetchGateway, 15000);  // gateway git/rebuild state (local, cheap)
</script>
</body>
</html>"""
