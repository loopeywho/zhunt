"""The Ollama wire format: /api/chat, /api/generate, /api/tags, /api/version.

Only the Chat Completions provider call is faked, so routing, failover, telemetry
and the privacy rule run for real."""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, patch

import pytest

from tests.test_engine_dialects import (
    KEYS,
    auth,
    chat_body,
    chat_reply,
    daemon,
    provider_error,
    stream_of,
)
from zhunt import llm
from zhunt.ollama import UNSUPPORTED, build_request

LOCAL = {"client": ("127.0.0.1", 50000), "base_url": "http://127.0.0.1:4000"}
JSON = {"content-type": "application/json"}
CLOUD = {**JSON, "x-zhunt-private": "0"}  # the explicit cloud opt-in
PNG = "iVBORw0KGgo="


def local_daemon(**kwargs):
    return daemon(client_kwargs=LOCAL, **kwargs)


def ndjson(response) -> list[dict]:
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def streaming(*chunks: tuple[str | None, str]) -> AsyncMock:
    return AsyncMock(side_effect=lambda **kw: stream_of(list(chunks), kw["model"]))


def chat_post(client, body: dict, **kw):
    return client.post("/api/chat", headers=CLOUD, json=body, **kw)


# -- chat ---------------------------------------------------------------------


def test_chat_streams_ndjson_by_default_and_names_the_requested_model() -> None:
    call = streaming((None, "he"), ("stop", "llo"))
    with local_daemon(chat=call) as (client, _):
        response = chat_post(client, {"model": "llama3.2:3b", "messages": [{"role": "user", "content": "hi"}]})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    pieces = ndjson(response)
    assert {p["model"] for p in pieces} == {"llama3.2:3b"}
    assert "".join(p["message"]["content"] for p in pieces) == "hello"
    assert [p["done"] for p in pieces] == [False, False, True]
    assert pieces[-1]["done_reason"] == "stop" and pieces[-1]["message"]["role"] == "assistant"
    assert response.headers["x-zhunt-model"]


def test_chat_stream_false_is_one_object_with_counts() -> None:
    reply = chat_reply("hello", usage={"prompt_tokens": 7, "completion_tokens": 3})
    with local_daemon(chat=AsyncMock(return_value=reply)) as (client, _):
        response = chat_post(
            client, {"model": "qwen3:8b", "stream": False, "messages": [{"role": "user", "content": "hi"}]}
        )

    body = response.json()
    assert body["message"] == {"role": "assistant", "content": "hello"}
    assert body["done"] is True and body["done_reason"] == "stop"
    assert (body["prompt_eval_count"], body["eval_count"]) == (7, 3)
    assert body["model"] == "qwen3:8b"


def test_reasoning_content_becomes_thinking() -> None:
    reply = llm.ModelResponse(dict(id="r", model="x", choices=[{
        "index": 0, "finish_reason": "stop",
        "message": {"role": "assistant", "content": "answer", "reasoning_content": "hmm"}}]))
    with local_daemon(chat=AsyncMock(return_value=reply)) as (client, _):
        body = chat_post(client, {"model": "m", "stream": False, "messages": [{"role": "user", "content": "x"}]}).json()

    assert body["message"]["thinking"] == "hmm"


def test_options_format_and_system_reach_the_provider() -> None:
    call = AsyncMock(return_value=chat_reply("{}"))
    with local_daemon(chat=call) as (client, _):
        chat_post(client, {
            "model": "llama3.2:3b", "stream": False, "format": "json", "think": False,
            "options": {"temperature": 0.2, "top_p": 0.9, "num_predict": 64, "stop": "END", "num_ctx": 8192},
            "messages": [{"role": "system", "content": "be brief"}, {"role": "user", "content": "x"}],
        })

    kwargs = call.await_args.kwargs
    assert (kwargs["temperature"], kwargs["top_p"], kwargs["max_tokens"]) == (0.2, 0.9, 64)
    assert kwargs["stop"] == ["END"]
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["messages"][0] == {"role": "system", "content": "be brief"}
    assert "num_ctx" not in kwargs


def test_a_length_finish_is_reported_as_length() -> None:
    call = AsyncMock(return_value=chat_reply("cut", finish_reason="length"))
    with local_daemon(chat=call) as (client, _):
        body = chat_post(client, {
            "model": "m", "stream": False, "options": {"num_predict": 5},
            "messages": [{"role": "user", "content": "x"}],
        }).json()

    assert body["done_reason"] == "length"


