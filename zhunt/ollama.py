"""The Ollama wire format: /api/chat, /api/generate, /api/tags and /api/version.

Requests are translated to Chat Completions and sent down the same path as any
other chat (routing, failover, telemetry, the privacy rule); answers are
translated back. The translation is pure; ``mount`` adds the routes."""

from __future__ import annotations

import ipaddress
import json
import os
import secrets
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from zhunt import local_provider
from zhunt.brain import Tier
from zhunt.registry import RegistryError

if TYPE_CHECKING:
    from zhunt.engine import ZhuntProxyHook

VERSION = "0.0.0-zhunt"
AUTO = "zhunt-auto"
_REQUIRE_KEY_ENV = "ZHUNT_OLLAMA_REQUIRE_KEY"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})

# Ollama endpoints Zhunt does not serve: (method, path).
UNSUPPORTED = {
    ("POST", "/api/embeddings"), ("POST", "/api/embed"), ("POST", "/api/pull"),
    ("POST", "/api/push"), ("POST", "/api/show"), ("POST", "/api/create"),
    ("POST", "/api/copy"), ("DELETE", "/api/delete"), ("GET", "/api/ps"),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# -- request -------------------------------------------------------------------


def route_name(requested: str, known_aliases: frozenset[str]) -> str:
    """The Zhunt model alias for an Ollama model name. Zhunt's own aliases (and the
    private ones) are kept; anything else, like ``llama3.2:3b``, means ``zhunt-auto``."""

    name = requested.removesuffix(":latest")
    if name in known_aliases or name in local_provider.LOCAL_ALIASES:
        return name  # a private alias with no local model gets the privacy 503, never a cloud
    return AUTO


def _user_message(text: str, images: Any) -> dict[str, Any]:
    if not images:
        return {"role": "user", "content": text}
    if not isinstance(images, list) or not all(isinstance(i, str) for i in images):
        raise ValueError("images must be a list of base64 strings")
    parts: list[dict[str, Any]] = [{"type": "text", "text": text}] if text else []
    for image in images:
        parts.append({"type": "image_url", "image_url": {"url": _image_url(image)}})
    return {"role": "user", "content": parts}


def _image_url(image: str) -> str:
    # Ollama sends bare base64; the format is only a hint for the provider.
    if image.startswith("data:") or image.startswith(("http://", "https://")):
        return image
    kind = "jpeg" if image.startswith("/9j/") else "gif" if image.startswith("R0lG") else "png"
    if image.startswith("UklGR"):
        kind = "webp"
    return f"data:image/{kind};base64,{image}"


def build_request(kind: str, body: dict[str, Any], model: str) -> tuple[dict[str, Any] | None, bool]:
    """``(Chat Completions payload, stream)``; the payload is None for an empty
    request, which Ollama treats as "just load the model"."""

    stream = body.get("stream", True)  # Ollama streams unless told not to
    if type(stream) is not bool:
        raise ValueError("stream must be a boolean")
    if body.get("tools"):
        raise ValueError("tools are not supported on the Ollama routes; use /v1/chat/completions")
    messages: list[dict[str, Any]] = []
    if kind == "generate":
        prompt = body.get("prompt", "")
        if not isinstance(prompt, str):
            raise ValueError("prompt must be a string")
        if body.get("system"):
            messages.append({"role": "system", "content": str(body["system"])})
        if not prompt and not body.get("images"):
            return None, stream
        messages.append(_user_message(prompt, body.get("images")))
    else:
        raw = body.get("messages", [])
        if not isinstance(raw, list):
            raise ValueError("messages must be a list")
        if not raw:
            return None, stream
        for message in raw:
            if not isinstance(message, dict) or not isinstance(message.get("role"), str):
                raise ValueError("each message needs a role")
            content = message.get("content", "")
            if not isinstance(content, str):
                raise ValueError("message content must be a string")
            if message.get("images"):
                messages.append({**_user_message(content, message["images"]), "role": message["role"]})
            else:
                messages.append({"role": message["role"], "content": content})
    payload: dict[str, Any] = {"model": model, "messages": messages, "stream": stream}
    if stream:
        payload["stream_options"] = {"include_usage": True}
    options = body.get("options") or {}
    if not isinstance(options, dict):
        raise ValueError("options must be an object")
    for key in ("temperature", "top_p"):
        if options.get(key) is not None:
            payload[key] = options[key]
    limit = options.get("num_predict")
    if type(limit) is int and limit > 0:  # -1 and -2 mean "no limit" in Ollama
        payload["max_tokens"] = limit
    stop = options.get("stop")
    if isinstance(stop, str):
        stop = [stop]
    if stop:
        payload["stop"] = stop
    if body.get("format") == "json":
        payload["response_format"] = {"type": "json_object"}
    return payload, stream


# -- answers -------------------------------------------------------------------


def _piece(
    kind: str, model: str, text: str, thinking: str, done: bool, extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    out: dict[str, Any] = {"model": model, "created_at": _now()}
    if kind == "generate":
        out["response"] = text
        if thinking:
            out["thinking"] = thinking
    else:
        message: dict[str, Any] = {"role": "assistant", "content": text}
        if thinking:
            message["thinking"] = thinking
        out["message"] = message
    out["done"] = done
    out.update(extra or {})
    return out


def _closing(finish: Any, elapsed_ns: int, usage: dict[str, Any]) -> dict[str, Any]:
    # Wall clock only: the providers report no load or per-phase timings.
    return {
        "done_reason": "length" if finish == "length" else "stop",
        "total_duration": elapsed_ns,
        "load_duration": 0,
        "prompt_eval_count": usage.get("prompt_tokens", 0),
        "prompt_eval_duration": 0,
        "eval_count": usage.get("completion_tokens", 0),
        "eval_duration": elapsed_ns,
    }


def empty_answer(kind: str, model: str) -> dict[str, Any]:
    return _piece(kind, model, "", "", True, {"done_reason": "load"})


def complete_answer(kind: str, model: str, data: dict[str, Any], started: float) -> dict[str, Any]:
    """A Chat Completions answer as one Ollama object."""

    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    elapsed = int((time.monotonic() - started) * 1e9)
    return _piece(
        kind, model, message.get("content") or "", message.get("reasoning_content") or "", True,
        _closing(choice.get("finish_reason"), elapsed, data.get("usage") or {}),
    )


def _ndjson(obj: dict[str, Any]) -> bytes:
    return (json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n").encode()


def error_response(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


# -- routes --------------------------------------------------------------------


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _has_key(request: Request) -> bool:
    token = request.headers.get("authorization", "")
    token = token[7:].strip() if token.lower().startswith("bearer ") else token.strip()
    return bool(token) and secrets.compare_digest(
        token.encode("utf-8", "surrogateescape"),
        request.app.state.zhunt_master_key.encode("utf-8"),
    )


def check_access(request: Request) -> Response | None:
    """Ollama clients send no key, so these routes accept a request that comes from
    this computer *and* cannot have come from a web page: a loopback peer, a loopback
    ``Host`` (blocks DNS rebinding), no ``Origin`` header (browsers add one to
    cross-site posts) and, for a body, ``Content-Type: application/json`` (a type a
    page cannot send without a CORS preflight, which the daemon never grants).
    Anything else needs the master key as ``Authorization: Bearer <key>``.
    ``ZHUNT_OLLAMA_REQUIRE_KEY=1`` always requires the key."""

    if _has_key(request):
        return None
    if os.environ.get(_REQUIRE_KEY_ENV, "").strip().lower() in {"1", "true", "yes", "on"}:
        return error_response(401, "invalid Zhunt master key")
    host = request.headers.get("host", "")
    host_name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
    local = (
        _is_loopback(request.client.host if request.client else None)
        and host_name.lower() in _LOOPBACK_HOSTS
        and "origin" not in request.headers
    )
    if local and request.method == "POST":
        local = request.headers.get("content-type", "").split(";")[0].strip().lower() == "application/json"
    if local:
        return None
    return error_response(
        401, "Ollama routes accept keyless requests only from this computer; send the Zhunt master key as a Bearer token"
    )


def mount(app: FastAPI, hook: ZhuntProxyHook) -> None:
    from zhunt import server  # server imports this module

    def aliases() -> frozenset[str]:
        """The names ``/api/tags`` lists: what the daemon can route to now."""

        names = {AUTO, *(f"zhunt-{tier.value}" for tier in Tier)}
        if local_provider.PLATFORM in hook.catalog.providers:
            names |= local_provider.LOCAL_ALIASES
        known = set()
        for name in names:
            try:
                hook.coordinator.registry.resolve_alias(name)
            except RegistryError:
                continue
            known.add(name)
        return frozenset(known)

    def chat_route(kind: str):
        async def endpoint(request: Request) -> Response:
            if (denied := check_access(request)) is not None:
                return denied
            body = await server._json_object(request)
            if not isinstance(body, dict):
                return error_response(400, "request body must be a JSON object")
            requested = body.get("model")
            if not isinstance(requested, str) or not requested:
                return error_response(400, "model is required")
            try:
                payload, stream = build_request(kind, body, route_name(requested, aliases()))
            except ValueError as error:
                return error_response(400, str(error))
            if payload is None:
                answer = empty_answer(kind, requested)
                return JSONResponse(answer) if not stream else Response(
                    _ndjson(answer), media_type="application/x-ndjson"
                )
            started_at = time.monotonic()
            started = await server._start(
                hook, request, payload, "acompletion", "acompletion", on_error=error_response
            )
            if isinstance(started, Response):
                return started
            data, result, chunks, first = started
            headers = server._served_by(data)
            if chunks is None:
                answer = complete_answer(kind, requested, server._plain(result), started_at)
                return JSONResponse(answer, headers=headers)
            return StreamingResponse(
                _ndjson_stream(kind, requested, chunks, first),
                media_type="application/x-ndjson",
                headers={"cache-control": "no-cache", "x-accel-buffering": "no", **headers},
            )

        return endpoint

    async def _ndjson_stream(
        kind: str, requested: str, chunks: AsyncIterator[Any], first: Any
    ) -> AsyncIterator[bytes]:
        async def plain() -> AsyncIterator[dict[str, Any]]:
            if first is not None:
                yield server._plain(first)
            async for chunk in chunks:
                yield server._plain(chunk)

        started = time.monotonic()
        usage: dict[str, Any] = {}
        finish: Any = None
        count = 0
        try:
            async for event in plain():
                usage = event.get("usage") or usage
                for choice in event.get("choices") or []:
                    delta = choice.get("delta") or {}
                    text = delta.get("content") or ""
                    thinking = delta.get("reasoning_content") or ""
                    if text or thinking:
                        count += 1
                        yield _ndjson(_piece(kind, requested, text, thinking, False))
                    finish = choice.get("finish_reason") or finish
            if not usage:
                usage = {"completion_tokens": count}
            elapsed = int((time.monotonic() - started) * 1e9)
            yield _ndjson(_piece(kind, requested, "", "", True, _closing(finish, elapsed, usage)))
        except Exception as error:  # mid-stream: the hook has already recorded it
            yield _ndjson({"error": str(error) or type(error).__name__})
        finally:
            await chunks.aclose()

    async def tags(request: Request) -> Response:
        if (denied := check_access(request)) is not None:
            return denied
        stamp = _now()
        models = []
        for name in sorted(aliases()):
            full = f"{name}:latest"
            models.append({
                "name": full, "model": full, "modified_at": stamp, "size": 0, "digest": "",
                "details": {"format": "", "family": "zhunt", "parameter_size": "", "quantization_level": ""},
            })
        return JSONResponse({"models": models})

    async def version(request: Request) -> Response:
        if (denied := check_access(request)) is not None:
            return denied
        return JSONResponse({"version": VERSION})

    async def unsupported(request: Request) -> Response:
        if (denied := check_access(request)) is not None:
            return denied
        return error_response(501, f"Zhunt does not support {request.url.path} yet")

    app.add_api_route("/api/chat", chat_route("chat"), methods=["POST"])
    app.add_api_route("/api/generate", chat_route("generate"), methods=["POST"])
    app.add_api_route("/api/tags", tags, methods=["GET"])
    app.add_api_route("/api/version", version, methods=["GET"])
    for method, path in sorted(UNSUPPORTED):
        app.add_api_route(path, unsupported, methods=[method])
