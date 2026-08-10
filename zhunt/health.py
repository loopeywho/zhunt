"""Model availability validation and runtime provider health state."""

from __future__ import annotations

from collections.abc import Callable, Collection
from threading import RLock

from zhunt.registry import ModelRegistry


class ModelAvailabilityError(RuntimeError):
    """Raised when one or more configured models cannot be reached."""


class ModelHealth:
    """Track consecutive provider failures and exclude unhealthy models."""

    def __init__(
        self,
        models: Collection[str],
        *,
        failure_threshold: int = 2,
    ) -> None:
        if failure_threshold <= 0:
            raise ValueError("failure_threshold must be positive")
        self._models = frozenset(models)
        self.failure_threshold = failure_threshold
        self._failures: dict[str, int] = {}
        self._unhealthy: set[str] = set()
        self._lock = RLock()

    def healthy_models(
        self,
        *,
        exclude: Collection[str] = (),
    ) -> frozenset[str]:
        with self._lock:
            return frozenset(
                self._models.difference(self._unhealthy).difference(exclude)
            )

    def record_failure(self, model: str) -> bool:
        """Record a provider error; return whether the model is now unhealthy."""

        with self._lock:
            failures = self._failures.get(model, 0) + 1
            self._failures[model] = failures
            if failures >= self.failure_threshold:
                self._unhealthy.add(model)
            return model in self._unhealthy

    def record_success(self, model: str) -> None:
        with self._lock:
            if model in self._unhealthy:
                self._unhealthy.discard(model)
            self._failures.pop(model, None)

    def is_healthy(self, model: str) -> bool:
        with self._lock:
            return model not in self._unhealthy

    def failure_count(self, model: str) -> int:
        with self._lock:
            return self._failures.get(model, 0)


def validate_registry_models(
    registry: ModelRegistry,
    is_available: Callable[[str], bool],
) -> None:
    """Fail startup with every configured model that the provider rejects."""

    unavailable: list[str] = []
    for model in registry.model_ids():
        try:
            available = is_available(model)
        except Exception:
            available = False
        if not available:
            unavailable.append(model)
    if unavailable:
        names = ", ".join(sorted(unavailable))
        raise ModelAvailabilityError(
            f"configured models unavailable at startup: {names}"
        )


def litellm_model_available(model: str) -> bool:
    """Check that LiteLLM recognizes the model/provider syntax.

    LiteLLM's ``get_model_info`` is a pricing metadata lookup, not an
    availability check. Explicit ``api_base`` models are intentionally absent
    from that map, so startup must validate provider resolution instead.
    """

    import litellm

    try:
        resolved_model, provider, *_ = litellm.get_llm_provider(model)
    except Exception:
        return False
    return bool(resolved_model and provider)
