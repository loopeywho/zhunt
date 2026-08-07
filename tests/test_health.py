import unittest
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from zhunt.health import (
    ModelAvailabilityError,
    ModelHealth,
    litellm_model_available,
    validate_registry_models,
)
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

    def test_real_checker_accepts_shipped_provider_models(self) -> None:
        validate_registry_models(
            ModelRegistry.default(),
            litellm_model_available,
        )

        validate_registry_models(
            ModelRegistry.default(provider_id="nous-portal"),
            litellm_model_available,
        )

    def test_proxy_startup_uses_real_checker_for_shipped_registries(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {}, clear=False):
                create_proxy_app(
                    env_path=root / "default.env",
                    validate_startup=True,
                )
                portal_env = root / "portal.env"
                portal_env.write_text(
                    'ZHUNT_PROVIDER="nous-portal"\n',
                    encoding="utf-8",
                )
                create_proxy_app(
                    env_path=portal_env,
                    validate_startup=True,
                )

    def test_proxy_startup_accepts_explicit_copy_of_shipped_registry(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            registry_path = root / "models.yaml"
            registry_path.write_text(
                (Path(__file__).parent.parent / "models.yaml").read_text(
                    encoding="utf-8"
                ),
                encoding="utf-8",
            )

            create_proxy_app(
                registry_path=registry_path,
                env_path=root / "env",
                validate_startup=True,
            )


if __name__ == "__main__":
    unittest.main()
