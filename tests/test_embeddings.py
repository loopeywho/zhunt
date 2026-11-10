"""Local-only embeddings: /api/embed, /api/embeddings and /v1/embeddings."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest

from tests.test_engine_dialects import auth, daemon
from zhunt import embeddings

LOCAL = {"client": ("127.0.0.1", 50000), "base_url": "http://127.0.0.1:4000"}
JSON = {"content-type": "application/json"}
ANSWER = {"object": "list", "model": "nomic", "data": [
    {"index": 1, "embedding": [0.0, 1.0]}, {"index": 0, "embedding": [1.0, 0.0]}]}


def local_daemon():
    return daemon(chat=AsyncMock(), client_kwargs=LOCAL)


def test_api_embed_takes_a_string_or_list_and_keeps_order() -> None:
    fetch = AsyncMock(return_value=ANSWER)
    with patch.object(embeddings, "fetch", fetch), local_daemon() as (client, _):
        response = client.post("/api/embed", headers=JSON, json={"model": "nomic-embed-text", "input": ["a", "b"]})
    assert response.status_code == 200
    assert response.json() == {"model": "nomic-embed-text", "embeddings": [[1.0, 0.0], [0.0, 1.0]]}
    fetch.assert_awaited_once_with(["a", "b"])


def test_api_embeddings_is_the_legacy_single_vector_form() -> None:
    fetch = AsyncMock(return_value={"data": [{"index": 0, "embedding": [0.5, 0.5]}]})
    with patch.object(embeddings, "fetch", fetch), local_daemon() as (client, _):
        response = client.post("/api/embeddings", headers=JSON, json={"model": "nomic-embed-text", "prompt": "hi"})
    assert response.json() == {"embedding": [0.5, 0.5]}
    fetch.assert_awaited_once_with(["hi"], unit_length=False)  # Ollama's legacy route is unscaled


def test_v1_embeddings_needs_the_key_and_passes_the_answer_through() -> None:
    fetch = AsyncMock(return_value=dict(ANSWER))
    with patch.object(embeddings, "fetch", fetch), local_daemon() as (client, _):
        assert client.post("/v1/embeddings", headers=JSON, json={"input": "a"}).status_code == 401
        response = client.post("/v1/embeddings", headers={**auth(), **JSON}, json={"model": "m", "input": "a"})
    assert response.status_code == 200
    assert response.json()["model"] == "m" and len(response.json()["data"]) == 2


@pytest.mark.parametrize("body", [{"input": 3}, {"input": []}, {"input": ["a", 1]}])
def test_bad_input_is_400(body) -> None:
    with patch.object(embeddings, "fetch", AsyncMock()) as fetch, local_daemon() as (client, _):
        assert client.post("/api/embed", headers=JSON, json=body).status_code == 400
    fetch.assert_not_awaited()


def test_no_local_embedding_model_is_503_and_sends_nothing() -> None:
    with patch.dict(os.environ, {embeddings.URL_ENV: ""}), local_daemon() as (client, _):
        response = client.post("/api/embed", headers=JSON, json={"input": "a"})
    assert response.status_code == 503
    assert "nothing was sent to any cloud provider" in response.json()["error"]


def test_a_local_model_that_is_down_is_503() -> None:
    with patch.dict(os.environ, {embeddings.URL_ENV: "http://127.0.0.1:9/v1"}), local_daemon() as (client, _):
        response = client.post("/api/embeddings", headers=JSON, json={"prompt": "a"})
    assert response.status_code == 503
    assert "not answering" in response.json()["error"]


def test_a_remote_embedding_url_is_refused() -> None:
    with patch.dict(os.environ, {embeddings.URL_ENV: "https://api.example.com/v1"}), local_daemon() as (client, _):
        response = client.post("/api/embed", headers=JSON, json={"input": "a"})
    assert response.status_code == 503


def test_keyless_from_another_address_is_refused() -> None:
    fetch = AsyncMock(return_value=ANSWER)
    with patch.object(embeddings, "fetch", fetch), daemon(
        chat=AsyncMock(), client_kwargs={"client": ("192.168.1.9", 5), "base_url": "http://127.0.0.1:4000"}
    ) as (client, _):
        assert client.post("/api/embed", headers=JSON, json={"input": "a"}).status_code == 401
    fetch.assert_not_awaited()
