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
    ttft_ms: Decimal | None = None

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
    def from_path(
        cls,
        path: str | Path,
        *,
        provider_id: str | None = None,
    ) -> ModelRegistry:
        with Path(path).open(encoding="utf-8") as registry_file:
            return cls.from_data(yaml.safe_load(registry_file), provider_id=provider_id)

    @classmethod
    def default(cls, *, provider_id: str | None = None) -> ModelRegistry:
        packaged = resources.files("zhunt").joinpath("models.yaml")
        if packaged.is_file():
            with packaged.open(encoding="utf-8") as registry_file:
                return cls.from_data(
                    yaml.safe_load(registry_file),
                    provider_id=provider_id,
                )
        return cls.from_path(
            Path(__file__).parent.parent / "models.yaml",
            provider_id=provider_id,
        )

    @classmethod
    def from_data(
        cls,
        data: Any,
        *,
        provider_id: str | None = None,
    ) -> ModelRegistry:
        if not isinstance(data, Mapping):
            raise RegistryError("registry root must be a mapping")

        if provider_id is not None:
            profiles = data.get("providers")
            if isinstance(profiles, Mapping) and provider_id in profiles:
                profile = profiles[provider_id]
                if not isinstance(profile, Mapping):
                    raise RegistryError(
                        f"provider profile {provider_id!r} must be a mapping"
                    )
                data = profile

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

    def model_ids(self) -> tuple[str, ...]:
        """Return every configured upstream model identifier."""

        return tuple(
            model.model
            for models in self._tiers.values()
            for model in models
        )

    def projected_cost(
        self,
        model_id: str,
        *,
        input_tokens: int,
        output_tokens: int,
    ) -> Decimal:
        for models in self._tiers.values():
            for model in models:
                if model.model == model_id:
                    return model.projected_cost(
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                    )
        raise RegistryError(f"unknown model id: {model_id}")

    def top_model_cost(
        self,
        tier: Tier,
        *,
        input_tokens: int,
        output_tokens: int,
    ) -> Decimal:
        models = self._tiers.get(tier, ())
        if not models:
            raise RegistryError(f"no models available for tier {tier.value!r}")
        return max(
            model.projected_cost(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
            for model in models
        )

    def top_model_cost_all(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
    ) -> Decimal:
        """Return the counterfactual cost of the most expensive registry model."""

        models = [
            model
            for tier_models in self._tiers.values()
            for model in tier_models
        ]
        if not models:
            raise RegistryError("no models available for counterfactual cost")
        return max(
            model.projected_cost(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
            for model in models
        )

    def select_model(
        self,
        tier: Tier,
        *,
        input_tokens: int = 1,
        output_tokens: int = 1,
        healthy_models: Collection[str] | None = None,
        latency_metrics: Mapping[str, float] | None = None,
        latency_weight: float = 0.0,
    ) -> Model:
        if not 0 <= latency_weight <= 1:
            raise ValueError("latency_weight must be between 0 and 1")
        candidates = self._tiers.get(tier, ())
        if healthy_models is not None:
            candidates = tuple(
                model for model in candidates if model.model in healthy_models
            )
        if not candidates:
            raise RegistryError(f"no healthy models available for tier {tier.value!r}")

        costs = {
            model.model: model.projected_cost(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
            )
            for model in candidates
        }
        latency_values: dict[str, float] = {}
        for model in candidates:
            measured = latency_metrics.get(model.model) if latency_metrics else None
            configured = model.ttft_ms
            if measured is None and configured is not None:
                measured = float(configured)
            if measured is None:
                break
            latency_values[model.model] = measured
        else:
            if latency_weight > 0:
                min_cost, max_cost = min(costs.values()), max(costs.values())
                min_latency = min(latency_values.values())
                max_latency = max(latency_values.values())
                cost_span = max_cost - min_cost
                latency_span = max_latency - min_latency

                def weighted_score(model: Model) -> tuple[float, Decimal]:
                    cost_norm = (
                        float((costs[model.model] - min_cost) / cost_span)
                        if cost_span
                        else 0.0
                    )
                    latency_norm = (
                        (latency_values[model.model] - min_latency) / latency_span
                        if latency_span
                        else 0.0
                    )
                    score = (
                        (1 - latency_weight) * cost_norm
                        + latency_weight * latency_norm
                    )
                    return score, costs[model.model]

                return min(candidates, key=weighted_score)

        return min(
            candidates,
            key=lambda model: costs[model.model],
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
    raw_ttft = data.get("ttft_ms")
    ttft_ms: Decimal | None = None
    if raw_ttft is not None:
        try:
            ttft_ms = Decimal(str(raw_ttft))
        except InvalidOperation as error:
            raise RegistryError(f"model {model!r} has invalid ttft_ms") from error
        if ttft_ms < 0:
            raise RegistryError(f"model {model!r} has negative ttft_ms")
    return Model(
        model=model,
        input_cost=input_cost,
        output_cost=output_cost,
        ttft_ms=ttft_ms,
    )