def test_images_are_forwarded_as_image_parts_not_dropped() -> None:
    call = AsyncMock(return_value=chat_reply("a cat"))
    with local_daemon(chat=call) as (client, _):
        chat_post(client, {
            "model": "gemma4", "stream": False,
            "messages": [{"role": "user", "content": "what is this?", "images": [PNG]}],
        })

    content = call.await_args.kwargs["messages"][-1]["content"]
    assert content[0] == {"type": "text", "text": "what is this?"}
    assert content[1]["image_url"]["url"] == f"data:image/png;base64,{PNG}"


def test_empty_messages_load_without_a_provider_call() -> None:
    call = AsyncMock()
    with local_daemon(chat=call) as (client, _):
        body = chat_post(client, {"model": "m", "stream": False, "messages": []}).json()

    assert body["done"] is True and body["done_reason"] == "load"
    call.assert_not_awaited()


@pytest.mark.parametrize(
    "body",
    [
        {"messages": [{"role": "user", "content": "x"}]},
        {"model": "m", "messages": "no"},
        {"model": "m", "messages": [{"role": "user", "content": "x"}], "stream": "yes"},
        {"model": "m", "messages": [{"role": "user", "content": "x"}], "tools": [{"type": "function"}]},
        {"model": "m", "messages": [{"content": "x"}]},
    ],
)
def test_bad_requests_are_400_with_an_ollama_error(body) -> None:
    call = AsyncMock()
    with local_daemon(chat=call) as (client, _):
        response = chat_post(client, body)

    assert response.status_code == 400 and isinstance(response.json()["error"], str)
    call.assert_not_awaited()


def test_a_body_that_is_not_json_is_400() -> None:
    with local_daemon(chat=AsyncMock()) as (client, _):
        assert client.post("/api/chat", headers=CLOUD, content=b"{nope").status_code == 400


# -- generate -----------------------------------------------------------------


def test_generate_streams_response_pieces_and_takes_system_and_images() -> None:
    call = streaming(("stop", "ok"))
    with local_daemon(chat=call) as (client, _):
        response = client.post("/api/generate", headers=CLOUD, json={
            "model": "llama3.2:3b", "prompt": "describe", "system": "sys", "images": [PNG],
        })

    pieces = ndjson(response)
    assert pieces[0]["response"] == "ok" and pieces[-1]["done"] is True
    messages = call.await_args.kwargs["messages"]
    assert messages[0] == {"role": "system", "content": "sys"}
    assert messages[1]["content"][1]["type"] == "image_url"


def test_generate_stream_false_is_one_object() -> None:
    with local_daemon(chat=AsyncMock(return_value=chat_reply("done"))) as (client, _):
        body = client.post("/api/generate", headers=CLOUD, json={"model": "m", "prompt": "x", "stream": False}).json()

    assert body["response"] == "done" and body["done"] is True


def test_generate_without_a_prompt_is_a_load() -> None:
    call = AsyncMock()
    with local_daemon(chat=call) as (client, _):
        body = client.post("/api/generate", headers=CLOUD, json={"model": "m", "stream": False}).json()

    assert body["done_reason"] == "load" and body["response"] == ""
    call.assert_not_awaited()


# -- routing, telemetry, errors -----------------------------------------------


def test_the_opt_in_header_routes_any_name_normally_and_the_name_is_not_honoured() -> None:
    call = AsyncMock(return_value=chat_reply("hi"))
    with local_daemon(chat=call) as (client, hook):
        for name in ("llama3.2:3b", "qwen3:8b", "zhunt-auto:latest", "nonsense"):
            assert chat_post(client, {"model": name, "stream": False,
                                      "messages": [{"role": "user", "content": "hi"}]}).status_code == 200
        decisions = hook.decisions()

    assert call.await_count == 4
    assert {d["requested_alias"] for d in decisions} == {"zhunt-auto"}
    assert len(decisions) == 4  # recorded in telemetry like any other request


def _plain(name, **kw):
    return {"model": name, "stream": False, "messages": [{"role": "user", "content": "hi"}], **kw}


LOCAL_ENV = {**KEYS, "ZHUNT_LOCAL_PORT": "18080"}


