"""zhunt.llm, the provider client, against a fake HTTP transport."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from zhunt import llm


class FakeProvider:
    """Answers every request with the next queued response and records the request."""

    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)

    @property
    def body(self) -> dict[str, Any]:
        return json.loads(self.requests[-1].content)


@pytest.fixture
def provider(monkeypatch):
    fake = FakeProvider()
    monkeypatch.setattr(
        llm, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake))
    )
    return fake


def run(coro):
    return asyncio.run(coro)


async def collect(stream) -> list[dict[str, Any]]:
    return [dict(chunk) async for chunk in stream]


def sse(*events: Any, event_names: bool = False) -> bytes:
    lines = []
    for event in events:
        if event == "[DONE]":
            lines.append("data: [DONE]\n\n")
        elif event_names:
            lines.append(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n")
        else:
            lines.append(f"data: {json.dumps(event)}\n\n")
    return "".join(lines).encode()


CHAT_ANSWER = {
    "id": "c1",
    "object": "chat.completion",
    "model": "llama",
    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "hi"}}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
}
MESSAGES = [{"role": "user", "content": "hello"}]


# -- Chat Completions -------------------------------------------------------------


def test_named_routes_go_to_their_provider_with_the_users_key(provider) -> None:
    for route, url in (
        ("groq/llama", "https://api.groq.com/openai/v1/chat/completions"),
        ("cerebras/llama", "https://api.cerebras.ai/v1/chat/completions"),
        ("gemini/gemini-3.8-flash",
         "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"),
        ("openai/gpt-5", "https://api.openai.com/v1/chat/completions"),
    ):
        provider.responses.append(httpx.Response(200, json=CHAT_ANSWER))
        answer = run(llm.acompletion(model=route, messages=MESSAGES, api_key="k-1"))
        assert str(provider.requests[-1].url) == url
        assert provider.requests[-1].headers["authorization"] == "Bearer k-1"
        assert provider.body["model"] == route.split("/", 1)[1]
        assert answer["choices"][0]["message"]["content"] == "hi"


def test_a_catalog_api_base_is_used_and_model_ids_keep_their_slashes(provider) -> None:
    provider.responses.append(httpx.Response(200, json=CHAT_ANSWER))
    run(llm.acompletion(
        model="openai/kilo-auto/free", messages=MESSAGES, api_key="anonymous",
        api_base="https://api.kilo.ai/api/gateway/",
    ))
    assert str(provider.requests[0].url) == "https://api.kilo.ai/api/gateway/chat/completions"
    assert provider.body["model"] == "kilo-auto/free"


def _call(call_id: str, signature: str | None = None) -> dict[str, Any]:
    call = {"id": call_id, "type": "function", "function": {"name": "web_search", "arguments": "{}"}}
    if signature:
        call["extra_content"] = {"google": {"thought_signature": signature}}
    return call


TOOL_HISTORY = [
    {"role": "user", "content": "look it up"},
    {"role": "assistant", "content": "", "tool_calls": [_call("a"), _call("b")]},  # another model's turn
    {"role": "tool", "tool_call_id": "a", "content": "x"},
    {"role": "tool", "tool_call_id": "b", "content": "y"},
    {"role": "assistant", "content": "", "tool_calls": [_call("c", "real-sig")]},  # Gemini's own turn
    {"role": "tool", "tool_call_id": "c", "content": "z"},
]


def test_gemini_gets_a_placeholder_signature_on_tool_calls_it_did_not_make(provider) -> None:
    provider.responses.append(httpx.Response(200, json=CHAT_ANSWER))
    run(llm.acompletion(model="gemini/gemini-3.8-flash", messages=TOOL_HISTORY, api_key="k"))

    sent = [m.get("tool_calls") for m in provider.body["messages"] if m.get("tool_calls")]
    signatures = [[llm._thought_signature(c) for c in calls] for calls in sent]
    assert signatures == [["skip_thought_signature_validator", None], ["real-sig"]]
    assert "extra_content" not in TOOL_HISTORY[1]["tool_calls"][0]  # the caller's list is untouched


def test_other_providers_get_tool_calls_as_sent(provider) -> None:
    provider.responses.append(httpx.Response(200, json=CHAT_ANSWER))
    run(llm.acompletion(model="groq/llama", messages=TOOL_HISTORY, api_key="k"))

    assert provider.body["messages"] == TOOL_HISTORY


def test_raw_newlines_inside_provider_strings_are_accepted(provider) -> None:
    # Kilo's NVIDIA models send reasoning text with unescaped newlines.
    raw = json.dumps(CHAT_ANSWER).replace('"content": "hi"', '"content": "hi", "reasoning": "line one\nline two"').replace("\\n", "\n")
    provider.responses.append(httpx.Response(200, content=raw.encode(), headers={"content-type": "application/json"}))
    answer = run(llm.acompletion(model="groq/llama", messages=MESSAGES, api_key="k-1"))
    assert answer["choices"][0]["message"]["content"] == "hi"


def test_an_unknown_route_without_an_endpoint_is_a_client_error(provider) -> None:
    with pytest.raises(llm.BadRequestError):
        run(llm.acompletion(model="alpha/m", messages=MESSAGES))
    assert provider.requests == []


def test_only_standard_fields_reach_the_provider(provider) -> None:
    provider.responses.append(httpx.Response(200, json=CHAT_ANSWER))
    run(llm.acompletion(
        model="groq/llama", messages=MESSAGES, api_key="k",
        temperature=0.2, max_completion_tokens=50, tools=[], tool_choice="auto",
        metadata={"zhunt": {"tier": "chat"}}, proxy_server_request={"headers": {"authorization": "Bearer master"}},
        litellm_call_id="x", max_retries=0, num_retries=0, user="u", stream_options={"include_usage": True},
    ))
    assert provider.body == {
        "model": "llama", "messages": MESSAGES, "temperature": 0.2, "max_tokens": 50,
    }


def test_openai_gets_max_completion_tokens(provider) -> None:
    provider.responses.append(httpx.Response(200, json=CHAT_ANSWER))
    run(llm.acompletion(model="openai/gpt-5", messages=MESSAGES, api_key="k", max_tokens=9))
    assert provider.body["max_completion_tokens"] == 9 and "max_tokens" not in provider.body


def test_answers_keep_the_providers_rate_limit_headers(provider) -> None:
    provider.responses.append(
        httpx.Response(200, json=CHAT_ANSWER, headers={"x-ratelimit-limit-tokens": "6000"})
    )
    answer = run(llm.acompletion(model="groq/llama", messages=MESSAGES, api_key="k"))
    assert answer.headers["x-ratelimit-limit-tokens"] == "6000"


@pytest.mark.parametrize(
    ("status", "cls"),
    [
        (400, llm.BadRequestError), (401, llm.AuthenticationError), (402, llm.BadRequestError),
        (403, llm.PermissionDeniedError), (404, llm.NotFoundError), (413, llm.BadRequestError),
        (429, llm.RateLimitError), (500, llm.InternalServerError), (503, llm.ServiceUnavailableError),
    ],
)
def test_http_errors_keep_status_body_and_headers(provider, status: int, cls) -> None:
    body = {"error": {"message": "Rate limit reached on tokens per minute (TPM): Limit 6000"}}
    provider.responses.append(httpx.Response(status, json=body, headers={"retry-after": "7"}))
    with pytest.raises(cls) as raised:
        run(llm.acompletion(model="groq/llama", messages=MESSAGES, api_key="k"))
    error = raised.value
    assert error.status_code == status
    assert error.response_headers["retry-after"] == "7"
    assert "Limit 6000" in str(error)


def test_connection_failures_are_server_side_errors(monkeypatch) -> None:
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)

    monkeypatch.setattr(
        llm, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    )
    with pytest.raises(llm.APIConnectionError) as raised:
        run(llm.acompletion(model="groq/llama", messages=MESSAGES, api_key="k"))
    assert raised.value.status_code == 500


def test_streams_yield_chunks_until_done(provider) -> None:
    chunks = [
        {"id": "s", "choices": [{"index": 0, "delta": {"content": "he"}, "finish_reason": None}]},
        {"id": "s", "choices": [{"index": 0, "delta": {"content": "llo"}, "finish_reason": "stop"}]},
    ]
    provider.responses.append(httpx.Response(
        200, content=b": keep-alive\n\n" + sse(*chunks, "[DONE]"),
        headers={"content-type": "text/event-stream", "x-ratelimit-limit-requests": "30"},
    ))

    async def go():
        stream = await llm.acompletion(model="groq/llama", messages=MESSAGES, api_key="k", stream=True)
        got = [chunk async for chunk in stream]
        return got

    got = run(go())
    assert [dict(c) for c in got] == chunks
    assert got[-1].headers["x-ratelimit-limit-requests"] == "30"
    assert provider.body["stream"] is True


def test_a_stream_rejected_up_front_raises_before_any_chunk(provider) -> None:
    provider.responses.append(httpx.Response(429, json={"error": {"message": "slow down"}}))
    with pytest.raises(llm.RateLimitError):
        run(llm.acompletion(model="groq/llama", messages=MESSAGES, api_key="k", stream=True))


def test_an_error_event_mid_stream_raises(provider) -> None:
    provider.responses.append(httpx.Response(200, content=sse(
        {"choices": [{"index": 0, "delta": {"content": "par"}}]},
        {"error": {"message": "upstream died", "code": 503}},
    )))

    async def go():
        stream = await llm.acompletion(model="groq/llama", messages=MESSAGES, api_key="k", stream=True)
        return await collect(stream)

    with pytest.raises(llm.ServiceUnavailableError, match="upstream died"):
        run(go())


# -- Anthropic (premium) ------------------------------------------------------------

ANTHROPIC_ANSWER = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "claude-haiku-5-5",
    "content": [
        {"type": "text", "text": "Reading it."},
        {"type": "tool_use", "id": "toolu_1", "name": "read_file", "input": {"path": "a.py"}},
    ],
    "stop_reason": "tool_use",
    "usage": {"input_tokens": 12, "output_tokens": 5},
}


def test_chat_calls_to_anthropic_are_sent_as_messages_and_answered_as_chat(provider) -> None:
    provider.responses.append(httpx.Response(200, json=ANTHROPIC_ANSWER))
    answer = run(llm.acompletion(
        model="anthropic/claude-haiku-5-5",
        api_key="sk-ant",
        messages=[
            {"role": "system", "content": "Be brief."},
            {"role": "user", "content": "Open a.py"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_0", "type": "function", "function": {"name": "ls", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_0", "content": "a.py"},
        ],
        tools=[{"type": "function", "function": {"name": "read_file", "parameters": {"type": "object"}}}],
        tool_choice="required",
        stop="END",
    ))

    request = provider.requests[0]
    assert str(request.url) == "https://api.anthropic.com/v1/messages"
    assert request.headers["x-api-key"] == "sk-ant" and "authorization" not in request.headers
    assert request.headers["anthropic-version"] == llm.ANTHROPIC_VERSION
    assert provider.body == {
        "model": "claude-haiku-5-5",
        "system": "Be brief.",
        "max_tokens": 4096,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "Open a.py"}]},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "call_0", "name": "ls", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call_0", "content": "a.py"}]},
        ],
        "tools": [{"name": "read_file", "input_schema": {"type": "object"}}],
        "tool_choice": {"type": "any"},
        "stop_sequences": ["END"],
    }
    choice = answer["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] == "Reading it."
    assert choice["message"]["tool_calls"][0]["function"] == {
        "name": "read_file", "arguments": '{"path": "a.py"}'
    }
    assert answer["usage"] == {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}


def test_anthropic_streams_become_chat_chunks(provider) -> None:
    provider.responses.append(httpx.Response(200, content=sse(
        {"type": "message_start", "message": {"usage": {"input_tokens": 9, "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "ping"},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "content_block_start", "index": 1,
         "content_block": {"type": "tool_use", "id": "toolu_2", "name": "ls", "input": {}}},
        {"type": "content_block_delta", "index": 1,
         "delta": {"type": "input_json_delta", "partial_json": "{\"p\":1}"}},
        {"type": "content_block_stop", "index": 1},
        {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}, "usage": {"output_tokens": 4}},
        {"type": "message_stop"},
        event_names=True,
    )))

    async def go():
        stream = await llm.acompletion(
            model="anthropic/claude-haiku-5-5", messages=MESSAGES, api_key="k", stream=True
        )
        return await collect(stream)

    chunks = run(go())
    deltas = [c["choices"][0]["delta"] for c in chunks]
    assert "".join(d.get("content") or "" for d in deltas) == "Hi"
    calls = [call for d in deltas for call in d.get("tool_calls", [])]
    assert calls[0]["id"] == "toolu_2" and calls[0]["function"]["name"] == "ls"
    assert "".join(c["function"]["arguments"] for c in calls) == "{\"p\":1}"
    assert chunks[-1]["choices"][0]["finish_reason"] == "length"
    assert chunks[-1]["usage"] == {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13}
    assert provider.body["stream"] is True


def test_messages_to_anthropic_pass_straight_through(provider) -> None:
    provider.responses.append(httpx.Response(200, json=ANTHROPIC_ANSWER))
    answer = run(llm.anthropic_messages(
        model="anthropic/claude-haiku-5-5", api_key="k", max_tokens=100,
        messages=[{"role": "user", "content": "hi"}], system="S",
        metadata={"zhunt": {"tier": "chat"}}, litellm_call_id="x",
        proxy_server_request={"headers": {}},
    ))
    assert provider.body == {
        "model": "claude-haiku-5-5", "max_tokens": 100, "system": "S",
        "messages": [{"role": "user", "content": "hi"}],
    }
    assert dict(answer) == ANTHROPIC_ANSWER


# -- the other dialects through a Chat Completions provider ------------------------


def test_messages_to_a_chat_provider_are_translated_both_ways(provider) -> None:
    provider.responses.append(httpx.Response(200, json={
        "choices": [{"index": 0, "finish_reason": "length", "message": {
            "role": "assistant", "content": "Sure",
            "tool_calls": [{"id": "call_9", "type": "function",
                            "function": {"name": "read_file", "arguments": "{\"path\":\"x\"}"}}],
        }}],
        "usage": {"prompt_tokens": 7, "completion_tokens": 2},
    }))
    answer = run(llm.anthropic_messages(
        model="groq/llama", api_key="k", max_tokens=64,
        system=[{"type": "text", "text": "You are Claude Code."}],
        messages=[
            {"role": "user", "content": "Read x"},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "hm"},
                {"type": "tool_use", "id": "toolu_1", "name": "ls", "input": {}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": [{"type": "text", "text": "x"}]},
                {"type": "text", "text": "go on"},
            ]},
        ],
        tools=[
            {"name": "read_file", "description": "Read", "input_schema": {"type": "object"}},
            {"type": "web_search_20250305", "name": "web_search"},
        ],
        tool_choice={"type": "tool", "name": "read_file"},
    ))

    assert provider.body == {
        "model": "llama",
        "max_tokens": 64,
        "messages": [
            {"role": "system", "content": "You are Claude Code."},
            {"role": "user", "content": "Read x"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "toolu_1", "type": "function", "function": {"name": "ls", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "toolu_1", "content": "x"},
            {"role": "user", "content": "go on"},
        ],
        "tools": [{"type": "function", "function": {
            "name": "read_file", "parameters": {"type": "object"}, "description": "Read"}}],
        "tool_choice": {"type": "function", "function": {"name": "read_file"}},
    }
    assert answer["type"] == "message" and answer["stop_reason"] == "max_tokens"
    assert answer["content"] == [
        {"type": "text", "text": "Sure"},
        {"type": "tool_use", "id": "call_9", "name": "read_file", "input": {"path": "x"}},
    ]
    assert answer["usage"] == {"input_tokens": 7, "output_tokens": 2}


def test_responses_to_a_chat_provider_are_translated_both_ways(provider) -> None:
    provider.responses.append(httpx.Response(200, json={
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "call_2", "type": "function",
                            "function": {"name": "shell", "arguments": "{\"cmd\":\"ls\"}"}}],
        }}],
    }))
    answer = run(llm.aresponses(
        model="cerebras/qwen", api_key="k",
        instructions="You are Codex.",
        input=[
            {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "Be careful."}]},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "List files"}]},
            {"type": "reasoning", "summary": []},
            {"type": "function_call", "call_id": "call_1", "name": "shell", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_1", "output": "a.py"},
        ],
        tools=[
            {"type": "function", "name": "shell", "parameters": {"type": "object"}, "strict": False},
            {"type": "web_search"},
        ],
        tool_choice="auto",
        max_output_tokens=200,
        text={"format": {"type": "json_schema", "name": "out", "schema": {"type": "object"}}},
        store=False, include=[], previous_response_id="resp_0",
    ))

    assert provider.body == {
        "model": "qwen",
        "max_tokens": 200,
        "messages": [
            {"role": "system", "content": "You are Codex."},
            {"role": "system", "content": "Be careful."},
            {"role": "user", "content": "List files"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "shell", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "a.py"},
        ],
        "tools": [{"type": "function", "function": {
            "name": "shell", "parameters": {"type": "object"}, "strict": False}}],
        "tool_choice": "auto",
        "response_format": {"type": "json_schema", "json_schema": {"name": "out", "schema": {"type": "object"}}},
    }
    assert answer["object"] == "response" and answer["status"] == "completed"
    assert answer["output"] == [{
        "type": "function_call", "id": answer["output"][0]["id"], "call_id": "call_2",
        "name": "shell", "arguments": "{\"cmd\":\"ls\"}", "status": "completed",
    }]
    assert answer["usage"] is None  # the provider reported none


def test_a_length_stop_is_an_incomplete_response(provider) -> None:
    provider.responses.append(httpx.Response(200, json={
        "choices": [{"index": 0, "finish_reason": "length", "message": {"role": "assistant", "content": "cut"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2},
    }))
    answer = run(llm.aresponses(model="groq/llama", api_key="k", input="hi"))
    assert answer["status"] == "incomplete"
    assert answer["output"][0]["content"] == [{"type": "output_text", "text": "cut", "annotations": []}]
    assert answer["usage"] == {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}


# -- voice ----------------------------------------------------------------------------


def test_transcription_posts_the_audio_to_groq_whisper(provider) -> None:
    provider.responses.append(httpx.Response(200, json={"text": "hello there"}))
    result = run(llm.atranscription(
        model="groq/whisper-large-v3-turbo", file=("speech.webm", b"\x1aE", "audio/webm"),
        api_key="gsk", max_retries=0, timeout=60,
    ))
    request = provider.requests[0]
    assert str(request.url) == "https://api.groq.com/openai/v1/audio/transcriptions"
    assert request.headers["authorization"] == "Bearer gsk"
    assert b'name="model"\r\n\r\nwhisper-large-v3-turbo' in request.content
    assert b'filename="speech.webm"' in request.content
    assert result.text == "hello there"


def test_a_whisper_rate_limit_is_a_rate_limit_error(provider) -> None:
    provider.responses.append(httpx.Response(429, json={"error": {"message": "limit"}}))
    with pytest.raises(llm.RateLimitError):
        run(llm.atranscription(model="groq/whisper-large-v3-turbo", file=("a.webm", b"x", "audio/webm")))
