"""A model served on this computer (Zhunt Plus / InferSSD), added to the catalog.

The local provider is not in ``catalog.yaml``: its address, model and context
differ per machine, and the signed catalog feed replaces the packaged catalog.
It is built from settings in the environment (``~/.zhunt/env``, written by
``zhunt local setup``) and appended to whichever catalog is active.

Settings:
  ZHUNT_LOCAL_URL      full OpenAI-style base URL, e.g. http://127.0.0.1:8080/v1
  ZHUNT_LOCAL_PORT     or just the port on 127.0.0.1 (InferSSD's default is 8080)
  ZHUNT_LOCAL_MODEL    model name sent to the server (default "local-model")
  ZHUNT_LOCAL_CONTEXT  context window in tokens (default 1024)
  ZHUNT_LOCAL_TOOLS    "1" if the server handles tool calls (default off)

The URL must point at this computer (127.0.0.1, ::1 or localhost): a private chat
is only ever sent here, so a remote address would defeat the point.

Privacy rule: a chat marked private (header ``x-zhunt-private: 1``, or the model
alias ``zhunt-local`` / ``zhunt-private``) is only ever routed to this provider.
If it is not configured, not answering, or the chat does not fit, the request
fails with a clear message and nothing is sent anywhere else.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from urllib.parse import urlsplit

import httpx

from zhunt.brain import Tier
from zhunt.catalog import Catalog, CatalogModel, Limits, Provider

PLATFORM = "local"
URL_ENV = "ZHUNT_LOCAL_URL"
PORT_ENV = "ZHUNT_LOCAL_PORT"
MODEL_ENV = "ZHUNT_LOCAL_MODEL"
CONTEXT_ENV = "ZHUNT_LOCAL_CONTEXT"
TOOLS_ENV = "ZHUNT_LOCAL_TOOLS"
DEFAULT_PORT = 8080
DEFAULT_MODEL = "local-model"
DEFAULT_CONTEXT = 1024
PRIVATE_HEADER = "x-zhunt-private"
# Model aliases that mean "only my local model".
LOCAL_ALIASES = frozenset({"zhunt-local", "zhunt-private"})
_TRUTHY = {"1", "true", "yes", "on"}
_HEALTH_TIMEOUT = 2.0


class LocalConfigError(ValueError):
    """The local provider settings are present but unusable."""


@dataclass(frozen=True)
class LocalConfig:
    base_url: str
    model: str = DEFAULT_MODEL
    context_window: int = DEFAULT_CONTEXT
    supports_tools: bool = False


def config_from_env(env: Mapping[str, str]) -> LocalConfig | None:
    """The local provider settings, ``None`` when none are set.

    Raises ``LocalConfigError`` for settings that are set but wrong (a remote URL,
    a bad port), so the caller can say so instead of silently routing elsewhere.
    """

    url = (env.get(URL_ENV) or "").strip()
    port = (env.get(PORT_ENV) or "").strip()
    if not url and not port:
        return None
    if not url:
        if not port.isdigit() or not 1 <= int(port) <= 65_535:
            raise LocalConfigError(f"{PORT_ENV} must be a port number, got {port!r}")
        url = f"http://127.0.0.1:{int(port)}/v1"
    base_url = loopback_url(url)
    raw_context = (env.get(CONTEXT_ENV) or "").strip()
    if raw_context and (not raw_context.isdigit() or int(raw_context) < 1):
        raise LocalConfigError(f"{CONTEXT_ENV} must be a positive number of tokens")
    return LocalConfig(
        base_url=base_url,
        model=(env.get(MODEL_ENV) or "").strip() or DEFAULT_MODEL,
        context_window=int(raw_context) if raw_context else DEFAULT_CONTEXT,
        supports_tools=(env.get(TOOLS_ENV) or "").strip().lower() in _TRUTHY,
    )


def loopback_url(url: str) -> str:
    """``url`` without a trailing slash, if it is http(s) on this computer."""

    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise LocalConfigError(f"{URL_ENV} must be an http:// URL, got {url!r}")
    host = parts.hostname
    if host != "localhost":
        try:
            if not ipaddress.ip_address(host).is_loopback:
                raise ValueError
        except ValueError:
            raise LocalConfigError(
                f"{URL_ENV} must point at this computer (127.0.0.1, ::1 or localhost), "
                f"got {host!r}"
            ) from None
    try:
        parts.port  # noqa: B018 - raises on a malformed port
    except ValueError as error:
        raise LocalConfigError(f"{URL_ENV} has an invalid port") from error
    return url.rstrip("/")


def with_local_provider(catalog: Catalog, config: LocalConfig | None) -> Catalog:
    """``catalog`` plus the local provider (replacing any earlier one)."""

    providers = {k: v for k, v in catalog.providers.items() if k != PLATFORM}
    models = tuple(m for m in catalog.models if m.platform != PLATFORM)
    if config is None:
        return Catalog(providers, models)
    providers[PLATFORM] = Provider(
        platform=PLATFORM,
        name="Local model (Zhunt Plus)",
        base_url=config.base_url,
        key_env="ZHUNT_LOCAL_API_KEY",  # never needed; keyless
        verified_on=date.today(),
        litellm_provider="openai",
        free_note="Runs on this computer: free and unlimited, but slow. Private chats use only this.",
        keyless=True,
        local=True,
    )
    local_model = CatalogModel(
        platform=PLATFORM,
        model_id=config.model,
        # Every tier, so a private chat of any kind has somewhere to go. Ordinary
        # chats reach it only after every cloud model (see Selector).
        tiers=frozenset(Tier),
        limits=Limits(),
        intelligence=60,
        speed=1,  # ~2 tokens/second on a 24 GB Apple Silicon laptop (measured, one model)
        context_window=config.context_window,
        supports_tools=config.supports_tools,
        may_train_on_prompts=False,
        input_cost=Decimal(0),
        output_cost=Decimal(0),
    )
    return Catalog(providers, (*models, local_model))


def is_private(model_alias: object, headers: Mapping[str, str] | None) -> bool:
    """Whether a request asked to stay on this computer."""

    if isinstance(model_alias, str) and model_alias in LOCAL_ALIASES:
        return True
    if not headers:
        return False
    for key, value in headers.items():
        if key.lower() == PRIVATE_HEADER:
            return str(value).strip().lower() in _TRUTHY
    return False


async def is_answering(base_url: str, *, timeout: float = _HEALTH_TIMEOUT) -> bool:
    """Whether the local server answers ``GET /models`` (a loopback call only)."""

    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.get(f"{base_url}/models")
    except httpx.HTTPError:
        return False
    return response.status_code < 500
