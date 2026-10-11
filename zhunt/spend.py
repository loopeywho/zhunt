"""Monthly premium spend, so Pro's paid fallback stops at the user's cap.

Spend is the catalog price of each paid answer (tokens x per-token cost), which is
what the provider will bill give or take rounding. It is kept per UTC month in the
state database; without one it lasts until the daemon restarts.
"""

from __future__ import annotations

import time
from decimal import Decimal
from threading import Lock

from zhunt.state import StateStore

MONTHLY_CAP_ENV = "ZHUNT_MONTHLY_CAP"
DEFAULT_MONTHLY_CAP = 5.0


def month_of(ts: float) -> str:
    return time.strftime("%Y-%m", time.gmtime(ts))


def cap_from_env(env: dict[str, str] | None = None) -> float:
    import os

    raw = (env if env is not None else os.environ).get(MONTHLY_CAP_ENV, "").strip()
    try:
        value = float(raw) if raw else DEFAULT_MONTHLY_CAP
    except ValueError:
        return DEFAULT_MONTHLY_CAP
    return value if value >= 0 else DEFAULT_MONTHLY_CAP


def cost_of(input_tokens: int, output_tokens: int, input_cost: Decimal, output_cost: Decimal) -> float:
    return float(input_cost * input_tokens + output_cost * output_tokens)


class SpendLedger:
    def __init__(self, store: StateStore | None = None) -> None:
        self._store = store
        self._memory: dict[str, float] = {}
        self._lock = Lock()

    def add(self, model: str, usd: float, now: float | None = None) -> None:
        if usd <= 0:
            return
        month = month_of(time.time() if now is None else now)
        with self._lock:
            self._memory[month] = self._memory.get(month, 0.0) + usd
        if self._store is not None:
            self._store.add_spend(month, model, usd)

    def spent(self, now: float | None = None) -> float:
        month = month_of(time.time() if now is None else now)
        if self._store is not None and self._store.enabled:
            return self._store.spend(month)
        with self._lock:
            return self._memory.get(month, 0.0)
