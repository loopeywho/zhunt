"""Named local models for Ollama clients: ``translator`` and the like.

Ollama lets you name a model built from a base model plus a system prompt and
settings (a Modelfile). Scripts ask for those names. A profile gives Zhunt the
same thing: a name, the address of a local server running the base model, and
the system prompt and settings to add.

Profiles live in ``~/.zhunt/local_models.json`` (or the file named by
``ZHUNT_LOCAL_MODELS``), because system prompts are personal::

    {"translator": {"url": "http://127.0.0.1:8082/v1", "system": "You are ...",
                  "options": {"temperature": 0.7, "top_k": 20, "top_p": 0.95}}}

A request naming a profile goes only to that server (it is a model on this
computer), never to the cloud. If the server is not answering, the answer is a
503 and nothing is sent anywhere else.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from zhunt import local_provider

FILE_ENV = "ZHUNT_LOCAL_MODELS"
_TIMEOUT = httpx.Timeout(600.0, connect=5.0)
_NOTHING_SENT = "nothing was sent to any cloud provider"
# Ollama options passed on as llama.cpp server fields (temperature, top_p, stop
# and num_predict are already mapped by ollama.build_request).
_EXTRA_OPTIONS = ("top_k", "repeat_penalty", "min_p", "seed")


class ProfileError(Exception):
    pass


@dataclass(frozen=True)
class Profile:
    name: str
    url: str
    model: str = "local-model"
    system: str = ""
    options: dict[str, Any] = field(default_factory=dict)


def _path() -> Path:
    return Path(os.environ.get(FILE_ENV) or Path.home() / ".zhunt" / "local_models.json").expanduser()


def load() -> dict[str, Profile]:
    path = _path()
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ProfileError(f"{path}: {error}") from error
    if not isinstance(raw, dict):
        raise ProfileError(f"{path}: expected an object of named models")
    profiles = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("url"), str):
            raise ProfileError(f"{path}: {name!r} needs a url")
        try:
            url = local_provider.loopback_url(entry["url"])
        except local_provider.LocalConfigError as error:
            raise ProfileError(f"{path}: {name!r}: {error}") from error
        options = entry.get("options") or {}
        profiles[name] = Profile(
            name=name, url=url, model=str(entry.get("model") or "local-model"),
            system=str(entry.get("system") or ""), options=dict(options) if isinstance(options, dict) else {},
        )
    return profiles


def lookup(requested: str) -> Profile | None:
    """The profile for ``requested`` (``translator`` or ``translator:latest``)."""

    profiles = load()
    if requested in profiles:
        return profiles[requested]
    base, _, tag = requested.partition(":")
    return profiles.get(base) if tag == "latest" else None


def apply(profile: Profile, payload: dict[str, Any], request_options: dict[str, Any]) -> dict[str, Any]:
    """``payload`` with the profile's system prompt and settings, as Ollama does:
    a system message or option in the request wins over the profile's."""

    messages = payload.get("messages") or []
    if profile.system and not any(m.get("role") == "system" for m in messages):
        payload["messages"] = [{"role": "system", "content": profile.system}, *messages]
    defaults = profile.options
    for key in ("temperature", "top_p"):
        if key not in payload and defaults.get(key) is not None:
            payload[key] = defaults[key]
    if "stop" not in payload and defaults.get("stop"):
        payload["stop"] = defaults["stop"]
    for key in _EXTRA_OPTIONS:
        value = request_options.get(key, defaults.get(key))
        if value is not None:
            payload[key] = value
    payload["model"] = profile.model
    return payload


def _unavailable(profile: Profile) -> ProfileError:
    return ProfileError(f"local model {profile.name!r} at {profile.url} is not answering; {_NOTHING_SENT}")


async def complete(profile: Profile, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.post(f"{profile.url}/chat/completions", json={**payload, "stream": False})
    except httpx.HTTPError as error:
        raise _unavailable(profile) from error
    if response.status_code != 200:
        raise ProfileError(f"local model {profile.name!r}: {response.text[:300]}")
    return response.json()


async def stream(profile: Profile, payload: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    """Chat Completions chunks from the server. Connection errors before the first
    chunk raise ProfileError, so the caller can still answer 503."""

    client = httpx.AsyncClient(timeout=_TIMEOUT)
    try:
        request = client.build_request("POST", f"{profile.url}/chat/completions", json={**payload, "stream": True})
        response = await client.send(request, stream=True)
    except httpx.HTTPError as error:
        await client.aclose()
        raise _unavailable(profile) from error
    if response.status_code != 200:
        text = (await response.aread()).decode("utf-8", "replace")
        await response.aclose()
        await client.aclose()
        raise ProfileError(f"local model {profile.name!r}: {text[:300]}")

    async def chunks() -> AsyncIterator[dict[str, Any]]:
        try:
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                yield json.loads(data)
        finally:
            await response.aclose()
            await client.aclose()

    return chunks()
