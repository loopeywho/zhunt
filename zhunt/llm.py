"""Zhunt's provider client: a thin async layer over httpx.

Every provider zhunt routes to speaks OpenAI Chat Completions except Anthropic,
which speaks its own Messages API. ``acompletion`` is the one call the engine
makes; ``aresponses`` and ``anthropic_messages`` serve the other two inbound
dialects by translating to and from Chat Completions (``zhunt.translate``), and
``atranscription`` is Groq Whisper for voice input.

A model is ``<route>/<model id>``, the route being the catalog's
``litellm_provider`` (the name is kept so existing catalogs still load).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from zhunt import translate

DEFAULT_TIMEOUT = 600.0
ANTHROPIC_VERSION = "2023-06-01"
_ERROR_BODY_LIMIT = 4000

# Route -> (wire format, base URL used when the catalog gives no api_base).
_ROUTES: dict[str, tuple[str, str | None]] = {
    "openai": ("chat", "https://api.openai.com/v1"),
    "groq": ("chat", "https://api.groq.com/openai/v1"),
    "cerebras": ("chat", "https://api.cerebras.ai/v1"),
    "gemini": ("chat", "https://generativelanguage.googleapis.com/v1beta/openai"),
    "anthropic": ("anthropic", "https://api.anthropic.com"),
}
_OPENAI_BASE = _ROUTES["openai"][1]

# Only standard Chat Completions fields leave the machine; zhunt's own metadata,
# the original request and anything a client invents stay here.
_CHAT_PARAMS = frozenset({
    "frequency_penalty", "logit_bias", "logprobs", "max_completion_tokens", "max_tokens",
    "n", "parallel_tool_calls", "presence_penalty", "reasoning_effort", "response_format",
    "seed", "stop", "stream_options", "temperature", "tool_choice", "tools",
    "top_logprobs", "top_p",
})
_ANTHROPIC_PARAMS = frozenset({
    "max_tokens", "stop_sequences", "system", "temperature", "thinking", "tool_choice",
    "tools", "top_k", "top_p",
})


# -- errors ---------------------------------------------------------------------


class APIError(Exception):
    """A failed provider call. ``str()`` is the provider's own error text, which the
    engine reads for daily-quota and stated-limit markers."""

    status_code = 500

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response_headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        self.response_headers = response_headers

    def __str__(self) -> str:
        return self.message


class BadRequestError(APIError):
    status_code = 400


class AuthenticationError(APIError):
    status_code = 401


class PermissionDeniedError(APIError):
    status_code = 403


class NotFoundError(APIError):
    status_code = 404


class Timeout(APIError):
    status_code = 408


class RateLimitError(APIError):
    status_code = 429


class InternalServerError(APIError):
    status_code = 500


class APIConnectionError(APIError):
    status_code = 500


class ServiceUnavailableError(APIError):
    status_code = 503


_ERRORS_BY_STATUS: dict[int, type[APIError]] = {
    400: BadRequestError,
    401: AuthenticationError,
    403: PermissionDeniedError,
    404: NotFoundError,
    408: Timeout,
    422: BadRequestError,
    429: RateLimitError,
    503: ServiceUnavailableError,
}


def error_for(
    status: int, text: str, headers: Mapping[str, str] | None = None
) -> APIError:
    cls = _ERRORS_BY_STATUS.get(status) or (
        BadRequestError if 400 <= status < 500 else InternalServerError
    )
    detail = " ".join(text.split())[:_ERROR_BODY_LIMIT] or f"HTTP {status}"
    return cls(f"{cls.__name__}: {detail}", status_code=status, response_headers=headers)


# -- responses ------------------------------------------------------------------


class ModelResponse(dict):
    """A provider's JSON answer (or stream chunk); ``headers`` are the raw HTTP
    response headers, read for rate limits."""

    def __init__(
        self, data: Mapping[str, Any] = (), headers: Mapping[str, str] | None = None
    ) -> None:
        super().__init__(data)
        self.headers = headers


@dataclass(frozen=True)
class Transcription:
    text: str


# -- transport ------------------------------------------------------------------

_shared: tuple[asyncio.AbstractEventLoop, httpx.AsyncClient] | None = None


def _client() -> httpx.AsyncClient:
    """One pooled client per event loop (tests run several loops in one process)."""

    global _shared
    loop = asyncio.get_running_loop()
    if _shared is None or _shared[0] is not loop or _shared[1].is_closed:
        _shared = (loop, httpx.AsyncClient(timeout=DEFAULT_TIMEOUT))
    return _shared[1]


async def aclose() -> None:
    global _shared
    if _shared is not None:
        shared, _shared = _shared, None
        await shared[1].aclose()


async def _send(
    url: str, *, headers: Mapping[str, str], timeout: float | None, stream: bool = False,
    **content: Any,
) -> httpx.Response:
    client = _client()
    request = client.build_request(
        "POST", url, headers=headers, timeout=timeout or DEFAULT_TIMEOUT, **content
    )
    try:
        response = await client.send(request, stream=stream)
    except httpx.TimeoutException as error:
        raise Timeout(f"Timeout: {error or 'provider did not answer in time'}") from error
    except httpx.HTTPError as error:
        raise APIConnectionError(f"APIConnectionError: {error}") from error
    if response.status_code >= 400:
        body = await response.aread()
        await response.aclose()
        raise error_for(
            response.status_code, body.decode("utf-8", "replace"), response.headers
        )
    return response


async def _post_json(
    url: str, headers: Mapping[str, str], body: Mapping[str, Any], timeout: float | None
) -> ModelResponse:
    response = await _send(url, headers=headers, timeout=timeout, json=body)
    try:
        # strict=False: some providers send raw newlines inside strings (e.g. reasoning text)
        data = json.loads(response.text, strict=False)
    except ValueError:
        raise InternalServerError("InternalServerError: provider answer is not JSON") from None
    if not isinstance(data, Mapping):
        raise InternalServerError("InternalServerError: provider answer is not an object")
    return ModelResponse(data, response.headers)


async def _post_stream(
    url: str, headers: Mapping[str, str], body: Mapping[str, Any], timeout: float | None
) -> AsyncIterator[ModelResponse]:
    """Open the stream now (so an HTTP error raises here), then yield its events."""

    response = await _send(url, headers=headers, timeout=timeout, stream=True, json=body)
    return _sse_objects(response)


async def _sse_objects(response: httpx.Response) -> AsyncIterator[ModelResponse]:
    try:
        data: list[str] = []
        async for line in response.aiter_lines():
            if line:
                if line.startswith("data:"):
                    data.append(line[5:].removeprefix(" "))
                continue
            if not data:
                continue
            payload, data = "\n".join(data), []
            if payload == "[DONE]":
                return
            try:
                event = json.loads(payload, strict=False)
            except ValueError:
                continue
            if not isinstance(event, Mapping):
                continue
            _raise_stream_error(event, response.headers)
            yield ModelResponse(event, response.headers)
    except httpx.HTTPError as error:
        raise APIConnectionError(f"APIConnectionError: stream broke: {error}") from error
    finally:
        await response.aclose()


def _raise_stream_error(event: Mapping[str, Any], headers: Mapping[str, str]) -> None:
    """A provider that fails after the 200 says so in an ``error`` event."""

    error = event.get("error")
    if error is None:
        return
    code = error.get("code") if isinstance(error, Mapping) else None
    status = code if isinstance(code, int) and 400 <= code < 600 else 500
    if event.get("type") == "error" and isinstance(error, Mapping):
        # Anthropic: overloaded_error and friends carry no numeric code
        status = 429 if error.get("type") == "rate_limit_error" else 500
    raise error_for(status, json.dumps(event), headers)


def _route(model: str, api_base: str | None) -> tuple[str, str, str]:
    """``(wire, base_url, model_id)`` for ``<route>/<model id>``."""

    route, slash, model_id = model.partition("/")
    wire, default_base = _ROUTES.get(route, ("chat", None))
    base = api_base or default_base
    if not slash or not model_id or base is None:
        raise BadRequestError(
            f"BadRequestError: no endpoint known for model {model!r}", status_code=400
        )
    return wire, base.rstrip("/"), model_id


def _split(model: str) -> tuple[str, str]:
    """``(wire, model_id)``; the endpoint itself is resolved by the call that sends."""

    route, _, model_id = model.partition("/")
    return _ROUTES.get(route, ("chat", None))[0], model_id or model


def _bearer(api_key: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


def _anthropic_headers(api_key: str | None) -> dict[str, str]:
    headers = {"anthropic-version": ANTHROPIC_VERSION}
    if api_key:
        headers["x-api-key"] = api_key
    return headers


# -- calls ----------------------------------------------------------------------


async def acompletion(
    *,
    model: str,
    messages: list[Any],
    api_key: str | None = None,
    api_base: str | None = None,
    stream: bool = False,
    timeout: float | None = None,
    **params: Any,
) -> ModelResponse | AsyncIterator[ModelResponse]:
    """An OpenAI Chat Completions call: a response, or with ``stream`` an async
    iterator of chunks. Unknown keyword arguments are ignored."""

    wire, base, model_id = _route(model, api_base)
    body = _chat_body(params, stream=bool(stream), openai=base == _OPENAI_BASE)
    body.update(model=model_id, messages=messages)
    if wire == "anthropic":
        request = translate.chat_to_anthropic_request(body)
        url, headers = f"{base}/v1/messages", _anthropic_headers(api_key)
        if stream:
            request["stream"] = True
            events = await _post_stream(url, headers, request, timeout)
            return translate.anthropic_stream_to_chat(events, model_id)
        reply = await _post_json(url, headers, request, timeout)
        return ModelResponse(translate.anthropic_to_chat(reply, model_id), reply.headers)
    url, headers = f"{base}/chat/completions", _bearer(api_key)
    if stream:
        body["stream"] = True
        return await _post_stream(url, headers, body, timeout)
    return await _post_json(url, headers, body, timeout)


def _chat_body(params: Mapping[str, Any], *, stream: bool, openai: bool) -> dict[str, Any]:
    body = {k: v for k, v in params.items() if k in _CHAT_PARAMS and v is not None}
    if not body.get("tools"):
        for key in ("tools", "tool_choice", "parallel_tool_calls"):
            body.pop(key, None)  # providers reject these without tools
    if not stream:
        body.pop("stream_options", None)
    # OpenAI's current models take only max_completion_tokens; most others only max_tokens.
    old, new = ("max_tokens", "max_completion_tokens") if openai else (
        "max_completion_tokens", "max_tokens"
    )
    if old in body:
        value = body.pop(old)
        body.setdefault(new, value)
    return body


async def aresponses(
    *,
    model: str,
    input: Any,
    api_key: str | None = None,
    api_base: str | None = None,
    stream: bool = False,
    timeout: float | None = None,
    **params: Any,
) -> ModelResponse | AsyncIterator[dict[str, Any]]:
    """An OpenAI Responses call, served through Chat Completions."""

    _, model_id = _split(model)
    chat = translate.responses_to_chat_request(input, params)
    result = await acompletion(
        model=model, api_key=api_key, api_base=api_base, stream=stream, timeout=timeout,
        **chat,
    )
    if stream:
        return translate.chat_stream_to_responses(result, model_id)
    return ModelResponse(translate.chat_to_responses(result, model_id), result.headers)


async def anthropic_messages(
    *,
    model: str,
    messages: list[Any],
    api_key: str | None = None,
    api_base: str | None = None,
    stream: bool = False,
    timeout: float | None = None,
    **params: Any,
) -> ModelResponse | AsyncIterator[dict[str, Any]]:
    """An Anthropic Messages call: passed straight through to Anthropic, translated
    through Chat Completions for every other provider."""

    wire, model_id = _split(model)
    if wire == "anthropic":
        _, base, model_id = _route(model, api_base)
        body = {k: v for k, v in params.items() if k in _ANTHROPIC_PARAMS and v is not None}
        body.update(model=model_id, messages=messages)
        body.setdefault("max_tokens", translate.DEFAULT_ANTHROPIC_MAX_TOKENS)
        url, headers = f"{base}/v1/messages", _anthropic_headers(api_key)
        if stream:
            body["stream"] = True
            return await _post_stream(url, headers, body, timeout)
        return await _post_json(url, headers, body, timeout)
    chat = translate.anthropic_to_chat_request(messages, params)
    result = await acompletion(
        model=model, api_key=api_key, api_base=api_base, stream=stream, timeout=timeout,
        **chat,
    )
    if stream:
        return translate.chat_stream_to_anthropic(result, model_id)
    return ModelResponse(translate.chat_to_anthropic(result, model_id), result.headers)


async def atranscription(
    *,
    model: str,
    file: tuple[str, bytes, str],
    api_key: str | None = None,
    api_base: str | None = None,
    timeout: float | None = None,
    **_: Any,
) -> Transcription:
    """Speech to text on an OpenAI-compatible ``/audio/transcriptions`` endpoint."""

    _, base, model_id = _route(model, api_base)
    response = await _send(
        f"{base}/audio/transcriptions",
        headers=_bearer(api_key),
        timeout=timeout,
        files={"file": file},
        data={"model": model_id},
    )
    try:
        text = response.json().get("text")
    except (ValueError, AttributeError):
        text = None
    return Transcription(text=text if isinstance(text, str) else "")
