"""The Zhunt daemon: authenticated /v1 inference routes over the free-tier engine."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from zhunt import llm
from zhunt.adapters import (
    AnthropicMessagesAdapter,
    OpenAIChatCompletionsAdapter,
    OpenAIResponsesAdapter,
)
from zhunt.adapters.base import (
    AdapterError,
    WireAdapter,
    estimate_tokens,
    strip_thinking_blocks,
    system_first,
)
from zhunt.auth import ensure_master_key
from zhunt.router import (
    FailureKind,
    RoutingCoordinator,
    RoutingDecision,
    RoutingRequest,
)
from zhunt.telemetry import TelemetryLogger

if TYPE_CHECKING:
    from zhunt.workspace_api import WorkspaceDirs


_ADAPTERS: dict[str, WireAdapter] = {
    "completion": OpenAIChatCompletionsAdapter(),
    "acompletion": OpenAIChatCompletionsAdapter(),
    "responses": OpenAIResponsesAdapter(),
    "aresponses": OpenAIResponsesAdapter(),
    "anthropic_messages": AnthropicMessagesAdapter(),
    "aanthropic_messages": AnthropicMessagesAdapter(),
}

_RETRYABLE_PATHS = {
    "/v1/chat/completions",
    "/v1/messages",
    "/v1/responses",
}
_ATTEMPT_HEADER = b"x-zhunt-attempt-id"


class RetryMiddleware:
    """Replay one failed LLM request after the hook promotes its route."""

    def __init__(self, app: Any, hook: ZhuntProxyHook) -> None:
        self.app = app
        self.hook = hook

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if (
            scope.get("type") != "http"
            or scope.get("method") != "POST"
            or scope.get("path") not in _RETRYABLE_PATHS
        ):
            await self.app(scope, receive, send)
            return

        attempt_id = str(uuid4())
        retry_scope = _with_attempt_header(scope, attempt_id)
        recorded_request: list[dict[str, Any]] = []

        async def recording_receive() -> dict[str, Any]:
            message = await receive()
            if message.get("type") == "http.request":
                recorded_request.append(dict(message))
            return message

        first_response: list[dict[str, Any]] = []

        async def capture_first(message: dict[str, Any]) -> None:
            first_response.append(message)

        await self.app(retry_scope, recording_receive, capture_first)
        if not self.hook.has_retry(attempt_id):
            await _send_messages(first_response, send)
            return

        # v1 replays once; the free-tier hook allows several failovers.
        replayed: list[dict[str, Any]] = []
        for _ in range(getattr(self.hook, "max_replays", 1)):
            replayed = []
            replay = deque(recorded_request)

            async def replay_receive(
                replay: deque[dict[str, Any]] = replay,
            ) -> dict[str, Any]:
                if replay:
                    return replay.popleft()
                return await receive()

            async def capture_replay(
                message: dict[str, Any],
                replayed: list[dict[str, Any]] = replayed,
            ) -> None:
                replayed.append(message)

            await self.app(retry_scope, replay_receive, capture_replay)
            if not self.hook.has_retry(attempt_id):
                break
        self.hook.discard_retry(attempt_id)
        await _send_messages(replayed, send)


def create_proxy_app(
    *,
    env_path: Path | None = None,
    telemetry_path: Path | None = None,
    catalog_path: Path | None = None,
    workspace_dirs: WorkspaceDirs | None = None,
) -> Any:
    """The free-tier engine behind a small, authenticated inference surface."""

    from zhunt.engine import build_engine_hook
    from zhunt.workspace_api import WorkspaceDirs, mount_workspace

    # First: it also loads ~/.zhunt/env, where the provider keys live.
    master_key = ensure_master_key(env_path)
    hook = build_engine_hook(catalog_path=catalog_path, telemetry_path=telemetry_path)
    app = _inference_app(hook, master_key)
    if hook.catalog_feed is not None:
        app.state.background = [hook.catalog_feed.run]
    mount_workspace(app, hook, workspace_dirs or WorkspaceDirs())
    return RetryMiddleware(app, hook)


def run_proxy(
    *,
    host: str,
    port: int,
    allow_non_loopback: bool = False,
    telemetry_path: Path | None = None,
    catalog_path: Path | None = None,
) -> None:
    if not allow_non_loopback and host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(
            "refusing non-loopback host; pass --allow-non-loopback to opt in"
        )
    import uvicorn

    app = create_proxy_app(
        telemetry_path=telemetry_path or Path.home() / ".zhunt" / "telemetry.jsonl",
        catalog_path=catalog_path,
    )
    for line in app.hook.summary:
        print(line)
    uvicorn.run(app, host=host, port=port)


# path -> (hook call type, zhunt.llm function, stream framing)
_ROUTES = {
    "/v1/chat/completions": ("acompletion", "acompletion", "chat"),
    "/v1/responses": ("aresponses", "aresponses", "events"),
    "/v1/messages": ("anthropic_messages", "anthropic_messages", "events"),
}


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Long-running jobs registered on app.state.background (the catalog feed watcher).
    tasks = [asyncio.create_task(job()) for job in getattr(app.state, "background", ())]
    yield
    for task in tasks:
        task.cancel()
    await llm.aclose()


def _inference_app(hook: ZhuntProxyHook, master_key: str) -> FastAPI:
    """Return only the supported authenticated inference routes."""

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)
    app.state.zhunt_master_key = master_key
    auth = [Depends(_authenticate_local_request)]
    for path, (call_type, upstream, framing) in _ROUTES.items():
        app.add_api_route(
            path,
            _inference_endpoint(hook, call_type, upstream, framing),
            methods=["POST"],
            dependencies=auth,
        )
    app.add_api_route(
        "/v1/messages/count_tokens", _count_tokens, methods=["POST"], dependencies=auth
    )
    return app


def _inference_endpoint(
    hook: ZhuntProxyHook, call_type: str, upstream: str, framing: str
) -> Callable[[Request], Awaitable[Response]]:
    """One inference route, driving the hook through the lifecycle the LiteLLM
    proxy used: pre-call, provider call, then success, failure or stream."""

    async def endpoint(request: Request) -> Response:
        body = await _json_object(request)
        if not isinstance(body, dict):
            return _error_response(400, "request body must be a JSON object")
        data: dict[str, Any] = {
            **body,
            "proxy_server_request": {
                "url": str(request.url),
                "method": request.method,
                "headers": dict(request.headers),
                "body": deepcopy(body),
            },
        }
        try:
            data = await hook.async_pre_call_hook(None, None, data, call_type)
        except HTTPException as error:
            return _error_response(error.status_code, str(error.detail))
        # Looked up per call so tests can patch zhunt.llm.<function>.
        call = getattr(llm, upstream)
        alias = body.get("model") if framing == "chat" else None
        try:
            result = await call(**data)
            if not data.get("stream"):
                await hook.async_post_call_success_hook(data, None, result)
                return JSONResponse(_with_model(_plain(result), alias))
            chunks = hook.async_post_call_streaming_iterator_hook(None, result, data)
            # The first chunk is read before answering, so a provider that fails
            # at once still gets its own status code rather than a 200.
            first = await anext(chunks, None)
        except Exception as error:
            # A stream's own failure was already recorded by the iterator hook.
            await hook.async_post_call_failure_hook(data, error, None)
            return _error_response(_status_of(error), str(error) or type(error).__name__)
        return StreamingResponse(
            _sse(chunks, first, framing, alias),
            media_type="text/event-stream",
            headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
        )

    return endpoint


async def _sse(
    chunks: AsyncIterator[Any], first: Any, framing: str, alias: Any
) -> AsyncIterator[bytes]:
    try:
        if first is not None:
            yield _frame(first, framing, alias)
        async for chunk in chunks:
            yield _frame(chunk, framing, alias)
        if framing == "chat":
            yield b"data: [DONE]\n\n"
    except Exception as error:  # mid-stream: the hook has already recorded it
        status, message = _status_of(error), str(error) or type(error).__name__
        if framing == "chat":
            yield _data({"error": _error_body(status, message)})
        else:
            yield _data(
                {"type": "error", "error": {"type": "api_error", "message": message}},
                event="error",
            )
    finally:
        await chunks.aclose()


def _frame(chunk: Any, framing: str, alias: Any) -> bytes:
    payload = _plain(chunk)
    if framing == "chat":
        return _data(_with_model(payload, alias))
    return _data(payload, event=payload.get("type"))


def _data(payload: Mapping[str, Any], *, event: Any = None) -> bytes:
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    prefix = f"event: {event}\n" if isinstance(event, str) else ""
    return f"{prefix}data: {text}\n\n".encode()


def _plain(value: Any) -> dict[str, Any]:
    return dict(_response_mapping(value))


def _with_model(payload: dict[str, Any], alias: Any) -> dict[str, Any]:
    """Chat answers name the model the client asked for (zhunt-auto), as before;
    /zhunt/api/decisions says which model actually answered."""

    if isinstance(alias, str):
        payload["model"] = alias
    return payload


def _status_of(error: BaseException) -> int:
    status = getattr(error, "status_code", None)
    return status if isinstance(status, int) and 400 <= status < 600 else 500


def _error_body(status: int, message: str) -> dict[str, Any]:
    return {"message": message, "type": None, "param": None, "code": str(status)}


def _error_response(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": _error_body(status, message)}, status_code=status)


async def _json_object(request: Request) -> Any:
    try:
        return json.loads(await request.body())
    except ValueError:
        return None


async def _count_tokens(request: Request) -> dict[str, int]:
    """Anthropic's token counter, answered with zhunt's usual estimate."""

    body = await _json_object(request)
    if not isinstance(body, dict):
        body = {}
    counted = {k: body[k] for k in ("system", "messages", "tools") if k in body}
    return {"input_tokens": estimate_tokens(counted)}


def _authenticate_local_request(request: Request) -> str:
    """Accept only the local daemon's master key."""

    token = request.headers.get("authorization", "")
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    else:
        token = (
            request.headers.get("x-litellm-api-key")
            or request.headers.get("x-api-key")
            or ""
        )
    # Compare bytes: compare_digest raises TypeError on non-ASCII str (a 500).
    if not secrets.compare_digest(
        token.encode("utf-8", "surrogateescape"),
        request.app.state.zhunt_master_key.encode("utf-8"),
    ):
        raise HTTPException(status_code=401, detail="invalid Zhunt master key")
    return token


