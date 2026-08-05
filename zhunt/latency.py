"""In-process latency observations used by the phase 2.5c selector."""

from __future__ import annotations

from collections.abc import Mapping
from threading import RLock


class LatencyTracker:
    """Store an exponentially weighted moving average of model TTFT."""

    def __init__(self, *, smoothing: float = 0.25) -> None:
        if not 0 < smoothing <= 1:
            raise ValueError("smoothing must be in (0, 1]")
        self.smoothing = smoothing
        self._ttft_ms: dict[str, float] = {}
        self._lock = RLock()

    def observe(self, model: str, ttft_ms: float) -> None:
        if ttft_ms < 0:
            raise ValueError("ttft_ms cannot be negative")
        with self._lock:
            previous = self._ttft_ms.get(model)
            if previous is None:
                self._ttft_ms[model] = ttft_ms
            else:
                self._ttft_ms[model] = (
                    self.smoothing * ttft_ms
                    + (1 - self.smoothing) * previous
                )

    def snapshot(self) -> Mapping[str, float]:
        with self._lock:
            return dict(self._ttft_ms)

