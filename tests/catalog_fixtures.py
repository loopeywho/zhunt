"""Shared fake catalog and clock for the free-tier engine tests.

Every provider and model here is invented; no real limits or prices are implied.
"""

from __future__ import annotations

from typing import Any


class FakeClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def model(model_id: str, tiers: list[str], **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": model_id,
        "tiers": tiers,
        "intelligence": 50,
        "speed": 50,
        "context_window": 32_000,
        "supports_tools": False,
        "may_train_on_prompts": False,
    }
    data.update(overrides)
    return data


def provider(models: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": "Fake",
        "base_url": "https://fake.invalid/v1",
        "key_env": "FAKE_API_KEY",
        "verified_on": "2026-10-10",
        "models": models,
    }
    data.update(overrides)
    return data


def catalog_data() -> dict[str, Any]:
    return {
        "providers": {
            "alpha": provider(
                [
                    model(
                        "chat-fast", ["chat"], limits={"rpm": 5},
                        intelligence=30, speed=95,
                    ),
                    model(
                        "coder", ["chat", "coding"], limits={"rpd": 3},
                        intelligence=70, speed=60, supports_tools=True,
                    ),
                ]
            ),
            "beta": provider(
                [
                    model(
                        "trainer", ["chat", "coding"],
                        intelligence=90, speed=90, supports_tools=True,
                        may_train_on_prompts=True,
                    ),
                ]
            ),
            "gamma": provider(
                [
                    model(
                        "paid-chat", ["chat", "coding"], **{"in": 1.0, "out": 2.0},
                        intelligence=80, speed=80, supports_tools=True,
                    ),
                ]
            ),
            "delta": provider(
                [
                    model(
                        "thinker", ["reasoning"], limits={"rpd": 2},
                        intelligence=85, speed=40,
                    ),
                ]
            ),
        }
    }