def _original_request(
    data: Mapping[str, Any],
) -> tuple[Mapping[str, Any], Mapping[str, str] | None]:
    proxy_request = data.get("proxy_server_request")
    if not isinstance(proxy_request, Mapping):
        return data, None

    body = proxy_request.get("body")
    payload = body if isinstance(body, Mapping) else data
    raw_headers = proxy_request.get("headers")
    headers = (
        {
            str(key): str(value)
            for key, value in raw_headers.items()
        }
        if isinstance(raw_headers, Mapping)
        else None
    )
    return payload, headers


def _header(
    headers: Mapping[str, str] | None,
    name: str,
) -> str | None:
    if headers is None:
        return None
    target = name.lower()
    for key, value in headers.items():
        if key.lower() == target:
            return value
    return None


def _with_attempt_header(
    scope: Mapping[str, Any],
    attempt_id: str,
) -> dict[str, Any]:
    updated = dict(scope)
    headers = [
        (key, value)
        for key, value in scope.get("headers", [])
        if key.lower() != _ATTEMPT_HEADER
    ]
    headers.append((_ATTEMPT_HEADER, attempt_id.encode()))
    updated["headers"] = headers
    return updated


async def _send_messages(
    messages: list[dict[str, Any]],
    send: Callable[[dict[str, Any]], Awaitable[None]],
) -> None:
    for message in messages:
        await send(message)


