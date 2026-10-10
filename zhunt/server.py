"""LiteLLM proxy integration for the Zhunt daemon."""

from __future__ import annotations

import asyncio
import os
import secrets
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from litellm.integrations.custom_logger import CustomLogger

from zhunt.adapters import (
    AnthropicMessagesAdapter,
    OpenAIChatCompletionsAdapter,
    OpenAIResponsesAdapter,
)
from zhunt.adapters.base import AdapterError, WireAdapter, strip_thinking_blocks, system_first
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
    """Configure LiteLLM behind a small, authenticated inference surface."""

    import litellm
    from litellm.proxy import proxy_server

    from zhunt.engine import build_engine_hook
    from zhunt.workspace_api import WorkspaceDirs, mount_workspace

    proxy_server.master_key = ensure_master_key(env_path)
    hook = build_engine_hook(catalog_path=catalog_path, telemetry_path=telemetry_path)
    litellm.callbacks = [hook]
    proxy_server.save_worker_config(
        model="*",
        telemetry=False,
        drop_params=True,
    )
    app = _inference_app(proxy_server)
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


def _inference_app(proxy_server: Any) -> FastAPI:
    """Return only the supported authenticated inference routes."""

    from litellm.proxy.anthropic_endpoints import endpoints as anthropic
    from litellm.proxy.response_api_endpoints import endpoints as responses

    app = FastAPI(
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=proxy_server.app.router.lifespan_context,
    )
    app.add_api_route(
        "/v1/chat/completions",
        proxy_server.chat_completion,
        methods=["POST"],
    )
    app.add_api_route(
        "/v1/responses",
        responses.responses_api,
        methods=["POST"],
    )
    app.add_api_route(
        "/v1/messages",
        anthropic.anthropic_response,
        methods=["POST"],
    )
    app.add_api_route(
        "/v1/messages/count_tokens",
        anthropic.count_tokens,
        methods=["POST"],
    )
    for exception_type, handler in proxy_server.app.exception_handlers.items():
        app.add_exception_handler(exception_type, handler)
    app.state.zhunt_master_key = proxy_server.master_key
    app.dependency_overrides[proxy_server.user_api_key_auth] = (
        _authenticate_local_request
    )
    return app


def _authenticate_local_request(request: Request) -> Any:
    """Authenticate the local daemon without LiteLLM's optional DB layer."""

    from litellm.proxy._types import UserAPIKeyAuth

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
    return UserAPIKeyAuth(api_key=token)


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
