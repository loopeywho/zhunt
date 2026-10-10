"""Workspace HTTP API (docs/WORKSPACE_API.md): status, decisions, compare, layouts, themes."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles

from zhunt.engine import DECISION_LOG_SIZE, EngineProxyHook
from zhunt.server import _authenticate_local_request

MAX_LAYOUT_BYTES = 64 * 1024
MAX_COMPARE_MODELS = 4
_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")


@dataclass(frozen=True)
class WorkspaceDirs:
    """Where the workspace keeps user files; ``static`` is the packaged UI (may be absent)."""

    layouts: Path = field(default_factory=lambda: Path.home() / ".zhunt" / "layouts")
    themes: Path = field(default_factory=lambda: Path.home() / ".zhunt" / "themes")
    static: Path = Path(__file__).parent / "workspace"


def mount_workspace(app: FastAPI, hook: EngineProxyHook, dirs: WorkspaceDirs) -> None:
    app.include_router(build_router(hook, dirs))
    if dirs.static.is_dir():  # the UI ships in a separate change
        app.mount(
            "/workspace",
            StaticFiles(directory=dirs.static, html=True),
            name="workspace",
        )


def build_router(hook: EngineProxyHook, dirs: WorkspaceDirs) -> APIRouter:
    router = APIRouter(
        prefix="/zhunt/api", dependencies=[Depends(_authenticate_local_request)]
    )

    @router.get("/status")
    def status() -> dict[str, Any]:
        return hook.status()

    @router.get("/decisions")
    def decisions(
        limit: int = Query(50, ge=1, le=DECISION_LOG_SIZE),
        session: str | None = None,
    ) -> dict[str, Any]:
        return {"decisions": hook.decisions(limit=limit, session=session)}

    @router.post("/compare")
    async def compare(request: Request) -> dict[str, Any]:
        messages, model_ids = _compare_request(await _json_body(request))
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
            try:
                css = _path(folder, name, ".css").read_text(encoding="utf-8")
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
    except ValueError:
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
        if path.suffix == suffix and _NAME.fullmatch(path.stem) and path.is_file()
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
