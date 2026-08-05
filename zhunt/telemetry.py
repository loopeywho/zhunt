"""Local JSONL request telemetry and spend summaries."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

from zhunt.registry import ModelRegistry
from zhunt.router import RoutingDecision


class TelemetryLogger:
    """Append request events to a local-only JSONL file."""

    def __init__(self, path: Path, registry: ModelRegistry) -> None:
        self.path = path.expanduser()
        self.registry = registry
        self._lock = RLock()

    def record_request(
        self,
        *,
        app: str,
        decision: RoutingDecision,
        input_tokens: int,
        output_tokens: int,
        success: bool,
        failure: str | None = None,
    ) -> dict[str, Any]:
        actual_cost = self.registry.projected_cost(
            decision.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        counterfactual_cost = self.registry.top_model_cost(
            decision.tier,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        event: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": "request",
            "app": app,
            "session": decision.session_key,
            "tier": decision.tier.value,
            "model": decision.model,
            "requested_alias": decision.requested_alias,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "actual_cost": float(actual_cost),
            "counterfactual_top_model_cost": float(counterfactual_cost),
            "escalations": decision.escalation_count,
            "success": success,
        }
        if failure is not None:
            event["failure"] = failure
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as telemetry_file:
                telemetry_file.write(json.dumps(event, separators=(",", ":")) + "\n")
        return event


def summarize_telemetry(
    path: Path,
    *,
    day: date | None = None,
) -> dict[str, Any]:
    """Aggregate today's (or the requested day's) local request events."""

    target_day = day or datetime.now(timezone.utc).date()
    by_app: dict[str, dict[str, float | int]] = defaultdict(
        lambda: {
            "requests": 0,
            "actual_spend": 0.0,
            "counterfactual_spend": 0.0,
        }
    )
    path = path.expanduser()
    if not path.is_file():
        return {"day": target_day.isoformat(), "requests": 0, "by_app": {}}

    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
            timestamp = datetime.fromisoformat(event["timestamp"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if timestamp.date() != target_day or event.get("event") != "request":
            continue
        app = str(event.get("app", "unknown"))
        summary = by_app[app]
        summary["requests"] += 1
        summary["actual_spend"] += float(event.get("actual_cost", 0.0))
        summary["counterfactual_spend"] += float(
            event.get("counterfactual_top_model_cost", 0.0)
        )

    actual = sum(float(item["actual_spend"]) for item in by_app.values())
    counterfactual = sum(
        float(item["counterfactual_spend"]) for item in by_app.values()
    )
    return {
        "day": target_day.isoformat(),
        "requests": sum(int(item["requests"]) for item in by_app.values()),
        "actual_spend": actual,
        "counterfactual_spend": counterfactual,
        "savings": counterfactual - actual,
        "by_app": dict(by_app),
    }
