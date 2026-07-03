"""Microscope gateway dashboard + fleet control.

A small FastAPI app that serves a landing page listing every microscope on the
LAN, health-checks each one, and offers per-scope maintenance actions (restart
the seafront service, reboot the host, view the config and logs).

Two INDEPENDENT status signals are reported per scope so "box up, service down"
is visible at a glance:
  - host_up     : TCP connect to <host>:22 succeeds        (the computer is on)
  - service_up  : HTTP reply from <host>:<seafront_port>    (seafront is serving)
The 5s polling loop uses only those two cheap probes (no SSH), so it stays fast.
SSH is used ONLY for on-demand actions, via a dedicated key and a NARROW sudoers
rule on each box (systemctl {start,stop,restart,status} seafront + reboot).

Config comes from ../config/microscopes.json (shared with the Caddyfile
generator), so there is one source of truth.
"""
import asyncio
import json
import os
import pathlib
import re
import tempfile
import time

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config" / "microscopes.json").read_text())
MICROSCOPES = CONFIG["microscopes"]
BY_NAME = {m["name"]: m for m in MICROSCOPES}

DEPLOY_SCRIPT = ROOT / "scripts" / "deploy-seafront.sh"
DEPLOY_LOG = ROOT / "deploy.log"
# Single deploy at a time. `proc` is set while running; `rc` holds the last exit code.
DEPLOY: dict = {"proc": None, "rc": None, "version": None, "started": 0.0}

# SSH identity the dashboard uses to reach the scopes (created by
# scripts/setup-fleet-control.sh). Overridable via env for testing.
SSH_KEY = os.environ.get("FLEET_SSH_KEY", os.path.expanduser("~/.ssh/fleet"))
SSH_USER = os.environ.get("FLEET_SSH_USER", "pharmbio")
SEAFRONT_CONFIG_PATH = "~/seafront/config.json"  # on the scope

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


# --- SSH plumbing for on-demand actions ----------------------------------------
async def _ssh(name: str, argv: list[str], timeout: float = 20.0) -> tuple[int, str, str]:
    """Run a command on a scope over SSH with the fleet key. `argv` is a plain
    argv list executed on the remote (no shell), so scope name/host come only
    from our config — never interpolated from client input."""
    m = BY_NAME.get(name)
    if m is None:
        raise HTTPException(404, f"unknown microscope: {name}")
    cmd = [
        "ssh", "-i", SSH_KEY,
        "-o", "BatchMode=yes",                    # never hang on a password prompt
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=5",
        f"{SSH_USER}@{m['host']}", "--", *argv,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        raise HTTPException(504, f"ssh to {name} timed out")
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


# systemctl commands must match the sudoers rule installed by setup-fleet-control.sh
SYSTEMCTL = "/usr/bin/systemctl"


# --- version reporting: gateway storage + each box ----------------------------
GATEWAY_SRC = pathlib.Path.home() / "seafront-dist"   # what deploy-seafront.sh stages
BOX_SRC = "~/seafront-app"                            # where it lands on a box


def _parse_version(pyproject_text: str) -> str | None:
    m = re.search(r'^version\s*=\s*"([^"]+)"', pyproject_text, re.M)
    return m.group(1) if m else None


def _gateway_version() -> dict:
    """seafront version staged on the gateway (version string + source/commit id)."""
    pj = GATEWAY_SRC / "pyproject.toml"
    dv = GATEWAY_SRC / "DEPLOYED_VERSION"
    return {
        "version": _parse_version(pj.read_text()) if pj.exists() else None,
        "source": dv.read_text().strip().splitlines()[-1] if dv.exists() and dv.read_text().strip() else None,
    }


async def _box_version(name: str) -> dict:
    # One argv element: ssh space-joins argv and the remote shell re-parses, so a
    # multi-part ["sh","-c",...] loses its quoting — pass the whole line as one string.
    rc, out, _ = await _ssh(name, [
        f"cat {BOX_SRC}/DEPLOYED_VERSION 2>/dev/null; echo ':::'; "
        f"grep -m1 -E '^version' {BOX_SRC}/pyproject.toml 2>/dev/null",
    ], timeout=8.0)
    source = version = None
    if rc == 0 and ":::" in out:
        s, _, v = out.partition(":::")
        source = s.strip() or None
        m = re.search(r'"([^"]+)"', v)
        version = m.group(1) if m else None
    return {"name": name, "version": version, "source": source}


@app.get("/api/versions")
async def versions() -> JSONResponse:
    boxes = await asyncio.gather(*[_box_version(m["name"]) for m in MICROSCOPES])
    return JSONResponse({"gateway": _gateway_version(), "boxes": list(boxes)})


@app.get("/api/microscopes")
async def microscopes() -> JSONResponse:
    return JSONResponse(MICROSCOPES)


@app.get("/api/status")
async def status() -> JSONResponse:
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*[_check(client, m) for m in MICROSCOPES])
    return JSONResponse(list(results))


