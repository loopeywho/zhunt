"""Provider and model catalog for the free-tier engine."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

from zhunt.brain import Tier


class CatalogError(ValueError):
    """Raised when catalog data is invalid."""


@dataclass(frozen=True)
class Limits:
    """Per-key caps; ``None`` means the provider publishes no such cap."""

    rpm: int | None = None
    rpd: int | None = None
    tpm: int | None = None
    tpd: int | None = None


@dataclass(frozen=True)
class Provider:
    platform: str
    name: str
    base_url: str
    key_env: str
    verified_on: date


@dataclass(frozen=True)
class CatalogModel:
    """One routable model. ``intelligence`` and ``speed`` are hand-assigned 1-100
    scores (higher is better) used only to rank models against each other."""

    platform: str
    model_id: str
    tiers: frozenset[Tier]
    limits: Limits
    intelligence: int
    speed: int
    context_window: int
    supports_tools: bool
    may_train_on_prompts: bool
    input_cost: Decimal
    output_cost: Decimal

    @property
    def id(self) -> str:
        return f"{self.platform}/{self.model_id}"

    @property
    def free(self) -> bool:
        return self.input_cost == 0 and self.output_cost == 0


class Catalog:
    def __init__(
        self,
        providers: Mapping[str, Provider],
        models: tuple[CatalogModel, ...],
    ) -> None:
        self.providers = dict(providers)
        self.models = models

    def models_for_tier(self, tier: Tier) -> tuple[CatalogModel, ...]:
        return tuple(model for model in self.models if tier in model.tiers)

    @classmethod
    def from_path(cls, path: str | Path) -> Catalog:
        with Path(path).open(encoding="utf-8") as catalog_file:
            return cls.from_data(yaml.safe_load(catalog_file))

    @classmethod
    def from_data(cls, data: Any) -> Catalog:
        if not isinstance(data, Mapping):
            raise CatalogError("catalog root must be a mapping")
        raw_providers = data.get("providers")
        if not isinstance(raw_providers, Mapping) or not raw_providers:
            raise CatalogError("catalog requires a non-empty providers mapping")

        providers: dict[str, Provider] = {}
        models: list[CatalogModel] = []
        seen: set[str] = set()
        for platform, raw in raw_providers.items():
            if not isinstance(platform, str) or not isinstance(raw, Mapping):
                raise CatalogError("each provider must be a mapping")
            providers[platform] = Provider(
                platform=platform,
                name=_string(raw, "name", platform),
                base_url=_string(raw, "base_url", platform),
                key_env=_string(raw, "key_env", platform),
                verified_on=_date(raw.get("verified_on"), platform),
            )
            raw_models = raw.get("models")
            if not isinstance(raw_models, list) or not raw_models:
                raise CatalogError(f"provider {platform!r} requires models")
            for raw_model in raw_models:
                model = _parse_model(platform, raw_model)
                if model.id in seen:
                    raise CatalogError(f"duplicate model id: {model.id}")
                seen.add(model.id)
                models.append(model)
        return cls(providers, tuple(models))


def _string(raw: Mapping[str, Any], field: str, owner: str) -> str:
    value = raw.get(field)
    if not isinstance(value, str) or not value:
        raise CatalogError(f"{owner!r} requires a {field}")
    return value


def _date(value: Any, owner: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as error:
        raise CatalogError(
            f"provider {owner!r} requires verified_on as an ISO date"
        ) from error


def _int(raw: Mapping[str, Any], field: str, owner: str, *, low: int, high: int | None = None) -> int:
    value = raw.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < low:
        raise CatalogError(f"model {owner!r} has invalid {field}")
    if high is not None and value > high:
        raise CatalogError(f"model {owner!r} has invalid {field}")
    return value


def _flag(raw: Mapping[str, Any], field: str, owner: str, *, default: bool | None) -> bool:
    value = raw.get(field, default)
    if not isinstance(value, bool):
        raise CatalogError(f"model {owner!r} requires {field} to be true or false")
    return value


def _parse_limits(raw: Any, owner: str) -> Limits:
    if raw is None:
        return Limits()
    if not isinstance(raw, Mapping):
        raise CatalogError(f"model {owner!r} limits must be a mapping")
    unknown = set(raw) - {"rpm", "rpd", "tpm", "tpd"}
    if unknown:
        raise CatalogError(f"model {owner!r} has unknown limits: {sorted(unknown)}")
    values: dict[str, int | None] = {}
    for key in ("rpm", "rpd", "tpm", "tpd"):
        value = raw.get(key)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
        ):
            raise CatalogError(f"model {owner!r} has invalid {key} limit")
        values[key] = value
    return Limits(**values)


def _parse_cost(raw: Mapping[str, Any], field: str, owner: str) -> Decimal:
    try:
        cost = Decimal(str(raw.get(field, 0)))
    except InvalidOperation as error:
        raise CatalogError(f"model {owner!r} has invalid {field} price") from error
    if cost < 0:
        raise CatalogError(f"model {owner!r} has negative {field} price")
    return cost


def _parse_model(platform: str, raw: Any) -> CatalogModel:
    if not isinstance(raw, Mapping):
        raise CatalogError(f"models in provider {platform!r} must be mappings")
    model_id = _string(raw, "id", platform)
    owner = f"{platform}/{model_id}"
    raw_tiers = raw.get("tiers")
    if not isinstance(raw_tiers, list) or not raw_tiers:
        raise CatalogError(f"model {owner!r} requires tiers")
    try:
        tiers = frozenset(Tier(value) for value in raw_tiers)
    except ValueError as error:
        raise CatalogError(f"model {owner!r} has an unknown tier") from error
    return CatalogModel(
        platform=platform,
        model_id=model_id,
        tiers=tiers,
        limits=_parse_limits(raw.get("limits"), owner),
        intelligence=_int(raw, "intelligence", owner, low=1, high=100),
        speed=_int(raw, "speed", owner, low=1, high=100),
        context_window=_int(raw, "context_window", owner, low=1),
        supports_tools=_flag(raw, "supports_tools", owner, default=False),
        # No default: every model must state its training policy explicitly.
        may_train_on_prompts=_flag(raw, "may_train_on_prompts", owner, default=None),
        input_cost=_parse_cost(raw, "in", owner),
        output_cost=_parse_cost(raw, "out", owner),
    )
