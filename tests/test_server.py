"""zhunt's own /v1 routes: the request lifecycle, streaming in each dialect, errors.

Only the Chat Completions provider call (``zhunt.llm.acompletion``) is faked, so
the Responses and Anthropic Messages translations run for real.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock

from tests.test_engine_dialects import (
    CHAT,
    CODING,
    auth,
    called_models,
    chat_body,
    chat_reply,
    daemon,
    provider_error,
    stream_of,
)
from zhunt import llm


def events(text: str) -> list[tuple[str | None, Any]]:
    """``(event name, data)`` for each SSE frame."""

    parsed = []
    for frame in text.split("\n\n"):
        if not frame.strip():
            continue
        name, data = None, None
        for line in frame.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        parsed.append((name, data if data == "[DONE]" else json.loads(data)))
    return parsed


def streaming(*attempts: list[tuple[str | None, str]]) -> AsyncMock:
    """A provider that streams each attempt's ``(finish_reason, text)`` chunks in turn."""

    queue = list(attempts)
    return AsyncMock(side_effect=lambda **kwargs: stream_of(queue.pop(0), kwargs["model"]))


def test_chat_streams_name_the_requested_model_and_end_with_done() -> None:
    call = streaming([(None, "he"), ("stop", "llo")])
    with daemon(chat=call) as (client, _):
        response = client.post("/v1/chat/completions", headers=auth(), json=chat_body(stream=True))

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = events(response.text)
    assert frames[-1] == (None, "[DONE]")
    chunks = [data for _, data in frames[:-1]]
    assert {chunk["model"] for chunk in chunks} == {"zhunt-auto"}
    assert "".join(c["choices"][0]["delta"]["content"] for c in chunks) == "hello"


def test_chat_json_answers_name_the_requested_model() -> None:
    with daemon(chat=AsyncMock(return_value=chat_reply("hi"))) as (client, _):
        response = client.post("/v1/chat/completions", headers=auth(), json=chat_body())

    assert response.json()["model"] == "zhunt-auto"


def test_answers_name_the_model_that_served_them_in_a_header() -> None:
    # the first attempt is cut short, so the replay on the next tier answers
    call = AsyncMock(side_effect=[chat_reply(finish_reason="length"), chat_reply("recovered")])
    with daemon(chat=call) as (client, _):
        response = client.post("/v1/chat/completions", headers=auth(), json=chat_body())

    assert response.json()["model"] == "zhunt-auto"
    assert response.headers["x-zhunt-model"] == CODING


def test_streams_name_the_model_that_served_them_in_a_header() -> None:
    with daemon(chat=streaming([("stop", "hi")])) as (client, _):
        response = client.post("/v1/chat/completions", headers=auth(), json=chat_body(stream=True))

    assert response.headers["x-zhunt-model"] == CHAT


def test_a_stream_that_fails_before_its_first_chunk_keeps_the_error_status() -> None:
    call = AsyncMock(side_effect=[provider_error(llm.BadRequestError, 400)])
    with daemon(chat=call) as (client, hook):
        response = client.post("/v1/chat/completions", headers=auth(), json=chat_body(stream=True))
        decisions = hook.decisions()

    assert response.status_code == 400
    assert response.json() == {
        "error": {"message": "boom", "type": None, "param": None, "code": "400"}
    }
    assert [d["failure"] for d in decisions] == ["client-error"]


def test_an_error_mid_stream_is_sent_as_an_error_event() -> None:
    async def broken(**_):
        async def chunks():
            yield llm.ModelResponse({"choices": [{"index": 0, "delta": {"content": "par"}}]})
            raise provider_error(llm.BadRequestError, 400)

        return chunks()

    with daemon(chat=AsyncMock(side_effect=broken)) as (client, _):
        response = client.post("/v1/chat/completions", headers=auth(), json=chat_body(stream=True))

    assert response.status_code == 200
    *_, (_, last) = events(response.text)
    assert last == {"error": {"message": "boom", "type": None, "param": None, "code": "400"}}


def test_a_body_that_is_not_a_json_object_is_a_400() -> None:
    with daemon(chat=AsyncMock()) as (client, _):
        broken = client.post("/v1/chat/completions", headers=auth(), content=b"{not json")
        listed = client.post("/v1/chat/completions", headers=auth(), json=[1, 2])

    assert broken.status_code == listed.status_code == 400


