import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from zhunt.health import ModelAvailabilityError, ModelHealth, validate_registry_models
from zhunt.registry import ModelRegistry
from zhunt.server import create_proxy_app


REGISTRY = ModelRegistry.from_data(
    {
        "aliases": {"zhunt-auto": {"tier": "auto"}},
        "tiers": {
            "chat": [
                {"model": "provider/chat-a", "in": 0.1, "out": 0.2},
                {"model": "provider/chat-b", "in": 0.2, "out": 0.4},
            ]
        },
    }
)


class ModelHealthTests(unittest.TestCase):
    def test_consecutive_failures_mark_only_the_model_unhealthy(self) -> None:
        health = ModelHealth(REGISTRY.model_ids(), failure_threshold=2)

        self.assertFalse(health.record_failure("provider/chat-a"))
        self.assertEqual(
            health.healthy_models(),
            {"provider/chat-a", "provider/chat-b"},
        )
        self.assertTrue(health.record_failure("provider/chat-a"))
        self.assertEqual(health.healthy_models(), {"provider/chat-b"})

    def test_startup_validation_names_unavailable_models(self) -> None:
        with self.assertRaisesRegex(
            ModelAvailabilityError,
            "provider/chat-b",
        ):
            validate_registry_models(
                REGISTRY,
                lambda model: model != "provider/chat-b",
            )

    def test_proxy_startup_validation_fails_before_serving(self) -> None:
        with TemporaryDirectory() as directory:
            registry_path = Path(directory) / "models.yaml"
            registry_path.write_text(
                """\
aliases: {}
tiers:
  chat:
    - model: provider/missing
      in: 0.1
      out: 0.2
""",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ModelAvailabilityError,
                "provider/missing",
            ):
                create_proxy_app(
                    registry_path=registry_path,
                    env_path=Path(directory) / "env",
                    validate_startup=True,
                    availability_checker=lambda model: False,
                )


if __name__ == "__main__":
    unittest.main()
