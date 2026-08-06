import unittest

from zhunt.benchmark import BenchmarkCase, BenchmarkTurn, run_benchmark
from zhunt.registry import ModelRegistry


REGISTRY = ModelRegistry.from_data(
    {
        "aliases": {"zhunt-auto": {"tier": "auto"}},
        "tiers": {
            "chat": [
                {"model": "chat-cheap", "in": 0.1, "out": 0.2},
                {"model": "chat-expensive", "in": 1.0, "out": 2.0},
            ],
            "coding": [{"model": "coding", "in": 2.0, "out": 4.0}],
            "long-context": [{"model": "long", "in": 3.0, "out": 6.0}],
            "reasoning": [{"model": "reasoning", "in": 5.0, "out": 10.0}],
        },
    }
)


class BenchmarkTests(unittest.TestCase):
    def test_benchmark_reports_routing_and_costs(self) -> None:
        result = run_benchmark(
            REGISTRY,
            cases=(
                BenchmarkCase(
                    name="chat-case",
                    turns=(
                        BenchmarkTurn(
                            name="hello",
                            user_text="Hello",
                            input_tokens=1_000,
                            output_tokens=1_000,
                        ),
                    ),
                ),
            ),
        )

        self.assertEqual(result["requests"], 1)
        self.assertEqual(result["turns"][0]["model"], "chat-cheap")
        self.assertAlmostEqual(result["actual_projected_cost"], 0.0003)
        self.assertAlmostEqual(result["baseline_projected_cost"], 0.015)
        self.assertAlmostEqual(result["projected_savings"], 0.0147)
        self.assertFalse(result["provider_calls"])
        self.assertFalse(result["quality_measured"])

    def test_benchmark_exercises_session_stickiness(self) -> None:
        result = run_benchmark(
            REGISTRY,
            cases=(
                BenchmarkCase(
                    name="coding-case",
                    turns=(
                        BenchmarkTurn(
                            name="edit",
                            user_text="Fix this:\n```python\npass\n```",
                            input_tokens=100,
                            output_tokens=100,
                        ),
                        BenchmarkTurn(
                            name="follow-up",
                            user_text="Thanks",
                            input_tokens=100,
                            output_tokens=100,
                        ),
                    ),
                ),
            ),
        )

        self.assertEqual(result["turns"][0]["tier"], "coding")
        self.assertEqual(result["turns"][1]["tier"], "coding")
        self.assertTrue(result["turns"][1]["reused_session_route"])


if __name__ == "__main__":
    unittest.main()
