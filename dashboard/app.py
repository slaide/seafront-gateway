"""Microscope gateway dashboard.

A small FastAPI app that serves a landing page listing every microscope on the
LAN and, on demand, health-checks each one's seafront server. Runs in parallel
with Caddy (which reverse-proxies gateway:<proxy_port> -> <microscope>:8000).

Config comes from ../config/microscopes.json (shared with the Caddyfile
generator), so there is one source of truth.
"""
import asyncio
import json
import pathlib

import httpx
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG = json.loads((ROOT / "config" / "microscopes.json").read_text())
MICROSCOPES = CONFIG["microscopes"]

app = FastAPI(title="Microscope Gateway")


async def _check(client: httpx.AsyncClient, m: dict) -> dict:
    """Health-check one microscope's seafront server (any HTTP reply == online)."""
    url = f"http://{m['host']}:{m['seafront_port']}/"
    online = False
    try:
        r = await client.get(url, timeout=1.5)
        online = r.status_code < 500
    except Exception:
        online = False
    return {
        "name": m["name"],
        "host": m["host"],
        "proxy_port": m["proxy_port"],
        "online": online,
    }


@app.get("/api/microscopes")
async def microscopes() -> JSONResponse:
    return JSONResponse(MICROSCOPES)


@app.get("/api/status")
async def status() -> JSONResponse:
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*[_check(client, m) for m in MICROSCOPES])
    return JSONResponse(list(results))


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
          grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 12px;
          padding: 18px; display: flex; flex-direction: column; gap: 10px; }
  .name { font-size: 1.1rem; font-weight: 600; display: flex; align-items: center; gap: 8px; }
  .dot { width: 10px; height: 10px; border-radius: 50%; background: #6e7681; flex: none; }
  .dot.online { background: #3fb950; box-shadow: 0 0 8px #3fb95088; }
  .dot.offline { background: #f85149; }
  .meta { color: #8b949e; font-size: .82rem; font-family: ui-monospace, monospace; }
  a.open { margin-top: auto; text-align: center; text-decoration: none;
           background: #238636; color: #fff; padding: 9px; border-radius: 8px;
           font-weight: 600; }
  a.open.down { background: #30363d; color: #8b949e; pointer-events: none; }
  footer { color: #6e7681; font-size: .8rem; padding: 0 24px 24px; }
</style>
</head>
<body>
<header>
  <h1>🔬 Microscope Gateway</h1>
  <div class="sub">Select a microscope to open its control interface. Auto-refreshing.</div>
</header>
<div class="grid" id="grid"></div>
<footer id="foot">loading…</footer>
<script>
async function refresh() {
  let data;
  try { data = await (await fetch('/api/status')).json(); }
  catch (e) { document.getElementById('foot').textContent = 'gateway unreachable'; return; }
  const grid = document.getElementById('grid');
  grid.innerHTML = '';
  for (const m of data) {
    const url = `http://${location.hostname}:${m.proxy_port}/`;
    const state = m.online ? 'online' : 'offline';
    const card = document.createElement('div');
    card.className = 'card';
    card.innerHTML = `
      <div class="name"><span class="dot ${state}"></span>${m.name}</div>
      <div class="meta">${m.host}:${m.proxy_port} &middot; ${state}</div>
      <a class="open ${m.online ? '' : 'down'}" href="${url}">
        ${m.online ? 'Open' : 'Offline'}</a>`;
    grid.appendChild(card);
  }
  const up = data.filter(m => m.online).length;
  document.getElementById('foot').textContent =
    `${up}/${data.length} online · updated ${new Date().toLocaleTimeString()}`;
}
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>"""
