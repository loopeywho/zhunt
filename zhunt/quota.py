"""Per-key quota windows and failure cooldowns."""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from threading import RLock

from zhunt.catalog import Limits

# One (model id, key id) pair is the unit that quotas and cooldowns apply to.
Slot = tuple[str, str]

_MINUTE = 60.0
_DAY = 86_400
_COOLDOWN_STEPS = (120.0, 600.0, 3_600.0, 86_400.0)


class FailureKind(Enum):
    RATE_LIMIT = "rate-limit"
    SERVER = "server"
    # A rejected key or exhausted credit will not fix itself in two minutes.
    AUTH = "auth"
    CREDIT = "credit"


_STEP_OFFSET = {FailureKind.AUTH: 2, FailureKind.CREDIT: 2}


@dataclass
class _State:
    minute: deque[list[float]] = field(default_factory=deque)  # [timestamp, tokens]
    day: int = -1
    day_requests: int = 0
    day_tokens: int = 0
    failures: int = 0
    cooldown_until: float = 0.0


@dataclass
class Lease:
    """A reserved request; ``settle`` replaces the token estimate with the actual."""

    slot: Slot
    day: int
    entry: list[float]


class QuotaTracker:
    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._states: dict[Slot, _State] = {}
        self._lock = RLock()

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
            return Lease(slot=slot, day=state.day, entry=entry)

    def settle(self, lease: Lease, actual_tokens: int) -> None:
        if actual_tokens < 0:
            raise ValueError("tokens cannot be negative")
        with self._lock:
            delta = actual_tokens - int(lease.entry[1])
            lease.entry[1] = float(actual_tokens)
            state = self._states.get(lease.slot)
            if state is not None and state.day == lease.day:
                state.day_tokens = max(state.day_tokens + delta, 0)

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
        retry_after: float | None = None,
    ) -> float:
        """Bench the slot and return the cooldown in seconds.

        A provider-supplied ``retry-after`` is authoritative; otherwise the
        cooldown escalates 2 min, 10 min, 1 h, 24 h with each consecutive failure
        *episode*. Failures reported while the slot is already benched (parallel
        requests that were in flight together) belong to the same episode: they
        never shorten the cooldown and never climb the ladder.
        """

        with self._lock:
            now = self._clock()
            state = self._state(slot, now)
            new_episode = now >= state.cooldown_until
            if new_episode:
                state.failures += 1
            if retry_after is not None and retry_after > 0:
                duration = float(retry_after)
            else:
                step = max(state.failures - 1, 0) + _STEP_OFFSET.get(kind, 0)
                duration = _COOLDOWN_STEPS[min(step, len(_COOLDOWN_STEPS) - 1)]
            state.cooldown_until = max(state.cooldown_until, now + duration)
            return state.cooldown_until - now

    def record_success(self, slot: Slot) -> None:
        """A request succeeded. While the slot is benched this is an earlier,
        still-in-flight request finishing, so it must not lift the cooldown."""

        with self._lock:
            now = self._clock()
            state = self._state(slot, now)
            if state.cooldown_until <= now:
                state.failures = 0

    def cooldown_remaining(self, slot: Slot) -> float:
        with self._lock:
            now = self._clock()
            return max(self._state(slot, now).cooldown_until - now, 0.0)

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
