"""LiteLLM proxy integration for the Zhunt daemon."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from litellm.integrations.custom_logger import CustomLogger

from zhunt.adapters import (
    AnthropicMessagesAdapter,
    OpenAIChatCompletionsAdapter,
    OpenAIResponsesAdapter,
)
from zhunt.adapters.base import WireAdapter
from zhunt.auth import ensure_master_key
from zhunt.registry import ModelRegistry
from zhunt.router import FailureKind, RoutingCoordinator, RoutingDecision


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


@dataclass(frozen=True)
class _PendingRoute:
    decision: RoutingDecision
    attempt_id: str | None
    retry_attempt: bool = False


class ZhuntProxyHook(CustomLogger):
    """Route supported LiteLLM proxy calls before their provider request."""

    def __init__(self, coordinator: RoutingCoordinator) -> None:
        self.coordinator = coordinator
        self._pending: dict[str, _PendingRoute] = {}
        self._retry_routes: dict[str, RoutingDecision] = {}
        self._lock = RLock()

    async def async_pre_call_hook(
        self,
        user_api_key_dict: Any,
        cache: Any,
        data: dict[str, Any],
        call_type: str,
    ) -> dict[str, Any]:
        adapter = _ADAPTERS.get(call_type)
        if adapter is None:
            return data

        payload, headers = _original_request(data)
        attempt_id = _header(headers, _ATTEMPT_HEADER.decode())
        retry_decision = self._take_retry(attempt_id)
        retry_attempt = retry_decision is not None
        if retry_decision is None:
            requested_model = payload.get("model")
            if (
                isinstance(requested_model, str)
                and not self.coordinator.registry.has_alias(requested_model)
            ):
                # Unknown model ids are valid LiteLLM passthrough targets.
                data["model"] = requested_model
                return data
            decision = adapter.route(
                payload,
                self.coordinator,
                headers=headers,
            ).decision
        else:
            decision = retry_decision
        data["model"] = decision.model
        call_id = _call_id(data)
        with self._lock:
            self._pending[call_id] = _PendingRoute(
                decision=decision,
                attempt_id=attempt_id,
                retry_attempt=retry_attempt,
            )

        metadata = data.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            data["metadata"] = metadata
        metadata["zhunt"] = {
            "requested_alias": decision.requested_alias,
            "session_key": decision.session_key,
            "tier": decision.tier.value,
            "model": decision.model,
            "reused_session_route": decision.reused_session_route,
            "escalation_count": decision.escalation_count,
        }
        return data

    async def async_post_call_success_hook(
        self,
        data: dict[str, Any],
        user_api_key_dict: Any,
        response: Any,
    ) -> None:
        pending = self._pop_pending(data)
        if pending is None:
            return
        failure = _response_failure(response, request_data=data)
        if failure is None:
            self.coordinator.record_success(pending.decision)
        else:
            self._escalate(pending, failure)

    async def async_post_call_failure_hook(
        self,
        request_data: dict[str, Any],
        original_exception: Exception,
        user_api_key_dict: Any,
        traceback_str: str | None = None,
    ) -> None:
        pending = self._pop_pending(request_data)
        if pending is not None:
            self._escalate(pending, FailureKind.PROVIDER_ERROR)

    async def async_post_call_streaming_iterator_hook(
        self,
        user_api_key_dict: Any,
        response: Any,
        request_data: dict[str, Any],
    ) -> Any:
        failure: FailureKind | None = None
        try:
            async for chunk in response:
                failure = failure or _response_failure(
                    chunk,
                    request_data=request_data,
                )
                yield chunk
        except (asyncio.CancelledError, GeneratorExit):
            self._pop_pending(request_data)
            raise
        except Exception:
            pending = self._pop_pending(request_data)
            if pending is not None:
                self._escalate(pending, FailureKind.PROVIDER_ERROR)
            raise
        else:
            pending = self._pop_pending(request_data)
            if pending is None:
                return
            if failure is None:
                self.coordinator.record_success(pending.decision)
            else:
                self._escalate(pending, failure)

    def has_retry(self, attempt_id: str) -> bool:
        with self._lock:
            return attempt_id in self._retry_routes

    def discard_retry(self, attempt_id: str) -> None:
        with self._lock:
            self._retry_routes.pop(attempt_id, None)

    def _escalate(
        self,
        pending: _PendingRoute,
        failure: FailureKind,
    ) -> None:
        if pending.retry_attempt:
            # One original request gets at most one promotion. A failed replay
            # must not consume the next strike in the same turn.
            return
        promoted = self.coordinator.escalate(pending.decision, failure)
        if pending.attempt_id is not None:
            with self._lock:
                self._retry_routes[pending.attempt_id] = promoted

    def _take_retry(
        self,
        attempt_id: str | None,
    ) -> RoutingDecision | None:
        if attempt_id is None:
            return None
        with self._lock:
            return self._retry_routes.pop(attempt_id, None)

    def _pop_pending(
        self,
        data: Mapping[str, Any],
    ) -> _PendingRoute | None:
        call_id = data.get("litellm_call_id")
        if not isinstance(call_id, str):
            return None
        with self._lock:
            return self._pending.pop(call_id, None)


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

        second_response: list[dict[str, Any]] = []
        replay = deque(recorded_request)

        async def replay_receive() -> dict[str, Any]:
            if replay:
                return replay.popleft()
            return await receive()

        async def capture_second(message: dict[str, Any]) -> None:
            second_response.append(message)

        await self.app(retry_scope, replay_receive, capture_second)
        self.hook.discard_retry(attempt_id)
        await _send_messages(second_response, send)


def create_proxy_app(
    *,
    registry_path: Path | None = None,
    env_path: Path | None = None,
) -> Any:
    """Configure LiteLLM behind a small, authenticated inference surface."""

    import litellm
    from litellm.proxy import proxy_server

    master_key = ensure_master_key(env_path)
    proxy_server.master_key = master_key
    registry = (
        ModelRegistry.from_path(registry_path)
        if registry_path is not None
        else ModelRegistry.default()
    )
    hook = ZhuntProxyHook(RoutingCoordinator(registry=registry))
    litellm.callbacks = [hook]
    proxy_server.save_worker_config(
        model="*",
        telemetry=False,
        drop_params=True,
    )
    return RetryMiddleware(_inference_app(proxy_server), hook)


def run_proxy(
    *,
    host: str,
    port: int,
    registry_path: Path | None = None,
    allow_non_loopback: bool = False,
) -> None:
    if not allow_non_loopback and host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(
            "refusing non-loopback host; pass --allow-non-loopback to opt in"
        )
    import uvicorn

    uvicorn.run(
        create_proxy_app(registry_path=registry_path),
        host=host,
        port=port,
    )


def _inference_app(proxy_server: Any) -> FastAPI:
    """Return only the three supported POST inference routes."""

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
    if token != request.app.state.zhunt_master_key:
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