def _call_id(data: dict[str, Any]) -> str:
    call_id = data.get("litellm_call_id")
    if isinstance(call_id, str) and call_id:
        return call_id
    generated = str(uuid4())
    data["litellm_call_id"] = generated
    return generated


def _response_failure(
    response: Any,
    *,
    request_data: Mapping[str, Any] | None = None,
) -> FailureKind | None:
    client_set_output_cap = _client_set_output_cap(request_data)
    payload = _response_mapping(response)
    if payload.get("status") == "incomplete" and not client_set_output_cap:
        return FailureKind.TRUNCATION
    if payload.get("stop_reason") == "max_tokens" and not client_set_output_cap:
        return FailureKind.TRUNCATION
    delta = payload.get("delta")
    if (
        isinstance(delta, Mapping)
        and delta.get("stop_reason") == "max_tokens"
        and not client_set_output_cap
    ):
        return FailureKind.TRUNCATION
    nested_response = payload.get("response")
    if (
        isinstance(nested_response, Mapping)
        and nested_response.get("status") == "incomplete"
        and not client_set_output_cap
    ):
        return FailureKind.TRUNCATION

    choices = payload.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, Mapping):
                continue
            if (
                choice.get("finish_reason") in {"length", "max_tokens"}
                and not client_set_output_cap
            ):
                return FailureKind.TRUNCATION
            message = choice.get("message")
            if isinstance(message, Mapping) and message.get("refusal"):
                return FailureKind.REFUSAL

    if _contains_refusal(payload):
        return FailureKind.REFUSAL
    return None


def _client_set_output_cap(
    request_data: Mapping[str, Any] | None,
) -> bool:
    if request_data is None:
        return False
    payload, _ = _original_request(request_data)
    return any(
        key in payload
        for key in ("max_tokens", "max_completion_tokens", "max_output_tokens")
    )


def _response_mapping(response: Any) -> Mapping[str, Any]:
    if isinstance(response, Mapping):
        return response
    model_dump = getattr(response, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, Mapping):
            return dumped
    return {}


def _usage_tokens(response: Any) -> tuple[int | None, int | None]:
    payload = _response_mapping(response)
    usage = payload.get("usage")
    if not isinstance(usage, Mapping):
        return None, None
    input_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
    output_tokens = usage.get(
        "completion_tokens",
        usage.get("output_tokens"),
    )
    return (
        input_tokens if isinstance(input_tokens, int) else None,
        output_tokens if isinstance(output_tokens, int) else None,
    )


def _telemetry_wire_dialect(call_type: str) -> str:
    if call_type in {"anthropic_messages", "aanthropic_messages"}:
        return "anthropic-messages"
    if call_type in {"responses", "aresponses"}:
        return "openai-responses"
    return "openai-chat-completions"


def _contains_refusal(value: Any) -> bool:
    if isinstance(value, list):
        return any(_contains_refusal(item) for item in value)
    if isinstance(value, Mapping):
        event_type = value.get("type")
        if (
            isinstance(event_type, str)
            and "refusal" in event_type
        ):
            return True
        return any(_contains_refusal(item) for item in value.values())
    return False
