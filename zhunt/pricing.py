"""OpenRouter model pricing synchronization."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from ruamel.yaml import YAML


OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"


class PricingSyncError(RuntimeError):
    """Raised when OpenRouter pricing cannot be fetched or parsed."""


@dataclass(frozen=True)
class PricingSyncResult:
    updated: tuple[str, ...]
    unavailable: tuple[str, ...]
    cheaper_tiers: tuple[str, ...]


def fetch_openrouter_models(
    url: str = OPENROUTER_MODELS_URL,
) -> list[Mapping[str, object]]:
    request = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
        raise PricingSyncError(f"unable to fetch OpenRouter models: {error}") from error
    data = payload.get("data") if isinstance(payload, Mapping) else None
    if not isinstance(data, list) or not all(isinstance(item, Mapping) for item in data):
        raise PricingSyncError("OpenRouter models response has invalid data")
    return data


def sync_registry(
    registry_path: Path,
    *,
    fetcher: Callable[[], list[Mapping[str, object]]] = fetch_openrouter_models,
) -> PricingSyncResult:
    """Refresh matching registry prices from OpenRouter's models endpoint."""

    path = registry_path.expanduser()
    yaml = YAML()
    try:
        with path.open(encoding="utf-8") as registry_file:
            document = yaml.load(registry_file)
    except OSError as error:
        raise PricingSyncError(f"unable to read registry: {error}") from error
    if not isinstance(document, Mapping) or not isinstance(document.get("tiers"), Mapping):
        raise PricingSyncError("registry requires a tiers mapping")

    remote = {
        str(item["id"]): item
        for item in fetcher()
        if isinstance(item.get("id"), str)
    }
    before_minima = _tier_minima(document["tiers"])
    updated: list[str] = []
    unavailable: list[str] = []
    for models in document["tiers"].values():
        if not isinstance(models, list):
            continue
        for model in models:
            if not isinstance(model, Mapping) or not isinstance(model.get("model"), str):
                continue
            model_id = str(model["model"])
            remote_item = remote.get(model_id) or remote.get(_strip_provider(model_id))
            if remote_item is None:
                unavailable.append(model_id)
                continue
            pricing = remote_item.get("pricing")
            if not isinstance(pricing, Mapping):
                unavailable.append(model_id)
                continue
            input_cost = _per_token_to_per_million(pricing.get("input"))
            output_cost = _per_token_to_per_million(pricing.get("output"))
            if input_cost is None or output_cost is None:
                unavailable.append(model_id)
                continue
            model["in"] = float(input_cost)
            model["out"] = float(output_cost)
            updated.append(model_id)

    cheaper_tiers = tuple(
        tier
        for tier, before in before_minima.items()
        if _tier_minima(document["tiers"]).get(tier, before) < before
    )
    try:
        with path.open("w", encoding="utf-8") as registry_file:
            yaml.dump(document, registry_file)
    except OSError as error:
        raise PricingSyncError(f"unable to write registry: {error}") from error
    return PricingSyncResult(
        updated=tuple(updated),
        unavailable=tuple(unavailable),
        cheaper_tiers=cheaper_tiers,
    )


def _strip_provider(model_id: str) -> str:
    if "/" in model_id:
        provider, remainder = model_id.split("/", 1)
        if provider in {"openrouter", "custom"}:
            return remainder
    return model_id


def _per_token_to_per_million(value: object) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError):
        return None
    if parsed < 0:
        return None
    return parsed * Decimal(1_000_000)


def _tier_minima(tiers: Mapping[object, object]) -> dict[str, Decimal]:
    minima: dict[str, Decimal] = {}
    for tier, models in tiers.items():
        costs: list[Decimal] = []
        if isinstance(models, list):
            for model in models:
                if not isinstance(model, Mapping):
                    continue
                try:
                    costs.append(Decimal(str(model["in"])) + Decimal(str(model["out"])))
                except (KeyError, InvalidOperation):
                    continue
        if costs:
            minima[str(tier)] = min(costs)
    return minima
