"""Workspace HTTP API (docs/WORKSPACE_API.md): status, decisions, compare, layouts, themes, plan."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from zhunt.auth import remove_env_value, save_env_value
from zhunt.engine import DECISION_LOG_SIZE, EngineProxyHook
from zhunt.licence import Plan, current_plan
from zhunt.server import _authenticate_local_request

MAX_LAYOUT_BYTES = 64 * 1024
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
    themes: Path = field(default_factory=lambda: Path.home() / ".zhunt" / "themes")
    static: Path = Path(__file__).parent / "workspace"


# The workspace needs nothing but its own files and this daemon. Locking everything
# else down also stops a downloaded user theme from beaconing out via url().
_WORKSPACE_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
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


def mount_workspace(app: FastAPI, hook: EngineProxyHook, dirs: WorkspaceDirs) -> None:
    app.add_middleware(_WorkspaceHeaders)
    app.include_router(build_router(hook, dirs))
    if dirs.static.is_dir():  # the UI ships in a separate change
        app.mount(
            "/workspace",
            StaticFiles(directory=dirs.static, html=True),
            name="workspace",
        )


def build_router(
    hook: EngineProxyHook, dirs: WorkspaceDirs, plan: Callable[[], Plan] | None = None
) -> APIRouter:
    # Read on every request, so `zhunt licence activate` takes effect without a restart.
    plan = plan or (lambda: current_plan())
    router = APIRouter(
        prefix="/zhunt/api", dependencies=[Depends(_authenticate_local_request)]
    )

    @router.get("/status")
    def status() -> dict[str, Any]:
        return hook.status()

    @router.post("/keys")
    async def add_key(request: Request) -> dict[str, Any]:
        """Save a provider key pasted in the Connect window. It goes to ~/.zhunt/env
        (owner-only) and the environment of this process; it is never echoed back."""

        body = await _json_body(request, limit=4096)
        platform = body.get("platform") if isinstance(body, dict) else None
        key = body.get("key") if isinstance(body, dict) else None
        provider = hook.catalog.providers.get(platform) if isinstance(platform, str) else None
        if provider is None:
            raise HTTPException(404, "unknown provider")
        if not isinstance(key, str) or not _KEY.fullmatch(key.strip()):
            raise HTTPException(400, "that does not look like an API key (one line, no spaces)")
        save_env_value(provider.key_env, key.strip(), dirs.env_file)
        hook.reload_keys()
        return {"platform": provider.platform, "key_set": True}

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

        import litellm

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
            result = await litellm.atranscription(
                model=TRANSCRIBE_MODEL, file=(f"speech.{ext}", audio, ctype),
                api_key=key, max_retries=0, timeout=60,
            )
        except litellm.RateLimitError:
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
