"""OpenAI Responses request adapter."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from zhunt.adapters.base import (
    AdapterError,
    WireAdapter,
    contains_tool_content,
    estimate_tokens,
    require_model,
    session_id_from_headers,
    system_first,
    text_content,
)
from zhunt.router import RoutingDecision, RoutingRequest


class OpenAIResponsesAdapter(WireAdapter):
    def apply_route(
        self,
        payload: Mapping[str, Any],
        decision: RoutingDecision,
    ) -> dict[str, Any]:
        routed = super().apply_route(payload, decision)
        input_value = routed.get("input")
        if isinstance(input_value, list):
            routed["input"] = system_first(input_value)
        return routed

    def normalize(
        self,
        payload: Mapping[str, Any],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> RoutingRequest:
        model_alias = require_model(payload)
        input_value = payload.get("input", "")
        system_parts = [text_content(payload.get("instructions"))]
        user_messages: list[str] = []
        has_tool_calls = bool(payload.get("tools"))

        if isinstance(input_value, str):
            user_messages.append(input_value)
        elif isinstance(input_value, list):
            for item in input_value:
                if not isinstance(item, Mapping):
                    continue
                role = item.get("role")
                item_text = text_content(item.get("content"))
                if role in {"system", "developer"}:
                    system_parts.append(item_text)
                elif role == "user":
                    user_messages.append(item_text)
                has_tool_calls = has_tool_calls or contains_tool_content(item)
        else:
            raise AdapterError("Responses payload input must be a string or list")

        max_output_tokens = payload.get("max_output_tokens")
        if max_output_tokens is None:
            max_output_tokens = 1
        if not isinstance(max_output_tokens, int) or max_output_tokens < 0:
            raise AdapterError(
                "max_output_tokens must be a non-negative integer or null"
            )

        return RoutingRequest(
            model_alias=model_alias,
            user_text=user_messages[-1] if user_messages else "",
            first_user_message=user_messages[0] if user_messages else "",
            system_prompt="\n".join(part for part in system_parts if part),
            session_id=session_id_from_headers(headers)
            or _responses_session_id(payload),
            estimated_input_tokens=estimate_tokens(payload),
            estimated_output_tokens=max_output_tokens,
            has_tool_calls=has_tool_calls,
        )


def _responses_session_id(payload: Mapping[str, Any]) -> str | None:
    conversation = payload.get("conversation")
    if isinstance(conversation, str) and conversation:
        return conversation
    if isinstance(conversation, Mapping):
        conversation_id = conversation.get("id")
        if isinstance(conversation_id, str) and conversation_id:
            return conversation_id

    metadata = payload.get("metadata")
    if isinstance(metadata, Mapping):
        metadata_session = metadata.get("session_id")
        if isinstance(metadata_session, str) and metadata_session:
            return metadata_session

    previous_response_id = payload.get("previous_response_id")
    if isinstance(previous_response_id, str) and previous_response_id:
        return previous_response_id
    return None
