"""Named local models (Ollama Modelfile style) on the Ollama routes."""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import AsyncMock, patch

import pytest

from tests.test_engine_dialects import daemon
from zhunt import local_profiles

LOCAL = {"client": ("127.0.0.1", 50000), "base_url": "http://127.0.0.1:4000"}
JSON = {"content-type": "application/json"}
REPLY = {"choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 5, "completion_tokens": 1}}


@pytest.fixture
def profiles(tmp_path):
    path = tmp_path / "local_models.json"
    path.write_text(json.dumps({"translator": {
        "url": "http://127.0.0.1:8082/v1", "model": "qwen3-8b", "system": "You are a translator.",
        "options": {"temperature": 0.7, "top_k": 20, "top_p": 0.95, "stop": ["<|im_end|>"]}}}))
    with patch.dict(os.environ, {local_profiles.FILE_ENV: str(path)}):
        yield path


def test_lookup_takes_the_name_with_or_without_latest(profiles) -> None:
    assert local_profiles.lookup("translator").url == "http://127.0.0.1:8082/v1"
    assert local_profiles.lookup("translator:latest").name == "translator"
    assert local_profiles.lookup("translator:14b") is None
    assert local_profiles.lookup("llama3.2:3b") is None


def test_no_file_means_no_profiles(tmp_path) -> None:
    with patch.dict(os.environ, {local_profiles.FILE_ENV: str(tmp_path / "missing.json")}):
        assert local_profiles.lookup("translator") is None


def test_a_remote_url_is_refused(tmp_path) -> None:
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"x": {"url": "https://api.example.com/v1"}}))
    with patch.dict(os.environ, {local_profiles.FILE_ENV: str(path)}), pytest.raises(local_profiles.ProfileError):
        local_profiles.lookup("x")


def test_apply_adds_the_system_prompt_and_defaults_but_the_request_wins(profiles) -> None:
    profile = local_profiles.lookup("translator")
    payload = local_profiles.apply(profile, {"messages": [{"role": "user", "content": "hi"}], "temperature": 0.1}, {"top_k": 5})
    assert payload["messages"][0] == {"role": "system", "content": "You are a translator."}
    assert payload["temperature"] == 0.1 and payload["top_p"] == 0.95 and payload["top_k"] == 5
    assert payload["stop"] == ["<|im_end|>"] and payload["model"] == "qwen3-8b"
    own = local_profiles.apply(profile, {"messages": [{"role": "system", "content": "mine"}, {"role": "user", "content": "hi"}]}, {})
    assert [m["content"] for m in own["messages"]] == ["mine", "hi"]


def test_a_profile_name_goes_to_its_server_not_the_router(profiles) -> None:
    cloud = AsyncMock()
    complete = AsyncMock(return_value=REPLY)
    with patch.object(local_profiles, "complete", complete), daemon(chat=cloud, client_kwargs=LOCAL) as (client, _):
        response = client.post("/api/generate", headers=JSON, json={"model": "translator", "prompt": "hi", "stream": False})
    assert response.status_code == 200
    assert response.json()["response"] == "hello" and response.json()["model"] == "translator"
    assert response.headers["x-zhunt-model"] == "local/translator"
    sent = complete.await_args.args[1]
    assert sent["messages"][0]["content"] == "You are a translator." and sent["top_k"] == 20
    cloud.assert_not_awaited()


def test_a_caller_who_hangs_up_stops_the_local_model(profiles) -> None:
    """A runaway answer must not keep the local model busy after the caller gives up:
    Ollama stops generating then, and retries would otherwise queue behind it."""

    cancelled = []

    async def endless(profile, payload):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(True)
            raise

    from starlette.requests import Request
    from zhunt import ollama
    with patch.object(local_profiles, "complete", endless), \
            patch.object(Request, "is_disconnected", AsyncMock(return_value=True)), \
            patch.object(ollama, "HANGUP_POLL_SECONDS", 0.01), \
            daemon(chat=AsyncMock(), client_kwargs=LOCAL) as (client, _):
        client.post("/api/chat", headers=JSON, json={"model": "translator", "stream": False,
                                                     "messages": [{"role": "user", "content": "hi"}]})
    assert cancelled == [True]


def test_a_profile_streams_ndjson(profiles) -> None:
    async def chunks():
        for text in ("hel", "lo"):
            yield {"choices": [{"delta": {"content": text}}]}
        yield {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"completion_tokens": 2}}

    with patch.object(local_profiles, "stream", AsyncMock(return_value=chunks())), \
            daemon(chat=AsyncMock(), client_kwargs=LOCAL) as (client, _):
        response = client.post("/api/chat", headers=JSON, json={"model": "translator", "messages": [{"role": "user", "content": "hi"}]})
    lines = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    assert "".join(line["message"]["content"] for line in lines) == "hello"
    assert lines[-1]["done"] is True


def test_a_profile_whose_server_is_down_is_503_and_nothing_else_is_called(tmp_path) -> None:
    path = tmp_path / "p.json"
    path.write_text(json.dumps({"translator": {"url": "http://127.0.0.1:9/v1"}}))
    cloud = AsyncMock()
    with patch.dict(os.environ, {local_profiles.FILE_ENV: str(path)}), daemon(chat=cloud, client_kwargs=LOCAL) as (client, _):
        for stream in (False, True):
            response = client.post("/api/generate", headers=JSON, json={"model": "translator", "prompt": "hi", "stream": stream})
            assert response.status_code == 503
            assert "nothing was sent to any cloud provider" in response.json()["error"]
    cloud.assert_not_awaited()
