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

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config" / "microscopes.json").read_text())
MICROSCOPES = CONFIG["microscopes"]
BY_NAME = {m["name"]: m for m in MICROSCOPES}

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
    card.innerHTML = `
      <div class="name">${m.name}</div>
      <div class="signals">
        <div class="sig"><span class="dot ${hs}"></span>Computer: ${m.host_up ? 'online' : 'offline'}</div>
        <div class="sig"><span class="dot ${ss}"></span>seafront: ${m.service_up ? 'running' : 'down'}</div>
      </div>
      <div class="meta">${m.host}:${m.proxy_port}</div>
      <div class="btns">
        <a class="open ${m.service_up ? '' : 'down'}" href="${url}">${m.service_up ? 'Open' : 'Offline'}</a>
        <button onclick="act('${m.name}','restart-service')">Restart service</button>
        <button onclick="view('${m.name}','config')">View config</button>
        <button onclick="view('${m.name}','logs')">View logs</button>
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
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>"""
