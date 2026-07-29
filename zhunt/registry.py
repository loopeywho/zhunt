"""YAML-backed aliases, tiers, and model selection."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

from zhunt.brain import Tier


class RegistryError(ValueError):
    """Raised when registry data is invalid or cannot satisfy a request."""


@dataclass(frozen=True)
class Model:
    model: str
    input_cost: Decimal
    output_cost: Decimal

    def projected_cost(self, *, input_tokens: int, output_tokens: int) -> Decimal:
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("token counts cannot be negative")
        per_million = Decimal(1_000_000)
        return (
            self.input_cost * input_tokens + self.output_cost * output_tokens
        ) / per_million


class ModelRegistry:
    def __init__(
        self,
        *,
        aliases: Mapping[str, Tier | None],
        tiers: Mapping[Tier, tuple[Model, ...]],
    ) -> None:
        self._aliases = dict(aliases)
        self._tiers = dict(tiers)

    @classmethod
    def from_path(cls, path: str | Path) -> ModelRegistry:
        with Path(path).open(encoding="utf-8") as registry_file:
            return cls.from_data(yaml.safe_load(registry_file))

    @classmethod
    def default(cls) -> ModelRegistry:
        packaged = resources.files("zhunt").joinpath("models.yaml")
        if packaged.is_file():
            with packaged.open(encoding="utf-8") as registry_file:
                return cls.from_data(yaml.safe_load(registry_file))
        return cls.from_path(Path(__file__).parent.parent / "models.yaml")

    @classmethod
    def from_data(cls, data: Any) -> ModelRegistry:
        if not isinstance(data, Mapping):
            raise RegistryError("registry root must be a mapping")

        raw_aliases = data.get("aliases")
        raw_tiers = data.get("tiers")
        if not isinstance(raw_aliases, Mapping) or not isinstance(raw_tiers, Mapping):
            raise RegistryError("registry requires aliases and tiers mappings")

        tiers: dict[Tier, tuple[Model, ...]] = {}
        for raw_tier, raw_models in raw_tiers.items():
            tier = _parse_tier(raw_tier)
            if not isinstance(raw_models, list) or not raw_models:
                raise RegistryError(f"tier {tier.value!r} must contain models")
            tiers[tier] = tuple(_parse_model(item, tier) for item in raw_models)

        aliases: dict[str, Tier | None] = {}
        for alias, config in raw_aliases.items():
            if not isinstance(alias, str) or not isinstance(config, Mapping):
                raise RegistryError("each alias must map to a configuration")
            raw_target = config.get("tier")
            if raw_target == "auto":
                aliases[alias] = None
                continue
            target = _parse_tier(raw_target)
            if target not in tiers:
                raise RegistryError(
                    f"alias {alias!r} targets missing tier {target.value!r}"
                )
            aliases[alias] = target

        return cls(aliases=aliases, tiers=tiers)

    def resolve_alias(self, alias: str) -> Tier | None:
        try:
            return self._aliases[alias]
        except KeyError as error:
            raise RegistryError(f"unknown model alias: {alias}") from error

    def has_alias(self, alias: str) -> bool:
        return alias in self._aliases

    def select_model(
        self,
        tier: Tier,
        *,
        input_tokens: int = 1,
        output_tokens: int = 1,
        healthy_models: Collection[str] | None = None,
    ) -> Model:
        candidates = self._tiers.get(tier, ())
        if healthy_models is not None:
            candidates = tuple(
                model for model in candidates if model.model in healthy_models
            )
        if not candidates:
            raise RegistryError(f"no healthy models available for tier {tier.value!r}")

        return min(
            candidates,
            key=lambda model: model.projected_cost(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            ),
        )


def _parse_tier(value: Any) -> Tier:
    try:
        return Tier(value)
    except (TypeError, ValueError) as error:
        raise RegistryError(f"unknown tier: {value!r}") from error


def _parse_model(data: Any, tier: Tier) -> Model:
    if not isinstance(data, Mapping):
        raise RegistryError(f"models in tier {tier.value!r} must be mappings")
    model = data.get("model")
    if not isinstance(model, str) or not model:
        raise RegistryError(f"model in tier {tier.value!r} requires an id")
    try:
        input_cost = Decimal(str(data["in"]))
        output_cost = Decimal(str(data["out"]))
    except (KeyError, InvalidOperation) as error:
        raise RegistryError(f"model {model!r} has invalid pricing") from error
    if input_cost < 0 or output_cost < 0:
        raise RegistryError(f"model {model!r} has negative pricing")
    return Model(model=model, input_cost=input_cost, output_cost=output_cost)
