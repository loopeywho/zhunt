"""Loopback-only status dashboard for the local Zhunt daemon."""

from __future__ import annotations

import json
import secrets
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse

from zhunt.registry import ModelRegistry, RegistryError
from zhunt.telemetry import summarize_telemetry


def create_dashboard_app(
    *,
    home: Path | None = None,
    dashboard_token: str | None = None,
    telemetry_path: Path | None = None,
    registry_path: Path | None = None,
    provider_id: str | None = None,
    daemon_host: str = "127.0.0.1",
    daemon_port: int = 4000,
) -> FastAPI:
    """Create a token-protected, loopback-oriented dashboard application."""

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.home = (home or Path.home()).expanduser().resolve()
    app.state.dashboard_token = dashboard_token or secrets.token_urlsafe(24)
    app.state.telemetry_path = (
        telemetry_path or app.state.home / ".zhunt" / "telemetry.jsonl"
    ).expanduser()
    app.state.registry_path = registry_path
    app.state.provider_id = provider_id or _configured_provider(app.state.home)
    app.state.daemon_host = daemon_host
    app.state.daemon_port = daemon_port

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return _HTML

    @app.get("/api/status")
    async def status(
        request: Request,
        x_zhunt_dashboard_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _check_token(request, x_zhunt_dashboard_token)
        return _status_payload(request.app)

    return app


def _status_payload(app: FastAPI) -> dict[str, Any]:
    telemetry_path: Path = app.state.telemetry_path
    summary = summarize_telemetry(telemetry_path)
    try:
        registry = (
            ModelRegistry.from_path(
                app.state.registry_path,
                provider_id=app.state.provider_id,
            )
            if app.state.registry_path is not None
            else ModelRegistry.default(provider_id=app.state.provider_id)
        )
        models = [
            {
                "tier": tier.value,
                "model": model.model,
                "input_cost": float(model.input_cost),
                "output_cost": float(model.output_cost),
                "ttft_ms": float(model.ttft_ms) if model.ttft_ms is not None else None,
            }
            for tier, model in registry.models_by_tier()
        ]
        registry_error = None
    except RegistryError as error:
        models = []
        registry_error = str(error)

    return {
        "provider": app.state.provider_id,
        "daemon": {
            "host": app.state.daemon_host,
            "port": app.state.daemon_port,
            "reachable": _daemon_reachable(
                app.state.daemon_host,
                app.state.daemon_port,
            ),
        },
        "summary": summary,
        "last_request": _last_request(telemetry_path),
        "models": models,
        "registry_error": registry_error,
    }


def _daemon_reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.25):
            return True
    except OSError:
        return False


def _last_request(path: Path) -> str | None:
    if not path.is_file():
        return None
    for line in reversed(path.read_text(encoding="utf-8").splitlines()):
        try:
            event = json.loads(line)
            if event.get("event") != "request":
                continue
            timestamp = datetime.fromisoformat(str(event["timestamp"]))
            return timestamp.astimezone(timezone.utc).isoformat()
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return None


def _configured_provider(home: Path) -> str | None:
    path = home / ".zhunt" / "env"
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        name, separator, value = line.partition("=")
        if separator and name == "ZHUNT_PROVIDER":
            return value.strip().strip('"').strip("'") or None
    return None


def _check_token(request: Request, supplied: str | None) -> None:
    if not secrets.compare_digest(supplied or "", request.app.state.dashboard_token):
        raise HTTPException(status_code=403, detail="invalid dashboard token")


