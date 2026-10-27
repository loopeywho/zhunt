"""Workspace HTTP API (docs/WORKSPACE_API.md): status, decisions, compare, layouts, themes, plan."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import secrets
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from zhunt.auth import remove_env_value, save_env_value
from zhunt import images, llm
from zhunt.agent import briefs
from zhunt.agent.browser import find_browser
from zhunt.agent.runner import BROWSER_KEY_TOOLS, AgentRunner, get_keys_goal, parse_interval
from zhunt.agent import email_tools
from zhunt.agent.tools import AgentSettings, ToolError
from zhunt.catalog_feed import UPDATES_ENV
from zhunt.engine import DECISION_LOG_SIZE, EngineProxyHook
from zhunt.key_prefix import KEY_PLATFORMS, detect_platform
from zhunt.licence import Plan, current_plan
from zhunt.server import _authenticate_local_request

MAX_LAYOUT_BYTES = 64 * 1024
# Team chat bots: plain JSON files, one per bot, editable by hand.
BOT_ALIASES = ("zhunt-auto", "zhunt-chat", "zhunt-coding", "zhunt-long-context", "zhunt-reasoning")
MAX_BOT_INSTRUCTIONS = 4000
_COLOR = re.compile(r"#[0-9a-fA-F]{6}")
MAX_AVATAR_BYTES = 2 * 1024 * 1024
MAX_WALLPAPER_BYTES = 6 * 1024 * 1024
_DATA_URL = re.compile(r"data:(image/(?:jpeg|png|webp));base64,([A-Za-z0-9+/=]+)")
_AVATAR_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
MAX_COMPARE_MODELS = 4
MAX_COMPARE_BYTES = 1024 * 1024
# Voice input: Groq's free Whisper, on the user's own Groq key.
TRANSCRIBE_MODEL = "groq/whisper-large-v3-turbo"
MAX_AUDIO_BYTES = 10 * 1024 * 1024
_AUDIO_TYPES = {
    "audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "m4a",
    "audio/mpeg": "mp3", "audio/wav": "wav", "audio/x-wav": "wav",
}
_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
# API keys are printable ASCII without spaces; anything else is a paste mistake.
_KEY = re.compile(r"[\x21-\x7e]{8,512}")


@dataclass(frozen=True)
class WorkspaceDirs:
    """Where the workspace keeps user files; ``static`` is the packaged UI (may be absent)."""

    layouts: Path = field(default_factory=lambda: Path.home() / ".zhunt" / "layouts")
    env_file: Path = field(default_factory=lambda: Path.home() / ".zhunt" / "env")
    bots: Path = field(default_factory=lambda: Path.home() / ".zhunt" / "bots")
    # a personal wallpaper (AI-generated or uploaded): <wallpaper>.jpg|png|webp
    wallpaper: Path = field(default_factory=lambda: Path.home() / ".zhunt" / "wallpaper")
    agent: Path = field(default_factory=lambda: Path.home() / ".zhunt" / "agent.json")
    themes: Path = field(default_factory=lambda: Path.home() / ".zhunt" / "themes")
    static: Path = Path(__file__).parent / "workspace"


# The workspace needs nothing but its own files and this daemon. Locking everything
# else down also stops a downloaded user theme from beaconing out via url().
_WORKSPACE_STYLE_NONCE = secrets.token_urlsafe(32)
_WORKSPACE_CSP = (
    f"default-src 'none'; script-src 'self'; style-src 'self' 'nonce-{_WORKSPACE_STYLE_NONCE}'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
)


class _WorkspaceHeaders:
    """Add the CSP and friends to /workspace responses.

    Plain ASGI on purpose: Starlette's BaseHTTPMiddleware deadlocks with the replay
    middleware that wraps this app and calls it more than once per client request.
    """

    _HEADERS = (
        (b"content-security-policy", _WORKSPACE_CSP.encode()),
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"no-referrer"),
        # revalidate every load (a 304 when unchanged), so an updated zhunt never runs old modules
        (b"cache-control", b"no-cache"),
    )

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not scope["path"].startswith("/workspace"):
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Any) -> None:
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), *self._HEADERS]}
            await send(message)

        await self.app(scope, receive, send_with_headers)


def mount_workspace(app: FastAPI, hook: EngineProxyHook, dirs: WorkspaceDirs) -> AgentRunner:
    from zhunt.engineering import mount_engineering
    mount_engineering(app, dirs.agent.parent, _WORKSPACE_STYLE_NONCE)
    app.add_middleware(_WorkspaceHeaders)
    runner = AgentRunner(dirs.agent)
    # briefs are made on their schedule while the daemon runs, and saved tasks and loops pick up
    # where a restart left them (the lifespan starts these jobs)
    app.state.background = [*getattr(app.state, "background", ()), runner.run_brief_schedule, runner.resume]
    app.state.agent_runner = runner
    app.include_router(build_router(hook, dirs, runner=runner))
    if dirs.static.is_dir():  # the UI ships in a separate change
        app.mount(
            "/workspace",
            StaticFiles(directory=dirs.static, html=True),
            name="workspace",
        )
    return runner


def build_router(
    hook: EngineProxyHook, dirs: WorkspaceDirs, plan: Callable[[], Plan] | None = None, runner: AgentRunner | None = None
) -> APIRouter:
    runner = runner or AgentRunner(dirs.agent)
    # Read on every request, so `zhunt licence activate` takes effect without a restart.
    plan = plan or (lambda: current_plan())
    runner.plan = plan

    def save_found_key(key: str) -> str:
        """Save a key the agent found on a provider's page (it never passes through the model)."""

        provider = hook.catalog.providers.get(detect_platform(key) or "")
        if provider is None:
            raise ToolError("zhunt does not know which provider this key is for; paste it in Connect instead")
        save_env_value(provider.key_env, key.strip(), dirs.env_file)
        hook.reload_keys()
        return provider.name

    runner.save_key = save_found_key
    router = APIRouter(
        prefix="/zhunt/api", dependencies=[Depends(_authenticate_local_request)]
    )

    @router.get("/status")
    def status() -> dict[str, Any]:
        return hook.status()

    @router.post("/keys")
    async def add_key(request: Request) -> dict[str, Any]:
        """Save a provider key pasted in the Connect window. It goes to ~/.zhunt/env
        (owner-only) and the environment of this process; it is never echoed back.
        With no ``platform`` the provider is told from the key's prefix."""

        body = await _json_body(request, limit=4096)
        platform = body.get("platform") if isinstance(body, dict) else None
        key = body.get("key") if isinstance(body, dict) else None
        if not isinstance(key, str) or not _KEY.fullmatch(key.strip()):
            raise HTTPException(400, "that does not look like an API key (one line, no spaces)")
        if platform is None:
            platform = detect_platform(key)
            if platform not in hook.catalog.providers:
                raise HTTPException(
                    422, "zhunt cannot tell which provider this key is for; choose the provider"
                )
        provider = hook.catalog.providers.get(platform) if isinstance(platform, str) else None
        if provider is None:
            raise HTTPException(404, "unknown provider")
        save_env_value(provider.key_env, key.strip(), dirs.env_file)
        hook.reload_keys()
        return {"platform": provider.platform, "name": provider.name, "key_set": True}

    @router.post("/keys/detect")
    async def detect_key(request: Request) -> dict[str, Any]:
        """Which provider a key (say, one on the clipboard) is for, without saving it."""

        body = await _json_body(request, limit=4096)
        key = body.get("key") if isinstance(body, dict) else None
        provider = hook.catalog.providers.get(detect_platform(key) or "") if isinstance(key, str) and _KEY.fullmatch(key.strip()) else None
        if provider is None:
            return {"platform": None}
        return {"platform": provider.platform, "name": provider.name,
                "key_set": bool(hook.selector.key_ids(provider.platform))}

    @router.put("/policy/training")
    async def set_training(request: Request) -> dict[str, Any]:
        """Allow (or stop) providers that may train on prompts; saved like a key."""

        body = await _json_body(request, limit=256)
        allow = body.get("allow") if isinstance(body, dict) else None
        if not isinstance(allow, bool):
            raise HTTPException(400, "send {\"allow\": true} or {\"allow\": false}")
        save_env_value("ZHUNT_ALLOW_TRAINING", "1" if allow else "0", dirs.env_file)
        hook.set_allow_training(allow)
        return {"allow_training": allow}

    def _feed() -> Any:
        if hook.catalog_feed is None:
            raise HTTPException(409, "zhunt was started with --catalog, so it uses that file as given")
        return hook.catalog_feed

    @router.get("/catalog")
    def catalog_status() -> dict[str, Any]:
        """Which catalog is in use and whether updates are on. Never fetches."""

        if hook.catalog_feed is None:
            return {"active": {"source": "file", "version": 0}, "updates": None}
        return hook.catalog_feed.status()

    @router.post("/catalog/update")
    async def catalog_update() -> dict[str, Any]:
        """Check the feed once, now (the user asked): Pro gets latest, Free the monthly snapshot."""

        feed = _feed()
        result = await asyncio.to_thread(feed.update_now)
        return {"status": result.status, "message": result.message, **feed.status()}

    @router.put("/catalog/updates")
    async def set_catalog_updates(request: Request) -> dict[str, Any]:
        """The Connect window's switch: let the daemon check the latest catalog every 12 hours."""

        feed = _feed()
        body = await _json_body(request, limit=256)
        enabled = body.get("enabled") if isinstance(body, dict) else None
        if not isinstance(enabled, bool):
            raise HTTPException(400, "send {\"enabled\": true} or {\"enabled\": false}")
        if enabled and not hook.current_plan().has("catalog_live"):
            raise HTTPException(
                403, "automatic catalog updates are part of zhunt Pro; `zhunt catalog update` fetches the monthly snapshot"
            )
        save_env_value(UPDATES_ENV, "1" if enabled else "0", dirs.env_file)
        if not enabled:
            return feed.status()
        result = await asyncio.to_thread(feed.update_now)
        return {"status": result.status, "message": result.message, **feed.status()}

    @router.delete("/keys/{platform}")
    def remove_key(platform: str) -> dict[str, Any]:
        provider = hook.catalog.providers.get(platform)
        if provider is None:
            raise HTTPException(404, "unknown provider")
        remove_env_value(provider.key_env, dirs.env_file)
        hook.reload_keys()
        return {"platform": provider.platform, "key_set": False}

    @router.post("/transcribe")
    async def transcribe(request: Request) -> dict[str, Any]:
        """Speech to text for the chat microphone. Audio goes to Groq, nowhere else."""

        key = os.environ.get("GROQ_API_KEY")
        if not key:
            raise HTTPException(409, "voice input uses Groq's free Whisper: connect a Groq key first")
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        ext = _AUDIO_TYPES.get(ctype)
        if ext is None:
            raise HTTPException(415, "send webm, ogg, m4a, mp3 or wav audio")
        if int(request.headers.get("content-length") or 0) > MAX_AUDIO_BYTES:
            raise HTTPException(413, "recording too long (10 MB maximum)")
        audio = await request.body()
        if not audio:
            raise HTTPException(400, "no audio received")
        if len(audio) > MAX_AUDIO_BYTES:
            raise HTTPException(413, "recording too long (10 MB maximum)")
        try:
            result = await llm.atranscription(
                model=TRANSCRIBE_MODEL, file=(f"speech.{ext}", audio, ctype),
                api_key=key, max_retries=0, timeout=60,
            )
        except llm.RateLimitError:
            raise HTTPException(429, "Groq's free voice limit is reached; try again shortly") from None
        except Exception as error:  # never let provider text (which may echo the key) through unchecked
            detail = str(error).replace(key, "***")[:200]
            raise HTTPException(502, f"transcription failed: {detail}") from None
        return {"text": (getattr(result, "text", "") or "").strip()}

    @router.get("/plan")
    def get_plan() -> dict[str, Any]:
        return plan().to_api()

    @router.get("/decisions")
    def decisions(
        limit: int = Query(50, ge=1, le=DECISION_LOG_SIZE),
        session: str | None = None,
    ) -> dict[str, Any]:
        return {"decisions": hook.decisions(limit=limit, session=session)}

    @router.post("/compare")
    async def compare(request: Request) -> dict[str, Any]:
        messages, model_ids = _compare_request(
            await _json_body(request, limit=MAX_COMPARE_BYTES)
        )
        known = {model.id for model in hook.catalog.models}
        unknown = [mid for mid in model_ids if mid not in known]
        if unknown:
            raise HTTPException(404, f"unknown model: {', '.join(unknown)}")
        return {"results": await hook.compare(messages, model_ids)}

    @router.get("/layouts")
    def list_layouts() -> dict[str, Any]:
        return {"layouts": _names(dirs.layouts, ".json")}

    @router.get("/layouts/{name}")
    def get_layout(name: str) -> Any:
        path = _path(dirs.layouts, name, ".json")
        try:
            if path.is_symlink():
                raise FileNotFoundError(name)
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise HTTPException(404, f"no layout named {name!r}") from None
        except ValueError:
            raise HTTPException(500, f"layout {name!r} is not valid JSON") from None

    @router.put("/layouts/{name}")
    async def put_layout(name: str, request: Request) -> dict[str, Any]:
        path = _path(dirs.layouts, name, ".json")
        body = await _json_body(request, limit=MAX_LAYOUT_BYTES)
        if not isinstance(body, dict):
            raise HTTPException(400, "layout must be a JSON object")
        _write_private(path, json.dumps(body, indent=2).encode())
        return {"name": name}

    @router.delete("/layouts/{name}")
    def delete_layout(name: str) -> dict[str, Any]:
        try:
            _path(dirs.layouts, name, ".json").unlink()
        except FileNotFoundError:
            raise HTTPException(404, f"no layout named {name!r}") from None
        return {"name": name}

    @router.get("/bots")
    def list_bots() -> dict[str, Any]:
        bots = []
        for handle in _names(dirs.bots, ".json"):
            try:
                bot = _bot(handle, json.loads((dirs.bots / f"{handle}.json").read_text(encoding="utf-8")))
                bot["avatar"] = any((dirs.bots / f"{handle}{ext}").is_file() for ext in _AVATAR_EXT.values())
                bots.append(bot)
            except (ValueError, HTTPException):
                continue  # a hand-edited file that no longer parses is skipped, not fatal
        return {"bots": bots}

    @router.put("/bots/{handle}")
    async def put_bot(handle: str, request: Request) -> dict[str, Any]:
        path = _path(dirs.bots, handle, ".json")
        bot = _bot(handle, await _json_body(request, limit=16 * 1024))
        _write_private(path, json.dumps(bot, indent=2).encode())
        return bot

    @router.delete("/bots/{handle}")
    def delete_bot(handle: str) -> dict[str, Any]:
        try:
            _path(dirs.bots, handle, ".json").unlink()
        except FileNotFoundError:
            raise HTTPException(404, f"no bot named {handle!r}") from None
        _remove_avatar(handle)
        return {"handle": handle}

    # ---- AI pictures (Cloudflare Workers AI, the user's own free account) ----

    @router.get("/images/status")
    def images_status() -> dict[str, Any]:
        return {"connected": images.credentials() is not None, "provider": images.PROVIDER,
                "key_url": images.KEY_URL, "note": images.NOTE}

    @router.put("/images/connect")
    async def images_connect(request: Request) -> dict[str, Any]:
        body = await _json_body(request, limit=2048)
        account = str(body.get("account_id", "")).strip() if isinstance(body, dict) else ""
        token = str(body.get("token", "")).strip() if isinstance(body, dict) else ""
        if not images.valid_account(account):
            raise HTTPException(400, "the account ID is 32 letters and digits (Cloudflare dashboard, right-hand side)")
        if not images.valid_token(token):
            raise HTTPException(400, "that does not look like a Cloudflare API token")
        save_env_value(images.ACCOUNT_ENV, account, dirs.env_file)
        save_env_value(images.TOKEN_ENV, token, dirs.env_file)
        return {"connected": True}

    @router.delete("/images/connect")
    def images_disconnect() -> dict[str, Any]:
        remove_env_value(images.ACCOUNT_ENV, dirs.env_file)
        remove_env_value(images.TOKEN_ENV, dirs.env_file)
        return {"connected": False}

    @router.post("/images")
    async def make_image(request: Request) -> dict[str, Any]:
        body = await _json_body(request, limit=4096)
        prompt = body.get("prompt") if isinstance(body, dict) else None
        if not isinstance(prompt, str):
            raise HTTPException(400, "send {\"prompt\": \"...\"}")
        try:
            data = await images.generate(prompt)
        except images.ImageError as error:
            raise HTTPException(error.status, str(error)) from None
        return {"image": "data:image/jpeg;base64," + base64.b64encode(data).decode()}

    # ---- agent ----

    @router.get("/agent")
    def agent_state() -> dict[str, Any]:
        tools = runner.tools()
        return {
            "folders": runner.settings().to_json()["folders"],
            "tools": [{"name": t.name, "risk": t.risk, "description": t.description} for t in tools.values()],
            "tasks": [runner.tasks[i].summary() for i in reversed(runner.order) if i in runner.tasks],
            "loops": [loop.summary() for loop in runner.loops.values()],
        }

    @router.put("/agent/folders")
    async def agent_folders(request: Request) -> dict[str, Any]:
        body = await _json_body(request, limit=8192)
        raw = body.get("folders") if isinstance(body, dict) else None
        if not isinstance(raw, list) or len(raw) > 10 or not all(isinstance(f, str) for f in raw):
            raise HTTPException(400, "send {\"folders\": [\"/path\", ...]} (at most 10)")
        folders = []
        for item in raw:
            path = Path(item).expanduser().resolve()
            if not path.is_dir():
                raise HTTPException(400, f"{item!r} is not a folder on this computer")
            if path == Path(path.anchor) or path == Path.home().resolve():
                raise HTTPException(400, "share a specific folder, not the whole disk or your entire home folder")
            folders.append(path)
        _write_private(dirs.agent, json.dumps(AgentSettings(folders=folders).to_json(), indent=2).encode())
        return {"folders": [str(f) for f in folders]}

    @router.post("/agent/tasks")
    async def agent_start(request: Request) -> dict[str, Any]:
        goal, mode, tools = _agent_request(await _json_body(request, limit=16 * 1024), runner)
        return runner.start(goal, mode=mode, tools=tools).detail()

    @router.get("/agent/tasks/{task_id}")
    def agent_task(task_id: str, since: int = Query(0, ge=0)) -> dict[str, Any]:
        task = runner.tasks.get(task_id)
        if task is None:
            raise HTTPException(404, "no such task")
        return task.detail(since)

    @router.post("/agent/tasks/{task_id}/decide")
    async def agent_decide(task_id: str, request: Request) -> dict[str, Any]:
        body = await _json_body(request, limit=1024)
        if not isinstance(body, dict) or not isinstance(body.get("call_id"), str) or not isinstance(body.get("approve"), bool):
            raise HTTPException(400, "send {\"call_id\": \"...\", \"approve\": true|false}")
        if not runner.decide(task_id, body["call_id"], body["approve"]):
            raise HTTPException(409, "nothing is waiting for that decision")
        return {"ok": True}

    @router.post("/agent/tasks/{task_id}/stop")
    def agent_stop(task_id: str) -> dict[str, Any]:
        task = runner.stop(task_id)
        if task is None:
            raise HTTPException(404, "no such task")
        return task.summary()

    @router.post("/agent/keys")
    async def agent_get_keys() -> dict[str, Any]:
        """Start the agent on getting free keys: it opens each provider's key page in its own
        browser, the user signs in there, it creates a key and saves it with browser_save_key."""

        if not find_browser():
            raise HTTPException(409, "this needs Google Chrome or Chromium installed")
        wanted = [p for p in hook.status()["providers"]
                  if p["platform"] in KEY_PLATFORMS and not p["key_set"] and not p.get("keyless")
                  and p.get("key_url") and any(m["free"] for m in p.get("models", []))]
        if not wanted:
            raise HTTPException(409, "every free provider zhunt can fetch a key from is already connected")
        return runner.start(get_keys_goal(wanted), tools=BROWSER_KEY_TOOLS).detail()

    @router.post("/agent/loops")
    async def agent_loop(request: Request) -> dict[str, Any]:
        body = await _json_body(request, limit=16 * 1024)
        every = parse_interval(str(body.get("every", ""))) if isinstance(body, dict) else None
        if every is None:
            raise HTTPException(400, "every must look like 30m, 2h or 1d")
        goal, mode, tools = _agent_request(body, runner)
        return runner.add_loop(every, goal, mode=mode, tools=tools).summary()

    @router.delete("/agent/loops/{loop_id}")
    def agent_unloop(loop_id: str) -> dict[str, Any]:
        if not runner.remove_loop(loop_id):
            raise HTTPException(404, "no such loop")
        return {"id": loop_id}

    @router.get("/agent/briefs")
    def agent_briefs() -> dict[str, Any]:
        data = runner.brief_store.load()
        return {"settings": data["settings"], "running": runner.brief_running, "memory": data["memory"],
                "email_connected": email_tools.EmailAccount.from_env() is not None,
                "scheduled_available": plan().has("scheduled_briefs"),
                "briefs": list(reversed(data["briefs"]))}

    @router.put("/agent/briefs/settings")
    async def agent_brief_settings(request: Request) -> dict[str, Any]:
        body = await _json_body(request, limit=2048)
        if not isinstance(body, dict):
            raise HTTPException(400, "send a JSON object")
        data = runner.brief_store.load()
        if "enabled" in body:
            if not isinstance(body["enabled"], bool):
                raise HTTPException(400, "enabled is true or false")
            if body["enabled"] and not plan().has("scheduled_briefs"):
                raise HTTPException(403, "scheduled briefs are part of zhunt Pro; /brief makes one now for free")
            if body["enabled"] and not data["settings"]["enabled"]:
                data["enabled_at"] = time.time()  # missed times before this are never caught up
            data["settings"]["enabled"] = body["enabled"]
        if "times" in body:
            times = briefs.valid_times(body["times"])
            if times is None:
                raise HTTPException(400, "times are 1 to 6 local times like \"08:00\"")
            data["settings"]["times"] = times
        runner.brief_store.save(data)
        return data["settings"]

    @router.post("/agent/briefs/run")
    def agent_brief_run() -> dict[str, Any]:
        if email_tools.EmailAccount.from_env() is None:
            raise HTTPException(409, "connect your email first")
        if not runner.start_brief():
            raise HTTPException(409, "a brief is already being made")
        return {"running": True}

    @router.post("/agent/briefs/{brief_id}/items/{item_id}/act")
    def agent_brief_act(brief_id: str, item_id: str) -> dict[str, Any]:
        try:
            task = runner.act_on_brief_item(brief_id, item_id)
        except ToolError as error:
            raise HTTPException(409, str(error)) from None
        if task is None:
            raise HTTPException(404, "no such brief item")
        return task.detail()

    @router.post("/agent/briefs/{brief_id}/items/{item_id}/dismiss")
    async def agent_brief_dismiss(brief_id: str, item_id: str, request: Request) -> dict[str, Any]:
        body = await _json_body(request, limit=1024)
        skip = body.get("skip") if isinstance(body, dict) else None
        if skip not in (None, "sender", "topic"):
            raise HTTPException(400, "skip is \"sender\", \"topic\" or null")
        item = runner.dismiss_brief_item(brief_id, item_id, skip)
        if item is None:
            raise HTTPException(404, "no such brief item")
        return item

    @router.post("/agent/briefs/memory/forget")
    async def agent_brief_forget(request: Request) -> dict[str, Any]:
        body = await _json_body(request, limit=1024)
        kind = body.get("kind") if isinstance(body, dict) else None
        value = body.get("value") if isinstance(body, dict) else None
        if kind not in ("senders", "topics") or not isinstance(value, str):
            raise HTTPException(400, "send {\"kind\": \"senders\"|\"topics\", \"value\": \"...\"}")
        data = runner.brief_store.load()
        data["memory"][kind] = [v for v in data["memory"][kind] if v != value]
        runner.brief_store.save(data)
        return data["memory"]

    @router.get("/email/status")
    def email_status() -> dict[str, Any]:
        return email_tools.status()

    @router.put("/email/connect")
    async def email_connect(request: Request) -> dict[str, Any]:
        body = await _json_body(request, limit=2048)
        if not isinstance(body, dict):
            raise HTTPException(400, "send a JSON object")
        address, password = str(body.get("address", "")).strip(), str(body.get("password", "")).replace(" ", "").strip()
        preset = email_tools.PRESETS.get(str(body.get("provider", "")))
        imap = preset["imap"] if preset else str(body.get("imap", "")).strip()
        smtp = preset["smtp"] if preset else str(body.get("smtp", "")).strip()
        if "@" not in address or len(address) > 254:
            raise HTTPException(400, "enter your email address")
        if not 4 <= len(password) <= 200:
            raise HTTPException(400, "enter the app password from your email account's security settings")
        if not (email_tools.valid_hostport(imap) and email_tools.valid_hostport(smtp)):
            raise HTTPException(400, "choose a provider, or give the IMAP and SMTP servers as host:port")
        account = email_tools.EmailAccount(address, password, imap, smtp)
        try:
            await asyncio.to_thread(email_tools.check_login, account)
        except ToolError as error:
            raise HTTPException(400, str(error)) from None
        for name, value in ((email_tools.ADDRESS_ENV, address), (email_tools.PASSWORD_ENV, password),
                            (email_tools.IMAP_ENV, imap), (email_tools.SMTP_ENV, smtp)):
            save_env_value(name, value, dirs.env_file)
        return {"connected": True, "address": address}

    @router.delete("/email/connect")
    def email_disconnect() -> dict[str, Any]:
        for name in (email_tools.ADDRESS_ENV, email_tools.PASSWORD_ENV, email_tools.IMAP_ENV, email_tools.SMTP_ENV):
            remove_env_value(name, dirs.env_file)
        return {"connected": False}

    @router.get("/wallpaper")
    def get_wallpaper() -> dict[str, Any]:
        found = _picture_file(dirs.wallpaper)
        if found is None:
            raise HTTPException(404, "no personal wallpaper: the theme's own is used")
        mime, path = found
        return {"image": f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()}

    @router.put("/wallpaper")
    async def put_wallpaper(request: Request) -> dict[str, Any]:
        mime, data = _picture_body(await _json_body(request, limit=MAX_WALLPAPER_BYTES * 4 // 3 + 1024), MAX_WALLPAPER_BYTES)
        _remove_pictures(dirs.wallpaper)
        _write_private(dirs.wallpaper.with_suffix(_AVATAR_EXT[mime]), data)
        return {"wallpaper": True}

    @router.delete("/wallpaper")
    def delete_wallpaper() -> dict[str, Any]:
        _remove_pictures(dirs.wallpaper)
        return {"wallpaper": False}

    @router.get("/bots/{handle}/avatar")
    def get_avatar(handle: str) -> dict[str, Any]:
        _path(dirs.bots, handle, ".json")  # validates the handle
        for mime, ext in _AVATAR_EXT.items():
            path = dirs.bots / f"{handle}{ext}"
            if path.is_file() and not path.is_symlink():
                return {"image": f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()}
        raise HTTPException(404, "this bot has no picture")

    @router.put("/bots/{handle}/avatar")
    async def put_avatar(handle: str, request: Request) -> dict[str, Any]:
        """Save a picture (generated or uploaded) as data:image/...;base64,..."""

        _path(dirs.bots, handle, ".json")
        body = await _json_body(request, limit=MAX_AVATAR_BYTES * 4 // 3 + 1024)
        match = _DATA_URL.fullmatch(str(body.get("image", ""))) if isinstance(body, dict) else None
        if not match:
            raise HTTPException(400, "send a JPEG, PNG or WebP picture")
        data = base64.b64decode(match.group(2))
        if len(data) > MAX_AVATAR_BYTES:
            raise HTTPException(413, "pictures can be at most 2 MB")
        _remove_avatar(handle)
        _write_private(dirs.bots / f"{handle}{_AVATAR_EXT[match.group(1)]}", data)
        return {"handle": handle, "avatar": True}

    @router.delete("/bots/{handle}/avatar")
    def delete_avatar(handle: str) -> dict[str, Any]:
        _path(dirs.bots, handle, ".json")
        _remove_avatar(handle)
        return {"handle": handle, "avatar": False}

    def _remove_avatar(handle: str) -> None:
        for ext in _AVATAR_EXT.values():
            (dirs.bots / f"{handle}{ext}").unlink(missing_ok=True)

    @router.get("/themes")
    def list_themes() -> dict[str, Any]:
        user = set(_names(dirs.themes, ".css"))
        builtin = set(_names(dirs.static / "css" / "themes", ".css"))
        # A user theme replaces the built-in of the same name, so it is listed as theirs.
        return {
            "themes": [
                {"name": name, "builtin": name not in user}
                for name in sorted(user | builtin)
            ]
        }

    @router.get("/themes/{name}")
    def get_theme(name: str) -> PlainTextResponse:
        for folder in (dirs.themes, dirs.static / "css" / "themes"):
            candidate = _path(folder, name, ".css")
            try:
                if candidate.is_symlink():  # never follow a link out of the themes dir
                    raise FileNotFoundError(name)
                css = candidate.read_text(encoding="utf-8")
            except FileNotFoundError:
                continue
            return PlainTextResponse(css, media_type="text/css")
        raise HTTPException(404, f"no theme named {name!r}")

    return router


def _agent_request(body: Any, runner: AgentRunner) -> tuple[str, str, set[str] | None]:
    if not isinstance(body, dict):
        raise HTTPException(400, "send a JSON object")
    goal = body.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        raise HTTPException(400, "tell the agent what to do")
    mode = body.get("mode", "task")
    if mode not in ("task", "goal"):
        raise HTTPException(400, "mode is task or goal")
    tools = body.get("tools")
    if tools is not None:
        known = set(runner.tools())
        if not isinstance(tools, list) or not tools or not set(tools) <= known:
            raise HTTPException(400, f"tools must be a list of: {', '.join(sorted(known))}")
        tools = set(tools)
    return goal, mode, tools


def _picture_file(stem: Path) -> tuple[str, Path] | None:
    for mime, ext in _AVATAR_EXT.items():
        path = stem.with_suffix(ext)
        if path.is_file() and not path.is_symlink():
            return mime, path
    return None


def _picture_body(body: Any, limit: int) -> tuple[str, bytes]:
    """(mime, bytes) from {"image": "data:image/...;base64,..."}; JPEG, PNG or WebP only."""

    match = _DATA_URL.fullmatch(str(body.get("image", ""))) if isinstance(body, dict) else None
    if not match:
        raise HTTPException(400, "send a JPEG, PNG or WebP picture")
    data = base64.b64decode(match.group(2))
    if len(data) > limit:
        raise HTTPException(413, f"pictures can be at most {limit // (1024 * 1024)} MB")
    return match.group(1), data


def _remove_pictures(stem: Path) -> None:
    for ext in _AVATAR_EXT.values():
        stem.with_suffix(ext).unlink(missing_ok=True)


def _bot(handle: str, body: Any) -> dict[str, Any]:
    """A clean bot record from untrusted JSON (the PUT body or a hand-edited file)."""

    if not isinstance(body, dict):
        raise HTTPException(400, "a bot must be a JSON object")
    name = body.get("name")
    instructions = body.get("instructions", "")
    alias = body.get("alias", "zhunt-auto")
    color = body.get("color", "#7aa2f7")
    if not isinstance(name, str) or not 0 < len(name.strip()) <= 40:
        raise HTTPException(400, "a bot needs a name of 1-40 characters")
    if not isinstance(instructions, str) or len(instructions) > MAX_BOT_INSTRUCTIONS:
        raise HTTPException(400, f"instructions must be text of at most {MAX_BOT_INSTRUCTIONS} characters")
    if alias not in BOT_ALIASES:
        raise HTTPException(400, f"alias must be one of {', '.join(BOT_ALIASES)}")
    if not isinstance(color, str) or not _COLOR.fullmatch(color):
        raise HTTPException(400, "color must look like #7aa2f7")
    return {"handle": handle, "name": name.strip(), "instructions": instructions, "alias": alias, "color": color.lower()}


def _compare_request(body: Any) -> tuple[list[dict[str, Any]], list[str]]:
    messages = body.get("messages") if isinstance(body, dict) else None
    models = body.get("models") if isinstance(body, dict) else None
    if not (
        isinstance(messages, list)
        and messages
        and all(
            isinstance(m, dict)
            and isinstance(m.get("role"), str)
            and isinstance(m.get("content"), (str, list))
            for m in messages
        )
    ):
        raise HTTPException(400, "messages must be a non-empty list of {role, content}")
    if not (
        isinstance(models, list) and models and all(isinstance(m, str) for m in models)
    ):
        raise HTTPException(400, "models must be a non-empty list of model ids")
    if len(models) > MAX_COMPARE_MODELS:
        raise HTTPException(400, f"at most {MAX_COMPARE_MODELS} models per compare")
    return messages, list(dict.fromkeys(models))


async def _json_body(request: Request, *, limit: int | None = None) -> Any:
    """Parse the body as JSON; ``limit`` is enforced while streaming, not after."""

    raw = b""
    async for chunk in request.stream():
        raw += chunk
        if limit is not None and len(raw) > limit:
            raise HTTPException(413, f"body is larger than {limit} bytes")
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):  # RecursionError: absurdly deep nesting
        raise HTTPException(400, "body must be valid JSON") from None


def _path(folder: Path, name: str, suffix: str) -> Path:
    if not _NAME.fullmatch(name):
        raise HTTPException(
            400, "name must match ^[a-z0-9][a-z0-9_-]{0,39}$ (lowercase letters, digits, - and _)"
        )
    return folder / f"{name}{suffix}"


def _names(folder: Path, suffix: str) -> list[str]:
    if not folder.is_dir():
        return []
    return sorted(
        path.stem
        for path in folder.iterdir()
        if path.suffix == suffix
        and _NAME.fullmatch(path.stem)
        and path.is_file()
        and not path.is_symlink()
    )


def _write_private(path: Path, data: bytes) -> None:
    """Atomic replace; mkstemp creates the file 0600 so it is never briefly readable."""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