@app.post("/api/scope/{name}/restart-service")
async def restart_service(name: str) -> JSONResponse:
    rc, out, err = await _ssh(name, ["sudo", SYSTEMCTL, "restart", "seafront"])
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


DEFAULT_ZIP_URL = "https://github.com/slaide/seafront/archive/refs/heads/main.zip"


async def _run_deploy(args: list[str], cleanup: str | None = None) -> None:
    """Background task: run deploy-seafront.sh with `args`, streaming to DEPLOY_LOG."""
    with open(DEPLOY_LOG, "wb") as log:
        proc = await asyncio.create_subprocess_exec(
            "bash", str(DEPLOY_SCRIPT), *args,
            stdout=log, stderr=asyncio.subprocess.STDOUT, cwd=str(ROOT),
        )
        DEPLOY["proc"] = proc
        await proc.wait()
    DEPLOY["rc"] = proc.returncode
    DEPLOY["proc"] = None
    if cleanup:
        try:
            os.unlink(cleanup)
        except OSError:
            pass


@app.post("/api/seafront/stage")
async def seafront_stage(
    file: UploadFile | None = File(None),
    url: str | None = Form(None),
) -> JSONResponse:
    """STAGE a new seafront version onto the GATEWAY only — no box is touched.
    Source is either a zip URL the gateway fetches (browsers can't fetch GitHub
    cross-origin due to CORS, so the gateway does it) or an uploaded bundle/zip.
    Flush to each box separately via /api/scope/{name}/update."""
    if DEPLOY["proc"] is not None:
        raise HTTPException(409, "a stage/update is already running")
    if url is not None:
        target = url.strip() or DEFAULT_ZIP_URL
        DEPLOY.update(rc=None, version=target, started=time.time())
        DEPLOY_LOG.write_text(f"gateway staging from {target}…\n")
        asyncio.create_task(_run_deploy(["--stage-only", "--url", target]))
        return JSONResponse({"ok": True, "message": f"staging from {target}"})
    name = file.filename or "" if file is not None else ""
    if name.endswith((".tar", ".tar.zst", ".tzst", ".tar.gz", ".tgz")):
        mode = "--bundle"           # self-contained offline bundle (code + wheels + uv)
    elif name.endswith(".zip"):
        mode = "--zip"              # code-only zip (gateway builds deps → needs internet)
    else:
        raise HTTPException(400, "provide an offline bundle (.tar/.tar.zst), a code .zip, or a url")
    fd, up_path = tempfile.mkstemp(suffix="-" + name, prefix="seafront-upload-")
    with os.fdopen(fd, "wb") as f:
        while chunk := await file.read(1 << 20):
            f.write(chunk)
    DEPLOY.update(rc=None, version=name, started=time.time())
    DEPLOY_LOG.write_text(f"uploaded {name}, staging on the gateway…\n")
    asyncio.create_task(_run_deploy(["--stage-only", mode, up_path], cleanup=up_path))
    return JSONResponse({"ok": True, "message": f"staging {name}"})