_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Zhunt dashboard</title>
<style>
:root{color-scheme:light;--ink:#202124;--muted:#69707d;--line:#e5e7eb;--ok:#117a4b;--bad:#b42318}
*{box-sizing:border-box}body{font:15px system-ui,-apple-system,sans-serif;max-width:1080px;margin:0 auto;padding:2rem 1.2rem;color:var(--ink);background:#fbfbfc}
h1{margin:.2rem 0 .35rem;font-size:2rem}h2{margin:0 0 .8rem;font-size:1.1rem}.muted{color:var(--muted)}
.top{display:flex;justify-content:space-between;align-items:flex-start;gap:1rem;margin-bottom:1.5rem}.pill{border:1px solid var(--line);border-radius:999px;padding:.4rem .75rem;font-weight:650;white-space:nowrap}.ok{color:var(--ok)}.bad{color:var(--bad)}
.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:.75rem;margin-bottom:1.2rem}.card{background:white;border:1px solid var(--line);border-radius:.8rem;padding:1rem;box-shadow:0 2px 10px #20212408}.label{font-size:.78rem;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}.value{font-size:1.45rem;font-weight:700;margin-top:.25rem}
section{margin:1.2rem 0}.table{width:100%;border-collapse:collapse;background:white;border:1px solid var(--line);border-radius:.8rem;overflow:hidden}.table th,.table td{text-align:left;padding:.65rem .75rem;border-bottom:1px solid var(--line);font-size:.9rem}.table th{color:var(--muted);font-weight:600;background:#f6f7f8}.table tr:last-child td{border-bottom:0}.note{background:#fff8e1;border:1px solid #ecd58b;border-radius:.65rem;padding:.75rem;color:#6d5300}.error{color:var(--bad)}
@media(max-width:760px){.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.top{display:block}.pill{display:inline-block;margin-top:.75rem}.table{display:block;overflow-x:auto;white-space:nowrap}}
</style></head>
<body><div class="top"><div><p class="muted">Local-only Zhunt</p><h1>Routing dashboard</h1><p class="muted">No prompts, responses, or API keys are shown here.</p></div><div id="health" class="pill">Checking...</div></div>
<div class="grid"><div class="card"><div class="label">Provider</div><div id="provider" class="value">-</div></div><div class="card"><div class="label">Requests today</div><div id="requests" class="value">-</div></div><div class="card"><div class="label">Actual spend</div><div id="actual" class="value">-</div></div><div class="card"><div class="label">Estimated savings</div><div id="savings" class="value">-</div></div></div>
<section><h2>Today</h2><p id="counterfactual" class="muted">Loading counterfactual top-model spend...</p><p id="last" class="muted"></p></section>
<section><h2>Configured models</h2><div id="registry" class="muted">Loading...</div></section>
<section><h2>By wire protocol</h2><div id="dialects" class="muted">Loading...</div></section>
<p class="note">Savings are estimates based on local registry prices and the counterfactual top-model calculation. Refresh prices with <code>zhunt sync</code> before citing them publicly.</p>
<script>
const token=new URLSearchParams(location.search).get('token')||'';
const money=n=>'$'+Number(n||0).toFixed(6);
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function refresh(){try{const r=await fetch('/api/status',{headers:{'X-Zhunt-Dashboard-Token':token}});if(!r.ok)throw new Error('Dashboard authorization failed');const d=await r.json();const s=d.summary||{};const healthy=d.daemon&&d.daemon.reachable;const h=document.getElementById('health');h.textContent=healthy?'● Zhunt Active':'● Zhunt Offline';h.className='pill '+(healthy?'ok':'bad');document.getElementById('provider').textContent=d.provider||'Not configured';document.getElementById('requests').textContent=s.requests||0;document.getElementById('actual').textContent=money(s.actual_spend);document.getElementById('savings').textContent=money(s.savings);document.getElementById('counterfactual').textContent='Counterfactual top-model spend: '+money(s.counterfactual_spend);document.getElementById('last').textContent=d.last_request?'Last request: '+new Date(d.last_request).toLocaleString():'No request telemetry yet';if(d.registry_error){document.getElementById('registry').innerHTML='<p class="error">'+esc(d.registry_error)+'</p>';}else{let rows=(d.models||[]).map(m=>'<tr><td>'+esc(m.tier)+'</td><td>'+esc(m.model)+'</td><td>$'+Number(m.input_cost).toFixed(4)+'</td><td>$'+Number(m.output_cost).toFixed(4)+'</td></tr>').join('');document.getElementById('registry').innerHTML='<table class="table"><thead><tr><th>Tier</th><th>Model</th><th>Input / 1M</th><th>Output / 1M</th></tr></thead><tbody>'+rows+'</tbody></table>';}let dialectRows=Object.entries(s.by_wire_dialect||{}).map(([k,v])=>'<tr><td>'+esc(k)+'</td><td>'+v.requests+'</td><td>'+money(v.actual_spend)+'</td><td>'+money(v.counterfactual_spend)+'</td></tr>').join('');document.getElementById('dialects').innerHTML=dialectRows?'<table class="table"><thead><tr><th>Protocol</th><th>Requests</th><th>Actual</th><th>Counterfactual</th></tr></thead><tbody>'+dialectRows+'</tbody></table>':'<p class="muted">No requests yet.</p>';}catch(e){const h=document.getElementById('health');h.textContent='● Dashboard unavailable';h.className='pill bad';document.getElementById('last').textContent=e.message;}}
refresh();setInterval(refresh,5000);
</script></body></html>"""