@pytest.mark.parametrize("name", ["llama3.2:3b", "qwen3:8b", "nonsense"])
@pytest.mark.parametrize("stream", [False, True])
def test_unknown_names_go_only_to_the_local_model_by_default(name, stream) -> None:
    call = AsyncMock(side_effect=lambda **kw: stream_of([("stop", "local")], kw["model"]) if kw.get("stream")
                     else chat_reply("local"))
    with patch("zhunt.local_provider.is_answering", AsyncMock(return_value=True)):
        with local_daemon(chat=call, env=LOCAL_ENV) as (client, _):
            response = client.post("/api/chat", headers=JSON, json=_plain(name, stream=stream))

    assert response.status_code == 200
    assert call.await_args.kwargs["api_base"] == "http://127.0.0.1:18080/v1"
    assert call.await_count == 1


@pytest.mark.parametrize("path", ["/api/chat", "/api/generate"])
@pytest.mark.parametrize("stream", [False, True])
def test_default_private_with_no_local_model_is_503_and_calls_nothing(path, stream) -> None:
    call = AsyncMock(return_value=chat_reply("x"))
    body = {"model": "llama3.2:3b", "stream": stream, "prompt": "hi", "messages": [{"role": "user", "content": "hi"}]}
    with local_daemon(chat=call) as (client, _):
        response = client.post(path, headers=JSON, json=body)

    assert response.status_code == 503
    assert "nothing was sent to any cloud provider" in response.json()["error"]
    call.assert_not_awaited()


def test_default_private_with_the_local_server_down_is_503_and_calls_nothing() -> None:
    call = AsyncMock(return_value=chat_reply("x"))
    with patch("zhunt.local_provider.is_answering", AsyncMock(return_value=False)):
        with local_daemon(chat=call, env=LOCAL_ENV) as (client, _):
            response = client.post("/api/generate", headers=JSON, json={"model": "llama3.2:3b", "prompt": "hi", "stream": False})

    assert response.status_code == 503 and "not answering" in response.json()["error"]
    call.assert_not_awaited()


def test_default_private_chat_too_long_for_the_local_context_is_503_and_calls_nothing() -> None:
    call = AsyncMock(return_value=chat_reply("x"))
    with patch("zhunt.local_provider.is_answering", AsyncMock(return_value=True)):
        with local_daemon(chat=call, env=LOCAL_ENV) as (client, _):
            response = client.post("/api/chat", headers=JSON, json=_plain("m", messages=[
                {"role": "user", "content": "word " * 20000}]))

    assert response.status_code == 503
    call.assert_not_awaited()


@pytest.mark.parametrize("alias", ["zhunt-auto", "zhunt-coding", "zhunt-auto:latest"])
def test_naming_a_routing_alias_opts_into_the_cloud_without_the_header(alias) -> None:
    call = AsyncMock(return_value=chat_reply("cloud"))
    with local_daemon(chat=call) as (client, hook):
        response = client.post("/api/chat", headers=JSON, json=_plain(alias))
        requested = hook.decisions()[-1]["requested_alias"]

    assert response.status_code == 200 and requested == alias.removesuffix(":latest")
    assert not str(call.await_args.kwargs.get("api_base", "")).startswith("http://127.0.0.1")


def test_private_header_one_beats_a_cloud_alias() -> None:
    call = AsyncMock(return_value=chat_reply("x"))
    with local_daemon(chat=call) as (client, _):
        response = client.post("/api/chat", headers={**JSON, "x-zhunt-private": "1"}, json=_plain("zhunt-auto"))

    assert response.status_code == 503
    call.assert_not_awaited()


def test_a_tier_alias_is_kept() -> None:
    call = AsyncMock(return_value=chat_reply("hi"))
    with local_daemon(chat=call) as (client, hook):
        chat_post(client, {"model": "zhunt-coding", "stream": False, "messages": [{"role": "user", "content": "hi"}]})
        assert hook.decisions()[-1]["requested_alias"] == "zhunt-coding"


def test_a_failed_first_attempt_is_replayed_on_the_next_model() -> None:
    call = AsyncMock(side_effect=[chat_reply("cut", finish_reason="length"), chat_reply("recovered")])
    with local_daemon(chat=call) as (client, _):
        body = chat_post(client, {"model": "m", "stream": False, "messages": [{"role": "user", "content": "hi"}]}).json()

    assert body["message"]["content"] == "recovered" and call.await_count == 2


def test_a_provider_error_is_an_ollama_error_with_its_status() -> None:
    call = AsyncMock(side_effect=[provider_error(llm.BadRequestError, 400)])
    with local_daemon(chat=call) as (client, _):
        response = chat_post(client, {"model": "m", "stream": False, "messages": [{"role": "user", "content": "x"}]})

    assert response.status_code == 400 and response.json() == {"error": "boom"}


