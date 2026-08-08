"""Loopback-only onboarding UI for provider and app setup."""

from __future__ import annotations

import secrets
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse

from zhunt.auth import ensure_master_key, save_env_value
from zhunt.installer import Installer, InstallerError
from zhunt.providers import ProviderError, get_provider, provider_specs, validate_provider_key


def create_onboarding_app(
    *,
    home: Path | None = None,
    setup_token: str | None = None,
    validator=validate_provider_key,
) -> FastAPI:
    """Create the local setup app; mutating routes require a one-time token."""

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.home = (home or Path.home()).expanduser().resolve()
    app.state.setup_token = setup_token or secrets.token_urlsafe(24)
    app.state.validator = validator

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return _HTML

    @app.get("/api/providers")
    async def providers() -> list[dict[str, str]]:
        return [
            {
                "id": provider.id,
                "name": provider.name,
                "base_url": provider.base_url,
                "docs_url": provider.docs_url,
            }
            for provider in provider_specs()
        ]

    @app.post("/api/validate")
    async def validate(
        request: Request,
        x_zhunt_setup_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _check_token(request, x_zhunt_setup_token)
        body = await _json_body(request)
        provider_id = _string(body, "provider")
        api_key = _string(body, "api_key")
        try:
            provider = get_provider(provider_id)
            model_count = app.state.validator(provider_id, api_key)
        except ProviderError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"provider": provider.id, "name": provider.name, "models": model_count}

    @app.post("/api/configure")
    async def configure(
        request: Request,
        x_zhunt_setup_token: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _check_token(request, x_zhunt_setup_token)
        body = await _json_body(request)
        provider_id = _string(body, "provider")
        api_key = _string(body, "api_key")
        apps = body.get("apps", [])
        mode = body.get("mode", "api")
        if not isinstance(apps, list) or not all(isinstance(item, str) for item in apps):
            raise HTTPException(status_code=400, detail="apps must be a list of names")
        if mode not in {"api", "passthrough"}:
            raise HTTPException(status_code=400, detail="mode must be api or passthrough")
        try:
            provider = get_provider(provider_id)
            model_count = app.state.validator(provider_id, api_key)
            env_path = app.state.home / ".zhunt" / "env"
            ensure_master_key(env_path)
            save_env_value("ZHUNT_PROVIDER", provider.id, env_path)
            save_env_value(provider.key_env, api_key.strip(), env_path)
            results = [
                Installer(home=app.state.home).install(
                    app_name,
                    mode=mode,
                    base_url="http://127.0.0.1:4000",
                )
                for app_name in apps
            ]
        except (ProviderError, InstallerError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {
            "provider": provider.id,
            "models": model_count,
            "apps": [result.app for result in results],
        }

    return app


def _check_token(request: Request, supplied: str | None) -> None:
    if not secrets.compare_digest(supplied or "", request.app.state.setup_token):
        raise HTTPException(status_code=403, detail="invalid setup token")


async def _json_body(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except ValueError as error:
        raise HTTPException(status_code=400, detail="request body must be JSON") from error
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="request body must be an object")
    return body


def _string(body: dict[str, Any], name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=400, detail=f"{name} is required")
    return value


_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Zhunt setup</title>
<style>body{font:16px system-ui;max-width:680px;margin:3rem auto;padding:0 1rem;color:#202124}label{display:block;margin:1rem 0 .35rem;font-weight:600}select,input,button{font:inherit;padding:.65rem;border:1px solid #bbb;border-radius:.4rem;width:100%;box-sizing:border-box}button{margin-top:1.25rem;background:#222;color:white;cursor:pointer}.actions{display:flex;gap:.6rem}.actions button{flex:1}.actions .secondary{background:white;color:#202124}.apps{display:grid;grid-template-columns:repeat(2,1fr);gap:.4rem}.apps label{font-weight:400;margin:0}.apps input{width:auto}.warning{padding:.8rem;background:#fff3cd;border:1px solid #e0b84c;border-radius:.4rem}.status{margin-top:1rem;min-height:1.5rem}.closed{max-width:680px;margin:3rem auto;padding:0 1rem}</style></head>
<body><h1>Set up Zhunt</h1><p>Your key is sent only to this local setup page and saved in <code>~/.zhunt/env</code>.</p>
<label for="provider">Provider</label><select id="provider"></select>
<label for="key">API key</label><input id="key" type="password" autocomplete="off" placeholder="Paste your provider key">
<p class="warning"><strong>Billing warning:</strong> selected apps use API mode and are billed per token. Claude Code traffic does not use Claude Max. Do not select Claude Code if you intend to stay on a flat-rate Claude Max plan.</p>
<label>Apps to configure</label><div class="apps"><label><input type="checkbox" value="hermes"> Hermes</label><label><input type="checkbox" value="claude"> Claude Code</label><label><input type="checkbox" value="codex"> Codex</label><label><input type="checkbox" value="cursor"> Cursor</label><label><input type="checkbox" value="vscode"> VS Code</label></div>
<div class="actions"><button id="save">Validate key and configure</button><button id="close" class="secondary" type="button">Close setup</button></div><div class="status" id="status"></div>
<script>
const token=new URLSearchParams(location.search).get('token')||'';
const headers={'Content-Type':'application/json','X-Zhunt-Setup-Token':token};
const status=document.getElementById('status');
fetch('/api/providers').then(r=>r.json()).then(items=>{for(const p of items){const o=document.createElement('option');o.value=p.id;o.textContent=p.name;document.getElementById('provider').append(o)}});
document.getElementById('close').onclick=()=>{document.body.innerHTML='<main class="closed"><h1>Setup closed</h1><p>You can close this tab. The local setup process remains in your terminal until you stop it.</p></main>';window.close()};
document.getElementById('save').onclick=async()=>{const apps=[...document.querySelectorAll('.apps input:checked')].map(x=>x.value);if(apps.includes('claude')&&!confirm('Claude Code will use pay-per-token API billing and will not use Claude Max. Continue?'))return;status.textContent='Validating…';const body={provider:document.getElementById('provider').value,api_key:document.getElementById('key').value,apps,mode:'api'};const r=await fetch('/api/configure',{method:'POST',headers,body:JSON.stringify(body)});const d=await r.json();status.textContent=r.ok?`Configured ${d.apps.length} app(s); ${d.models} provider models available.`:(d.detail||'Setup failed')};
</script></body></html>"""