@app.post("/api/scope/{name}/update")
async def scope_update(name: str) -> JSONResponse:
    """Flush (push) the gateway's currently-staged seafront to ONE box. Does not
    restart it — a running acquisition keeps its old code until you restart."""
    if name not in BY_NAME:
        raise HTTPException(404, f"unknown microscope: {name}")
    if DEPLOY["proc"] is not None:
        raise HTTPException(409, "a stage/update is already running")
    DEPLOY.update(rc=None, version=f"push→{name}", started=time.time())
    DEPLOY_LOG.write_text(f"flushing gateway's staged seafront to {name}…\n")
    asyncio.create_task(_run_deploy(["--push-only", name]))
    return JSONResponse({"ok": True, "message": f"updating {name}"})


@app.get("/api/seafront/deploy/log")
async def seafront_deploy_log() -> JSONResponse:
    running = DEPLOY["proc"] is not None
    log = DEPLOY_LOG.read_text() if DEPLOY_LOG.exists() else ""
    return JSONResponse({"running": running, "rc": DEPLOY["rc"], "version": DEPLOY["version"], "log": log})


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
  .deploy { margin-top: 14px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
            font-size: .85rem; }
  .deploy label { color: #8b949e; }
  .deploy .hint { color: #6e7681; font-size: .78rem; }
  .deploy .sep { color: #6e7681; }
  .stale { color: #d29922; }
  button.hot { border-color: #bb8009; color: #f0c674; }
  .grid { display: grid; gap: 16px; padding: 24px;
          grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 12px;
          padding: 18px; display: flex; flex-direction: column; gap: 12px; }
  .name { font-size: 1.1rem; font-weight: 600; }
  .signals { display: flex; flex-direction: column; gap: 6px; font-size: .85rem; }
  .sig { display: flex; align-items: center; gap: 8px; }
  .dot { width: 10px; height: 10px; border-radius: 50%; background: #6e7681; flex: none; }
  .dot.up { background: #3fb950; box-shadow: 0 0 8px #3fb95088; }
  .dot.down { background: #f85149; }
  .meta { color: #8b949e; font-size: .8rem; font-family: ui-monospace, monospace; }
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
  #modalbody { margin: 0; padding: 16px 18px; overflow: auto; white-space: pre;
               font: 13px/1.45 ui-monospace, monospace; color: #c9d1d9; }
  #toast { position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%);
           background: #161b22; border: 1px solid #30363d; border-radius: 8px;
           padding: 10px 16px; font-size: .85rem; display: none; }
</style>
</head>
<body>
<header>
  <h1>🔬 Microscope Gateway</h1>
  <div class="sub">Fleet status &amp; maintenance. Auto-refreshing every 5s.</div>
  <div class="deploy">
    <label>Gateway storage:</label>
    <strong id="gwver">…</strong>
    <span class="sep">— stage a new version:</span>
    <button onclick="stageFromGitHub()">from GitHub (dev)</button>
    <input type="file" id="zipfile" accept=".tar,.tar.zst,.tzst,.tgz,.tar.gz,.zip">
    <button onclick="stageUpload()">from bundle/zip</button>
    <span class="hint">staging updates the gateway only — flush to each microscope with its Update button</span>
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
  setTimeout(refresh, 1500);
}
let VERS = {gateway: {}, boxes: {}};
function fmtVer(o) { return o && o.version ? (o.version + (o.source ? ' · ' + o.source : '')) : null; }
async function fetchVersions() {
  try {
    const v = await (await fetch('/api/versions')).json();
    VERS.gateway = v.gateway || {}; VERS.boxes = {};
    (v.boxes || []).forEach(b => VERS.boxes[b.name] = b);
    document.getElementById('gwver').textContent = fmtVer(VERS.gateway) || 'nothing staged';
  } catch (e) { document.getElementById('gwver').textContent = '?'; }
  refresh();
}
function openDeployModal(title) {
  document.getElementById('modaltitle').textContent = title;
  document.getElementById('modalbody').textContent = 'starting…';
  document.getElementById('modal').classList.add('show');
}
async function submitDeploy(endpoint, fd) {
  let r;
  try { r = await fetch(endpoint, {method: 'POST', body: fd}); }
  catch (e) { document.getElementById('modalbody').textContent = 'request failed: ' + e; return; }
  if (!r.ok) {
    const j = await r.json().catch(() => ({}));
    document.getElementById('modalbody').textContent = 'error: ' + (j.detail || r.status);
    return;
  }
  pollDeploy();
}
async function stageFromGitHub() {
  const fd = new FormData(); fd.append('url', '');   // empty => server's default main.zip
  openDeployModal('Stage to gateway — GitHub (latest main)');
  submitDeploy('/api/seafront/stage', fd);
}
async function stageUpload() {
  const inp = document.getElementById('zipfile');
  if (!inp.files.length) { toast('choose a bundle/zip first'); return; }
  const fd = new FormData(); fd.append('file', inp.files[0]);
  openDeployModal('Stage to gateway — ' + inp.files[0].name);
  submitDeploy('/api/seafront/stage', fd);
}
async function updateBox(name) {
  const gw = fmtVer(VERS.gateway) || 'staged version';
  if (!confirm(`Flush the gateway's ${gw} to ${name}?\\n\\nThis does NOT restart a running service — a live acquisition keeps its current code until you restart it.`)) return;
  openDeployModal('Update ' + name + ' → ' + gw);
  submitDeploy('/api/scope/' + name + '/update', new FormData());
}
async function pollDeploy() {
  const box = document.getElementById('modalbody');
  try {
    const j = await (await fetch('/api/seafront/deploy/log')).json();
    box.textContent = j.log || '(no output yet)';
    box.scrollTop = box.scrollHeight;
    if (j.running) { setTimeout(pollDeploy, 1500); }
    else { box.textContent += `\\n\\n--- finished (exit ${j.rc}) ---`; box.scrollTop = box.scrollHeight; fetchVersions(); }
  } catch (e) { box.textContent += '\\npoll error: ' + e; }
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
    const bv = VERS.boxes[m.name] || {};
    const bstr = fmtVer(bv) || '—';
    const gstr = fmtVer(VERS.gateway);
    const stale = gstr && bv.version && (bv.version !== VERS.gateway.version || bv.source !== VERS.gateway.source);
    card.innerHTML = `
      <div class="name">${m.name}</div>
      <div class="signals">
        <div class="sig"><span class="dot ${hs}"></span>Computer: ${m.host_up ? 'online' : 'offline'}</div>
        <div class="sig"><span class="dot ${ss}"></span>seafront: ${m.service_up ? 'running' : 'down'}</div>
      </div>
      <div class="meta">${m.host}:${m.proxy_port}</div>
      <div class="meta">seafront ${bstr}${stale ? ' <span class="stale">(gateway has ' + gstr + ')</span>' : ''}</div>
      <div class="btns">
        <a class="open ${m.service_up ? '' : 'down'}" href="${url}">${m.service_up ? 'Open' : 'Offline'}</a>
        <button onclick="act('${m.name}','restart-service')">Restart service</button>
        <button onclick="view('${m.name}','config')">View config</button>
        <button onclick="view('${m.name}','logs')">View logs</button>
        <button style="grid-column:1/3" class="${stale ? 'hot' : ''}" onclick="updateBox('${m.name}')">
          Update${gstr ? ' → ' + gstr : ''}</button>
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
fetchVersions();
setInterval(refresh, 5000);
setInterval(fetchVersions, 30000);   // versions need SSH per box — poll gently
</script>
</body>
</html>"""