def test_a_stream_failing_at_once_keeps_the_error_status() -> None:
    call = AsyncMock(side_effect=[provider_error(llm.BadRequestError, 400)])
    with local_daemon(chat=call) as (client, _):
        response = chat_post(client, {"model": "m", "messages": [{"role": "user", "content": "x"}]})

    assert response.status_code == 400 and "error" in response.json()


def test_an_error_mid_stream_is_an_error_line() -> None:
    async def broken(**_):
        async def chunks():
            yield llm.ModelResponse({"choices": [{"index": 0, "delta": {"content": "par"}}]})
            raise provider_error(llm.BadRequestError, 400)

        return chunks()

    with local_daemon(chat=AsyncMock(side_effect=broken)) as (client, _):
        response = chat_post(client, {"model": "m", "messages": [{"role": "user", "content": "x"}]})

    *_, last = ndjson(response)
    assert last == {"error": "boom"}


# -- private aliases ----------------------------------------------------------


@pytest.mark.parametrize("alias", ["zhunt-local", "zhunt-private", "zhunt-local:latest"])
def test_private_aliases_go_only_to_the_local_model(alias) -> None:
    call = AsyncMock(return_value=chat_reply("secret"))
    with patch("zhunt.local_provider.is_answering", AsyncMock(return_value=True)):
        with local_daemon(chat=call, env={**KEYS, "ZHUNT_LOCAL_PORT": "18080"}) as (client, _):
            body = chat_post(client, {"model": alias, "stream": False, "messages": [{"role": "user", "content": "hi"}]}).json()

    assert body["message"]["content"] == "secret"
    assert call.await_args.kwargs["api_base"] == "http://127.0.0.1:18080/v1"


def test_a_private_alias_without_a_local_model_is_503_and_calls_nothing() -> None:
    call = AsyncMock(return_value=chat_reply("x"))
    with local_daemon(chat=call) as (client, _):
        response = chat_post(client, {"model": "zhunt-private", "stream": False, "messages": [{"role": "user", "content": "hi"}]})

    assert response.status_code == 503
    assert "nothing was sent to any cloud provider" in response.json()["error"]
    call.assert_not_awaited()


def test_the_private_header_works_on_ollama_routes() -> None:
    call = AsyncMock(return_value=chat_reply("x"))
    with local_daemon(chat=call) as (client, _):
        response = client.post("/api/chat", headers={**JSON, "x-zhunt-private": "1"}, json={
            "model": "llama3.2:3b", "stream": False, "messages": [{"role": "user", "content": "hi"}]})

    assert response.status_code == 503
    call.assert_not_awaited()


# -- tags, version, 501 -------------------------------------------------------


def test_tags_lists_routable_names_in_ollama_form() -> None:
    with local_daemon(chat=AsyncMock()) as (client, _):
        models = client.get("/api/tags").json()["models"]

    names = {m["name"] for m in models}
    assert "zhunt-auto:latest" in names and "zhunt-coding:latest" in names
    assert not any(n.startswith("zhunt-local") for n in names)  # no local model set up
    assert all({"name", "model", "modified_at", "size", "digest", "details"} <= set(m) for m in models)


def test_tags_lists_the_private_aliases_when_a_local_model_exists() -> None:
    with local_daemon(chat=AsyncMock(), env={**KEYS, "ZHUNT_LOCAL_PORT": "18080"}) as (client, _):
        names = [m["name"] for m in client.get("/api/tags").json()["models"]]

    assert names[:2] == ["zhunt-local:latest", "zhunt-private:latest"]  # local first
    assert "zhunt-auto:latest" in names


def test_version() -> None:
    with local_daemon(chat=AsyncMock()) as (client, _):
        assert "version" in client.get("/api/version").json()


def test_tags_digests_are_full_length_so_ollama_list_can_print_them() -> None:
    with local_daemon(chat=AsyncMock()) as (client, _):
        models = client.get("/api/tags").json()["models"]

    digests = [m["digest"] for m in models]
    assert all(len(d) == 64 and int(d, 16) >= 0 for d in digests)
    assert len(set(digests)) == len(digests)


def test_root_says_ollama_is_running_for_the_cli_heartbeat() -> None:
    with local_daemon(chat=AsyncMock()) as (client, _):
        assert client.head("/").status_code == 200
        response = client.get("/")
    assert response.text == "Ollama is running"


