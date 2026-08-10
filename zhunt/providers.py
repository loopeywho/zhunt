"""Provider catalog and OpenAI-compatible account validation."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

class ProviderError(RuntimeError):
    """Raised when a provider cannot be configured or validated."""


@dataclass(frozen=True)
class ProviderSpec:
    id: str
    name: str
    base_url: str
    key_env: str
    models_path: str = "/models"
    docs_url: str = ""

    @property
    def models_url(self) -> str:
        return f"{self.base_url.rstrip('/')}{self.models_path}"

PROVIDERS: dict[str, ProviderSpec] = {
    "openrouter": ProviderSpec(
        id="openrouter",
        name="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        key_env="OPENROUTER_API_KEY",
        docs_url="https://openrouter.ai/docs",
    ),
    "nous-portal": ProviderSpec(
        id="nous-portal",
        name="Nous Portal",
        base_url="https://api.portal.ai/v1",
        key_env="PORTAL_API_KEY",
        docs_url="https://platform.portal.ai/docs.html",
    ),
    "openai": ProviderSpec(
        id="openai",
        name="OpenAI",
        base_url="https://api.openai.com/v1",
        key_env="OPENAI_API_KEY",
        docs_url="https://platform.openai.com/docs",
    ),
    "anthropic": ProviderSpec(
        id="anthropic",
        name="Anthropic",
        base_url="https://api.anthropic.com/v1",
        key_env="ANTHROPIC_API_KEY",
        docs_url="https://platform.claude.com/docs",
    ),
}


def provider_specs() -> tuple[ProviderSpec, ...]:
    return tuple(PROVIDERS.values())


def get_provider(provider_id: str) -> ProviderSpec:
    try:
        return PROVIDERS[provider_id]
    except KeyError as error:
        raise ProviderError(f"unknown provider: {provider_id}") from error


def validate_provider_key(
    provider_id: str,
    api_key: str,
    *,
    fetcher: Callable[[Request], object] | None = None,
) -> int:
    """Validate a key by calling the provider's authenticated model list."""

    if not api_key.strip():
        raise ProviderError("API key is required")
    provider = get_provider(provider_id)
    request = Request(
        provider.models_url,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {api_key.strip()}",
        },
    )
    try:
        response = fetcher(request) if fetcher is not None else _fetch(request)
    except (HTTPError, URLError, TimeoutError, OSError) as error:
        raise ProviderError(f"unable to validate {provider.name} key: {error}") from error
    if not isinstance(response, Mapping):
        raise ProviderError("provider returned an invalid models response")
    models = response.get("data")
    if not isinstance(models, list):
        error = response.get("error")
        detail = error.get("message") if isinstance(error, Mapping) else None
        raise ProviderError(str(detail or "provider returned no models"))
    return sum(1 for model in models if isinstance(model, Mapping) and model.get("id"))


def configured_provider(env: Mapping[str, str] | None = None) -> ProviderSpec | None:
    values = env or os.environ
    provider_id = values.get("ZHUNT_PROVIDER")
    if not provider_id:
        return None
    return get_provider(provider_id)


def _fetch(request: Request) -> Mapping[str, object]:
    with urlopen(request, timeout=15) as response:
        payload = json.load(response)
    return payload if isinstance(payload, Mapping) else {}
