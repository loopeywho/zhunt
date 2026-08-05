import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from zhunt.brain import Tier
from zhunt.registry import ModelRegistry
from zhunt.router import RoutingDecision
from zhunt.telemetry import TelemetryLogger, summarize_telemetry


REGISTRY = ModelRegistry.from_data(
    {
        "aliases": {"zhunt-auto": {"tier": "auto"}},
        "tiers": {
            "chat": [
                {"model": "provider/chat-cheap", "in": 0.1, "out": 0.2},
                {"model": "provider/chat-top", "in": 1.0, "out": 2.0},
            ],
            "reasoning": [
                {"model": "provider/reasoning-top", "in": 5.0, "out": 10.0},
            ],
        },
    }
)


class TelemetryTests(unittest.TestCase):
    def test_record_and_summarize_actual_vs_counterfactual_spend(self) -> None:
        decision = RoutingDecision(
            session_key="id:session",
            requested_alias="zhunt-auto",
            tier=Tier.CHAT,
            model="provider/chat-cheap",
            classification=None,
            explicit_override=False,
            reused_session_route=False,
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "telemetry.jsonl"
            logger = TelemetryLogger(path, REGISTRY)
            event = logger.record_request(
                app="openai-chat-completions",
                decision=decision,
                input_tokens=1_000_000,
                output_tokens=1_000_000,
                success=True,
            )
            summary = summarize_telemetry(
                path,
                day=datetime.now(timezone.utc).date(),
            )

        self.assertEqual(event["actual_cost"], 0.3)
        self.assertEqual(event["counterfactual_top_model_cost"], 15.0)
        self.assertEqual(summary["requests"], 1)
        self.assertEqual(summary["actual_spend"], 0.3)
        self.assertEqual(summary["counterfactual_spend"], 15.0)
        self.assertEqual(summary["savings"], 14.7)
        self.assertEqual(summary["by_app"]["openai-chat-completions"]["requests"], 1)

    def test_invalid_lines_are_ignored(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "telemetry.jsonl"
            path.write_text("not-json\n", encoding="utf-8")
            summary = summarize_telemetry(path)

        self.assertEqual(summary["requests"], 0)


if __name__ == "__main__":
    unittest.main()