def test_show_describes_any_name_without_calling_a_provider() -> None:
    call = AsyncMock()
    with local_daemon(chat=call) as (client, _):
        response = client.post("/api/show", headers=JSON, json={"model": "llama3.2:3b"})
        assert client.post("/api/show", headers=JSON, json={}).status_code == 400
    assert response.status_code == 200
    assert response.json()["capabilities"] == ["completion"]
    call.assert_not_awaited()


@pytest.mark.parametrize(("method", "path"), sorted(UNSUPPORTED))
def test_unsupported_endpoints_are_501_json(method, path) -> None:
    with local_daemon(chat=AsyncMock()) as (client, _):
        response = client.request(method, path, headers=CLOUD, json={})

    assert response.status_code == 501
    assert path in response.json()["error"]


# -- auth ---------------------------------------------------------------------

CASES = [
    ("POST", "/api/chat", {"model": "m", "stream": False, "messages": [{"role": "user", "content": "x"}]}),
    ("POST", "/api/generate", {"model": "m", "prompt": "x", "stream": False}),
    ("GET", "/api/tags", None),
    ("GET", "/api/version", None),
    ("GET", "/", None),
    ("POST", "/api/show", {"model": "m"}),
    ("POST", "/api/embeddings", {}),
]


def send(client, method, path, body, headers):
    return client.request(method, path, headers=headers, **({"json": body} if body is not None else {}))


@pytest.mark.parametrize(("method", "path", "body"), CASES)
def test_keyless_from_this_computer_works(method, path, body) -> None:
    with local_daemon(chat=AsyncMock(return_value=chat_reply("ok"))) as (client, _):
        assert send(client, method, path, body, CLOUD).status_code in (200, 501)


@pytest.mark.parametrize(("method", "path", "body"), CASES)
def test_keyless_from_another_address_is_refused(method, path, body) -> None:
    call = AsyncMock(return_value=chat_reply("ok"))
    with daemon(chat=call, client_kwargs={"client": ("192.168.1.9", 5), "base_url": "http://127.0.0.1:4000"}) as (client, _):
        assert send(client, method, path, body, CLOUD).status_code == 401
    call.assert_not_awaited()


@pytest.mark.parametrize(
    "headers",
    [
        {**JSON, "host": "evil.example:4000"},  # DNS rebinding
        {**JSON, "origin": "https://evil.example"},  # a web page
        {"content-type": "text/plain"},  # a page's "simple" post
    ],
)
def test_browser_shaped_keyless_requests_are_refused(headers) -> None:
    call = AsyncMock(return_value=chat_reply("ok"))
    with local_daemon(chat=call) as (client, _):
        response = client.post("/api/chat", headers=headers, content=json.dumps(
            {"model": "m", "stream": False, "messages": [{"role": "user", "content": "x"}]}))
    assert response.status_code == 401
    call.assert_not_awaited()


def test_the_master_key_is_accepted_from_anywhere_and_a_wrong_key_is_not() -> None:
    call = AsyncMock(return_value=chat_reply("ok"))
    body = {"model": "zhunt-auto", "stream": False, "messages": [{"role": "user", "content": "x"}]}
    with daemon(chat=call, client_kwargs={"client": ("192.168.1.9", 5), "base_url": "http://example.test"}) as (client, _):
        assert client.post("/api/chat", headers={**auth(), **JSON}, json=body).status_code == 200
        assert client.post("/api/chat", headers={"authorization": "Bearer wrong", **JSON}, json=body).status_code == 401


def test_require_key_setting_turns_keyless_off() -> None:
    with patch.dict(os.environ, {"ZHUNT_OLLAMA_REQUIRE_KEY": "1"}):
        with local_daemon(chat=AsyncMock(return_value=chat_reply("ok"))) as (client, _):
            assert client.get("/api/version").status_code == 401
            assert client.get("/api/version", headers=auth()).status_code == 200


def test_the_v1_routes_still_need_the_key_from_this_computer() -> None:
    with local_daemon(chat=AsyncMock(return_value=chat_reply("ok"))) as (client, _):
        assert client.post("/v1/chat/completions", headers=CLOUD, json=chat_body()).status_code == 401
        assert client.post("/v1/chat/completions", headers={**auth(), **JSON}, json=chat_body()).status_code == 200


def test_build_request_is_pure() -> None:
    payload, stream = build_request("chat", {"messages": [{"role": "user", "content": "x"}]}, "zhunt-auto")
    assert stream is True and payload["stream_options"] == {"include_usage": True}
