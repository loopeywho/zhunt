# Parts adapted from FreeLLMAPI (MIT, (c) 2026 Tashfeen Ahmed); see THIRD_PARTY_NOTICES.md
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
# Headroom has no say until only 20% of the tightest window is left, then the
# score ramps linearly down to 10% of itself at zero.
_HEADROOM_RAMP_START = 0.2
_HEADROOM_FLOOR = 0.1
# Live failure penalty: +3 per 429, +1 per other failure, capped at 10 and
# decaying one point every 2 minutes. At the cap a model keeps 40% of its score.
_PENALTY_RATE_LIMIT = 3.0
_PENALTY_FAILURE = 1.0
_PENALTY_MAX = 10.0
_PENALTY_DECAY_SECONDS = 120.0
_PENALTY_MAX_DAMP = 0.6


def headroom_factor(remaining: float) -> float:
    """Score multiplier for the remaining fraction (0-1) of the tightest window."""

    remaining = min(max(remaining, 0.0), 1.0)
    if remaining >= _HEADROOM_RAMP_START:
        return 1.0
    return _HEADROOM_FLOOR + (1 - _HEADROOM_FLOOR) * remaining / _HEADROOM_RAMP_START


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

        if self._rng is None:
            return self.mean(model_id)
        with self._lock:
            successes, failures, _ = self._decayed(model_id)
            return self._rng.betavariate(successes + 1, failures + 1)

    def mean(self, model_id: str) -> float:
        """Posterior mean: the stable figure to report, whatever ``score`` samples."""

        with self._lock:
            successes, failures, _ = self._decayed(model_id)
        return (successes + 1) / (successes + failures + 2)

    def _decayed(self, model_id: str) -> tuple[float, float, float]:
        successes, failures, stamp = self._counts.get(model_id, (0.0, 0.0, 0.0))
        if stamp:
            elapsed = max(self._clock() - stamp, 0.0)  # a clock stepped back must not grow counts
            factor = 0.5 ** (elapsed / _HALF_LIFE_SECONDS)
            successes, failures = successes * factor, failures * factor
        return successes, failures, stamp


class Penalties:
    """Short-lived demotion of models that are failing right now.

    Unlike ``Reliability`` (days of history), this reacts within minutes and
    fades within minutes, so a burst of 429s sinks a model only briefly.
    """

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._entries: dict[str, tuple[float, float]] = {}  # penalty, last hit
        self._lock = RLock()

    def record_failure(self, model_id: str, *, rate_limited: bool) -> None:
        weight = _PENALTY_RATE_LIMIT if rate_limited else _PENALTY_FAILURE
        with self._lock:
            now = self._clock()
            current = self._decayed(model_id, now)
            self._entries[model_id] = (min(current + weight, _PENALTY_MAX), now)

    def record_success(self, model_id: str) -> None:
        with self._lock:
            entry = self._entries.get(model_id)
            if entry is None:
                return
            penalty, stamp = entry
            if penalty <= 1:
                del self._entries[model_id]
            else:
                self._entries[model_id] = (penalty - 1, stamp)

    def penalty(self, model_id: str) -> float:
        with self._lock:
            return self._decayed(model_id, self._clock())

    def factor(self, model_id: str) -> float:
        """Score multiplier: 1.0 with no penalty, 0.4 at the cap."""

        return 1 - (min(self.penalty(model_id), _PENALTY_MAX) / _PENALTY_MAX) * _PENALTY_MAX_DAMP

    def _decayed(self, model_id: str, now: float) -> float:
        penalty, stamp = self._entries.get(model_id, (0.0, now))
        steps = max(now - stamp, 0.0) // _PENALTY_DECAY_SECONDS
        return max(penalty - steps, 0.0)


@dataclass(frozen=True)
class SelectionPolicy:
    allow_paid: bool = False
    allow_training: bool = False
    # what status reports for a paid model while allow_paid is off
    paid_block_reason: str = "paid"


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
        penalties: Penalties | None = None,
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
        self.penalties = penalties or Penalties()

    @property
    def policy(self) -> SelectionPolicy:
        return self._policy

    def set_policy(self, policy: SelectionPolicy) -> None:
        self._policy = policy

    def set_keys(self, keys: Mapping[str, Sequence[str]]) -> None:
        self._keys = {platform: tuple(ids) for platform, ids in keys.items()}

    def key_ids(self, platform: str) -> tuple[str, ...]:
        return self._keys.get(platform, ())

    def blocked_reason(self, model: CatalogModel) -> str | None:
        """Why policy or a missing key excludes the model outright, else ``None``."""

        if not self._keys.get(model.platform):
            return "no key"
        if model.may_train_on_prompts and not self._policy.allow_training:
            return "may train on prompts"
        if not model.free and not self._policy.allow_paid:
            return self._policy.paid_block_reason
        return None

    def ranked(
        self,
        tier: Tier,
        *,
        tokens: int = 1,
        requires_tools: bool = False,
        exclude: frozenset[str] = frozenset(),
    ) -> list[Candidate]:
        """Allowed, un-benched candidates for one tier: free first, then paid."""

        found: list[Candidate] = []
        for model in self._catalog.models_for_tier(tier):
            if model.id in exclude or not self._allowed(model, tokens, requires_tools):
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
        exclude: frozenset[str] = frozenset(),
    ) -> tuple[Candidate, Lease]:
        """Reserve the best candidate with quota left, dropping a tier if needed.

        ``exclude`` is a preference (e.g. a model that just truncated): it is honoured
        when another model qualifies and ignored when nothing else does.
        """

        if exclude:
            try:
                return self._acquire(tier, tokens, requires_tools, exclude)
            except NoCandidateError:
                pass
        return self._acquire(tier, tokens, requires_tools, frozenset())

    def _acquire(
        self,
        tier: Tier,
        tokens: int,
        requires_tools: bool,
        exclude: frozenset[str],
    ) -> tuple[Candidate, Lease]:

        lower_first = sorted(
            (t for t in Tier if t.rank <= tier.rank), key=lambda t: -t.rank
        )
        for attempt in lower_first:
            for candidate in self.ranked(
                attempt, tokens=tokens, requires_tools=requires_tools, exclude=exclude
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
        return base * headroom_factor(headroom) * self.penalties.factor(model.id)
