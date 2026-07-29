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

from litellm.integrations.custom_logger import CustomLogger

from zhunt.adapters import (
    AnthropicMessagesAdapter,
    OpenAIChatCompletionsAdapter,
    OpenAIResponsesAdapter,
)
from zhunt.adapters.base import WireAdapter
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
        if retry_decision is None:
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
        failure = _response_failure(response)
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
                failure = failure or _response_failure(chunk)
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
) -> Any:
    """Configure and return LiteLLM's ASGI proxy application."""

    import litellm
    from litellm.proxy import proxy_server

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
    return RetryMiddleware(proxy_server.app, hook)


def run_proxy(
    *,
    host: str,
    port: int,
    registry_path: Path | None = None,
) -> None:
    import uvicorn

    uvicorn.run(
        create_proxy_app(registry_path=registry_path),
        host=host,
        port=port,
    )


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


def _response_failure(response: Any) -> FailureKind | None:
    payload = _response_mapping(response)
    if payload.get("status") == "incomplete":
        return FailureKind.TRUNCATION
    if payload.get("stop_reason") == "max_tokens":
        return FailureKind.TRUNCATION
    delta = payload.get("delta")
    if (
        isinstance(delta, Mapping)
        and delta.get("stop_reason") == "max_tokens"
    ):
        return FailureKind.TRUNCATION
    nested_response = payload.get("response")
    if (
        isinstance(nested_response, Mapping)
        and nested_response.get("status") == "incomplete"
    ):
        return FailureKind.TRUNCATION

    choices = payload.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, Mapping):
                continue
            if choice.get("finish_reason") in {"length", "max_tokens"}:
                return FailureKind.TRUNCATION
            message = choice.get("message")
            if isinstance(message, Mapping) and message.get("refusal"):
                return FailureKind.REFUSAL

    if _contains_refusal(payload):
        return FailureKind.REFUSAL
    return None


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
