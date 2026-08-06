"""Offline routing and projected-cost benchmark."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from zhunt.registry import ModelRegistry
from zhunt.router import RoutingCoordinator, RoutingRequest


@dataclass(frozen=True)
class BenchmarkTurn:
    """A sanitized, provider-free request representative."""

    name: str
    user_text: str
    input_tokens: int
    output_tokens: int
    has_tool_calls: bool = False
    system_prompt: str = ""


@dataclass(frozen=True)
class BenchmarkCase:
    """A sequence of turns sharing one session."""

    name: str
    turns: tuple[BenchmarkTurn, ...]


DEFAULT_CASES: tuple[BenchmarkCase, ...] = (
    BenchmarkCase(
        name="chat",
        turns=(
            BenchmarkTurn(
                name="simple-answer",
                user_text="What does this command do?",
                input_tokens=120,
                output_tokens=160,
            ),
        ),
    ),
    BenchmarkCase(
        name="coding-agent",
        turns=(
            BenchmarkTurn(
                name="edit-request",
                user_text="Fix this function:\n```python\nreturn value\n```",
                input_tokens=900,
                output_tokens=500,
                has_tool_calls=True,
            ),
            BenchmarkTurn(
                name="coding-follow-up",
                user_text="The test still fails on an empty input.",
                input_tokens=700,
                output_tokens=350,
            ),
        ),
    ),
    BenchmarkCase(
        name="long-context",
        turns=(
            BenchmarkTurn(
                name="large-repository-review",
                user_text="Summarize the relevant changes and identify risks.",
                input_tokens=20_000,
                output_tokens=1_000,
            ),
        ),
    ),
    BenchmarkCase(
        name="reasoning",
        turns=(
            BenchmarkTurn(
                name="proof-request",
                user_text="Prove that the proposed invariant holds for every case.",
                input_tokens=1_200,
                output_tokens=900,
            ),
        ),
    ),
)


def run_benchmark(
    registry: ModelRegistry,
    *,
    cases: tuple[BenchmarkCase, ...] = DEFAULT_CASES,
) -> dict[str, Any]:
    """Run deterministic routing and cost projections without provider calls.

    The baseline is the most expensive configured registry model for each turn.
    This is a counterfactual comparison, not a claim about a user's current
    provider configuration. Provider quality, real latency, and invoice cost
    are intentionally not measured here.
    """

    coordinator = RoutingCoordinator(registry=registry)
    turns: list[dict[str, Any]] = []
    actual_total = Decimal("0")
    baseline_total = Decimal("0")

    for case in cases:
        if not case.turns:
            continue
        first_user_message = case.turns[0].user_text
        for turn in case.turns:
            request = RoutingRequest(
                model_alias="zhunt-auto",
                user_text=turn.user_text,
                first_user_message=first_user_message,
                system_prompt=turn.system_prompt,
                session_id=f"benchmark:{case.name}",
                estimated_input_tokens=turn.input_tokens,
                estimated_output_tokens=turn.output_tokens,
                has_tool_calls=turn.has_tool_calls,
            )
            decision = coordinator.route(request)
            actual = registry.projected_cost(
                decision.model,
                input_tokens=turn.input_tokens,
                output_tokens=turn.output_tokens,
            )
            baseline = registry.top_model_cost_all(
                input_tokens=turn.input_tokens,
                output_tokens=turn.output_tokens,
            )
            actual_total += actual
            baseline_total += baseline
            turns.append(
                {
                    "case": case.name,
                    "turn": turn.name,
                    "tier": decision.tier.value,
                    "model": decision.model,
                    "reused_session_route": decision.reused_session_route,
                    "input_tokens": turn.input_tokens,
                    "output_tokens": turn.output_tokens,
                    "projected_cost": float(actual),
                    "baseline_cost": float(baseline),
                    "projected_savings": float(baseline - actual),
                }
            )

    savings = baseline_total - actual_total
    savings_percent = (
        (savings / baseline_total * Decimal(100))
        if baseline_total
        else Decimal(0)
    )
    return {
        "benchmark": "offline-routing-v1",
        "requests": len(turns),
        "actual_projected_cost": float(actual_total),
        "baseline_projected_cost": float(baseline_total),
        "projected_savings": float(savings),
        "projected_savings_percent": float(savings_percent),
        "quality_measured": False,
        "latency_measured": False,
        "provider_calls": False,
        "turns": turns,
    }
