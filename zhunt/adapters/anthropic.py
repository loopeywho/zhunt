"""Anthropic Messages request adapter."""

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


class AnthropicMessagesAdapter(WireAdapter):
    def normalize(
        self,
        payload: Mapping[str, Any],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> RoutingRequest:
        model_alias = require_model(payload)
        messages = payload.get("messages")
        if not isinstance(messages, list):
            raise AdapterError("Anthropic Messages payload requires a messages list")

        user_messages = [
            message
            for message in messages
            if isinstance(message, Mapping) and message.get("role") == "user"
        ]
        first_user_message = (
            text_content(user_messages[0].get("content")) if user_messages else ""
        )
        user_text = (
            text_content(user_messages[-1].get("content")) if user_messages else ""
        )
        metadata = payload.get("metadata")
        metadata_session_id = (
            metadata.get("session_id") if isinstance(metadata, Mapping) else None
        )
        if not isinstance(metadata_session_id, str):
            metadata_session_id = None

        max_tokens = payload.get("max_tokens", 1)
        if not isinstance(max_tokens, int) or max_tokens < 0:
            raise AdapterError("max_tokens must be a non-negative integer")

        return RoutingRequest(
            model_alias=model_alias,
            user_text=user_text,
            first_user_message=first_user_message,
            system_prompt=text_content(payload.get("system")),
            session_id=session_id_from_headers(headers) or metadata_session_id,
            estimated_input_tokens=estimate_tokens(payload),
            estimated_output_tokens=max_tokens,
            has_tool_calls=bool(payload.get("tools"))
            or any(
                contains_tool_content(message.get("content"))
                for message in messages
                if isinstance(message, Mapping)
            ),
        )
