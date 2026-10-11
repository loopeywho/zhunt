"""Translation between the inbound dialects and OpenAI Chat Completions.

Chat Completions is the one format zhunt sends to providers. Requests arriving as
OpenAI Responses or Anthropic Messages are turned into it, and the answers (JSON
or a stream) are turned back. Anthropic is the one provider that needs the
reverse: Chat Completions out, Anthropic Messages on the wire.
"""

from __future__ import annotations

import itertools
import json
import time
from collections.abc import AsyncIterator, Mapping
from typing import Any
from uuid import uuid4

from zhunt.adapters.base import text_content

# Anthropic requires max_tokens; LiteLLM's default when a caller sent none.
DEFAULT_ANTHROPIC_MAX_TOKENS = 4096

_TO_ANTHROPIC_STOP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "refusal",
}
_TO_CHAT_FINISH = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "pause_turn": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "refusal": "content_filter",
}


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:24]}"


def _items(value: Any) -> list[Mapping[str, Any]]:
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _json_object(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, Mapping):
        return dict(arguments)
    try:
        parsed = json.loads(arguments or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _first_choice(response: Mapping[str, Any]) -> tuple[Mapping[str, Any], str | None]:
    """The first choice's message and finish reason."""

    choices = _items(response.get("choices"))
    if not choices:
        return {}, None
    message = choices[0].get("message")
    return (message if isinstance(message, Mapping) else {}), choices[0].get("finish_reason")


def _usage(response: Mapping[str, Any]) -> tuple[int, int] | None:
    usage = response.get("usage")
    if not isinstance(usage, Mapping):
        return None
    prompt = usage.get("prompt_tokens", usage.get("input_tokens"))
    completion = usage.get("completion_tokens", usage.get("output_tokens"))
    if not isinstance(prompt, int) or not isinstance(completion, int):
        return None
    return prompt, completion


def _image_url(url: str) -> dict[str, Any]:
    return {"type": "image_url", "image_url": {"url": url}}


def _user_content(parts: list[dict[str, Any]]) -> str | list[dict[str, Any]]:
    """Plain text when there are no images: every provider takes a string."""

    if all(part["type"] == "text" for part in parts):
        return "\n".join(part["text"] for part in parts)
    return parts


def _function_tool(name: Any, description: Any, parameters: Any, strict: Any = None) -> dict:
    function: dict[str, Any] = {
        "name": name,
        "parameters": parameters if isinstance(parameters, Mapping)
        else {"type": "object", "properties": {}},
    }
    if isinstance(description, str) and description:
        function["description"] = description
    if isinstance(strict, bool):
        function["strict"] = strict
    return {"type": "function", "function": function}


def _tool_call(call_id: Any, name: Any, arguments: Any) -> dict[str, Any]:
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments if arguments is not None else {})
    return {
        "id": call_id if isinstance(call_id, str) and call_id else _new_id("call"),
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


# -- Anthropic Messages -> Chat Completions ------------------------------------------


def anthropic_to_chat_request(
    messages: list[Any], params: Mapping[str, Any]
) -> dict[str, Any]:
    chat: list[dict[str, Any]] = []
    system = text_content(params.get("system"))
    if system:
        chat.append({"role": "system", "content": system})
    for message in _items(messages):
        role, content = message.get("role"), message.get("content")
        if isinstance(content, str):
            chat.append({"role": role, "content": content})
        elif role == "assistant":
            chat.append(_anthropic_assistant(_items(content)))
        else:
            chat.extend(_anthropic_user(_items(content)))
    request: dict[str, Any] = {"messages": chat}
    tools = [
        _function_tool(t.get("name"), t.get("description"), t.get("input_schema"))
        for t in _items(params.get("tools"))
        if "input_schema" in t  # server tools (web search...) have none and are dropped
    ]
    if tools:
        request["tools"] = tools
        choice = _anthropic_tool_choice(params.get("tool_choice"))
        if choice is not None:
            request["tool_choice"] = choice
    for source, target in (
        ("max_tokens", "max_tokens"),
        ("temperature", "temperature"),
        ("top_p", "top_p"),
        ("stop_sequences", "stop"),
    ):
        if params.get(source) is not None:
            request[target] = params[source]
    return request


def _anthropic_assistant(blocks: list[Mapping[str, Any]]) -> dict[str, Any]:
    text = "".join(b.get("text") or "" for b in blocks if b.get("type") == "text")
    message: dict[str, Any] = {"role": "assistant", "content": text or None}
    calls = [
        _tool_call(b.get("id"), b.get("name"), b.get("input"))
        for b in blocks
        if b.get("type") == "tool_use"
    ]
    if calls:
        message["tool_calls"] = calls
    return message


def _anthropic_user(blocks: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Tool results become tool messages (first, so they follow the assistant's
    calls); everything else one user message."""

    tool_messages: list[dict[str, Any]] = []
    parts: list[dict[str, Any]] = []
    for block in blocks:
        kind = block.get("type")
        if kind == "tool_result":
            tool_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id"),
                    "content": text_content(block.get("content")),
                }
            )
        elif kind == "text":
            parts.append({"type": "text", "text": block.get("text") or ""})
        elif kind == "image":
            source = block.get("source")
            if isinstance(source, Mapping) and source.get("type") == "base64":
                parts.append(
                    _image_url(f"data:{source.get('media_type')};base64,{source.get('data')}")
                )
            elif isinstance(source, Mapping) and isinstance(source.get("url"), str):
                parts.append(_image_url(source["url"]))
    if parts:
        tool_messages.append({"role": "user", "content": _user_content(parts)})
    return tool_messages


def _anthropic_tool_choice(choice: Any) -> Any:
    if not isinstance(choice, Mapping):
        return None
    kind = choice.get("type")
    if kind == "tool":
        return {"type": "function", "function": {"name": choice.get("name")}}
    return {"auto": "auto", "any": "required", "none": "none"}.get(kind)


def chat_to_anthropic(response: Mapping[str, Any], model: str) -> dict[str, Any]:
    message, finish = _first_choice(response)
    content: list[dict[str, Any]] = []
    text = text_content(message.get("content")) or message.get("refusal") or ""
    if text:
        content.append({"type": "text", "text": text})
    for call in _items(message.get("tool_calls")):
        function = call.get("function") if isinstance(call.get("function"), Mapping) else {}
        content.append(
            {
                "type": "tool_use",
                "id": call.get("id") or _new_id("toolu"),
                "name": function.get("name"),
                "input": _json_object(function.get("arguments")),
            }
        )
    usage = _usage(response) or (0, 0)
    return {
        "id": _new_id("msg"),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": _TO_ANTHROPIC_STOP.get(finish, "end_turn"),
        "stop_sequence": None,
        "usage": {"input_tokens": usage[0], "output_tokens": usage[1]},
    }


async def chat_stream_to_anthropic(
    chunks: AsyncIterator[Mapping[str, Any]], model: str
) -> AsyncIterator[dict[str, Any]]:
    """Chat Completions chunks as Anthropic stream events."""

    yield {
        "type": "message_start",
        "message": {
            "id": _new_id("msg"),
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    }
    index = -1
    open_block: Any = None  # "text", or ("tool", chat tool index)
    tool_blocks: dict[int, int] = {}
    finish: str | None = None
    usage: tuple[int, int] | None = None
    async for chunk in chunks:
        usage = _usage(chunk) or usage
        for choice in _items(chunk.get("choices"))[:1]:
            delta = choice.get("delta") if isinstance(choice.get("delta"), Mapping) else {}
            text = delta.get("content")
            if isinstance(text, str) and text:
                if open_block != "text":
                    if open_block is not None:
                        yield {"type": "content_block_stop", "index": index}
                    index += 1
                    open_block = "text"
                    yield {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {"type": "text", "text": ""},
                    }
                yield {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "text_delta", "text": text},
                }
            for call in _items(delta.get("tool_calls")):
                position = call.get("index", 0)
                function = call.get("function") if isinstance(call.get("function"), Mapping) else {}
                if position not in tool_blocks:
                    if open_block is not None:
                        yield {"type": "content_block_stop", "index": index}
                    index += 1
                    tool_blocks[position] = index
                    open_block = ("tool", position)
                    yield {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {
                            "type": "tool_use",
                            "id": call.get("id") or _new_id("toolu"),
                            "name": function.get("name"),
                            "input": {},
                        },
                    }
                arguments = function.get("arguments")
                if isinstance(arguments, str) and arguments:
                    yield {
                        "type": "content_block_delta",
                        "index": tool_blocks[position],
                        "delta": {"type": "input_json_delta", "partial_json": arguments},
                    }
            finish = choice.get("finish_reason") or finish
    if open_block is not None:
        yield {"type": "content_block_stop", "index": index}
    input_tokens, output_tokens = usage or (0, 0)
    yield {
        "type": "message_delta",
        "delta": {"stop_reason": _TO_ANTHROPIC_STOP.get(finish, "end_turn"), "stop_sequence": None},
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    }
    yield {"type": "message_stop"}


# -- Chat Completions -> Anthropic Messages (the premium provider) --------------------


def chat_to_anthropic_request(body: Mapping[str, Any]) -> dict[str, Any]:
    system: list[str] = []
    messages: list[dict[str, Any]] = []
    for message in _items(body.get("messages")):
        role, content = message.get("role"), message.get("content")
        if role in ("system", "developer"):
            system.append(text_content(content))
        elif role == "assistant":
            blocks: list[dict[str, Any]] = []
            text = text_content(content)
            if text:
                blocks.append({"type": "text", "text": text})
            for call in _items(message.get("tool_calls")):
                function = call.get("function") if isinstance(call.get("function"), Mapping) else {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.get("id") or _new_id("toolu"),
                        "name": function.get("name"),
                        "input": _json_object(function.get("arguments")),
                    }
                )
            _append(messages, "assistant", blocks)
        elif role in ("tool", "function"):
            result = {
                "type": "tool_result",
                "tool_use_id": message.get("tool_call_id"),
                "content": text_content(content),
            }
            _append(messages, "user", [result])
        else:
            _append(messages, "user", _anthropic_blocks(content))
    request: dict[str, Any] = {
        "model": body.get("model"),
        "messages": messages,
        "max_tokens": body.get("max_tokens")
        or body.get("max_completion_tokens")
        or DEFAULT_ANTHROPIC_MAX_TOKENS,
    }
    joined = "\n".join(part for part in system if part)
    if joined:
        request["system"] = joined
    for key in ("temperature", "top_p"):
        if body.get(key) is not None:
            request[key] = body[key]
    stop = body.get("stop")
    if stop:
        request["stop_sequences"] = [stop] if isinstance(stop, str) else list(stop)
    tools = []
    for tool in _items(body.get("tools")):
        function = tool.get("function") if isinstance(tool.get("function"), Mapping) else {}
        anthropic_tool = {
            "name": function.get("name"),
            "input_schema": function.get("parameters") or {"type": "object", "properties": {}},
        }
        if function.get("description"):
            anthropic_tool["description"] = function["description"]
        tools.append(anthropic_tool)
    if tools:
        request["tools"] = tools
        choice = body.get("tool_choice")
        if isinstance(choice, Mapping):
            name = choice.get("function", {}).get("name") if isinstance(choice.get("function"), Mapping) else None
            request["tool_choice"] = {"type": "tool", "name": name}
        elif choice in ("auto", "required", "none"):
            request["tool_choice"] = {"type": {"auto": "auto", "required": "any", "none": "none"}[choice]}
    return request


def _anthropic_blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    blocks: list[dict[str, Any]] = []
    for part in _items(content):
        if part.get("type") == "text" and part.get("text"):
            blocks.append({"type": "text", "text": part["text"]})
        elif part.get("type") == "image_url":
            image = part.get("image_url")
            url = image.get("url") if isinstance(image, Mapping) else image
            if not isinstance(url, str):
                continue
            if url.startswith("data:") and ";base64," in url:
                media_type, data = url[5:].split(";base64,", 1)
                source = {"type": "base64", "media_type": media_type, "data": data}
            else:
                source = {"type": "url", "url": url}
            blocks.append({"type": "image", "source": source})
    return blocks


def _append(messages: list[dict[str, Any]], role: str, blocks: list[dict[str, Any]]) -> None:
    """Anthropic wants strictly alternating roles, so same-role turns merge."""

    if not blocks:
        return
    if messages and messages[-1]["role"] == role:
        messages[-1]["content"].extend(blocks)
    else:
        messages.append({"role": role, "content": blocks})


def anthropic_to_chat(reply: Mapping[str, Any], model: str) -> dict[str, Any]:
    blocks = _items(reply.get("content"))
    text = "".join(b.get("text") or "" for b in blocks if b.get("type") == "text")
    message: dict[str, Any] = {"role": "assistant", "content": text or None}
    calls = [
        _tool_call(b.get("id"), b.get("name"), b.get("input"))
        for b in blocks
        if b.get("type") == "tool_use"
    ]
    if calls:
        message["tool_calls"] = calls
    usage = reply.get("usage") if isinstance(reply.get("usage"), Mapping) else {}
    prompt, completion = usage.get("input_tokens") or 0, usage.get("output_tokens") or 0
    return {
        "id": reply.get("id") or _new_id("chatcmpl"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": _TO_CHAT_FINISH.get(reply.get("stop_reason"), "stop"),
            }
        ],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        },
    }


async def anthropic_stream_to_chat(
    events: AsyncIterator[Mapping[str, Any]], model: str
) -> AsyncIterator[dict[str, Any]]:
    """Anthropic stream events as Chat Completions chunks."""

    chunk_id, created = _new_id("chatcmpl"), int(time.time())
    tool_index: dict[int, int] = {}
    prompt = 0

    def chunk(delta: dict[str, Any], finish: str | None = None, **extra: Any) -> dict[str, Any]:
        return {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
            **extra,
        }

    async for event in events:
        kind = event.get("type")
        if kind == "message_start":
            message = event.get("message") if isinstance(event.get("message"), Mapping) else {}
            usage = message.get("usage") if isinstance(message.get("usage"), Mapping) else {}
            prompt = usage.get("input_tokens") or 0
            yield chunk({"role": "assistant", "content": ""})
        elif kind == "content_block_start":
            block = event.get("content_block") if isinstance(event.get("content_block"), Mapping) else {}
            if block.get("type") == "tool_use":
                position = tool_index.setdefault(event.get("index", 0), len(tool_index))
                yield chunk({"tool_calls": [{
                    "index": position, "id": block.get("id"), "type": "function",
                    "function": {"name": block.get("name"), "arguments": ""},
                }]})
        elif kind == "content_block_delta":
            delta = event.get("delta") if isinstance(event.get("delta"), Mapping) else {}
            if delta.get("type") == "text_delta":
                yield chunk({"content": delta.get("text") or ""})
            elif delta.get("type") == "input_json_delta":
                position = tool_index.get(event.get("index", 0), 0)
                yield chunk({"tool_calls": [{
                    "index": position, "function": {"arguments": delta.get("partial_json") or ""},
                }]})
        elif kind == "message_delta":
            delta = event.get("delta") if isinstance(event.get("delta"), Mapping) else {}
            usage = event.get("usage") if isinstance(event.get("usage"), Mapping) else {}
            completion = usage.get("output_tokens") or 0
            prompt = usage.get("input_tokens") or prompt
            yield chunk(
                {},
                _TO_CHAT_FINISH.get(delta.get("stop_reason"), "stop"),
                usage={
                    "prompt_tokens": prompt,
                    "completion_tokens": completion,
                    "total_tokens": prompt + completion,
                },
            )


# -- OpenAI Responses -> Chat Completions ---------------------------------------------


def responses_to_chat_request(input_value: Any, params: Mapping[str, Any]) -> dict[str, Any]:
    messages: list[dict[str, Any]] = []
    instructions = text_content(params.get("instructions"))
    if instructions:
        messages.append({"role": "system", "content": instructions})
    if isinstance(input_value, str):
        messages.append({"role": "user", "content": input_value})
    for item in _items(input_value):
        kind = item.get("type") or ("message" if "role" in item else None)
        if kind == "message":
            messages.append(_responses_message(item))
        elif kind == "function_call":
            call = _tool_call(item.get("call_id"), item.get("name"), item.get("arguments"))
            last = messages[-1] if messages else None
            if last is not None and last["role"] == "assistant":
                last.setdefault("tool_calls", []).append(call)
            else:
                messages.append({"role": "assistant", "content": None, "tool_calls": [call]})
        elif kind == "function_call_output":
            output = item.get("output")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item.get("call_id"),
                    "content": output if isinstance(output, str) else text_content(output)
                    or json.dumps(output),
                }
            )
    request: dict[str, Any] = {"messages": messages}
    tools = [
        _function_tool(t.get("name"), t.get("description"), t.get("parameters"), t.get("strict"))
        for t in _items(params.get("tools"))
        if t.get("type") == "function"  # built-in tools (web search...) are dropped
    ]
    if tools:
        request["tools"] = tools
        choice = params.get("tool_choice")
        if choice in ("auto", "required", "none"):
            request["tool_choice"] = choice
        elif isinstance(choice, Mapping) and choice.get("type") == "function":
            request["tool_choice"] = {"type": "function", "function": {"name": choice.get("name")}}
        if isinstance(params.get("parallel_tool_calls"), bool):
            request["parallel_tool_calls"] = params["parallel_tool_calls"]
    for source, target in (
        ("max_output_tokens", "max_tokens"),
        ("temperature", "temperature"),
        ("top_p", "top_p"),
    ):
        if params.get(source) is not None:
            request[target] = params[source]
    text = params.get("text")
    fmt = text.get("format") if isinstance(text, Mapping) else None
    if isinstance(fmt, Mapping) and fmt.get("type") == "json_schema":
        schema = {k: fmt[k] for k in ("name", "schema", "strict", "description") if k in fmt}
        request["response_format"] = {"type": "json_schema", "json_schema": schema}
    elif isinstance(fmt, Mapping) and fmt.get("type") == "json_object":
        request["response_format"] = {"type": "json_object"}
    return request


def _responses_message(item: Mapping[str, Any]) -> dict[str, Any]:
    role = item.get("role")
    role = "system" if role == "developer" else role  # most free providers lack "developer"
    content = item.get("content")
    if isinstance(content, str):
        return {"role": role, "content": content}
    parts: list[dict[str, Any]] = []
    for part in _items(content):
        kind = part.get("type")
        if kind in ("input_text", "output_text", "text"):
            parts.append({"type": "text", "text": part.get("text") or ""})
        elif kind == "refusal":
            parts.append({"type": "text", "text": part.get("refusal") or ""})
        elif kind == "input_image" and isinstance(part.get("image_url"), str):
            parts.append(_image_url(part["image_url"]))
    if role != "user":
        return {"role": role, "content": "\n".join(p.get("text", "") for p in parts)}
    return {"role": role, "content": _user_content(parts)}


def _response_object(
    response_id: str,
    created: int,
    model: str,
    output: list[dict[str, Any]],
    status: str,
    usage: tuple[int, int] | None,
) -> dict[str, Any]:
    return {
        "id": response_id,
        "object": "response",
        "created_at": created,
        "status": status,
        "model": model,
        "output": output,
        "incomplete_details": {"reason": "max_output_tokens"} if status == "incomplete" else None,
        "error": None,
        "usage": None if usage is None else {
            "input_tokens": usage[0],
            "output_tokens": usage[1],
            "total_tokens": usage[0] + usage[1],
        },
    }


def _message_item(item_id: str, text: str, status: str, kind: str = "output_text") -> dict:
    part = (
        {"type": "refusal", "refusal": text}
        if kind == "refusal"
        else {"type": "output_text", "text": text, "annotations": []}
    )
    return {
        "type": "message",
        "id": item_id,
        "status": status,
        "role": "assistant",
        "content": [part] if status == "completed" else [],
    }


def _call_item(item_id: str, call_id: Any, name: Any, arguments: str, status: str) -> dict:
    return {
        "type": "function_call",
        "id": item_id,
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
        "status": status,
    }


def chat_to_responses(response: Mapping[str, Any], model: str) -> dict[str, Any]:
    message, finish = _first_choice(response)
    output: list[dict[str, Any]] = []
    text = text_content(message.get("content"))
    if text:
        output.append(_message_item(_new_id("msg"), text, "completed"))
    elif isinstance(message.get("refusal"), str) and message["refusal"]:
        output.append(_message_item(_new_id("msg"), message["refusal"], "completed", "refusal"))
    for call in _items(message.get("tool_calls")):
        function = call.get("function") if isinstance(call.get("function"), Mapping) else {}
        output.append(
            _call_item(
                _new_id("fc"), call.get("id"), function.get("name"),
                function.get("arguments") or "", "completed",
            )
        )
    status = "incomplete" if finish == "length" else "completed"
    return _response_object(
        _new_id("resp"), int(time.time()), model, output, status, _usage(response)
    )


async def chat_stream_to_responses(
    chunks: AsyncIterator[Mapping[str, Any]], model: str
) -> AsyncIterator[dict[str, Any]]:
    """Chat Completions chunks as Responses stream events."""

    response_id, created = _new_id("resp"), int(time.time())
    sequence = itertools.count()

    def event(kind: str, **fields: Any) -> dict[str, Any]:
        return {"type": kind, "sequence_number": next(sequence), **fields}

    started = _response_object(response_id, created, model, [], "in_progress", None)
    yield event("response.created", response=started)
    yield event("response.in_progress", response=started)

    # Output items in the order they started: one text message, then tool calls.
    text_item: dict[str, Any] | None = None
    calls: dict[int, dict[str, Any]] = {}
    order: list[dict[str, Any]] = []
    finish: str | None = None
    usage: tuple[int, int] | None = None
    async for chunk in chunks:
        usage = _usage(chunk) or usage
        for choice in _items(chunk.get("choices"))[:1]:
            delta = choice.get("delta") if isinstance(choice.get("delta"), Mapping) else {}
            text = delta.get("content")
            if isinstance(text, str) and text:
                if text_item is None:
                    text_item = {"kind": "text", "id": _new_id("msg"), "index": len(order), "text": ""}
                    order.append(text_item)
                    yield event(
                        "response.output_item.added",
                        output_index=text_item["index"],
                        item=_message_item(text_item["id"], "", "in_progress"),
                    )
                    yield event(
                        "response.content_part.added",
                        item_id=text_item["id"], output_index=text_item["index"],
                        content_index=0,
                        part={"type": "output_text", "text": "", "annotations": []},
                    )
                text_item["text"] += text
                yield event(
                    "response.output_text.delta",
                    item_id=text_item["id"], output_index=text_item["index"],
                    content_index=0, delta=text,
                )
            for call in _items(delta.get("tool_calls")):
                position = call.get("index", 0)
                function = call.get("function") if isinstance(call.get("function"), Mapping) else {}
                item = calls.get(position)
                if item is None:
                    item = {
                        "kind": "call", "id": _new_id("fc"), "index": len(order),
                        "call_id": call.get("id") or _new_id("call"),
                        "name": function.get("name"), "arguments": "",
                    }
                    calls[position] = item
                    order.append(item)
                    yield event(
                        "response.output_item.added",
                        output_index=item["index"],
                        item=_call_item(item["id"], item["call_id"], item["name"], "", "in_progress"),
                    )
                arguments = function.get("arguments")
                if isinstance(arguments, str) and arguments:
                    item["arguments"] += arguments
                    yield event(
                        "response.function_call_arguments.delta",
                        item_id=item["id"], output_index=item["index"], delta=arguments,
                    )
            finish = choice.get("finish_reason") or finish

    output: list[dict[str, Any]] = []
    for item in order:
        if item["kind"] == "text":
            done = _message_item(item["id"], item["text"], "completed")
            part = done["content"][0]
            yield event(
                "response.output_text.done",
                item_id=item["id"], output_index=item["index"], content_index=0,
                text=item["text"],
            )
            yield event(
                "response.content_part.done",
                item_id=item["id"], output_index=item["index"], content_index=0, part=part,
            )
        else:
            done = _call_item(item["id"], item["call_id"], item["name"], item["arguments"], "completed")
            yield event(
                "response.function_call_arguments.done",
                item_id=item["id"], output_index=item["index"], arguments=item["arguments"],
            )
        output.append(done)
        yield event("response.output_item.done", output_index=item["index"], item=done)
    status = "incomplete" if finish == "length" else "completed"
    final = _response_object(response_id, created, model, output, status, usage)
    yield event(f"response.{status}", response=final)
