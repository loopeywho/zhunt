"""Embeddings from a model on this computer: Ollama's ``/api/embed`` and
``/api/embeddings`` and OpenAI's ``/v1/embeddings``.

Embeddings are local only. They are sent to ``ZHUNT_LOCAL_EMBED_URL`` (an
OpenAI-style base URL on this computer, e.g. ``http://127.0.0.1:8081/v1``,
served by Zhunt Plus with an embedding model such as nomic-embed-text) and never
to a cloud provider: the text being embedded is usually private notes.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response

from zhunt import local_provider, ollama

URL_ENV = "ZHUNT_LOCAL_EMBED_URL"
_TIMEOUT = 120.0
_NOTHING_SENT = "nothing was sent to any cloud provider"


class EmbeddingError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def base_url() -> str:
    url = (os.environ.get(URL_ENV) or "").strip()
    if not url:
        raise EmbeddingError(503, f"no local embedding model is set up ({URL_ENV}); {_NOTHING_SENT}")
    try:
        return local_provider.loopback_url(url)
    except local_provider.LocalConfigError as error:
        raise EmbeddingError(503, f"{URL_ENV}: {error}; {_NOTHING_SENT}") from error


async def fetch(inputs: list[str], *, unit_length: bool = True) -> dict[str, Any]:
    """The local server's OpenAI-style answer for ``inputs``.

    Like Ollama, ``/api/embed`` and ``/v1/embeddings`` return unit-length vectors
    and the older ``/api/embeddings`` returns them unscaled (nomic-embed-text:
    length ~23). Stores that measure straight-line distance (Chroma's default)
    need the same kind they were filled with. ``embd_normalize: -1`` is
    llama.cpp's switch for unscaled vectors."""

    url = base_url()
    body: dict[str, Any] = {"input": inputs}
    if not unit_length:
        body["embd_normalize"] = -1
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(f"{url}/embeddings", json=body)
    except httpx.HTTPError as error:
        raise EmbeddingError(
            503, f"the local embedding model at {url} is not answering; {_NOTHING_SENT}"
        ) from error
    if response.status_code != 200:
        raise EmbeddingError(response.status_code, f"local embedding model: {response.text[:300]}")
    return response.json()


def _vectors(answer: dict[str, Any]) -> list[list[float]]:
    rows = sorted(answer.get("data") or [], key=lambda row: row.get("index", 0))
    return [row["embedding"] for row in rows]


def _texts(value: Any, field: str) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and value and all(isinstance(item, str) for item in value):
        return value
    raise EmbeddingError(400, f"{field} must be a string or a list of strings")


def mount(app: FastAPI, auth: Any) -> None:
    from zhunt import server  # server imports this module

    async def body_of(request: Request) -> dict[str, Any]:
        body = await server._json_object(request)
        if not isinstance(body, dict):
            raise EmbeddingError(400, "request body must be a JSON object")
        return body

    async def ollama_embed(request: Request) -> Response:
        if (denied := ollama.check_access(request)) is not None:
            return denied
        try:
            body = await body_of(request)
            vectors = _vectors(await fetch(_texts(body.get("input"), "input")))
        except EmbeddingError as error:
            return ollama.error_response(error.status, str(error))
        return JSONResponse({"model": body.get("model"), "embeddings": vectors})

    async def ollama_embeddings(request: Request) -> Response:
        if (denied := ollama.check_access(request)) is not None:
            return denied
        try:
            body = await body_of(request)
            vectors = _vectors(await fetch(_texts(body.get("prompt"), "prompt"), unit_length=False))
        except EmbeddingError as error:
            return ollama.error_response(error.status, str(error))
        return JSONResponse({"embedding": vectors[0]})

    async def openai_embeddings(request: Request) -> Response:
        try:
            body = await body_of(request)
            answer = await fetch(_texts(body.get("input"), "input"))
        except EmbeddingError as error:
            return JSONResponse({"error": {"message": str(error), "type": "zhunt_error"}}, status_code=error.status)
        answer["model"] = body.get("model") or answer.get("model")
        return JSONResponse(answer)

    app.add_api_route("/api/embed", ollama_embed, methods=["POST"])
    app.add_api_route("/api/embeddings", ollama_embeddings, methods=["POST"])
    app.add_api_route("/v1/embeddings", openai_embeddings, methods=["POST"], dependencies=[Depends(auth)])
