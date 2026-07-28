"""OpenAI Chat Completions request adapter."""

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
    text_content,
)
from zhunt.router import RoutingRequest


class OpenAIChatCompletionsAdapter(WireAdapter):
    def normalize(
        self,
        payload: Mapping[str, Any],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> RoutingRequest:
        model_alias = require_model(payload)
        messages = payload.get("messages")
        if not isinstance(messages, list):
            raise AdapterError(
                "Chat Completions payload requires a messages list"
            )

        system_parts: list[str] = []
        user_messages: list[str] = []
        has_tool_calls = bool(payload.get("tools"))
        for message in messages:
            if not isinstance(message, Mapping):
                continue
            role = message.get("role")
            content = message.get("content")
            if role in {"system", "developer"}:
                system_parts.append(text_content(content))
            elif role == "user":
                user_messages.append(text_content(content))
            if role in {"tool", "function"}:
                has_tool_calls = True
            has_tool_calls = (
                has_tool_calls
                or bool(message.get("tool_calls"))
                or bool(message.get("function_call"))
                or contains_tool_content(content)
            )

        max_tokens = payload.get(
            "max_completion_tokens",
            payload.get("max_tokens", 1),
        )
        if max_tokens is None:
            max_tokens = 1
        if not isinstance(max_tokens, int) or max_tokens < 0:
            raise AdapterError(
                "max_completion_tokens/max_tokens must be a non-negative integer"
            )

        metadata = payload.get("metadata")
        metadata_session_id = (
            metadata.get("session_id") if isinstance(metadata, Mapping) else None
        )
        if not isinstance(metadata_session_id, str):
            metadata_session_id = None

        return RoutingRequest(
            model_alias=model_alias,
            user_text=user_messages[-1] if user_messages else "",
            first_user_message=user_messages[0] if user_messages else "",
            system_prompt="\n".join(part for part in system_parts if part),
            session_id=session_id_from_headers(headers) or metadata_session_id,
            estimated_input_tokens=estimate_tokens(payload),
            estimated_output_tokens=max_tokens,
            has_tool_calls=has_tool_calls,
        )
