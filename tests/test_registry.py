import unittest
from decimal import Decimal

from zhunt.brain import Tier
from zhunt.registry import ModelRegistry, RegistryError


REGISTRY_DATA = {
    "aliases": {
        "zhunt-auto": {"tier": "auto"},
        "zhunt-chat": {"tier": "chat"},
    },
    "tiers": {
        "chat": [
            {"model": "provider/preferred", "in": 0.4, "out": 0.4},
            {"model": "provider/input-cheap", "in": 0.1, "out": 0.8},
        ]
    },
}


class ModelRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ModelRegistry.from_data(REGISTRY_DATA)

    def test_auto_alias_defers_to_classifier(self) -> None:
        self.assertIsNone(self.registry.resolve_alias("zhunt-auto"))

    def test_concrete_alias_resolves_to_tier(self) -> None:
        self.assertEqual(self.registry.resolve_alias("zhunt-chat"), Tier.CHAT)

    def test_unknown_alias_is_rejected(self) -> None:
        with self.assertRaisesRegex(RegistryError, "unknown model alias"):
            self.registry.resolve_alias("not-an-alias")

    def test_selection_uses_projected_request_cost(self) -> None:
        input_heavy = self.registry.select_model(
            Tier.CHAT, input_tokens=10_000, output_tokens=10
        )
        balanced = self.registry.select_model(
            Tier.CHAT, input_tokens=1_000, output_tokens=1_000
        )

        self.assertEqual(input_heavy.model, "provider/input-cheap")
        self.assertEqual(balanced.model, "provider/preferred")
        self.assertEqual(
            balanced.projected_cost(input_tokens=1_000, output_tokens=1_000),
            Decimal("0.0008"),
        )

    def test_unhealthy_models_are_excluded(self) -> None:
        selected = self.registry.select_model(
            Tier.CHAT,
            healthy_models={"provider/input-cheap"},
        )

        self.assertEqual(selected.model, "provider/input-cheap")

    def test_latency_weight_can_choose_faster_measured_model(self) -> None:
        selected = self.registry.select_model(
            Tier.CHAT,
            input_tokens=10_000,
            output_tokens=10,
            latency_metrics={
                "provider/input-cheap": 500.0,
                "provider/preferred": 10.0,
            },
            latency_weight=1.0,
        )

        self.assertEqual(selected.model, "provider/preferred")

    def test_missing_latency_measurements_preserve_cost_selection(self) -> None:
        selected = self.registry.select_model(
            Tier.CHAT,
            input_tokens=10_000,
            output_tokens=10,
            latency_metrics={"provider/preferred": 10.0},
            latency_weight=1.0,
        )

        self.assertEqual(selected.model, "provider/input-cheap")

    def test_empty_healthy_set_fails_closed(self) -> None:
        with self.assertRaisesRegex(RegistryError, "no healthy models"):
            self.registry.select_model(Tier.CHAT, healthy_models=set())

    def test_alias_cannot_target_missing_tier(self) -> None:
        with self.assertRaisesRegex(RegistryError, "targets missing tier"):
            ModelRegistry.from_data(
                {
                    "aliases": {"zhunt-coding": {"tier": "coding"}},
                    "tiers": REGISTRY_DATA["tiers"],
                }
            )

    def test_default_registry_loads_all_core_tiers(self) -> None:
        registry = ModelRegistry.default()

        for tier in Tier:
            self.assertIsNotNone(registry.select_model(tier))


if __name__ == "__main__":
    unittest.main()
