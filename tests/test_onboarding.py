import unittest
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from zhunt.onboarding import create_onboarding_app


class OnboardingTests(unittest.TestCase):
    def test_setup_page_carries_claude_billing_warning(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_onboarding_app(home=Path(directory), setup_token="setup-token")
            with TestClient(app) as client:
                response = client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn("will not use Claude Max", response.text)

    def test_provider_catalog_is_available_without_secret(self) -> None:
        with TemporaryDirectory() as directory:
            app = create_onboarding_app(
                home=Path(directory),
                setup_token="setup-token",
                validator=lambda provider, key: 2,
            )
            with TestClient(app) as client:
                response = client.get("/api/providers")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["id"] for item in response.json()],
            ["openrouter", "nous-portal"],
        )

    def test_configuration_requires_token_and_persists_provider_key(self) -> None:
        with TemporaryDirectory() as directory:
            home = Path(directory)
            with patch.dict(os.environ, {}, clear=False):
                app = create_onboarding_app(
                    home=home,
                    setup_token="setup-token",
                    validator=lambda provider, key: 4,
                )
                with TestClient(app) as client:
                    denied = client.post(
                        "/api/configure",
                        json={"provider": "nous-portal", "api_key": "sk-portal-test"},
                    )
                    configured = client.post(
                        "/api/configure",
                        headers={"X-Zhunt-Setup-Token": "setup-token"},
                        json={"provider": "nous-portal", "api_key": "sk-portal-test"},
                    )

            env_text = (home / ".zhunt" / "env").read_text(encoding="utf-8")

        self.assertEqual(denied.status_code, 403)
        self.assertEqual(configured.status_code, 200, configured.text)
        self.assertEqual(configured.json()["models"], 4)
        self.assertIn('ZHUNT_PROVIDER="nous-portal"', env_text)
        self.assertIn('PORTAL_API_KEY="sk-portal-test"', env_text)


if __name__ == "__main__":
    unittest.main()
