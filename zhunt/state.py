# Parts adapted from FreeLLMAPI (MIT, (c) 2026 Tashfeen Ahmed); see THIRD_PARTY_NOTICES.md
"""SQLite persistence for quota usage, cooldowns, learned limits and premium spend.

The quota tracker keeps working state in memory and writes through to this
store, so a restart neither forgets today's usage (and overruns a daily cap) nor
lifts a cooldown early. Any database error degrades to memory-only operation
with a single warning: losing persistence must never take the daemon down.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path
from threading import RLock
from typing import Any

logger = logging.getLogger(__name__)

STATE_DB_ENV = "ZHUNT_STATE_DB"
# Usage and expired cooldowns older than this are pruned.
RETENTION_SECONDS = 2 * 86_400.0
_PRUNE_EVERY_SECONDS = 3_600.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY,
    model TEXT NOT NULL,
    key TEXT NOT NULL,
    ts REAL NOT NULL,
    tokens INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS usage_ts ON usage (ts);
CREATE TABLE IF NOT EXISTS cooldowns (
    model TEXT NOT NULL,
    key TEXT NOT NULL,
    until REAL NOT NULL,
    level INTEGER NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (model, key)
);
CREATE TABLE IF NOT EXISTS spend (
    month TEXT NOT NULL,
    model TEXT NOT NULL,
    usd REAL NOT NULL,
    PRIMARY KEY (month, model)
);
CREATE TABLE IF NOT EXISTS learned_limits (
    model TEXT NOT NULL,
    axis TEXT NOT NULL,
    value INTEGER NOT NULL,
    PRIMARY KEY (model, axis)
);
"""


def default_state_path() -> Path:
    override = os.environ.get(STATE_DB_ENV)
    return Path(override).expanduser() if override else Path.home() / ".zhunt" / "state.db"


class StateStore:
    """Thin write-through store. Every method is a no-op once disabled."""

    def __init__(self, path: Path) -> None:
        path = Path(path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = sqlite3.connect(
            path, check_same_thread=False, isolation_level=None
        )
        self._conn.executescript(_SCHEMA)
        self._lock = RLock()
        self._last_prune = 0.0

    @classmethod
    def open(cls, path: Path | None = None) -> StateStore | None:
        """The store at ``path``, or ``None`` (with one warning) if it cannot open."""

        target = path or default_state_path()
        try:
            return cls(target)
        except (OSError, sqlite3.Error) as error:
            logger.warning(
                "zhunt: cannot open quota state at %s (%s); usage and cooldowns "
                "will not survive a restart",
                target,
                error,
            )
            return None

    @property
    def enabled(self) -> bool:
        return self._conn is not None

    def add_request(self, model: str, key: str, ts: float, tokens: int) -> int | None:
        cursor = self._run(
            "INSERT INTO usage (model, key, ts, tokens) VALUES (?, ?, ?, ?)",
            (model, key, ts, tokens),
        )
        if cursor is None:
            return None
        if ts - self._last_prune >= _PRUNE_EVERY_SECONDS or ts < self._last_prune:
            self.prune(ts)
        return cursor.lastrowid

    def set_tokens(self, row_id: int, tokens: int) -> None:
        self._run("UPDATE usage SET tokens = ? WHERE id = ?", (tokens, row_id))

    def usage_since(self, since: float) -> list[tuple[str, str, int, float, int]]:
        """``(model, key, row id, timestamp, tokens)`` rows newer than ``since``."""

        cursor = self._run(
            "SELECT model, key, id, ts, tokens FROM usage WHERE ts > ? ORDER BY ts",
            (since,),
        )
        return cursor.fetchall() if cursor is not None else []

    def save_cooldown(
        self, model: str, key: str, until: float, level: int, source: str
    ) -> None:
        self._run(
            "INSERT INTO cooldowns (model, key, until, level, source) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT (model, key) DO UPDATE SET "
            "until = excluded.until, level = excluded.level, source = excluded.source",
            (model, key, until, level, source),
        )

    def cooldowns(self) -> list[tuple[str, str, float, int, str]]:
        cursor = self._run("SELECT model, key, until, level, source FROM cooldowns", ())
        return cursor.fetchall() if cursor is not None else []

    def save_limit(self, model: str, axis: str, value: int) -> None:
        self._run(
            "INSERT INTO learned_limits (model, axis, value) VALUES (?, ?, ?) "
            "ON CONFLICT (model, axis) DO UPDATE SET value = excluded.value",
            (model, axis, value),
        )

    def learned_limits(self) -> list[tuple[str, str, int]]:
        cursor = self._run("SELECT model, axis, value FROM learned_limits", ())
        return cursor.fetchall() if cursor is not None else []

    def add_spend(self, month: str, model: str, usd: float) -> None:
        self._run(
            "INSERT INTO spend (month, model, usd) VALUES (?, ?, ?) "
            "ON CONFLICT (month, model) DO UPDATE SET usd = usd + excluded.usd",
            (month, model, usd),
        )

    def spend(self, month: str) -> float:
        """Premium spend recorded for ``month`` (``YYYY-MM``, UTC). Never pruned."""

        cursor = self._run("SELECT COALESCE(SUM(usd), 0) FROM spend WHERE month = ?", (month,))
        return float(cursor.fetchone()[0]) if cursor is not None else 0.0

    def prune(self, now: float) -> None:
        self._last_prune = now
        cutoff = now - RETENTION_SECONDS
        self._run("DELETE FROM usage WHERE ts <= ?", (cutoff,))
        # A cleared streak (level 0) whose bench is long over carries nothing.
        self._run("DELETE FROM cooldowns WHERE until <= ?", (cutoff,))

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _run(self, sql: str, params: tuple[Any, ...]) -> sqlite3.Cursor | None:
        with self._lock:
            if self._conn is None:
                return None
            try:
                return self._conn.execute(sql, params)
            except sqlite3.Error as error:
                logger.warning(
                    "zhunt: quota state database failed (%s); continuing in memory only",
                    error,
                )
                self._conn = None
                return None
