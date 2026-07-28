"""LiteLLM proxy integration for the Zhunt daemon."""

from __future__ import annotations

from collections.abc import Mapping
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


class ZhuntProxyHook(CustomLogger):
    """Route supported LiteLLM proxy calls before their provider request."""

    def __init__(self, coordinator: RoutingCoordinator) -> None:
        self.coordinator = coordinator
        self._pending: dict[str, RoutingDecision] = {}
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
        routed = adapter.route(payload, self.coordinator, headers=headers)
        data["model"] = routed.decision.model
        call_id = _call_id(data)
        with self._lock:
            self._pending[call_id] = routed.decision

        metadata = data.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
            data["metadata"] = metadata
        metadata["zhunt"] = {
            "requested_alias": routed.decision.requested_alias,
            "session_key": routed.decision.session_key,
            "tier": routed.decision.tier.value,
            "model": routed.decision.model,
            "reused_session_route": routed.decision.reused_session_route,
            "escalation_count": routed.decision.escalation_count,
        }
        return data

    async def async_post_call_success_hook(
        self,
        data: dict[str, Any],
        user_api_key_dict: Any,
        response: Any,
    ) -> None:
        decision = self._pop_decision(data)
        if decision is None:
            return
        failure = _response_failure(response)
        if failure is None:
            self.coordinator.record_success(decision)
        else:
            self.coordinator.escalate(decision, failure)

    async def async_post_call_failure_hook(
        self,
        request_data: dict[str, Any],
        original_exception: Exception,
        user_api_key_dict: Any,
        traceback_str: str | None = None,
    ) -> None:
        decision = self._pop_decision(request_data)
        if decision is not None:
            self.coordinator.escalate(decision, FailureKind.PROVIDER_ERROR)

    def _pop_decision(
        self,
        data: Mapping[str, Any],
    ) -> RoutingDecision | None:
        call_id = data.get("litellm_call_id")
        if not isinstance(call_id, str):
            return None
        with self._lock:
            return self._pending.pop(call_id, None)


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
    return proxy_server.app


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

    if _contains_refusal(payload.get("output")):
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
        if value.get("type") == "refusal":
            return True
        return any(_contains_refusal(item) for item in value.values())
    return False