def test_messages_stream_as_anthropic_events_from_a_chat_provider() -> None:
    call = streaming([(None, "Hel"), ("stop", "lo")])
    body = {"model": "claude-sonnet-4-5", "max_tokens": 50, "stream": True,
            "messages": [{"role": "user", "content": "Hi"}]}
    with daemon(chat=call) as (client, _):
        response = client.post("/v1/messages", headers=auth(), json=body)

    assert response.status_code == 200
    frames = events(response.text)
    names = [name for name, _ in frames]
    assert names == [
        "message_start", "content_block_start", "content_block_delta", "content_block_delta",
        "content_block_stop", "message_delta", "message_stop",
    ]
    assert all(data["type"] == name for name, data in frames)
    assert "".join(d["delta"]["text"] for n, d in frames if n == "content_block_delta") == "Hello"
    assert frames[-2][1]["delta"]["stop_reason"] == "end_turn"
    assert call.await_args.kwargs["messages"] == [{"role": "user", "content": "Hi"}]
    assert call.await_args.kwargs["max_tokens"] == 50


def test_an_uncapped_truncated_messages_stream_is_replayed_one_tier_up() -> None:
    call = streaming([("length", "partial")], [("stop", "complete")])
    body = {"model": "zhunt-auto", "stream": True, "messages": [{"role": "user", "content": "Hi"}]}
    with daemon(chat=call) as (client, hook):
        response = client.post("/v1/messages", headers=auth("trunc-messages"), json=body)
        pending, retries = hook._pending, hook._retries

    assert response.status_code == 200
    assert called_models(call) == [CHAT, CODING]
    assert "complete" in response.text and "partial" not in response.text
    assert pending == {} and retries == {}


def test_responses_stream_as_responses_events_from_a_chat_provider() -> None:
    call = streaming([(None, "Hel"), ("stop", "lo")])
    with daemon(chat=call) as (client, _):
        response = client.post(
            "/v1/responses", headers=auth(), json={"model": "gpt-5-codex", "input": "Hi", "stream": True}
        )

    assert response.status_code == 200
    frames = events(response.text)
    names = [name for name, _ in frames]
    assert names[:2] == ["response.created", "response.in_progress"]
    assert names[-1] == "response.completed"
    assert [d["sequence_number"] for _, d in frames] == list(range(len(frames)))
    deltas = [d["delta"] for n, d in frames if n == "response.output_text.delta"]
    assert "".join(deltas) == "Hello"
    final = frames[-1][1]["response"]
    assert final["status"] == "completed"
    assert final["output"][0]["content"][0]["text"] == "Hello"


def test_an_incomplete_responses_stream_is_replayed_one_tier_up() -> None:
    call = streaming([("length", "partial")], [("stop", "complete")])
    with daemon(chat=call) as (client, _):
        response = client.post(
            "/v1/responses", headers=auth("trunc-responses"),
            json={"model": "zhunt-auto", "input": "Hi", "stream": True},
        )

    assert response.status_code == 200
    assert called_models(call) == [CHAT, CODING]
    assert events(response.text)[-1][0] == "response.completed"


def test_responses_and_messages_json_answers_are_translated_from_chat() -> None:
    call = AsyncMock(return_value=chat_reply("ok", usage={"prompt_tokens": 2, "completion_tokens": 1}))
    with daemon(chat=call) as (client, _):
        responses = client.post("/v1/responses", headers=auth(), json={"model": "x", "input": "Hi"})
        messages = client.post(
            "/v1/messages", headers=auth(),
            json={"model": "x", "max_tokens": 5, "messages": [{"role": "user", "content": "Hi"}]},
        )

    assert responses.json()["output"][0]["content"][0]["text"] == "ok"
    assert responses.json()["usage"]["total_tokens"] == 3
    assert messages.json()["content"] == [{"type": "text", "text": "ok"}]
    assert messages.json()["usage"] == {"input_tokens": 2, "output_tokens": 1}


def test_count_tokens_estimates_the_prompt() -> None:
    with daemon(chat=AsyncMock()) as (client, _):
        short = client.post("/v1/messages/count_tokens", headers=auth(),
                            json={"model": "x", "messages": [{"role": "user", "content": "hi"}]})
        long = client.post("/v1/messages/count_tokens", headers=auth(),
                           json={"model": "x", "messages": [{"role": "user", "content": "hi " * 400}]})

    assert 0 < short.json()["input_tokens"] < long.json()["input_tokens"]


def test_responses_stream_records_provider_usage_in_routing_decisions():
    async def chunks():
        yield llm.ModelResponse({"choices": [{"index": 0, "delta": {"content": "done"}, "finish_reason": "stop"}]})
        yield llm.ModelResponse({"choices": [], "usage": {"prompt_tokens": 123, "completion_tokens": 45}})
    with daemon(chat=AsyncMock(return_value=chunks())) as (client, hook):
        response = client.post('/v1/responses', headers=auth(), json={
            'model': 'zhunt-auto', 'input': 'Hello', 'stream': True,
        })
        assert response.status_code == 200
        decision = hook.decisions(limit=1)[0]
        assert decision['input_tokens'] == 123
        assert decision['output_tokens'] == 45
