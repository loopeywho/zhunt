# Parts adapted from FreeLLMAPI (MIT, (c) 2026 Tashfeen Ahmed); see THIRD_PARTY_NOTICES.md
"""Per-key quota windows and failure cooldowns."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from enum import Enum
from threading import RLock
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from zhunt.catalog import Limits
from zhunt.state import StateStore

# One (model id, key id) pair is the unit that quotas and cooldowns apply to.
Slot = tuple[str, str]

_MINUTE = 60.0
_HOUR = 3_600.0
_DAY = 86_400
_COOLDOWN_STEPS = (120.0, 600.0, 3_600.0, 86_400.0)
# A per-minute 429 clears within about one window and never climbs the ladder.
_TRANSIENT_COOLDOWN = 90.0
# With no published daily cap, "daily quota spent" is only a guess from repeated
# 429s within the hour, so that guess never benches past the ladder's 10 minutes.
_GUESSED_EXHAUSTION_HITS = 2
_GUESSED_EXHAUSTION_CAP = 600.0
_MAX_COOLDOWN = 86_400.0
# Three failures of one model inside 15 minutes bench it on every key for 10.
_MODEL_FAILURE_WINDOW = 900.0
_MODEL_FAILURE_THRESHOLD = 3
_MODEL_FAILURE_COOLDOWN = 600.0


class FailureKind(Enum):
    # A 429. Treated as per-minute unless the daily counter is at its cap.
    RATE_LIMIT = "rate-limit"
    # A 429 whose body says the day's allowance is spent: bench until reset.
    DAILY_QUOTA = "daily-quota"
    SERVER = "server"
    # 401: a bad key, re-tried after a few minutes in case it was fixed.
    AUTH = "auth"
    # 402: out of credit, which will not come back within the day.
    CREDIT = "credit"
    # 403: this model is not on the key's tier or plan.
    FORBIDDEN = "forbidden"
    # 404/410: the provider no longer serves this model.
    NOT_FOUND = "not-found"


# Fixed benches (seconds, provenance) for failures the ladder cannot fix sooner.
_FIXED_COOLDOWN = {
    FailureKind.AUTH: (300.0, "auth"),
    FailureKind.CREDIT: (86_400.0, "credit"),
    FailureKind.FORBIDDEN: (86_400.0, "tier"),
    FailureKind.NOT_FOUND: (86_400.0, "not-found"),
}


@dataclass
class _State:
    minute: deque[list[float]] = field(default_factory=deque)  # [timestamp, tokens]
    day: int = -1
    day_requests: int = 0
    day_tokens: int = 0
    failures: int = 0
    cooldown_until: float = 0.0
    cooldown_source: str = ""
    rate_limit_hits: list[float] = field(default_factory=list)


@dataclass
class Lease:
    """A reserved request; ``settle`` replaces the token estimate with the actual."""

    slot: Slot
    day: int
    entry: list[float]
    row_id: int | None = None


class QuotaTracker:
    """In-memory windows, optionally written through to a ``StateStore`` so
    daily usage and cooldowns survive a restart."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        store: StateStore | None = None,
    ) -> None:
        self._clock = clock
        self._states: dict[Slot, _State] = {}
        self._lock = RLock()
        self._model_failures: dict[str, list[float]] = {}
        self._store = store
        if store is not None:
            self._load(store)

    def try_acquire(
        self,
        slot: Slot,
        limits: Limits,
        *,
        tokens: int = 0,
    ) -> Lease | None:
        """Atomically check every window and, if all pass, record the request."""

        if tokens < 0:
            raise ValueError("tokens cannot be negative")
        with self._lock:
            now = self._clock()
            state = self._state(slot, now)
            if state.cooldown_until > now:
                return None
            minute_tokens = sum(entry[1] for entry in state.minute)
            if limits.rpm is not None and len(state.minute) + 1 > limits.rpm:
                return None
            if limits.tpm is not None and minute_tokens + tokens > limits.tpm:
                return None
            if limits.rpd is not None and state.day_requests + 1 > limits.rpd:
                return None
            if limits.tpd is not None and state.day_tokens + tokens > limits.tpd:
                return None
            entry = [now, float(tokens)]
            state.minute.append(entry)
            state.day_requests += 1
            state.day_tokens += tokens
            row_id = (
                self._store.add_request(slot[0], slot[1], now, tokens)
                if self._store is not None
                else None
            )
            return Lease(slot=slot, day=state.day, entry=entry, row_id=row_id)

    def settle(self, lease: Lease, actual_tokens: int) -> None:
        if actual_tokens < 0:
            raise ValueError("tokens cannot be negative")
        with self._lock:
            delta = actual_tokens - int(lease.entry[1])
            lease.entry[1] = float(actual_tokens)
            state = self._states.get(lease.slot)
            if state is not None and state.day == lease.day:
                state.day_tokens = max(state.day_tokens + delta, 0)
            if self._store is not None and lease.row_id is not None:
                self._store.set_tokens(lease.row_id, actual_tokens)

    def headroom(self, slot: Slot, limits: Limits) -> float:
        """Remaining fraction (0-1) of the tightest window; 1.0 when uncapped."""

        with self._lock:
            now = self._clock()
            state = self._state(slot, now)
            minute_tokens = sum(entry[1] for entry in state.minute)
            used = (
                (len(state.minute), limits.rpm),
                (minute_tokens, limits.tpm),
                (state.day_requests, limits.rpd),
                (state.day_tokens, limits.tpd),
            )
            remaining = [
                max(1.0 - amount / cap, 0.0) for amount, cap in used if cap is not None
            ]
            return min(remaining, default=1.0)

    def record_failure(
        self,
        slot: Slot,
        kind: FailureKind,
        *,
        limits: Limits = Limits(),
        retry_after: float | None = None,
        daily_reset_zone: str | None = None,
    ) -> float:
        """Bench the slot and return the cooldown in seconds.

        A 429 gets a fixed 90 s unless the daily quota is really spent: the
        daily counter is at its cap in ``limits`` or, with no daily cap known,
        429s keep coming within the hour (that guess is capped at 10 min). Only
        then, and on server errors, does the cooldown climb 2 min, 10 min, 1 h,
        24 h with each consecutive failure *episode*. A spent daily quota benches
        until the provider's next reset (see ``_daily_reset``). ``retry_after``
        is otherwise a floor, capped at a day.

        Failures reported while the slot is already benched (parallel requests
        that were in flight together) belong to the same episode: they never
        shorten the cooldown and never climb the ladder.
        """

        with self._lock:
            now = self._clock()
            state = self._state(slot, now)
            new_episode = now >= state.cooldown_until
            retry = min(max(retry_after or 0.0, 0.0), _MAX_COOLDOWN)
            if kind is FailureKind.DAILY_QUOTA:
                duration = _daily_reset(now, retry, daily_reset_zone)
                source = "authoritative"
            elif kind in _FIXED_COOLDOWN:
                duration, source = _FIXED_COOLDOWN[kind]
            elif kind is FailureKind.RATE_LIMIT:
                duration = self._rate_limit_cooldown(state, limits, now, new_episode)
                source = "heuristic"
            else:
                duration, source = _climb(state, new_episode), "heuristic"
            if retry > duration:
                duration, source = retry, "authoritative"
            if now + duration > state.cooldown_until:
                state.cooldown_until = now + duration
                state.cooldown_source = source
            self._persist_cooldown(slot, state)
            return state.cooldown_until - now

    def record_model_failure(self, model_id: str, slots: Sequence[Slot]) -> bool:
        """Count one failure of the model; on the third within 15 minutes bench
        it on every slot in ``slots`` for 10 minutes. True when it benched."""

        with self._lock:
            now = self._clock()
            window = [
                t for t in self._model_failures.get(model_id, ())
                if now - t < _MODEL_FAILURE_WINDOW
            ]
            window.append(now)
            if len(window) < _MODEL_FAILURE_THRESHOLD:
                self._model_failures[model_id] = window
                return False
            self._model_failures.pop(model_id, None)  # one bench per streak
            until = now + _MODEL_FAILURE_COOLDOWN
            for slot in slots:
                state = self._state(slot, now)
                if state.cooldown_until >= until:
                    continue  # never shorten a longer, better-informed bench
                state.cooldown_until = until
                state.cooldown_source = "model-streak"
                self._persist_cooldown(slot, state)
            return True

    def record_success(self, slot: Slot) -> None:
        """A request succeeded. While the slot is benched this is an earlier,
        still-in-flight request finishing, so it must not lift the cooldown."""

        with self._lock:
            now = self._clock()
            state = self._state(slot, now)
            # A served request is the best evidence the model is healthy again.
            self._model_failures.pop(slot[0], None)
            if state.cooldown_until <= now:
                state.rate_limit_hits.clear()
                if state.failures:
                    state.failures = 0
                    self._persist_cooldown(slot, state)

    def cooldown_remaining(self, slot: Slot) -> float:
        with self._lock:
            now = self._clock()
            return max(self._state(slot, now).cooldown_until - now, 0.0)

    def _rate_limit_cooldown(
        self, state: _State, limits: Limits, now: float, new_episode: bool
    ) -> float:
        spent = (limits.rpd is not None and state.day_requests >= limits.rpd) or (
            limits.tpd is not None and state.day_tokens >= limits.tpd
        )
        if spent:
            return _climb(state, new_episode)
        if limits.rpd is None and limits.tpd is None and new_episode:
            state.rate_limit_hits = [
                t for t in state.rate_limit_hits if t > now - _HOUR
            ] + [now]
            if len(state.rate_limit_hits) >= _GUESSED_EXHAUSTION_HITS:
                return min(_climb(state, new_episode), _GUESSED_EXHAUSTION_CAP)
        return _TRANSIENT_COOLDOWN

    def _persist_cooldown(self, slot: Slot, state: _State) -> None:
        if self._store is not None:
            self._store.save_cooldown(
                slot[0],
                slot[1],
                state.cooldown_until,
                state.failures,
                state.cooldown_source,
            )

    def _load(self, store: StateStore) -> None:
        """Rebuild today's counters, the live minute window and cooldowns."""

        now = self._clock()
        store.prune(now)
        today = int(now // _DAY)
        for model, key, _, ts, tokens in store.usage_since(today * _DAY - 1):
            if int(ts // _DAY) != today:
                continue
            state = self._state((model, key), now)
            state.day_requests += 1
            state.day_tokens += tokens
            if ts > now - _MINUTE:
                state.minute.append([ts, float(tokens)])
        for model, key, until, level, source in store.cooldowns():
            state = self._state((model, key), now)
            state.cooldown_until = until
            state.failures = level
            state.cooldown_source = source

    def _state(self, slot: Slot, now: float) -> _State:
        state = self._states.setdefault(slot, _State())
        while state.minute and state.minute[0][0] <= now - _MINUTE:
            state.minute.popleft()
        day = int(now // _DAY)
        if state.day != day:
            state.day = day
            state.day_requests = 0
            state.day_tokens = 0
        return state


def _climb(state: _State, new_episode: bool) -> float:
    """The next escalation-ladder step; only a new episode moves up a rung."""

    if new_episode:
        state.failures += 1
    step = max(state.failures - 1, 0)
    return _COOLDOWN_STEPS[min(step, len(_COOLDOWN_STEPS) - 1)]


def _daily_reset(now: float, retry: float, zone: str | None) -> float:
    """Seconds until a spent daily allowance comes back.

    With no fixed reset zone the provider's own ``retry-after`` wins (a rolling
    daily window can reopen well before midnight), else UTC midnight. A fixed
    local reset (Google: midnight Pacific) cannot come early, so there a shorter
    ``retry-after`` describes some other, per-minute limit and only extends.
    """

    if zone is None:
        return retry or seconds_until_midnight(now, "UTC")
    return max(seconds_until_midnight(now, zone), retry)


def seconds_until_midnight(now: float, zone: str) -> float:
    """Seconds from ``now`` to the next local midnight in ``zone`` (DST-aware),
    at least a minute. Falls back to UTC when the zone database is missing."""

    tz: tzinfo
    try:
        tz = ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError):
        tz = timezone.utc
    local = datetime.fromtimestamp(now, tz)
    midnight = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), tz)
    return max(midnight.timestamp() - now, 60.0)
