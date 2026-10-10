"""Free-first model selection over the catalog, quota tracker and reliability."""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from threading import RLock

from zhunt.brain import Tier
from zhunt.catalog import Catalog, CatalogModel
from zhunt.quota import Lease, QuotaTracker, Slot

_HALF_LIFE_SECONDS = 2 * 86_400.0
_HEADROOM_FLOOR = 0.05


class NoCandidateError(RuntimeError):
    """Raised when no allowed model has quota left in the requested tier or below."""


class Reliability:
    """Decayed success/failure counts per model, scored as a Beta posterior."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        rng: random.Random | None = None,
    ) -> None:
        self._clock = clock
        self._rng = rng
        self._counts: dict[str, tuple[float, float, float]] = {}
        self._lock = RLock()

    def record(self, model_id: str, *, success: bool) -> None:
        with self._lock:
            successes, failures, _ = self._decayed(model_id)
            if success:
                successes += 1
            else:
                failures += 1
            self._counts[model_id] = (successes, failures, self._clock())

    def score(self, model_id: str) -> float:
        """Posterior mean, or one Thompson sample when an ``rng`` was supplied."""

        with self._lock:
            successes, failures, _ = self._decayed(model_id)
        if self._rng is not None:
            return self._rng.betavariate(successes + 1, failures + 1)
        return (successes + 1) / (successes + failures + 2)

    def _decayed(self, model_id: str) -> tuple[float, float, float]:
        successes, failures, stamp = self._counts.get(model_id, (0.0, 0.0, 0.0))
        if stamp:
            factor = 0.5 ** ((self._clock() - stamp) / _HALF_LIFE_SECONDS)
            successes, failures = successes * factor, failures * factor
        return successes, failures, stamp


@dataclass(frozen=True)
class SelectionPolicy:
    allow_paid: bool = False
    allow_training: bool = False


@dataclass(frozen=True)
class Candidate:
    model: CatalogModel
    key_id: str
    tier: Tier
    degraded: bool
    score: float

    @property
    def slot(self) -> Slot:
        return (self.model.id, self.key_id)


class Selector:
    def __init__(
        self,
        catalog: Catalog,
        quota: QuotaTracker,
        reliability: Reliability,
        keys: Mapping[str, Sequence[str]],
        *,
        policy: SelectionPolicy = SelectionPolicy(),
        weights: tuple[float, float, float] = (0.5, 0.25, 0.25),
    ) -> None:
        if len(weights) != 3 or any(w < 0 for w in weights) or sum(weights) <= 0:
            raise ValueError("weights must be three non-negative numbers")
        self._catalog = catalog
        self._quota = quota
        self._reliability = reliability
        self._keys = {platform: tuple(ids) for platform, ids in keys.items()}
        self._policy = policy
        total = sum(weights)
        self._weights = tuple(w / total for w in weights)

    def ranked(
        self,
        tier: Tier,
        *,
        tokens: int = 1,
        requires_tools: bool = False,
    ) -> list[Candidate]:
        """Allowed, un-benched candidates for one tier: free first, then paid."""

        found: list[Candidate] = []
        for model in self._catalog.models_for_tier(tier):
            if not self._allowed(model, tokens, requires_tools):
                continue
            for key_id in self._keys.get(model.platform, ()):
                slot = (model.id, key_id)
                if self._quota.cooldown_remaining(slot) > 0:
                    continue
                found.append(
                    Candidate(
                        model=model,
                        key_id=key_id,
                        tier=tier,
                        degraded=False,
                        score=self._score(model, slot),
                    )
                )
        free = sorted((c for c in found if c.model.free), key=lambda c: -c.score)
        paid = sorted(
            (c for c in found if not c.model.free),
            key=lambda c: (c.model.input_cost + c.model.output_cost, -c.score),
        )
        return free + paid

    def acquire(
        self,
        tier: Tier,
        *,
        tokens: int = 1,
        requires_tools: bool = False,
    ) -> tuple[Candidate, Lease]:
        """Reserve the best candidate with quota left, dropping a tier if needed."""

        lower_first = sorted(
            (t for t in Tier if t.rank <= tier.rank), key=lambda t: -t.rank
        )
        for attempt in lower_first:
            for candidate in self.ranked(
                attempt, tokens=tokens, requires_tools=requires_tools
            ):
                lease = self._quota.try_acquire(
                    candidate.slot, candidate.model.limits, tokens=tokens
                )
                if lease is not None:
                    return (
                        Candidate(
                            model=candidate.model,
                            key_id=candidate.key_id,
                            tier=attempt,
                            degraded=attempt != tier,
                            score=candidate.score,
                        ),
                        lease,
                    )
        raise NoCandidateError(
            f"no allowed model is available for tier {tier.value!r} (missing key, "
            "over quota, benched after errors, or blocked by privacy/paid settings)"
        )

    def _allowed(self, model: CatalogModel, tokens: int, requires_tools: bool) -> bool:
        if not model.free and not self._policy.allow_paid:
            return False
        if model.may_train_on_prompts and not self._policy.allow_training:
            return False
        if requires_tools and not model.supports_tools:
            return False
        return model.context_window >= tokens

    def _score(self, model: CatalogModel, slot: Slot) -> float:
        w_reliability, w_speed, w_intelligence = self._weights
        base = (
            w_reliability * self._reliability.score(model.id)
            + w_speed * model.speed / 100
            + w_intelligence * model.intelligence / 100
        )
        headroom = self._quota.headroom(slot, model.limits)
        return base * max(headroom, _HEADROOM_FLOOR)
