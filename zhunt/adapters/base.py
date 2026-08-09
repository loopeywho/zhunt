"""Shared adapter contract for protocol-specific request normalization."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from collections.abc import Collection, Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from zhunt.router import RoutingCoordinator, RoutingDecision, RoutingRequest


class AdapterError(ValueError):
    """Raised when an inbound payload cannot be normalized safely."""


@dataclass(frozen=True)
class RoutedPayload:
    request: RoutingRequest
    decision: RoutingDecision
    upstream_payload: dict[str, Any]


class WireAdapter(ABC):
    """Normalize a dialect, invoke the shared router, then rewrite its model."""

    @abstractmethod
    def normalize(
        self,
        payload: Mapping[str, Any],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> RoutingRequest:
        raise NotImplementedError

    def apply_route(
        self,
        payload: Mapping[str, Any],
        decision: RoutingDecision,
    ) -> dict[str, Any]:
        routed = deepcopy(dict(payload))
        routed["model"] = decision.model
        return routed

    def route(
        self,
        payload: Mapping[str, Any],
        coordinator: RoutingCoordinator,
        *,
        headers: Mapping[str, str] | None = None,
        healthy_models: Collection[str] | None = None,
        latency_metrics: Mapping[str, float] | None = None,
    ) -> RoutedPayload:
        request = self.normalize(payload, headers=headers)
        decision = coordinator.route(
            request,
            healthy_models=healthy_models,
            latency_metrics=latency_metrics,
        )
        return RoutedPayload(
            request=request,
            decision=decision,
            upstream_payload=self.apply_route(payload, decision),
        )


def require_model(payload: Mapping[str, Any]) -> str:
    model = payload.get("model")
    if not isinstance(model, str) or not model:
        raise AdapterError("payload requires a non-empty model alias")
    return model


def session_id_from_headers(
    headers: Mapping[str, str] | None,
) -> str | None:
    if not headers:
        return None
    normalized = {key.lower(): value for key, value in headers.items()}
    return normalized.get("x-session-id") or normalized.get("x-conversation-id")


def text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [text_content(item) for item in content]
        return "\n".join(part for part in parts if part)
    if isinstance(content, Mapping):
        text = content.get("text")
        return text if isinstance(text, str) else ""
    return ""


def contains_tool_content(content: Any) -> bool:
    if isinstance(content, list):
        return any(contains_tool_content(item) for item in content)
    if isinstance(content, Mapping):
        content_type = content.get("type")
        return content_type in {
            "function_call",
            "function_call_output",
            "tool_call",
            "tool_result",
            "tool_use",
        } or (
            isinstance(content_type, str)
            and (
                content_type.endswith("_call")
                or content_type.endswith("_call_output")
            )
        )
    return False


def estimate_tokens(payload: Mapping[str, Any]) -> int:
    serialized = json.dumps(payload, separators=(",", ":"), default=str)
    return max(1, len(serialized) // 4)


def system_first(items: list[Any]) -> list[Any]:
    """Move system/developer-role items to the front, preserving relative order.

    OpenAI-style wire formats (Chat Completions ``messages``, Responses
    ``input``) allow a system/developer-role item anywhere in the array.
    Anthropic-family backends do not: reproduced 2026-08-09 against
    ``openrouter/anthropic/claude-sonnet-5`` — a system item at any position
    other than the very start (or a directive-only empty-content item)
    fails with "role 'system' must precede an 'assistant' message or end
    the array", identically across four backend infra providers (Anthropic
    direct, AWS Bedrock, Google, Azure), confirming it's a translation
    constraint, not one provider's quirk. Reordering once here, at the
    adapter boundary Zhunt owns, fixes it for every downstream provider
    rather than depending on a LiteLLM-internal translation fix. A pure
    reorder (no merging, no dropped content) is deliberately conservative —
    every other role's relative order is untouched.
    """

    leading: list[Any] = []
    rest: list[Any] = []
    for item in items:
        if isinstance(item, Mapping) and item.get("role") in {"system", "developer"}:
            leading.append(item)
        else:
            rest.append(item)
    return leading + rest
